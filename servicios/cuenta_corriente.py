# ============================================================
# Servicio de cuenta corriente — PostgreSQL
# ============================================================
# Lee envíos/facturas y pagos directamente de la base de datos.
# El admin carga los datos desde el panel.
# ============================================================

from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import functools
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

import psycopg2

from core.database import get_conn
from servicios.auditoria import registrar_evento_con_cursor
from servicios.conflictos_db import mensaje_conflicto_db
from servicios.diferencias_cliente import presentar_diferencia


_CENTAVO = Decimal("0.01")
_AMBITOS_CONTABLES = ("NACIONAL", "INTERNACIONAL")
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


class ConflictoContableError(ValueError):
    """La base rechazó la operación por una regla contable (trigger/constraint).

    Es un ``ValueError`` para que los endpoints existentes, que ya muestran
    ese tipo como mensaje al operador, lo traten como conflicto de negocio y
    no como error del servidor. La transacción ya fue revertida.
    """


def _conflictos_como_valueerror(funcion):
    """Convierte violaciones del schema en ``ConflictoContableError``.

    Sólo traduce lo que ``servicios.conflictos_db`` reconoce como regla de
    negocio; cualquier otro error de PostgreSQL se propaga sin cambios.
    """
    @functools.wraps(funcion)
    def envoltura(*args, **kwargs):
        try:
            return funcion(*args, **kwargs)
        except psycopg2.Error as exc:
            mensaje = mensaje_conflicto_db(exc)
            if mensaje is None:
                raise
            raise ConflictoContableError(mensaje) from exc
    return envoltura


def _decimal_monto(valor: Any, *, permitir_cero: bool = True) -> Decimal:
    """Convierte plata sin pasar por float y normaliza a centavos exactos."""
    if valor is None or valor == "":
        monto = Decimal("0")
    else:
        try:
            monto = Decimal(str(valor))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError("El monto no es válido.") from exc
    if not monto.is_finite():
        raise ValueError("El monto no es válido.")
    monto = monto.quantize(_CENTAVO, rounding=ROUND_HALF_UP)
    if monto < 0 or (not permitir_cero and monto == 0):
        raise ValueError("El monto debe ser mayor que cero.")
    return monto


def _ambito_contable(valor: Any, *, requerido: bool = True) -> Optional[str]:
    ambito = str(valor or "").strip().upper()
    if not ambito and not requerido:
        return None
    if ambito not in _AMBITOS_CONTABLES:
        raise ValueError("El ámbito debe ser NACIONAL o INTERNACIONAL.")
    return ambito


def _normalizar_aplicaciones(
    aplicaciones: Optional[Mapping[str, Any]],
) -> Optional[Dict[str, Decimal]]:
    """Normaliza una decisión explícita; None significa 'no reemplazar'."""
    if aplicaciones is None:
        return None
    normalizadas: Dict[str, Decimal] = {}
    for ambito_crudo, monto_crudo in aplicaciones.items():
        ambito = _ambito_contable(ambito_crudo)
        monto = _decimal_monto(monto_crudo)
        if monto == 0:
            continue
        if ambito in normalizadas:
            raise ValueError(f"El ámbito {ambito} está repetido.")
        normalizadas[ambito] = monto
    return normalizadas


def _normalizar_destinos_documentales(
    destinos: Optional[Iterable[Any]],
) -> Optional[List[tuple[str, int]]]:
    """Normaliza IDs opacos F:<factura> / E:<envío>; None preserva decisión."""
    if destinos is None:
        return None
    normalizados: List[tuple[str, int]] = []
    vistos = set()
    for valor in destinos:
        match = re.fullmatch(r"([FE]):([1-9][0-9]*)", str(valor or "").strip().upper())
        if not match:
            raise ValueError("La selección contiene un documento inválido.")
        clave = (match.group(1), int(match.group(2)))
        if clave in vistos:
            raise ValueError("La selección contiene un documento repetido.")
        vistos.add(clave)
        normalizados.append(clave)
    return normalizados


def _idempotency_key(valor: Optional[str]) -> Optional[str]:
    """Valida el token opaco; None mantiene compatibles cargas no interactivas."""
    if valor is None:
        return None
    if not isinstance(valor, str):
        raise ValueError("La clave de idempotencia no es válida.")
    clave = valor.strip()
    if not _IDEMPOTENCY_KEY_RE.fullmatch(clave):
        raise ValueError(
            "La clave de idempotencia debe tener entre 32 y 128 caracteres "
            "alfanuméricos, guion o guion bajo."
        )
    return clave


def _fecha_comparable(valor: Any) -> str:
    return valor.isoformat() if hasattr(valor, "isoformat") else str(valor)


def _fc_normalizada(valor: Any) -> str:
    """Espeja exactamente la expresión del índice PostgreSQL de FC."""
    return re.sub(r"[^A-Z0-9]", "", str(valor or "").strip().upper())


def _parse_monto(valor) -> float:
    if valor is None or valor == "":
        return 0.0
    try:
        return float(valor)
    except (ValueError, TypeError):
        return 0.0


def get_facturado_real(cliente: str) -> float:
    """
    Suma el monto de envíos activos del cliente (estado != CANCELADO / NC).
    """
    cliente = cliente.strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE((
                        SELECT SUM(monto_ars) FROM envios
                        WHERE cliente_id=%s
                          AND estado NOT IN ('CANCELADO','NC')
                    ), 0)
                    + COALESCE((
                        SELECT SUM(a.monto_ars)
                        FROM ajustes_cliente a
                        JOIN envios e ON e.solicitud_id=a.solicitud_id
                        WHERE e.cliente_id=%s AND e.estado='ACTIVO'
                          AND a.estado='APLICADO'
                    ), 0) AS total
                """,
                (cliente, cliente),
            )
            row = cur.fetchone()
    return round(float(row["total"]) if row else 0.0, 2)


def get_facturas_recientes(
    cliente: str, limite: Optional[int] = 10
) -> List[Dict[str, Any]]:
    """Cargos del cliente ordenados por fecha descendente.

    Conserva si cada cargo ya tiene número de factura. ``limite=None`` se usa
    en la cuenta corriente completa; los previews deben pasar un número.
    """
    cliente = cliente.strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            query = """
                SELECT e.id, e.fecha,
                       COALESCE(
                           fc.tipo || ' ' || LPAD(fc.punto_venta::text, 4, '0')
                           || '-' || LPAD(fc.numero::text, 8, '0'),
                           NULLIF(BTRIM(e.nro_fc), '')
                       ) AS nro_fc,
                       e.monto_ars, e.descripcion,
                       (fc.pdf IS NOT NULL OR e.factura_pdf IS NOT NULL) AS tiene_pdf
                FROM envios e
                LEFT JOIN LATERAL (
                    SELECT f.tipo, f.punto_venta, f.numero, f.pdf
                    FROM facturas_cliente_items i
                    JOIN facturas_cliente f ON f.id=i.factura_id
                    WHERE i.envio_id=e.id AND f.estado='EMITIDA'
                    ORDER BY f.id DESC LIMIT 1
                ) fc ON TRUE
                WHERE e.cliente_id = %s
                  AND e.estado NOT IN ('CANCELADO', 'NC')
                  AND e.monto_ars > 0
                ORDER BY e.fecha DESC
            """
            params = [cliente]
            if limite is not None:
                query += " LIMIT %s"
                params.append(max(1, int(limite)))
            cur.execute(query, tuple(params))
            rows = cur.fetchall()

    facturas = []
    for r in rows:
        nro_fc = str(r["nro_fc"] or "").strip()
        facturas.append({
            "id": r["id"],
            "fecha": r["fecha"].strftime("%d/%m/%Y") if r["fecha"] else "",
            # Los cargos automáticos de guías no tienen nro de factura: sin el
            # fallback a la descripción, el timeline mostraría filas mudas.
            "nro_fc": nro_fc or str(r["descripcion"] or ""),
            "facturado": bool(nro_fc),
            "monto_ars": float(r["monto_ars"] or 0),
            "tiene_pdf": bool(r["tiene_pdf"]),
        })
    return facturas


def get_factura_pdf(envio_id: int, cliente_id: Optional[str] = None):
    """
    (contenido, nombre) del PDF de la factura, o None. Con cliente_id se
    exige que la factura sea de ESE cliente (el portal sólo ve las propias).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            if cliente_id:
                cur.execute("""
                    SELECT factura_pdf, factura_nombre, nro_fc
                    FROM envios WHERE id = %s AND cliente_id = %s
                """, (envio_id, cliente_id.strip().upper()))
            else:
                cur.execute("""
                    SELECT factura_pdf, factura_nombre, nro_fc
                    FROM envios WHERE id = %s
                """, (envio_id,))
            row = cur.fetchone()
    if not row or not row["factura_pdf"]:
        return None
    nombre = row["factura_nombre"] or f"factura_{row['nro_fc'] or envio_id}.pdf"
    return (bytes(row["factura_pdf"]), nombre)


def get_pagos(cliente: str) -> List[Dict[str, Any]]:
    """Lista de pagos recibidos del cliente, ordenados por fecha asc."""
    cliente = cliente.strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, fecha, monto_ars, metodo, referencia, nota, estado,
                       (comprobante IS NOT NULL) AS tiene_comprobante
                FROM pagos
                WHERE cliente_id = %s
                  AND COALESCE(estado, 'APROBADO') <> 'RECHAZADO'
                ORDER BY fecha ASC
                """,
                (cliente,),
            )
            rows = cur.fetchall()

    pagos = []
    for r in rows:
        pagos.append({
            "id": r["id"],
            "fecha": r["fecha"].strftime("%d/%m/%Y") if r["fecha"] else "",
            "monto_ars": float(r["monto_ars"] or 0),
            "metodo": str(r["metodo"] or ""),
            "referencia": str(r["referencia"] or ""),
            "nota": str(r["nota"] or ""),
            # NULL = pago viejo cargado por el admin antes de que existiera
            # el estado: cuenta como aprobado.
            "estado": str(r["estado"] or "APROBADO"),
            "tiene_comprobante": bool(r["tiene_comprobante"]),
        })
    return pagos


def total_pagado(cliente: str) -> float:
    """
    SOLO pagos aprobados: un pago informado por el cliente con comprobante
    queda PENDIENTE y no toca el saldo hasta que el admin lo verifica
    (decisión de Leandro 28/07 — nadie se acredita plata con un comprobante
    sin revisar).
    """
    cliente = cliente.strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(monto_ars), 0) AS total FROM pagos
                WHERE cliente_id = %s
                  AND COALESCE(estado, 'APROBADO') = 'APROBADO'
                """,
                (cliente,),
            )
            row = cur.fetchone()
    return round(float(row["total"]) if row else 0.0, 2)


def get_resumen_clientes_bulk(solo_activos: bool = True) -> List[Dict[str, Any]]:
    """
    Devuelve facturado y pagado de TODOS los clientes en UNA sola query.
    Reemplaza el patrón N+1 de iterar y llamar get_facturado_real + total_pagado.

    Uso típico (admin_home):
        resumen = get_resumen_clientes_bulk(solo_activos=True)
        # cada item: {cliente_id, email, nombre, facturado, pagado, saldo}
    """
    where_activos = "WHERE c.test = FALSE"
    if solo_activos:
        where_activos += " AND c.activo = TRUE"
    sql = f"""
        WITH fact AS (
            SELECT cliente_id, COALESCE(SUM(monto_ars), 0) AS facturado
            FROM envios
            WHERE estado NOT IN ('CANCELADO', 'NC')
            GROUP BY cliente_id
        ),
        pag AS (
            SELECT cliente_id, COALESCE(SUM(monto_ars), 0) AS pagado
            FROM pagos
            WHERE COALESCE(estado, 'APROBADO') = 'APROBADO'
            GROUP BY cliente_id
        ),
        aju AS (
            SELECT e.cliente_id, COALESCE(SUM(a.monto_ars), 0) AS ajuste
            FROM ajustes_cliente a
            JOIN envios e ON e.solicitud_id = a.solicitud_id
            WHERE a.estado = 'APLICADO' AND e.estado = 'ACTIVO'
            GROUP BY e.cliente_id
        )
        SELECT
            c.cliente_id,
            c.email,
            c.nombre,
            c.activo,
            COALESCE(fact.facturado, 0) + COALESCE(aju.ajuste, 0) AS facturado,
            COALESCE(pag.pagado, 0)    AS pagado
        FROM clientes c
        LEFT JOIN fact ON fact.cliente_id = c.cliente_id
        LEFT JOIN pag  ON pag.cliente_id  = c.cliente_id
        LEFT JOIN aju  ON aju.cliente_id  = c.cliente_id
        {where_activos}
        ORDER BY c.cliente_id
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()

    resumen = []
    for r in rows:
        facturado = round(float(r["facturado"] or 0), 2)
        pagado = round(float(r["pagado"] or 0), 2)
        resumen.append({
            "cliente_id": r["cliente_id"],
            "email": r["email"] or "",
            "nombre": r["nombre"] or "",
            "activo": bool(r["activo"]),
            "facturado": facturado,
            "pagado": pagado,
            "saldo": round(facturado - pagado, 2),
        })
    return resumen


def saldo(cliente: str, total_facturado_ars: float) -> Dict[str, float]:
    pagado = total_pagado(cliente)
    return {
        "facturado_ars": round(total_facturado_ars, 2),
        "pagado_ars": round(pagado, 2),
        "saldo_pendiente_ars": round(total_facturado_ars - pagado, 2),
    }


def _armar_resumen_ambitos(fila: Mapping[str, Any]) -> Dict[str, Any]:
    """Arma el resumen con Decimal y verifica que ningún crédito se duplique."""
    envios_nac = _decimal_monto(fila.get("debe_nacional"))
    envios_int = _decimal_monto(fila.get("debe_internacional"))
    envios_sc = _decimal_monto(fila.get("debe_sin_clasificar"))
    facturado_nac = _decimal_monto(fila.get("facturado_nacional"))
    facturado_int = _decimal_monto(fila.get("facturado_internacional"))
    facturado_sc = _decimal_monto(fila.get("facturado_sin_clasificar"))
    ajuste_debito_nac = _decimal_monto(fila.get("ajuste_debito_nacional"))
    ajuste_debito_int = _decimal_monto(fila.get("ajuste_debito_internacional"))
    ajuste_debito_sc = _decimal_monto(fila.get("ajuste_debito_sin_clasificar"))
    ajuste_facturado_nac = _decimal_monto(
        fila.get("ajuste_debito_facturado_nacional")
    )
    ajuste_facturado_int = _decimal_monto(
        fila.get("ajuste_debito_facturado_internacional")
    )
    ajuste_facturado_sc = _decimal_monto(
        fila.get("ajuste_debito_facturado_sin_clasificar")
    )
    ajuste_credito_nac = _decimal_monto(fila.get("ajuste_credito_nacional"))
    ajuste_credito_int = _decimal_monto(fila.get("ajuste_credito_internacional"))
    ajuste_credito_sc = _decimal_monto(fila.get("ajuste_credito_sin_clasificar"))
    debe_nac = envios_nac + ajuste_debito_nac
    debe_int = envios_int + ajuste_debito_int
    debe_sc = envios_sc + ajuste_debito_sc
    pendiente_nac = (
        envios_nac - facturado_nac + ajuste_debito_nac - ajuste_facturado_nac
    )
    pendiente_int = (
        envios_int - facturado_int + ajuste_debito_int - ajuste_facturado_int
    )
    pendiente_sc = (
        envios_sc - facturado_sc + ajuste_debito_sc - ajuste_facturado_sc
    )
    pagos_aplicados_nac = _decimal_monto(fila.get("haber_nacional"))
    pagos_aplicados_int = _decimal_monto(fila.get("haber_internacional"))
    haber_nac = pagos_aplicados_nac + ajuste_credito_nac
    haber_int = pagos_aplicados_int + ajuste_credito_int
    haber_sc = ajuste_credito_sc
    aprobado = _decimal_monto(fila.get("pagos_aprobados"))
    pendiente = _decimal_monto(fila.get("pagos_pendientes"))
    aplicado = pagos_aplicados_nac + pagos_aplicados_int
    if aplicado > aprobado:
        raise RuntimeError(
            "Invariante contable rota: las aplicaciones superan los pagos aprobados."
        )
    sin_imputar = aprobado - aplicado
    saldo_nac = debe_nac - haber_nac
    saldo_int = debe_int - haber_int
    debe_total = debe_nac + debe_int + debe_sc
    saldo_antes_sin_imputar = saldo_nac + saldo_int + debe_sc - haber_sc
    saldo_consolidado = debe_total - haber_sc - ajuste_credito_nac - ajuste_credito_int - aprobado
    if aplicado + sin_imputar != aprobado:
        raise RuntimeError("Invariante contable rota: el crédito aprobado no cierra.")
    if saldo_antes_sin_imputar - sin_imputar != saldo_consolidado:
        raise RuntimeError("Invariante contable rota: el saldo consolidado no cierra.")
    nacional = {
        "facturado_ars": facturado_nac + ajuste_facturado_nac,
        "pendiente_facturacion_ars": pendiente_nac,
        "debe_ars": debe_nac,
        "haber_ars": haber_nac,
        "saldo_ars": saldo_nac,
        "envios_ars": envios_nac,
        "diferencias_debito_ars": ajuste_debito_nac,
        "diferencias_credito_ars": ajuste_credito_nac,
    }
    internacional = {
        "facturado_ars": facturado_int + ajuste_facturado_int,
        "pendiente_facturacion_ars": pendiente_int,
        "debe_ars": debe_int,
        "haber_ars": haber_int,
        "saldo_ars": saldo_int,
        "envios_ars": envios_int,
        "diferencias_debito_ars": ajuste_debito_int,
        "diferencias_credito_ars": ajuste_credito_int,
    }
    consolidado = {
        "facturado_ars": (
            facturado_nac + facturado_int + facturado_sc
            + ajuste_facturado_nac + ajuste_facturado_int + ajuste_facturado_sc
        ),
        "pendiente_facturacion_ars": pendiente_nac + pendiente_int + pendiente_sc,
        "debe_ars": debe_total,
        # Consolidado sí reconoce todo pago aprobado, incluso lo no imputado.
        "haber_ars": aprobado + ajuste_credito_nac + ajuste_credito_int + haber_sc,
        "haber_aplicado_ars": aplicado,
        "pagos_aprobados_ars": aprobado,
        "credito_aprobado_sin_imputar_ars": sin_imputar,
        "saldo_antes_credito_sin_imputar_ars": saldo_antes_sin_imputar,
        "saldo_ars": saldo_consolidado,
        "envios_ars": envios_nac + envios_int + envios_sc,
        "pagos_ars": aprobado,
        "diferencias_debito_ars": (
            ajuste_debito_nac + ajuste_debito_int + ajuste_debito_sc
        ),
        "diferencias_credito_ars": (
            ajuste_credito_nac + ajuste_credito_int + ajuste_credito_sc
        ),
        "diferencias_netas_ars": (
            ajuste_debito_nac + ajuste_debito_int + ajuste_debito_sc
            - ajuste_credito_nac - ajuste_credito_int - ajuste_credito_sc
        ),
    }
    return {
        "nacional": nacional,
        "internacional": internacional,
        "consolidado": consolidado,
        "credito_sin_imputar_ars": sin_imputar,
        "cargos_sin_clasificar_ars": debe_sc,
        "pagos_pendientes_ars": pendiente,
    }


def resumen_cuenta_por_ambito(cliente: str) -> Dict[str, Any]:
    """Debe/haber separado por ámbito y crédito aprobado aún no imputado.

    Los cargos históricos sin ámbito quedan en SIN_CLASIFICAR. Los pagos
    PENDIENTE se informan, pero no forman parte del haber ni del saldo.
    """
    cliente = cliente.strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH cargos AS (
                    SELECT
                        COALESCE(SUM(monto_ars) FILTER (
                            WHERE ambito = 'NACIONAL'
                        ), 0) AS debe_nacional,
                        COALESCE(SUM(monto_ars) FILTER (
                            WHERE ambito = 'INTERNACIONAL'
                        ), 0) AS debe_internacional,
                        COALESCE(SUM(monto_ars) FILTER (
                            WHERE ambito IS NULL
                               OR ambito NOT IN ('NACIONAL', 'INTERNACIONAL')
                        ), 0) AS debe_sin_clasificar,
                        COALESCE(SUM(monto_ars) FILTER (
                            WHERE ambito = 'NACIONAL'
                              AND (
                                  NULLIF(BTRIM(nro_fc), '') IS NOT NULL
                                  OR EXISTS (
                                      SELECT 1 FROM facturas_cliente_items i
                                      JOIN facturas_cliente f ON f.id=i.factura_id
                                      WHERE i.envio_id=envios.id
                                        AND f.estado='EMITIDA'
                                  )
                              )
                        ), 0) AS facturado_nacional,
                        COALESCE(SUM(monto_ars) FILTER (
                            WHERE ambito = 'INTERNACIONAL'
                              AND (
                                  NULLIF(BTRIM(nro_fc), '') IS NOT NULL
                                  OR EXISTS (
                                      SELECT 1 FROM facturas_cliente_items i
                                      JOIN facturas_cliente f ON f.id=i.factura_id
                                      WHERE i.envio_id=envios.id
                                        AND f.estado='EMITIDA'
                                  )
                              )
                        ), 0) AS facturado_internacional,
                        COALESCE(SUM(monto_ars) FILTER (
                            WHERE (ambito IS NULL
                               OR ambito NOT IN ('NACIONAL', 'INTERNACIONAL'))
                              AND (
                                  NULLIF(BTRIM(nro_fc), '') IS NOT NULL
                                  OR EXISTS (
                                      SELECT 1 FROM facturas_cliente_items i
                                      JOIN facturas_cliente f ON f.id=i.factura_id
                                      WHERE i.envio_id=envios.id
                                        AND f.estado='EMITIDA'
                                  )
                              )
                        ), 0) AS facturado_sin_clasificar
                    FROM envios
                    WHERE cliente_id = %s
                      AND estado NOT IN ('CANCELADO', 'NC')
                      AND monto_ars > 0
                ),
                aplicaciones AS (
                    SELECT
                        COALESCE(SUM(pa.monto_ars) FILTER (
                            WHERE pa.ambito = 'NACIONAL'
                        ), 0) AS haber_nacional,
                        COALESCE(SUM(pa.monto_ars) FILTER (
                            WHERE pa.ambito = 'INTERNACIONAL'
                        ), 0) AS haber_internacional
                    FROM pagos_aplicaciones pa
                    JOIN pagos p ON p.id = pa.pago_id
                    WHERE p.cliente_id = %s
                      AND COALESCE(p.estado, 'APROBADO') = 'APROBADO'
                      AND pa.estado = 'APLICADA'
                ),
                ajustes AS (
                    SELECT
                        COALESCE(SUM(ABS(a.monto_ars)) FILTER (
                            WHERE e.ambito='NACIONAL' AND a.tipo='DEBITO'
                        ), 0) AS ajuste_debito_nacional,
                        COALESCE(SUM(ABS(a.monto_ars)) FILTER (
                            WHERE e.ambito='INTERNACIONAL' AND a.tipo='DEBITO'
                        ), 0) AS ajuste_debito_internacional,
                        COALESCE(SUM(ABS(a.monto_ars)) FILTER (
                            WHERE (e.ambito IS NULL OR e.ambito NOT IN ('NACIONAL','INTERNACIONAL'))
                              AND a.tipo='DEBITO'
                        ), 0) AS ajuste_debito_sin_clasificar,
                        COALESCE(SUM(ABS(a.monto_ars)) FILTER (
                            WHERE e.ambito='NACIONAL' AND a.tipo='DEBITO'
                              AND EXISTS (
                                  SELECT 1 FROM facturas_cliente_items i
                                  JOIN facturas_cliente f ON f.id=i.factura_id
                                  WHERE i.ajuste_id=a.id AND f.estado='EMITIDA'
                              )
                        ), 0) AS ajuste_debito_facturado_nacional,
                        COALESCE(SUM(ABS(a.monto_ars)) FILTER (
                            WHERE e.ambito='INTERNACIONAL' AND a.tipo='DEBITO'
                              AND EXISTS (
                                  SELECT 1 FROM facturas_cliente_items i
                                  JOIN facturas_cliente f ON f.id=i.factura_id
                                  WHERE i.ajuste_id=a.id AND f.estado='EMITIDA'
                              )
                        ), 0) AS ajuste_debito_facturado_internacional,
                        COALESCE(SUM(ABS(a.monto_ars)) FILTER (
                            WHERE (e.ambito IS NULL OR e.ambito NOT IN ('NACIONAL','INTERNACIONAL'))
                              AND a.tipo='DEBITO'
                              AND EXISTS (
                                  SELECT 1 FROM facturas_cliente_items i
                                  JOIN facturas_cliente f ON f.id=i.factura_id
                                  WHERE i.ajuste_id=a.id AND f.estado='EMITIDA'
                              )
                        ), 0) AS ajuste_debito_facturado_sin_clasificar,
                        COALESCE(SUM(ABS(a.monto_ars)) FILTER (
                            WHERE e.ambito='NACIONAL' AND a.tipo='CREDITO'
                        ), 0) AS ajuste_credito_nacional,
                        COALESCE(SUM(ABS(a.monto_ars)) FILTER (
                            WHERE e.ambito='INTERNACIONAL' AND a.tipo='CREDITO'
                        ), 0) AS ajuste_credito_internacional,
                        COALESCE(SUM(ABS(a.monto_ars)) FILTER (
                            WHERE (e.ambito IS NULL OR e.ambito NOT IN ('NACIONAL','INTERNACIONAL'))
                              AND a.tipo='CREDITO'
                        ), 0) AS ajuste_credito_sin_clasificar
                    FROM ajustes_cliente a
                    JOIN envios e ON e.solicitud_id=a.solicitud_id
                    WHERE e.cliente_id=%s AND e.estado='ACTIVO'
                      AND a.estado='APLICADO'
                ),
                pagos_totales AS (
                    SELECT
                        COALESCE(SUM(monto_ars) FILTER (
                            WHERE COALESCE(estado, 'APROBADO') = 'APROBADO'
                        ), 0) AS pagos_aprobados,
                        COALESCE(SUM(monto_ars) FILTER (
                            WHERE estado = 'PENDIENTE'
                        ), 0) AS pagos_pendientes
                    FROM pagos
                    WHERE cliente_id = %s
                )
                SELECT cargos.*, aplicaciones.*, ajustes.*, pagos_totales.*
                FROM cargos CROSS JOIN aplicaciones CROSS JOIN ajustes
                CROSS JOIN pagos_totales
                """,
                (cliente, cliente, cliente, cliente),
            )
            fila = cur.fetchone()
    return _armar_resumen_ambitos(fila or {})


def _paginas_visibles(actual: int, total: int) -> List[Optional[int]]:
    if total <= 7:
        return list(range(1, total + 1))
    candidatas = {1, total, actual - 1, actual, actual + 1}
    numeros = sorted(n for n in candidatas if 1 <= n <= total)
    salida: List[Optional[int]] = []
    anterior = 0
    for numero in numeros:
        if anterior and numero - anterior > 1:
            salida.append(None)
        salida.append(numero)
        anterior = numero
    return salida


def movimientos_cuenta_paginados(
    cliente: str,
    ambito: str = "consolidado",
    tipo: str = "todos",
    pagina: int = 1,
    page_size: int = 25,
) -> Dict[str, Any]:
    """Contrato del portal: página numerada, con filtros SQL por ámbito/tipo."""
    cliente = cliente.strip().upper()
    ambito_filtro = str(ambito or "consolidado").strip().lower()
    tipo_filtro = str(tipo or "todos").strip().lower()
    if ambito_filtro not in {"consolidado", "nacional", "internacional"}:
        raise ValueError("El filtro de ámbito no es válido.")
    if tipo_filtro not in {"todos", "cargos", "pagos", "diferencias", "revision"}:
        raise ValueError("El filtro de tipo de movimiento no es válido.")
    pagina = max(1, int(pagina))
    page_size = max(1, min(int(page_size), 100))
    ambito_sql = (
        None if ambito_filtro == "consolidado" else ambito_filtro.upper()
    )

    cte = """
        WITH aplicaciones_pago AS (
            SELECT pago_id, COALESCE(SUM(monto_ars), 0) AS aplicado
            FROM pagos_aplicaciones
            WHERE estado = 'APLICADA'
            GROUP BY pago_id
        ),
        movimientos AS (
            SELECT
                e.fecha,
                e.created_at,
                40 AS tipo_orden,
                e.id AS origen_id,
                CASE WHEN fc.id IS NOT NULL
                          OR NULLIF(BTRIM(e.nro_fc), '') IS NOT NULL
                     THEN 'FC' ELSE 'PENDIENTE_FACTURA' END AS tipo,
                CASE WHEN e.ambito IN ('NACIONAL', 'INTERNACIONAL')
                     THEN e.ambito ELSE 'SIN_CLASIFICAR' END AS ambito,
                CASE
                    WHEN e.solicitud_id IS NOT NULL THEN 'Flete'
                    ELSE COALESCE(NULLIF(BTRIM(e.descripcion), ''), 'Envío')
                END AS concepto,
                NULL::text AS referencia,
                e.monto_ars AS debe_ars,
                0::numeric AS haber_ars,
                e.monto_ars AS monto_ars,
                e.estado,
                (fc.id IS NOT NULL
                 OR NULLIF(BTRIM(e.nro_fc), '') IS NOT NULL) AS facturado,
                e.id AS envio_id,
                NULL::integer AS pago_id,
                e.solicitud_id,
                CASE WHEN fc.id IS NOT NULL AND fc.pdf IS NOT NULL
                     THEN '/portal/facturas/' || fc.id::text || '/pdf'
                     WHEN e.factura_pdf IS NOT NULL
                     THEN '/portal/facturas-legacy/' || e.id::text || '/pdf'
                END AS archivo_url,
                COALESCE(
                    NULLIF(BTRIM(e.tracking), ''),
                    NULLIF(BTRIM(s.tracking), '')
                ) AS numero_guia,
                NULLIF(BTRIM(s.dest_nombre), '') AS destinatario,
                NULLIF(BTRIM(s.remitente_nombre), '') AS remitente,
                e.monto_ars AS valor_envio_ars,
                COALESCE(
                    fc.tipo || ' ' || LPAD(fc.punto_venta::text, 4, '0')
                      || '-' || LPAD(fc.numero::text, 8, '0'),
                    NULLIF(BTRIM(e.nro_fc), '')
                ) AS numero_factura,
                NULL::jsonb AS diferencia_detalle
            FROM envios e
            LEFT JOIN solicitudes_guia s
              ON s.id = e.solicitud_id
             AND s.cliente_id = e.cliente_id
            LEFT JOIN LATERAL (
                SELECT f.id, f.tipo, f.punto_venta, f.numero, f.pdf
                FROM facturas_cliente_items i
                JOIN facturas_cliente f ON f.id=i.factura_id
                WHERE i.envio_id=e.id AND f.estado='EMITIDA'
                ORDER BY f.id DESC LIMIT 1
            ) fc ON TRUE
            WHERE e.cliente_id = %s
              AND e.estado NOT IN ('CANCELADO', 'NC')
              AND e.monto_ars > 0

            UNION ALL

            SELECT
                p.fecha, pa.updated_at, 30, pa.id, 'PAGO', pa.ambito,
                COALESCE(NULLIF(BTRIM(p.metodo), ''), 'Pago'), p.referencia,
                0::numeric, pa.monto_ars, pa.monto_ars, 'APROBADO',
                FALSE, NULL::integer, p.id,
                NULL::integer,
                CASE WHEN p.comprobante IS NOT NULL
                     THEN '/portal/pagos/' || p.id::text || '/comprobante' END,
                NULL::text, NULL::text, NULL::text, NULL::numeric, NULL::text,
                NULL::jsonb
            FROM pagos_aplicaciones pa
            JOIN pagos p ON p.id = pa.pago_id
            WHERE p.cliente_id = %s
              AND COALESCE(p.estado, 'APROBADO') = 'APROBADO'
              AND pa.estado = 'APLICADA'

            UNION ALL

            SELECT
                p.fecha, p.created_at, 20, p.id, 'PAGO', 'SIN_IMPUTAR',
                'Crédito sin imputar', p.referencia,
                0::numeric, p.monto_ars - COALESCE(ap.aplicado, 0),
                p.monto_ars - COALESCE(ap.aplicado, 0), 'APROBADO',
                FALSE, NULL::integer, p.id,
                NULL::integer,
                CASE WHEN p.comprobante IS NOT NULL
                     THEN '/portal/pagos/' || p.id::text || '/comprobante' END,
                NULL::text, NULL::text, NULL::text, NULL::numeric, NULL::text,
                NULL::jsonb
            FROM pagos p
            LEFT JOIN aplicaciones_pago ap ON ap.pago_id = p.id
            WHERE p.cliente_id = %s
              AND COALESCE(p.estado, 'APROBADO') = 'APROBADO'
              AND p.monto_ars > COALESCE(ap.aplicado, 0)

            UNION ALL

            SELECT
                p.fecha, p.created_at, 10, p.id, 'PAGO_PENDIENTE', 'SIN_IMPUTAR',
                COALESCE(NULLIF(BTRIM(p.metodo), ''), 'Pago informado'), p.referencia,
                0::numeric, 0::numeric, p.monto_ars, 'PENDIENTE',
                FALSE, NULL::integer, p.id,
                NULL::integer,
                CASE WHEN p.comprobante IS NOT NULL
                     THEN '/portal/pagos/' || p.id::text || '/comprobante' END,
                NULL::text, NULL::text, NULL::text, NULL::numeric, NULL::text,
                NULL::jsonb
            FROM pagos p
            WHERE p.cliente_id = %s
              AND p.estado = 'PENDIENTE'

            UNION ALL

            SELECT
                a.aplicado_at::date, a.aplicado_at, 35, a.id,
                'DIFERENCIA',
                CASE WHEN e.ambito IN ('NACIONAL','INTERNACIONAL')
                     THEN e.ambito ELSE 'SIN_CLASIFICAR' END,
                CASE
                    WHEN ABS(c.tax_cliente_ars) > 0
                     AND ABS(c.diferencia_flete_ars) > 0 THEN 'Diferencia + TAX'
                    WHEN ABS(c.tax_cliente_ars) > 0 THEN 'TAX'
                    ELSE 'Diferencia de envío'
                END,
                COALESCE(c.motivo_diferencia, a.motivo),
                CASE WHEN a.tipo='DEBITO' THEN ABS(a.monto_ars) ELSE 0 END,
                CASE WHEN a.tipo='CREDITO' THEN ABS(a.monto_ars) ELSE 0 END,
                ABS(a.monto_ars), a.estado, FALSE, e.id, NULL::integer,
                a.solicitud_id, NULL::text,
                COALESCE(
                    NULLIF(BTRIM(s.tracking), ''),
                    NULLIF(BTRIM(e.tracking), '')
                ),
                NULLIF(BTRIM(s.dest_nombre), ''),
                NULLIF(BTRIM(s.remitente_nombre), ''),
                a.precio_nuevo_ars,
                NULLIF(BTRIM(e.nro_fc), ''),
                JSONB_BUILD_OBJECT(
                    'peso_inicial_kg', c.peso_cotizado_kg,
                    'peso_facturado_kg', c.peso_final_facturado_kg,
                    'diferencia_peso_kg', CASE
                        WHEN c.peso_cotizado_kg IS NOT NULL
                         AND c.peso_final_facturado_kg IS NOT NULL
                        THEN c.peso_final_facturado_kg-c.peso_cotizado_kg
                    END,
                    'base_peso', c.peso_base_facturado,
                    'motivo', c.motivo_diferencia,
                    'concepto_courier', COALESCE((
                        SELECT STRING_AGG(DISTINCT COALESCE(
                            NULLIF(BTRIM(i.descripcion), ''),
                            REPLACE(i.concepto_tipo, '_', ' ')
                        ), ' · ')
                        FROM factura_courier_item_matches m
                        JOIN facturas_courier_items i ON i.id=m.item_id
                        WHERE m.solicitud_id=a.solicitud_id
                          AND m.estado='CONFIRMADO'
                          AND i.concepto_tipo <> 'FLETE'
                    ), '')
                )
            FROM ajustes_cliente a
            JOIN conciliaciones_envio c ON c.id=a.conciliacion_id
            JOIN envios e ON e.solicitud_id=a.solicitud_id
            JOIN solicitudes_guia s ON s.id=a.solicitud_id
            WHERE e.cliente_id=%s AND e.estado='ACTIVO'
              AND a.estado='APLICADO'
        ),
        filtrados AS (
            SELECT * FROM movimientos
            WHERE (%s IS NULL OR ambito = %s)
              AND (
                  %s = 'todos'
                  OR (%s = 'cargos' AND tipo IN ('FC', 'PENDIENTE_FACTURA'))
                  OR (%s = 'pagos' AND tipo = 'PAGO')
                  OR (%s = 'diferencias' AND tipo = 'DIFERENCIA')
                  OR (%s = 'revision' AND tipo = 'PAGO_PENDIENTE')
              )
        )
    """
    filtros = (
        cliente, cliente, cliente, cliente, cliente,
        ambito_sql, ambito_sql,
        tipo_filtro, tipo_filtro, tipo_filtro, tipo_filtro, tipo_filtro,
    )
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(cte + "SELECT COUNT(*) AS total FROM filtrados", filtros)
            fila_total = cur.fetchone()
            total = int(fila_total["total"] if fila_total else 0)
            total_paginas = max(1, (total + page_size - 1) // page_size)
            pagina = min(pagina, total_paginas)
            offset = (pagina - 1) * page_size
            cur.execute(
                cte + """
                SELECT * FROM filtrados
                ORDER BY fecha DESC, created_at DESC, tipo_orden DESC, origen_id DESC
                LIMIT %s OFFSET %s
                """,
                filtros + (page_size, offset),
            )
            items = [dict(fila) for fila in cur.fetchall()]

    for item in items:
        item.pop("tipo_orden", None)
        item.pop("origen_id", None)
        if item.get("fecha"):
            item["fecha"] = item["fecha"].strftime("%d/%m/%Y")
        for campo in ("debe_ars", "haber_ars", "monto_ars", "valor_envio_ars"):
            if item.get(campo) is None and campo == "valor_envio_ars":
                continue
            item[campo] = Decimal(str(item.get(campo) or 0)).quantize(_CENTAVO)
        if item.get("tipo") == "DIFERENCIA":
            item["diferencia_detalle"] = presentar_diferencia(
                item.get("diferencia_detalle")
            )
    desde = offset + 1 if total else 0
    hasta = min(offset + len(items), total)
    return {
        "items": items,
        "pagina_actual": pagina,
        "total_paginas": total_paginas,
        "total_resultados": total,
        "pagina_desde": desde,
        "pagina_hasta": hasta,
        "paginas_visibles": _paginas_visibles(pagina, total_paginas),
    }


def resumir_facturacion(facturas: List[Dict[str, Any]]) -> Dict[str, float]:
    """Separa cargos facturados de los que aún esperan factura."""
    facturado = round(sum(
        _parse_monto(fc.get("monto_ars"))
        for fc in facturas
        if fc.get("facturado")
    ), 2)
    pendiente = round(sum(
        _parse_monto(fc.get("monto_ars"))
        for fc in facturas
        if not fc.get("facturado")
    ), 2)
    return {
        "facturado_ars": facturado,
        "pendiente_ars": pendiente,
        "total_cargos_ars": round(facturado + pendiente, 2),
    }


def movimientos(cliente: str, facturas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Timeline mezclado: facturas + pagos ordenados por fecha desc."""
    items = []
    for fc in facturas:
        facturado = bool(fc.get("facturado", True))
        items.append({
            "fecha": fc.get("fecha", ""),
            "tipo": "FC" if facturado else "PENDIENTE_FACTURA",
            "concepto": fc.get("nro_fc", ""),
            "monto_ars": float(fc.get("monto_ars", 0)),
            # Para el link "ver factura" del portal, cuando el admin adjuntó el PDF.
            "envio_id": fc.get("id"),
            "tiene_pdf": bool(fc.get("tiene_pdf")),
        })
    for p in get_pagos(cliente):
        pendiente = p.get("estado") == "PENDIENTE"
        items.append({
            "fecha": p["fecha"],
            # Un pago informado y sin verificar se MUESTRA (el cliente tiene
            # que ver que su aviso llegó) pero rotulado: no está en el saldo
            # hasta que el admin lo apruebe.
            "tipo": "PAGO_PENDIENTE" if pendiente else "PAGO",
            "concepto": f"{p['metodo']} {p['referencia']}".strip()
                        + (" · en verificación" if pendiente else ""),
            "monto_ars": -p["monto_ars"],
        })

    def _parse_fecha(s: str):
        try:
            return datetime.strptime(s, "%d/%m/%Y")
        except (ValueError, TypeError):
            return datetime.min

    items.sort(key=lambda x: _parse_fecha(x["fecha"]), reverse=True)
    return items


# ── Funciones de escritura (para el admin) ───────────────────

# Tipos de comprobante aceptados. La validación es por CONTENIDO (firma del
# archivo), no por extensión: renombrar un .exe a .pdf no lo convierte en PDF.
_FIRMAS_COMPROBANTE = {
    b"%PDF": "application/pdf",
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG": "image/png",
}

# Tope de tamaño APLICADO EN EL HANDLER, además del tope por tamaño del
# middleware de main.py (12 MB para multipart). Este segundo control acota a
# 8 MB el comprobante específico: se piden tope+1 bytes y si vino de más, se
# rechaza sin cargar el resto en memoria (starlette ya lo tiene en un spool
# de disco). Defensa en profundidad, no se apoya en un solo control.
COMPROBANTE_MAX_BYTES = 8 * 1024 * 1024


async def leer_comprobante_con_tope(archivo) -> bytes:
    """Lee un UploadFile hasta 8 MB; más que eso es rechazo, no comprobante."""
    if archivo is None or not hasattr(archivo, "read"):
        return b""
    contenido = await archivo.read(COMPROBANTE_MAX_BYTES + 1)
    if len(contenido) > COMPROBANTE_MAX_BYTES:
        raise ValueError("El archivo supera el máximo de 8 MB.")
    return contenido


def validar_comprobante(contenido: bytes) -> str:
    """Devuelve el content-type real, o lanza ValueError si no es JPG/PNG/PDF."""
    if not contenido:
        raise ValueError("El archivo llegó vacío.")
    for firma, tipo in _FIRMAS_COMPROBANTE.items():
        if contenido.startswith(firma):
            return tipo
    raise ValueError("El comprobante tiene que ser una foto (JPG/PNG) o un PDF.")


def listar_destinos_pago(cliente_id: str) -> List[Dict[str, Any]]:
    """Facturas con saldo y cargos activos todavía no facturados.

    Sólo las aplicaciones APLICADA son pago efectivo. Las SOLICITADA se
    informan como reserva para no ofrecer dos veces el mismo saldo mientras
    el comprobante espera revisión.
    """
    cliente = str(cliente_id or "").strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH aplicaciones_factura AS (
                    SELECT pa.factura_id,
                           COALESCE(SUM(pa.monto_ars) FILTER (
                               WHERE pa.estado='APLICADA'
                           ), 0) AS pagado,
                           COALESCE(SUM(pa.monto_ars) FILTER (
                               WHERE pa.estado='SOLICITADA'
                           ), 0) AS solicitado
                      FROM pagos_aplicaciones pa
                     WHERE pa.factura_id IS NOT NULL
                     GROUP BY pa.factura_id
                ),
                aplicaciones_envio AS (
                    SELECT pa.envio_id,
                           COALESCE(SUM(pa.monto_ars) FILTER (
                               WHERE pa.estado='APLICADA'
                           ), 0) AS pagado,
                           COALESCE(SUM(pa.monto_ars) FILTER (
                               WHERE pa.estado='SOLICITADA'
                           ), 0) AS solicitado
                      FROM pagos_aplicaciones pa
                     WHERE pa.envio_id IS NOT NULL
                     GROUP BY pa.envio_id
                ),
                facturas AS (
                    SELECT
                        'F:' || f.id::text AS clave,
                        'FACTURA'::text AS clase,
                        f.id AS origen_id,
                        f.fecha_emision AS fecha,
                        f.tipo || ' ' || LPAD(f.punto_venta::text, 4, '0')
                            || '-' || LPAD(f.numero::text, 8, '0') AS descripcion,
                        NULL::text AS tracking,
                        MIN(COALESCE(e.ambito, ea.ambito)) AS ambito,
                        f.total AS total,
                        COALESCE(af.pagado, 0)
                            + COALESCE(SUM(ae.pagado), 0) AS pagado,
                        COALESCE(af.solicitado, 0)
                            + COALESCE(SUM(ae.solicitado), 0) AS solicitado
                    FROM facturas_cliente f
                    JOIN facturas_cliente_items i ON i.factura_id=f.id
                    LEFT JOIN envios e ON e.id=i.envio_id
                    LEFT JOIN ajustes_cliente a ON a.id=i.ajuste_id
                    LEFT JOIN envios ea ON ea.solicitud_id=a.solicitud_id
                    LEFT JOIN aplicaciones_factura af ON af.factura_id=f.id
                    LEFT JOIN aplicaciones_envio ae ON ae.envio_id=i.envio_id
                    WHERE f.cliente_id=%s AND f.tipo='FC' AND f.estado='EMITIDA'
                    GROUP BY f.id, af.pagado, af.solicitado
                ),
                cargos AS (
                    SELECT
                        'E:' || e.id::text AS clave,
                        'ENVIO'::text AS clase,
                        e.id AS origen_id,
                        e.fecha,
                        COALESCE(NULLIF(BTRIM(e.descripcion), ''), 'Envío')
                            AS descripcion,
                        COALESCE(NULLIF(BTRIM(e.tracking), ''),
                                 NULLIF(BTRIM(s.tracking), '')) AS tracking,
                        e.ambito,
                        e.monto_ars AS total,
                        COALESCE(ae.pagado, 0) AS pagado,
                        COALESCE(ae.solicitado, 0) AS solicitado
                    FROM envios e
                    LEFT JOIN solicitudes_guia s ON s.id=e.solicitud_id
                    LEFT JOIN aplicaciones_envio ae ON ae.envio_id=e.id
                    WHERE e.cliente_id=%s AND e.estado='ACTIVO'
                      AND e.monto_ars > 0
                      AND e.ambito IN ('NACIONAL','INTERNACIONAL')
                      AND NULLIF(BTRIM(e.nro_fc), '') IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM facturas_cliente_items i
                          JOIN facturas_cliente f ON f.id=i.factura_id
                          WHERE i.envio_id=e.id AND f.estado='EMITIDA'
                      )
                )
                SELECT *, GREATEST(total-pagado, 0) AS saldo,
                          GREATEST(total-pagado-solicitado, 0) AS disponible
                  FROM (
                      SELECT * FROM facturas
                      UNION ALL
                      SELECT * FROM cargos
                  ) destinos
                 WHERE total > pagado
                 ORDER BY CASE WHEN clase='FACTURA' THEN 0 ELSE 1 END,
                          fecha, origen_id
                """,
                (cliente, cliente),
            )
            filas = [dict(fila) for fila in cur.fetchall()]
    for fila in filas:
        for campo in ("total", "pagado", "solicitado", "saldo", "disponible"):
            fila[campo] = _decimal_monto(fila.get(campo))
    return filas


def _armar_aplicaciones_documentales(
    cur,
    *,
    cliente_id: str,
    monto_pago: Decimal,
    destinos: List[tuple[str, int]],
    pago_excluir: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Bloquea cada objetivo y distribuye el pago en el orden elegido."""
    restante = monto_pago
    salida: List[Dict[str, Any]] = []
    for clase, origen_id in destinos:
        if restante <= 0:
            break
        if clase == "F":
            cur.execute(
                """
                SELECT id, cliente_id, tipo, estado, total
                  FROM facturas_cliente WHERE id=%s FOR UPDATE
                """,
                (origen_id,),
            )
            documento = cur.fetchone()
            if (
                not documento or documento["cliente_id"] != cliente_id
                or documento["tipo"] != "FC" or documento["estado"] != "EMITIDA"
            ):
                raise ValueError("Una factura seleccionada ya no está disponible.")
            cur.execute(
                """
                SELECT MIN(COALESCE(e.ambito, ea.ambito)) AS ambito_min,
                       MAX(COALESCE(e.ambito, ea.ambito)) AS ambito_max
                  FROM facturas_cliente_items i
             LEFT JOIN envios e ON e.id=i.envio_id
             LEFT JOIN ajustes_cliente a ON a.id=i.ajuste_id
             LEFT JOIN envios ea ON ea.solicitud_id=a.solicitud_id
                 WHERE i.factura_id=%s
                """,
                (origen_id,),
            )
            estado_doc = cur.fetchone()
            if (
                not estado_doc
                or estado_doc["ambito_min"] not in _AMBITOS_CONTABLES
                or estado_doc["ambito_min"] != estado_doc["ambito_max"]
            ):
                raise ValueError("La factura no tiene un ámbito contable válido.")
            # La aplicación directa a factura se repite una vez por cada ítem
            # en el JOIN anterior. Se calcula aparte para evitar multiplicarla.
            cur.execute(
                """
                SELECT COALESCE(SUM(monto_ars), 0) AS directo
                  FROM pagos_aplicaciones
                 WHERE factura_id=%s
                   AND estado IN ('SOLICITADA','APLICADA')
                   AND (%s::integer IS NULL OR pago_id<>%s::integer)
                """,
                (origen_id, pago_excluir, pago_excluir),
            )
            directo = _decimal_monto(cur.fetchone()["directo"])
            # cubierto sólo debe conservar las aplicaciones heredadas por
            # envíos; el OR del JOIN también incluyó las directas por ítem.
            cur.execute(
                """
                SELECT COALESCE(SUM(pa.monto_ars), 0) AS heredado
                  FROM pagos_aplicaciones pa
                 WHERE pa.envio_id IN (
                       SELECT envio_id FROM facturas_cliente_items
                        WHERE factura_id=%s AND envio_id IS NOT NULL
                 )
                   AND pa.estado IN ('SOLICITADA','APLICADA')
                   AND (%s::integer IS NULL OR pa.pago_id<>%s::integer)
                """,
                (origen_id, pago_excluir, pago_excluir),
            )
            cubierto = directo + _decimal_monto(cur.fetchone()["heredado"])
            total = _decimal_monto(documento["total"])
            ambito = estado_doc["ambito_min"]
            objetivo = {"factura_id": origen_id, "envio_id": None}
        else:
            cur.execute(
                """
                SELECT id, cliente_id, estado, monto_ars, ambito
                  FROM envios WHERE id=%s FOR UPDATE
                """,
                (origen_id,),
            )
            documento = cur.fetchone()
            if (
                not documento or documento["cliente_id"] != cliente_id
                or documento["estado"] != "ACTIVO"
                or documento["ambito"] not in _AMBITOS_CONTABLES
            ):
                raise ValueError("Un cargo seleccionado ya no está disponible.")
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM facturas_cliente_items i
                    JOIN facturas_cliente f ON f.id=i.factura_id
                    WHERE i.envio_id=%s AND f.estado='EMITIDA'
                ) AS facturado,
                COALESCE((
                    SELECT SUM(pa.monto_ars) FROM pagos_aplicaciones pa
                    WHERE pa.envio_id=%s
                      AND pa.estado IN ('SOLICITADA','APLICADA')
                      AND (%s::integer IS NULL OR pa.pago_id<>%s::integer)
                ), 0) AS cubierto
                """,
                (origen_id, origen_id, pago_excluir, pago_excluir),
            )
            estado_doc = cur.fetchone()
            if estado_doc["facturado"] and pago_excluir is None:
                raise ValueError("Un cargo seleccionado ya fue facturado.")
            total = _decimal_monto(documento["monto_ars"])
            cubierto = _decimal_monto(estado_doc["cubierto"])
            ambito = documento["ambito"]
            objetivo = {"factura_id": None, "envio_id": origen_id}
        disponible = total - cubierto
        if disponible <= 0:
            raise ValueError("Un documento seleccionado ya no tiene saldo disponible.")
        monto = min(restante, disponible)
        salida.append({**objetivo, "ambito": ambito, "monto": monto})
        restante -= monto
    return salida


@_conflictos_como_valueerror
def registrar_pago(
    cliente_id: str,
    fecha: str,        # "YYYY-MM-DD"
    monto_ars: Any,
    metodo: str,
    referencia: str = "",
    nota: str = "",
    estado: str = "APROBADO",
    comprobante: Optional[bytes] = None,
    comprobante_nombre: str = "",
    aplicaciones: Optional[Mapping[str, Any]] = None,
    destinos: Optional[Iterable[Any]] = None,
    actor_tipo: Optional[str] = None,
    actor_ref: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> int:
    """
    Alta de pago. El admin carga APROBADO (impacta el saldo al instante);
    el cliente informa PENDIENTE (no impacta hasta que el admin lo apruebe).
    """
    tipo = validar_comprobante(comprobante) if comprobante else None
    estado_normalizado = str(estado or "").strip().upper()
    if estado_normalizado not in {"PENDIENTE", "APROBADO", "RECHAZADO"}:
        raise ValueError("El estado del pago no es válido.")
    monto_decimal = _decimal_monto(monto_ars, permitir_cero=False)
    cliente_normalizado = cliente_id.upper()
    clave_idempotencia = _idempotency_key(idempotency_key)
    normalizadas = _normalizar_aplicaciones(aplicaciones) or {}
    destinos_normalizados = _normalizar_destinos_documentales(destinos)
    if normalizadas and destinos_normalizados:
        raise ValueError("No mezcles imputación por ámbito y por documento.")
    if sum(normalizadas.values(), Decimal("0")) > monto_decimal:
        raise ValueError("Las aplicaciones superan el monto del pago.")
    if estado_normalizado == "RECHAZADO" and normalizadas:
        raise ValueError("Un pago rechazado no admite aplicaciones.")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pagos (cliente_id, fecha, monto_ars, metodo, referencia,
                                   nota, estado, comprobante, comprobante_tipo,
                                   comprobante_nombre, idempotency_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (cliente_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL
                DO NOTHING
                RETURNING id
                """,
                (cliente_normalizado, fecha, monto_decimal, metodo, referencia, nota,
                 estado_normalizado,
                 psycopg2.Binary(comprobante) if comprobante else None,
                 tipo, comprobante_nombre[:160] if comprobante_nombre else None,
                 clave_idempotencia),
            )
            insertado = cur.fetchone()
            if not insertado:
                # El INSERT concurrente ya terminó antes de que ON CONFLICT
                # retorne. Se toma la fila para verificar que la clave no haya
                # sido reutilizada para otra operación financiera.
                cur.execute(
                    """
                    SELECT id, fecha, monto_ars, metodo, referencia, estado
                    FROM pagos
                    WHERE cliente_id = %s AND idempotency_key = %s
                    FOR UPDATE
                    """,
                    (cliente_normalizado, clave_idempotencia),
                )
                existente = cur.fetchone()
                if not existente:
                    raise RuntimeError("No se pudo recuperar el pago idempotente.")
                misma_operacion = (
                    _fecha_comparable(existente["fecha"]) == _fecha_comparable(fecha)
                    and _decimal_monto(existente["monto_ars"], permitir_cero=False)
                    == monto_decimal
                    and str(existente.get("metodo") or "") == str(metodo or "")
                    and str(existente.get("referencia") or "")
                    == str(referencia or "")
                    and str(existente.get("estado") or "APROBADO").upper()
                    == estado_normalizado
                )
                if not misma_operacion:
                    raise ValueError(
                        "La clave de idempotencia ya fue usada para otro pago."
                    )
                estado_aplicacion_esperado = (
                    "SOLICITADA"
                    if estado_normalizado == "PENDIENTE"
                    else "APLICADA"
                )
                cur.execute(
                    """
                    SELECT ambito, monto_ars, estado, factura_id, envio_id
                      FROM pagos_aplicaciones
                     WHERE pago_id=%s
                     ORDER BY id
                    """,
                    (existente["id"],),
                )
                filas_existentes = [dict(fila) for fila in cur.fetchall()]
                if destinos_normalizados is not None:
                    esperadas = _armar_aplicaciones_documentales(
                        cur,
                        cliente_id=cliente_normalizado,
                        monto_pago=monto_decimal,
                        destinos=destinos_normalizados,
                        pago_excluir=int(existente["id"]),
                    )
                    firma_existente = [
                        (
                            fila.get("factura_id"), fila.get("envio_id"),
                            _decimal_monto(fila["monto_ars"]), fila["estado"],
                        ) for fila in filas_existentes
                    ]
                    firma_esperada = [
                        (
                            fila["factura_id"], fila["envio_id"], fila["monto"],
                            estado_aplicacion_esperado,
                        ) for fila in esperadas
                    ]
                    if firma_existente != firma_esperada:
                        raise ValueError(
                            "La clave de idempotencia ya fue usada con otra "
                            "aplicación documental."
                        )
                else:
                    aplicaciones_existentes = {
                        str(fila["ambito"]): (
                            _decimal_monto(fila["monto_ars"]), str(fila["estado"])
                        )
                        for fila in filas_existentes
                    }
                    aplicaciones_esperadas = {
                        ambito: (monto, estado_aplicacion_esperado)
                        for ambito, monto in normalizadas.items()
                    }
                    if aplicaciones_existentes != aplicaciones_esperadas:
                        raise ValueError(
                            "La clave de idempotencia ya fue usada con otra "
                            "aplicación contable."
                        )
                # Reintento puro: no toca aplicaciones y no duplica auditoría.
                return int(existente["id"])

            pago_id = int(insertado["id"])
            estado_aplicacion = (
                "SOLICITADA" if estado_normalizado == "PENDIENTE" else "APLICADA"
            )
            documentales = []
            if destinos_normalizados is not None:
                documentales = _armar_aplicaciones_documentales(
                    cur,
                    cliente_id=cliente_normalizado,
                    monto_pago=monto_decimal,
                    destinos=destinos_normalizados,
                )
                for aplicacion in documentales:
                    cur.execute(
                        """
                        INSERT INTO pagos_aplicaciones (
                            pago_id, ambito, monto_ars, estado,
                            factura_id, envio_id, updated_at
                        ) VALUES (%s,%s,%s,%s,%s,%s,NOW())
                        """,
                        (
                            pago_id, aplicacion["ambito"], aplicacion["monto"],
                            estado_aplicacion, aplicacion["factura_id"],
                            aplicacion["envio_id"],
                        ),
                    )
            else:
                for ambito in _AMBITOS_CONTABLES:
                    monto = normalizadas.get(ambito)
                    if monto is None:
                        continue
                    cur.execute(
                        """
                        INSERT INTO pagos_aplicaciones
                            (pago_id, ambito, monto_ars, estado, updated_at)
                        VALUES (%s, %s, %s, %s, NOW())
                        """,
                        (pago_id, ambito, monto, estado_aplicacion),
                    )
            registrar_evento_con_cursor(
                cur,
                event="cuenta.registrar_pago",
                actor_type=actor_tipo or (
                    "cliente" if estado_normalizado == "PENDIENTE" else "admin"
                ),
                actor_ref=actor_ref or cliente_id.upper(),
                ip=None,
                method=None,
                path=None,
                status_code=201,
                success=True,
                request_id=None,
                metadata={
                    "pago_id": pago_id,
                    "cliente_id": cliente_normalizado,
                    "monto_ars": str(monto_decimal),
                    "estado": estado_normalizado,
                    "aplicaciones": {
                        clave: str(valor) for clave, valor in normalizadas.items()
                    },
                    "documentos": [
                        {
                            "factura_id": fila["factura_id"],
                            "envio_id": fila["envio_id"],
                            "monto_ars": str(fila["monto"]),
                        }
                        for fila in documentales
                    ],
                },
            )
            return pago_id


def pagos_pendientes() -> List[Dict[str, Any]]:
    """Cola admin con la imputación SOLICITADA, agregada sin consultas N+1."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    p.id, p.cliente_id, p.fecha, p.monto_ars, p.metodo,
                    p.referencia, p.nota, p.comprobante_nombre,
                    (p.comprobante IS NOT NULL) AS tiene_comprobante,
                    p.created_at,
                    COALESCE(SUM(pa.monto_ars) FILTER (
                        WHERE pa.estado = 'SOLICITADA'
                          AND pa.ambito = 'NACIONAL'
                    ), 0) AS monto_nacional,
                    COALESCE(SUM(pa.monto_ars) FILTER (
                        WHERE pa.estado = 'SOLICITADA'
                          AND pa.ambito = 'INTERNACIONAL'
                    ), 0) AS monto_internacional,
                    COALESCE(JSONB_AGG(JSONB_BUILD_OBJECT(
                        'factura_id', pa.factura_id,
                        'envio_id', pa.envio_id,
                        'ambito', pa.ambito,
                        'monto', pa.monto_ars,
                        'documento', CASE
                            WHEN f.id IS NOT NULL THEN
                                f.tipo || ' ' || LPAD(f.punto_venta::text,4,'0')
                                || '-' || LPAD(f.numero::text,8,'0')
                            WHEN e.id IS NOT NULL THEN
                                'Envío ' || COALESCE(NULLIF(BTRIM(e.tracking),''),
                                                     e.id::text)
                            ELSE 'Aplicación histórica ' || pa.ambito
                        END
                    ) ORDER BY pa.id) FILTER (
                        WHERE pa.id IS NOT NULL AND pa.estado='SOLICITADA'
                    ), '[]'::jsonb) AS detalle_aplicaciones
                FROM pagos p
                LEFT JOIN pagos_aplicaciones pa ON pa.pago_id = p.id
                LEFT JOIN facturas_cliente f ON f.id=pa.factura_id
                LEFT JOIN envios e ON e.id=pa.envio_id
                WHERE p.estado = 'PENDIENTE'
                GROUP BY p.id
                ORDER BY p.created_at ASC
            """)
            filas = [dict(r) for r in cur.fetchall()]
    for fila in filas:
        nacional = _decimal_monto(fila.get("monto_nacional"))
        internacional = _decimal_monto(fila.get("monto_internacional"))
        fila["monto_nacional"] = nacional
        fila["monto_internacional"] = internacional
        fila["aplicaciones"] = {
            ambito: monto
            for ambito, monto in (
                ("NACIONAL", nacional), ("INTERNACIONAL", internacional)
            )
            if monto > 0
        }
    return filas


@_conflictos_como_valueerror
def resolver_pago(
    pago_id: int,
    aprobar: bool,
    aplicaciones: Optional[Mapping[str, Any]] = None,
    *,
    actor_tipo: str = "admin",
    actor_ref: Optional[str] = None,
) -> bool:
    """
    Resuelve un pago y, al aprobar, puede fijar su imputación NACIONAL/INTERNACIONAL.

    Sólo resuelve un PENDIENTE. Al aprobarlo, None confirma las aplicaciones
    SOLICITADA que eligió el cliente; un mapping legacy explícito las reemplaza
    por distribución de ámbito. Un APROBADO es inmutable en este flujo.
    """
    normalizadas = _normalizar_aplicaciones(aplicaciones)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, cliente_id, monto_ars,
                       COALESCE(estado, 'APROBADO') AS estado
                FROM pagos
                WHERE id = %s
                FOR UPDATE
                """,
                (pago_id,),
            )
            pago = cur.fetchone()
            if not pago:
                return False

            estado_actual = str(pago["estado"] or "APROBADO").upper()
            if not aprobar:
                if estado_actual != "PENDIENTE" or normalizadas not in (None, {}):
                    return False
                cur.execute(
                    "DELETE FROM pagos_aplicaciones WHERE pago_id = %s",
                    (pago_id,),
                )
                cur.execute(
                    """
                    UPDATE pagos SET estado = 'RECHAZADO'
                    WHERE id = %s AND estado = 'PENDIENTE'
                    RETURNING id
                    """,
                    (pago_id,),
                )
                cambio = cur.fetchone() is not None
                if cambio:
                    registrar_evento_con_cursor(
                        cur,
                        event="cuenta.resolver_pago",
                        actor_type=actor_tipo,
                        actor_ref=actor_ref or "admin",
                        ip=None,
                        method=None,
                        path=None,
                        status_code=200,
                        success=True,
                        request_id=None,
                        metadata={"pago_id": pago_id, "decision": "RECHAZADO"},
                    )
                return cambio

            if estado_actual != "PENDIENTE":
                return False

            monto_pago = _decimal_monto(pago["monto_ars"], permitir_cero=False)
            if normalizadas is not None and sum(
                normalizadas.values(), Decimal("0")
            ) > monto_pago:
                raise ValueError("Las aplicaciones superan el monto del pago.")

            cur.execute(
                """
                UPDATE pagos SET estado = 'APROBADO'
                WHERE id = %s AND estado = 'PENDIENTE'
                RETURNING id
                """,
                (pago_id,),
            )
            if cur.fetchone() is None:
                return False

            cambio_aplicaciones = False
            if normalizadas is None:
                cur.execute(
                    """
                    UPDATE pagos_aplicaciones
                       SET estado='APLICADA', updated_at=NOW()
                     WHERE pago_id=%s AND estado='SOLICITADA'
                    """,
                    (pago_id,),
                )
                cambio_aplicaciones = cur.rowcount > 0
            else:
                cur.execute(
                    """
                    SELECT ambito, monto_ars, estado
                    FROM pagos_aplicaciones
                    WHERE pago_id = %s
                    ORDER BY ambito
                    """,
                    (pago_id,),
                )
                actuales = {
                    str(fila["ambito"]): (
                        _decimal_monto(fila["monto_ars"]), str(fila["estado"])
                    )
                    for fila in cur.fetchall()
                }
                deseadas = {
                    ambito: (monto, "APLICADA")
                    for ambito, monto in normalizadas.items()
                }
                cambio_aplicaciones = actuales != deseadas
                if cambio_aplicaciones:
                    cur.execute(
                        "DELETE FROM pagos_aplicaciones WHERE pago_id = %s",
                        (pago_id,),
                    )
                    for ambito in _AMBITOS_CONTABLES:
                        monto = normalizadas.get(ambito)
                        if monto is None:
                            continue
                        cur.execute(
                            """
                            INSERT INTO pagos_aplicaciones
                                (pago_id, ambito, monto_ars, estado, updated_at)
                            VALUES (%s, %s, %s, 'APLICADA', NOW())
                            """,
                            (pago_id, ambito, monto),
                        )

            cambio = True
            if cambio:
                registrar_evento_con_cursor(
                    cur,
                    event="cuenta.resolver_pago",
                    actor_type=actor_tipo,
                    actor_ref=actor_ref or "admin",
                    ip=None,
                    method=None,
                    path=None,
                    status_code=200,
                    success=True,
                    request_id=None,
                    metadata={
                        "pago_id": pago_id,
                        "decision": "APROBADO",
                        "aplicaciones": {
                            clave: str(valor)
                            for clave, valor in (normalizadas or {}).items()
                        },
                        "solicitud_documental_confirmada": normalizadas is None,
                    },
                )
            return cambio


def get_comprobante(pago_id: int, cliente_id: Optional[str] = None):
    """
    (contenido, tipo, nombre) del comprobante, o None.
    Con cliente_id se exige que el pago sea de ESE cliente: el portal sólo
    muestra comprobantes propios; el admin (cliente_id=None) ve todos.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            if cliente_id:
                cur.execute("""
                    SELECT comprobante, comprobante_tipo, comprobante_nombre
                    FROM pagos WHERE id = %s AND cliente_id = %s
                """, (pago_id, cliente_id.strip().upper()))
            else:
                cur.execute("""
                    SELECT comprobante, comprobante_tipo, comprobante_nombre
                    FROM pagos WHERE id = %s
                """, (pago_id,))
            row = cur.fetchone()
    if not row or not row["comprobante"]:
        return None
    return (bytes(row["comprobante"]), row["comprobante_tipo"] or "application/octet-stream",
            row["comprobante_nombre"] or f"comprobante_{pago_id}")


@_conflictos_como_valueerror
def registrar_envio(
    cliente_id: str,
    fecha: str,        # "YYYY-MM-DD"
    monto_ars: Any,
    estado: str = "ACTIVO",
    descripcion: str = "",
    tracking: str = "",
    ambito: str = "",
    actor_tipo: str = "admin",
    actor_ref: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> int:
    """Alta manual de un cargo (débito) en la cuenta corriente.

    Nunca escribe ``envios.nro_fc``, ``factura_pdf`` ni ``factura_nombre``:
    esas columnas son legado de sólo lectura, protegidas además por trigger.
    La documentación fiscal se hace por lote en ``facturas_cliente``.
    """
    ambito_normalizado = _ambito_contable(ambito)
    monto_decimal = _decimal_monto(monto_ars, permitir_cero=False)
    cliente_normalizado = cliente_id.upper()
    clave_idempotencia = _idempotency_key(idempotency_key)
    estado_normalizado = str(estado or "").strip().upper()
    if estado_normalizado not in {"ACTIVO", "CANCELADO"}:
        raise ValueError("El cargo manual debe ser ACTIVO o CANCELADO; una NC no es FC.")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO envios
                    (cliente_id, fecha, monto_ars, estado, descripcion,
                     tracking, ambito, idempotency_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (cliente_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL
                DO NOTHING
                RETURNING id
                """,
                (cliente_normalizado, fecha, monto_decimal,
                 estado_normalizado, descripcion, tracking,
                 ambito_normalizado, clave_idempotencia),
            )
            insertado = cur.fetchone()
            if not insertado:
                cur.execute(
                    """
                    SELECT id, fecha, monto_ars, estado, descripcion,
                           tracking, ambito
                    FROM envios
                    WHERE cliente_id = %s AND idempotency_key = %s
                    FOR UPDATE
                    """,
                    (cliente_normalizado, clave_idempotencia),
                )
                existente = cur.fetchone()
                if not existente:
                    raise RuntimeError(
                        "No se pudo recuperar el cargo idempotente."
                    )
                misma_operacion = (
                    _fecha_comparable(existente["fecha"])
                    == _fecha_comparable(fecha)
                    and _decimal_monto(
                        existente["monto_ars"], permitir_cero=False
                    ) == monto_decimal
                    and str(existente.get("estado") or "").upper()
                    == estado_normalizado
                    and str(existente.get("descripcion") or "")
                    == str(descripcion or "")
                    and str(existente.get("tracking") or "")
                    == str(tracking or "")
                    and str(existente.get("ambito") or "").upper()
                    == ambito_normalizado
                )
                if not misma_operacion:
                    raise ValueError(
                        "La clave de idempotencia ya fue usada para otro cargo."
                    )
                return int(existente["id"])

            envio_id = int(insertado["id"])
            registrar_evento_con_cursor(
                cur,
                event="cuenta.registrar_cargo_manual",
                actor_type=actor_tipo,
                actor_ref=actor_ref or "admin",
                ip=None,
                method=None,
                path=None,
                status_code=201,
                success=True,
                request_id=None,
                metadata={
                    "envio_id": envio_id,
                    "cliente_id": cliente_normalizado,
                    "monto_ars": str(monto_decimal),
                    "estado": estado_normalizado,
                    "ambito": ambito_normalizado,
                },
            )
            return envio_id


# ``facturar_cargo`` (FC por cargo escribiendo envios.nro_fc/factura_pdf) fue
# retirada: el legado quedó de sólo lectura y la base rechaza esas escrituras
# con el trigger trg_proteger_fc_legacy_envios. La documentación fiscal vive
# en servicios/facturacion_clientes.py.


def clasificar_cargo_sin_ambito(
    envio_id: int,
    ambito: str,
    *,
    cliente_id: str,
    actor_tipo: str = "admin",
    actor_ref: Optional[str] = None,
):
    """Saca un cargo de cuarentena sin poder reclasificar uno ya cerrado."""
    ambito_normalizado = _ambito_contable(ambito)
    cliente_normalizado = str(cliente_id or "").strip().upper()
    if not cliente_normalizado:
        raise ValueError("Falta el cliente propietario del cargo.")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, cliente_id, monto_ars, ambito
                FROM envios
                WHERE id = %s AND cliente_id = %s
                FOR UPDATE
                """,
                (envio_id, cliente_normalizado),
            )
            cargo = cur.fetchone()
            if (
                not cargo
                or str(cargo.get("ambito") or "").upper() in _AMBITOS_CONTABLES
            ):
                return False
            cur.execute(
                """
                UPDATE envios
                SET ambito = %s
                WHERE id = %s AND cliente_id = %s
                  AND (ambito IS NULL OR ambito NOT IN ('NACIONAL', 'INTERNACIONAL'))
                RETURNING id, cliente_id, monto_ars, ambito
                """,
                (ambito_normalizado, envio_id, cliente_normalizado),
            )
            actualizado = cur.fetchone()
            if not actualizado:
                return False
            resultado = dict(actualizado)
            resultado["monto_ars"] = _decimal_monto(resultado["monto_ars"])
            registrar_evento_con_cursor(
                cur,
                event="cuenta.clasificar_cargo",
                actor_type=actor_tipo,
                actor_ref=actor_ref or "admin",
                ip=None,
                method=None,
                path=None,
                status_code=200,
                success=True,
                request_id=None,
                metadata={
                    "envio_id": envio_id,
                    "cliente_id": resultado["cliente_id"],
                    "monto_ars": str(resultado["monto_ars"]),
                    "ambito": ambito_normalizado,
                },
            )
            return resultado


def cancelar_envio(
    envio_id: int,
    *,
    cliente_id: Optional[str] = None,
    actor_tipo: str = "admin",
    actor_ref: Optional[str] = None,
):
    """Transición única ACTIVO→CANCELADO con ownership y auditoría atómica."""
    cliente_normalizado = (
        str(cliente_id or "").strip().upper() if cliente_id is not None else None
    )
    if cliente_id is not None and not cliente_normalizado:
        raise ValueError("Falta el cliente propietario del cargo.")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE envios
                SET estado = 'CANCELADO'
                WHERE id = %s
                  AND estado = 'ACTIVO'
                  AND NULLIF(BTRIM(nro_fc), '') IS NULL
                  AND (%s IS NULL OR cliente_id = %s)
                RETURNING id, cliente_id, monto_ars, ambito, estado
                """,
                (envio_id, cliente_normalizado, cliente_normalizado),
            )
            actualizado = cur.fetchone()
            if not actualizado:
                return False
            resultado = dict(actualizado)
            resultado["monto_ars"] = _decimal_monto(resultado["monto_ars"])
            registrar_evento_con_cursor(
                cur,
                event="cuenta.cancelar_cargo",
                actor_type=actor_tipo,
                actor_ref=actor_ref or "admin",
                ip=None,
                method=None,
                path=None,
                status_code=200,
                success=True,
                request_id=None,
                metadata={
                    "envio_id": resultado["id"],
                    "cliente_id": resultado["cliente_id"],
                    "monto_ars": str(resultado["monto_ars"]),
                    "ambito": resultado.get("ambito"),
                    "estado": "CANCELADO",
                },
            )
            return resultado


def cargar_guia_emitida(solicitud_id: int) -> bool:
    """
    Débito automático: al emitirse una guía, el cargo entra solo a la cuenta
    corriente del cliente por `precio_tauro_ars` (lo que TAURO le cobra, con
    su margen ya incluido — no confundir con `precio_cliente_final_ars`, que
    es lo que el cliente le cobra a SU comprador).

    Antes esto era doble carga manual: el admin emitía la guía y después
    tenía que acordarse de facturarla a mano. Si se olvidaba, el saldo del
    cliente mentía en silencio.

    Idempotente por el índice único sobre solicitud_id: reintentos, doble
    click o un reinicio a mitad de camino no pueden duplicar el cargo.
    Devuelve True cuando el cargo quedó garantizado (nuevo o preexistente).
    Si falta solicitud/precio, lanza: la guía ya existe y el caller debe
    conservar una tarea persistente de conciliación.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.cliente_id, s.precio_tauro_ars, s.tracking,
                       s.courier, s.ambito, s.producto_alias,
                       s.remitente_pais, s.destino_pais,
                       r.solicitud_anterior_id,
                       r.tracking_anterior,
                       r.estado AS reemision_estado,
                       e_actual.id AS cargo_actual_id,
                       e_actual.estado AS cargo_actual_estado
                FROM solicitudes_guia s
                LEFT JOIN solicitudes_guia_reemisiones r
                  ON r.solicitud_nueva_id=s.id
                LEFT JOIN envios e_actual ON e_actual.solicitud_id=s.id
                WHERE s.id = %s
                FOR UPDATE OF s
            """, (solicitud_id,))
            sol = cur.fetchone()

            if not sol:
                raise ValueError(f"La solicitud {solicitud_id} no existe: sin cargo")
            monto = _decimal_monto(sol["precio_tauro_ars"])
            if monto <= 0:
                # Sin precio no se inventa un cargo: se avisa para que el
                # admin lo facture a mano, que es mejor que un débito en 0
                # que nadie revisaría jamás.
                raise ValueError(
                    f"La solicitud {solicitud_id} no tiene precio_tauro_ars: "
                    "el cargo no se generó"
                )

            from servicios.couriers_urls import ambito_envio
            ambito = ambito_envio(dict(sol))
            if ambito == "sin_clasificar":
                raise ValueError(
                    f"La solicitud {solicitud_id} no tiene una ruta suficiente "
                    "para clasificar su cargo: requiere revisión"
                )
            ambito = ambito.upper()

            anterior = None
            if sol.get("solicitud_anterior_id"):
                cur.execute(
                    """
                    SELECT s.id, s.cliente_id, s.estado, s.tracking,
                           s.origen_plataforma,
                           e.id AS cargo_id, e.estado AS cargo_estado,
                           e.nro_fc AS cargo_nro_fc,
                           EXISTS (
                               SELECT 1 FROM ajustes_cliente a
                               WHERE a.solicitud_id=s.id
                                 AND a.estado <> 'ANULADO'
                           ) AS tiene_ajustes_contables,
                           EXISTS (
                               SELECT 1 FROM recolecciones p
                               WHERE p.solicitud_id=s.id
                                 AND p.estado IN (
                                   'AGENDANDO','AGENDADA','CANCELANDO',
                                   'VERIFICAR_COURIER'
                                 )
                           ) AS tiene_recoleccion_activa
                    FROM solicitudes_guia s
                    JOIN envios e ON e.solicitud_id=s.id
                    WHERE s.id=%s AND s.cliente_id=%s
                    FOR UPDATE OF s, e
                    """,
                    (int(sol["solicitud_anterior_id"]), sol["cliente_id"]),
                )
                anterior = cur.fetchone()
                if sol.get("reemision_estado") == "EMITIDA":
                    if (anterior and anterior.get("estado") == "REEMPLAZADO"
                            and anterior.get("cargo_estado") == "CANCELADO"
                            and sol.get("cargo_actual_id")
                            and sol.get("cargo_actual_estado") == "ACTIVO"):
                        return True
                    raise ValueError(
                        "El reemplazo figura emitido pero sus cargos no conservan "
                        "el estado esperado; requiere conciliación."
                    )
                if (not anterior or anterior.get("estado") != "GUIA_LISTA"
                        or anterior.get("cargo_estado") != "ACTIVO"
                        or str(anterior.get("cargo_nro_fc") or "").strip()
                        or anterior.get("tiene_ajustes_contables")
                        or anterior.get("tiene_recoleccion_activa")):
                    raise ValueError(
                        "La guía anterior cambió de estado o fue facturada; "
                        "la reemisión necesita conciliación y no se aplicó."
                    )

            descripcion = (f"Guía {sol['tracking'] or 's/n'} · "
                           f"{sol['producto_alias'] or 'envío'} → {sol['destino_pais'] or ''} · "
                           f"{(sol['courier'] or 'FEDEX').upper()} · cargo automático")
            cur.execute("""
                INSERT INTO envios
                    (cliente_id, fecha, nro_fc, monto_ars, estado, descripcion,
                     tracking, solicitud_id, ambito)
                VALUES (%s, CURRENT_DATE, '', %s, 'ACTIVO', %s, %s, %s, %s)
                ON CONFLICT (solicitud_id) WHERE solicitud_id IS NOT NULL
                DO UPDATE SET ambito = COALESCE(envios.ambito, EXCLUDED.ambito)
                WHERE envios.ambito IS NULL OR envios.ambito = EXCLUDED.ambito
                RETURNING id
            """, (sol["cliente_id"].upper(), monto, descripcion,
                  sol["tracking"] or "", solicitud_id, ambito))
            fila = cur.fetchone()

            if not fila:
                raise ValueError(
                    f"El cargo de la solicitud {solicitud_id} ya existe en otro ámbito; "
                    "requiere conciliación"
                )

            if anterior:
                # El nuevo cargo y la baja del anterior viven en la misma
                # transacción. Nunca puede quedar la cuenta corriente con dos
                # guías activas por una sola corrección.
                cur.execute(
                    """
                    UPDATE envios
                    SET estado='CANCELADO',
                        descripcion=COALESCE(descripcion, '') ||
                          %s
                    WHERE id=%s AND estado='ACTIVO'
                      AND COALESCE(nro_fc, '')=''
                    RETURNING id
                    """,
                    (
                        f" · reemplazada por guía {sol['tracking']}",
                        anterior["cargo_id"],
                    ),
                )
                if cur.fetchone() is None:
                    raise ValueError(
                        "No se pudo retirar el cargo de la guía anterior; "
                        "la reemisión necesita conciliación."
                    )
                cur.execute(
                    """
                    UPDATE solicitudes_guia
                    SET estado='REEMPLAZADO', updated_at=NOW()
                    WHERE id=%s AND estado='GUIA_LISTA'
                    RETURNING id
                    """,
                    (anterior["id"],),
                )
                if cur.fetchone() is None:
                    raise ValueError(
                        "No se pudo marcar la guía anterior como reemplazada."
                    )
                cur.execute(
                    """
                    UPDATE solicitudes_guia_reemisiones
                    SET estado='EMITIDA', tracking_nuevo=%s,
                        riesgo_estado='VIGILAR',
                        tracking_anterior_consultado_at=NULL,
                        tracking_anterior_error=NULL,
                        tracking_anterior_error_at=NULL,
                        completed_at=NOW(), updated_at=NOW()
                    WHERE solicitud_anterior_id=%s
                      AND solicitud_nueva_id=%s
                      AND estado='PENDIENTE'
                    RETURNING id
                    """,
                    (sol["tracking"], anterior["id"], solicitud_id),
                )
                if cur.fetchone() is None:
                    raise ValueError(
                        "No se pudo cerrar el registro de reemplazo de la guía."
                    )
                # Si nació de Shopify/Tiendanube, el pedido apunta a la guía
                # final; así sólo se comunica el tracking vigente.
                if str(anterior.get("origen_plataforma") or "").strip():
                    cur.execute(
                        """
                        UPDATE pedidos_tienda
                        SET solicitud_id=%s
                        WHERE cliente_id=%s AND solicitud_id=%s
                        """,
                        (solicitud_id, sol["cliente_id"], anterior["id"]),
                    )
                registrar_evento_con_cursor(
                    cur,
                    event="portal.reemision_emitida",
                    actor_type="sistema",
                    actor_ref="cuenta_corriente",
                    ip=None,
                    method=None,
                    path=None,
                    status_code=200,
                    success=True,
                    request_id=None,
                    metadata={
                        "solicitud_anterior_id": int(anterior["id"]),
                        "solicitud_nueva_id": int(solicitud_id),
                        "tracking_anterior": anterior.get("tracking"),
                        "tracking_nuevo": sol.get("tracking"),
                        "cargo_anterior_id": int(anterior["cargo_id"]),
                        "cargo_nuevo_id": int(fila["id"]),
                    },
                )

    if fila:
        print(f"[cta_cte] cargo automático: solicitud {solicitud_id} → "
              f"ARS {monto:,.0f} a {sol['cliente_id']}")
        return True
    print(f"[cta_cte] la solicitud {solicitud_id} ya tenía su cargo: no se duplica")
    return True


def get_envios_cliente(cliente: str) -> List[Dict[str, Any]]:
    """Todos los envíos del cliente — para el admin."""
    cliente = cliente.strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM envios WHERE cliente_id = %s ORDER BY fecha DESC",
                (cliente,),
            )
            rows = cur.fetchall()
    return [dict(r) for r in rows]
