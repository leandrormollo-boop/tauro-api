# ============================================================
# Servicio de cuenta corriente — PostgreSQL
# ============================================================
# Lee envíos/facturas y pagos directamente de la base de datos.
# El admin carga los datos desde el panel.
# ============================================================

from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from typing import Any, Dict, List, Mapping, Optional

import psycopg2

from core.database import get_conn
from servicios.auditoria import registrar_evento_con_cursor


_CENTAVO = Decimal("0.01")
_AMBITOS_CONTABLES = ("NACIONAL", "INTERNACIONAL")
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


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
                SELECT COALESCE(SUM(monto_ars), 0) AS total
                FROM envios
                WHERE cliente_id = %s
                  AND estado NOT IN ('CANCELADO', 'NC')
                """,
                (cliente,),
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
                SELECT id, fecha, nro_fc, monto_ars, descripcion,
                       (factura_pdf IS NOT NULL) AS tiene_pdf
                FROM envios
                WHERE cliente_id = %s
                  AND estado NOT IN ('CANCELADO', 'NC')
                  AND monto_ars > 0
                ORDER BY fecha DESC
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
    where_activos = "WHERE c.activo = TRUE" if solo_activos else ""
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
        )
        SELECT
            c.cliente_id,
            c.email,
            c.nombre,
            c.activo,
            COALESCE(fact.facturado, 0) AS facturado,
            COALESCE(pag.pagado, 0)    AS pagado
        FROM clientes c
        LEFT JOIN fact ON fact.cliente_id = c.cliente_id
        LEFT JOIN pag  ON pag.cliente_id  = c.cliente_id
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
    debe_nac = _decimal_monto(fila.get("debe_nacional"))
    debe_int = _decimal_monto(fila.get("debe_internacional"))
    debe_sc = _decimal_monto(fila.get("debe_sin_clasificar"))
    facturado_nac = _decimal_monto(fila.get("facturado_nacional"))
    facturado_int = _decimal_monto(fila.get("facturado_internacional"))
    facturado_sc = _decimal_monto(fila.get("facturado_sin_clasificar"))
    pendiente_nac = debe_nac - facturado_nac
    pendiente_int = debe_int - facturado_int
    pendiente_sc = debe_sc - facturado_sc
    haber_nac = _decimal_monto(fila.get("haber_nacional"))
    haber_int = _decimal_monto(fila.get("haber_internacional"))
    aprobado = _decimal_monto(fila.get("pagos_aprobados"))
    pendiente = _decimal_monto(fila.get("pagos_pendientes"))
    aplicado = haber_nac + haber_int
    if aplicado > aprobado:
        raise RuntimeError(
            "Invariante contable rota: las aplicaciones superan los pagos aprobados."
        )
    sin_imputar = aprobado - aplicado
    saldo_nac = debe_nac - haber_nac
    saldo_int = debe_int - haber_int
    debe_total = debe_nac + debe_int + debe_sc
    saldo_antes_sin_imputar = saldo_nac + saldo_int + debe_sc
    saldo_consolidado = debe_total - aprobado
    if aplicado + sin_imputar != aprobado:
        raise RuntimeError("Invariante contable rota: el crédito aprobado no cierra.")
    if saldo_antes_sin_imputar - sin_imputar != saldo_consolidado:
        raise RuntimeError("Invariante contable rota: el saldo consolidado no cierra.")
    nacional = {
        "facturado_ars": facturado_nac,
        "pendiente_facturacion_ars": pendiente_nac,
        "debe_ars": debe_nac,
        "haber_ars": haber_nac,
        "saldo_ars": saldo_nac,
    }
    internacional = {
        "facturado_ars": facturado_int,
        "pendiente_facturacion_ars": pendiente_int,
        "debe_ars": debe_int,
        "haber_ars": haber_int,
        "saldo_ars": saldo_int,
    }
    consolidado = {
        "facturado_ars": facturado_nac + facturado_int + facturado_sc,
        "pendiente_facturacion_ars": pendiente_nac + pendiente_int + pendiente_sc,
        "debe_ars": debe_total,
        # Consolidado sí reconoce todo pago aprobado, incluso lo no imputado.
        "haber_ars": aprobado,
        "haber_aplicado_ars": aplicado,
        "pagos_aprobados_ars": aprobado,
        "credito_aprobado_sin_imputar_ars": sin_imputar,
        "saldo_antes_credito_sin_imputar_ars": saldo_antes_sin_imputar,
        "saldo_ars": saldo_consolidado,
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
                              AND NULLIF(BTRIM(nro_fc), '') IS NOT NULL
                        ), 0) AS facturado_nacional,
                        COALESCE(SUM(monto_ars) FILTER (
                            WHERE ambito = 'INTERNACIONAL'
                              AND NULLIF(BTRIM(nro_fc), '') IS NOT NULL
                        ), 0) AS facturado_internacional,
                        COALESCE(SUM(monto_ars) FILTER (
                            WHERE (ambito IS NULL
                               OR ambito NOT IN ('NACIONAL', 'INTERNACIONAL'))
                              AND NULLIF(BTRIM(nro_fc), '') IS NOT NULL
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
                SELECT cargos.*, aplicaciones.*, pagos_totales.*
                FROM cargos CROSS JOIN aplicaciones CROSS JOIN pagos_totales
                """,
                (cliente, cliente, cliente),
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
    if tipo_filtro not in {"todos", "cargos", "pagos", "revision"}:
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
                CASE WHEN NULLIF(BTRIM(e.nro_fc), '') IS NOT NULL
                     THEN 'FC' ELSE 'PENDIENTE_FACTURA' END AS tipo,
                CASE WHEN e.ambito IN ('NACIONAL', 'INTERNACIONAL')
                     THEN e.ambito ELSE 'SIN_CLASIFICAR' END AS ambito,
                COALESCE(NULLIF(BTRIM(e.nro_fc), ''), e.descripcion, '') AS concepto,
                NULL::text AS referencia,
                e.monto_ars AS debe_ars,
                0::numeric AS haber_ars,
                e.monto_ars AS monto_ars,
                e.estado,
                (NULLIF(BTRIM(e.nro_fc), '') IS NOT NULL) AS facturado,
                e.id AS envio_id,
                NULL::integer AS pago_id,
                CASE WHEN e.factura_pdf IS NOT NULL
                     THEN '/portal/facturas/' || e.id::text || '/pdf' END AS archivo_url
            FROM envios e
            WHERE e.cliente_id = %s
              AND e.estado NOT IN ('CANCELADO', 'NC')
              AND e.monto_ars > 0

            UNION ALL

            SELECT
                p.fecha, pa.updated_at, 30, pa.id, 'PAGO', pa.ambito,
                BTRIM(CONCAT_WS(' ', p.metodo, p.referencia)), p.referencia,
                0::numeric, pa.monto_ars, pa.monto_ars, 'APROBADO',
                FALSE, NULL::integer, p.id,
                CASE WHEN p.comprobante IS NOT NULL
                     THEN '/portal/pagos/' || p.id::text || '/comprobante' END
            FROM pagos_aplicaciones pa
            JOIN pagos p ON p.id = pa.pago_id
            WHERE p.cliente_id = %s
              AND COALESCE(p.estado, 'APROBADO') = 'APROBADO'
              AND pa.estado = 'APLICADA'

            UNION ALL

            SELECT
                p.fecha, p.created_at, 20, p.id, 'PAGO', 'SIN_IMPUTAR',
                BTRIM(CONCAT_WS(' ', 'Crédito sin imputar', p.metodo)), p.referencia,
                0::numeric, p.monto_ars - COALESCE(ap.aplicado, 0),
                p.monto_ars - COALESCE(ap.aplicado, 0), 'APROBADO',
                FALSE, NULL::integer, p.id,
                CASE WHEN p.comprobante IS NOT NULL
                     THEN '/portal/pagos/' || p.id::text || '/comprobante' END
            FROM pagos p
            LEFT JOIN aplicaciones_pago ap ON ap.pago_id = p.id
            WHERE p.cliente_id = %s
              AND COALESCE(p.estado, 'APROBADO') = 'APROBADO'
              AND p.monto_ars > COALESCE(ap.aplicado, 0)

            UNION ALL

            SELECT
                p.fecha, p.created_at, 10, p.id, 'PAGO_PENDIENTE', 'SIN_IMPUTAR',
                BTRIM(CONCAT_WS(' ', p.metodo, p.referencia)), p.referencia,
                0::numeric, 0::numeric, p.monto_ars, 'PENDIENTE',
                FALSE, NULL::integer, p.id,
                CASE WHEN p.comprobante IS NOT NULL
                     THEN '/portal/pagos/' || p.id::text || '/comprobante' END
            FROM pagos p
            WHERE p.cliente_id = %s
              AND p.estado = 'PENDIENTE'
        ),
        filtrados AS (
            SELECT * FROM movimientos
            WHERE (%s IS NULL OR ambito = %s)
              AND (
                  %s = 'todos'
                  OR (%s = 'cargos' AND tipo IN ('FC', 'PENDIENTE_FACTURA'))
                  OR (%s = 'pagos' AND tipo = 'PAGO')
                  OR (%s = 'revision' AND tipo = 'PAGO_PENDIENTE')
              )
        )
    """
    filtros = (
        cliente, cliente, cliente, cliente,
        ambito_sql, ambito_sql,
        tipo_filtro, tipo_filtro, tipo_filtro, tipo_filtro,
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
        for campo in ("debe_ars", "haber_ars", "monto_ars"):
            item[campo] = Decimal(str(item.get(campo) or 0)).quantize(_CENTAVO)
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
                cur.execute(
                    """
                    SELECT ambito, monto_ars, estado
                    FROM pagos_aplicaciones
                    WHERE pago_id = %s
                    ORDER BY ambito
                    """,
                    (existente["id"],),
                )
                aplicaciones_existentes = {
                    str(fila["ambito"]): (
                        _decimal_monto(fila["monto_ars"]),
                        str(fila["estado"]),
                    )
                    for fila in cur.fetchall()
                }
                estado_aplicacion_esperado = (
                    "SOLICITADA"
                    if estado_normalizado == "PENDIENTE"
                    else "APLICADA"
                )
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
                    ), 0) AS monto_internacional
                FROM pagos p
                LEFT JOIN pagos_aplicaciones pa ON pa.pago_id = p.id
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

    Sólo resuelve un PENDIENTE. Al aprobarlo, None descarta la solicitud del
    cliente y deja todo como crédito sin imputar; un mapping explícito fija la
    decisión atómicamente. Un APROBADO es inmutable en este flujo, incluso si
    llega una segunda aprobación concurrente. Nunca hay FIFO ni backfill.
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

            # Una solicitud del cliente nunca se convierte en haber por sí
            # sola. Si el caller no envía decisión explícita, se aprueba todo
            # como crédito sin imputar.
            if normalizadas is None:
                normalizadas = {}
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
            if normalizadas is not None:
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


def registrar_envio(
    cliente_id: str,
    fecha: str,        # "YYYY-MM-DD"
    monto_ars: Any,
    nro_fc: str = "",
    estado: str = "ACTIVO",
    descripcion: str = "",
    tracking: str = "",
    factura_pdf: Optional[bytes] = None,
    factura_nombre: str = "",
    ambito: str = "",
    actor_tipo: str = "admin",
    actor_ref: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> int:
    # La factura adjunta se valida por contenido igual que los comprobantes:
    # acá sólo tiene sentido un PDF o una foto del documento.
    if factura_pdf:
        validar_comprobante(factura_pdf)
    ambito_normalizado = _ambito_contable(ambito)
    monto_decimal = _decimal_monto(monto_ars, permitir_cero=False)
    cliente_normalizado = cliente_id.upper()
    clave_idempotencia = _idempotency_key(idempotency_key)
    fc = str(nro_fc or "").strip()
    if fc and not _fc_normalizada(fc):
        raise ValueError("Ingresá un número de factura válido.")
    estado_normalizado = str(estado or "").strip().upper()
    if estado_normalizado not in {"ACTIVO", "CANCELADO"}:
        raise ValueError("El cargo manual debe ser ACTIVO o CANCELADO; una NC no es FC.")
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO envios
                        (cliente_id, fecha, nro_fc, monto_ars, estado, descripcion,
                         tracking, factura_pdf, factura_nombre, ambito,
                         idempotency_key)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (cliente_id, idempotency_key)
                        WHERE idempotency_key IS NOT NULL
                    DO NOTHING
                    RETURNING id
                    """,
                    (cliente_normalizado, fecha, fc, monto_decimal,
                     estado_normalizado, descripcion, tracking,
                     psycopg2.Binary(factura_pdf) if factura_pdf else None,
                     factura_nombre[:160] if factura_nombre else None,
                     ambito_normalizado, clave_idempotencia),
                )
                insertado = cur.fetchone()
                if not insertado:
                    cur.execute(
                        """
                        SELECT id, fecha, nro_fc, monto_ars, estado, descripcion,
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
                        and _fc_normalizada(existente.get("nro_fc"))
                        == _fc_normalizada(fc)
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
    except psycopg2.errors.UniqueViolation as exc:
        constraint = getattr(getattr(exc, "diag", None), "constraint_name", "")
        if constraint == "uq_envios_fc_normalizada":
            raise ValueError(
                "Ya existe una factura con ese número."
            ) from exc
        raise


def facturar_cargo(
    envio_id: int,
    cliente_id: str,
    nro_fc: str,
    factura_pdf: bytes,
    factura_nombre: str = "",
    *,
    actor_tipo: str = "admin",
    actor_ref: Optional[str] = None,
):
    """Asocia evidencia de FC a un cargo activo sin alterar monto ni ámbito."""
    cliente_normalizado = str(cliente_id or "").strip().upper()
    if not cliente_normalizado:
        raise ValueError("Falta el cliente propietario del cargo.")
    fc = str(nro_fc or "").strip()
    if not _fc_normalizada(fc):
        raise ValueError("Ingresá un número de factura válido.")
    contenido = bytes(factura_pdf or b"")
    if not contenido:
        raise ValueError("Adjuntá el PDF de la factura.")
    if validar_comprobante(contenido) != "application/pdf":
        raise ValueError("La factura debe adjuntarse en formato PDF.")
    nombre = str(factura_nombre or "").strip()[:160]
    if not nombre:
        nombre = f"factura_{fc}.pdf"[:160]

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, cliente_id, monto_ars, estado, ambito, nro_fc,
                           factura_pdf, factura_nombre
                    FROM envios
                    WHERE id = %s AND cliente_id = %s
                    FOR UPDATE
                    """,
                    (envio_id, cliente_normalizado),
                )
                cargo = cur.fetchone()
                if not cargo or str(cargo["estado"] or "").upper() != "ACTIVO":
                    return False

                fc_existente = str(cargo.get("nro_fc") or "").strip()
                if fc_existente:
                    mismo_payload = (
                        fc_existente == fc
                        and bytes(cargo.get("factura_pdf") or b"") == contenido
                        and str(cargo.get("factura_nombre") or "") == nombre
                    )
                    if not mismo_payload:
                        raise ValueError(
                            "El cargo ya está facturado con otro comprobante."
                        )
                    # Reintento exacto: garantiza el resultado sin nueva
                    # escritura ni un segundo evento de auditoría.
                    return {
                        "id": int(cargo["id"]),
                        "cliente_id": cargo["cliente_id"],
                        "monto_ars": _decimal_monto(cargo["monto_ars"]),
                        "ambito": cargo.get("ambito"),
                        "nro_fc": fc_existente,
                        "factura_nombre": cargo.get("factura_nombre"),
                    }

                cur.execute(
                    """
                    UPDATE envios
                    SET nro_fc = %s, factura_pdf = %s, factura_nombre = %s
                    WHERE id = %s AND cliente_id = %s
                      AND estado = 'ACTIVO'
                      AND NULLIF(BTRIM(nro_fc), '') IS NULL
                    RETURNING id, cliente_id, monto_ars, ambito, nro_fc,
                              factura_nombre
                    """,
                    (
                        fc,
                        psycopg2.Binary(contenido),
                        nombre,
                        envio_id,
                        cliente_normalizado,
                    ),
                )
                actualizado = cur.fetchone()
                if not actualizado:
                    return False
                resultado = dict(actualizado)
                resultado["monto_ars"] = _decimal_monto(resultado["monto_ars"])
                registrar_evento_con_cursor(
                    cur,
                    event="cuenta.facturar_cargo",
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
                        "nro_fc": resultado["nro_fc"],
                    },
                )
                return resultado
    except psycopg2.errors.UniqueViolation as exc:
        constraint = getattr(getattr(exc, "diag", None), "constraint_name", "")
        if constraint == "uq_envios_fc_normalizada":
            raise ValueError("Ya existe una factura con ese número.") from exc
        raise


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
                SELECT cliente_id, precio_tauro_ars, tracking, courier, ambito,
                       producto_alias, remitente_pais, destino_pais
                FROM solicitudes_guia WHERE id = %s
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
