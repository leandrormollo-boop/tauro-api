"""Facturación TAURO por lote sobre cargos ya asentados.

Una factura no crea deuda: documenta cargos y ajustes existentes. Los campos
legacy de ``envios`` se leen como fallback, pero este módulo nunca los escribe.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable

import psycopg2

from core.database import get_conn
from servicios.auditoria import registrar_evento_con_cursor
from servicios.cuenta_corriente import validar_comprobante


CENTAVO = Decimal("0.01")
TIPOS = frozenset({"FC", "NC"})
AMBITOS = frozenset({"NACIONAL", "INTERNACIONAL"})
ESTADOS = frozenset({"EMITIDA", "ANULADA"})


class FacturacionClienteError(RuntimeError):
    """El lote no cumple una invariante documental o contable."""


def _texto(valor: Any, maximo: int = 200) -> str:
    return str(valor or "").strip()[:maximo]


def _dinero(valor: Any, campo: str, *, permite_cero: bool = False) -> Decimal:
    try:
        numero = Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FacturacionClienteError(f"{campo} no es un importe válido.") from exc
    if not numero.is_finite():
        raise FacturacionClienteError(f"{campo} no es un importe válido.")
    numero = numero.quantize(CENTAVO, rounding=ROUND_HALF_UP)
    if numero < 0 or (not permite_cero and numero == 0):
        raise FacturacionClienteError(f"{campo} debe ser mayor que cero.")
    return numero


def _fecha(valor: Any, campo: str, *, opcional: bool = False) -> date | None:
    if valor in (None, "") and opcional:
        return None
    if isinstance(valor, date):
        return valor
    try:
        return date.fromisoformat(str(valor))
    except (TypeError, ValueError) as exc:
        raise FacturacionClienteError(f"{campo} no es una fecha válida.") from exc


def _entero_positivo(valor: Any, campo: str) -> int:
    try:
        numero = int(str(valor).strip())
    except (TypeError, ValueError) as exc:
        raise FacturacionClienteError(f"{campo} no es válido.") from exc
    if numero <= 0:
        raise FacturacionClienteError(f"{campo} debe ser mayor que cero.")
    return numero


def numero_factura_visible(tipo: str, punto_venta: Any, numero: Any) -> str:
    return f"{_texto(tipo, 2)} {int(punto_venta):04d}-{int(numero):08d}"


def _normalizar_seleccion(items: Iterable[Any]) -> list[tuple[str, int]]:
    seleccion = []
    vistos = set()
    for valor in items or ():
        match = re.fullmatch(r"([EA]):([1-9][0-9]*)", _texto(valor, 40).upper())
        if not match:
            raise FacturacionClienteError("La selección contiene una partida inválida.")
        clave = (match.group(1), int(match.group(2)))
        if clave in vistos:
            raise FacturacionClienteError("La selección contiene una partida repetida.")
        vistos.add(clave)
        seleccion.append(clave)
    if not seleccion:
        raise FacturacionClienteError("Seleccioná al menos un cargo o diferencia.")
    return seleccion


def listar_partidas_facturables(
    cliente_id: str,
    *,
    tipo: str = "FC",
    ambito: str = "INTERNACIONAL",
    desde: date | str | None = None,
    hasta: date | str | None = None,
) -> list[dict[str, Any]]:
    """Partidas activas y todavía no incluidas en otra factura emitida."""
    cliente = _texto(cliente_id, 80).upper()
    tipo = _texto(tipo, 2).upper()
    ambito = _texto(ambito, 20).upper()
    if tipo not in TIPOS or ambito not in AMBITOS:
        raise FacturacionClienteError("Tipo de factura o ámbito inválido.")
    desde_fecha = _fecha(desde, "Desde", opcional=True)
    hasta_fecha = _fecha(hasta, "Hasta", opcional=True)
    if desde_fecha and hasta_fecha and hasta_fecha < desde_fecha:
        raise FacturacionClienteError("El rango de fechas está invertido.")

    with get_conn() as conn:
        with conn.cursor() as cur:
            if tipo == "FC":
                cur.execute(
                    """
                    SELECT 'E:' || e.id::text AS clave, 'ENVIO' AS clase,
                           e.id AS origen_id, e.fecha, e.monto_ars AS monto,
                           COALESCE(NULLIF(BTRIM(e.tracking), ''),
                                    NULLIF(BTRIM(s.tracking), '')) AS tracking,
                           COALESCE(NULLIF(BTRIM(s.dest_nombre), ''),
                                    NULLIF(BTRIM(e.descripcion), ''), 'Envío')
                               AS descripcion,
                           e.ambito
                      FROM envios e
                 LEFT JOIN solicitudes_guia s ON s.id=e.solicitud_id
                     WHERE e.cliente_id=%s AND e.estado='ACTIVO'
                       AND e.ambito=%s AND e.monto_ars > 0
                       AND NULLIF(BTRIM(e.nro_fc), '') IS NULL
                       AND (%s::date IS NULL OR e.fecha >= %s::date)
                       AND (%s::date IS NULL OR e.fecha <= %s::date)
                       AND NOT EXISTS (
                           SELECT 1 FROM facturas_cliente_items i
                           JOIN facturas_cliente f ON f.id=i.factura_id
                           WHERE i.envio_id=e.id AND f.estado='EMITIDA'
                       )

                    UNION ALL

                    SELECT 'A:' || a.id::text, 'AJUSTE', a.id,
                           a.aplicado_at::date, ABS(a.monto_ars),
                           COALESCE(NULLIF(BTRIM(s.tracking), ''),
                                    NULLIF(BTRIM(e.tracking), '')),
                           COALESCE(NULLIF(BTRIM(a.motivo), ''),
                                    'Diferencia de envío'), e.ambito
                      FROM ajustes_cliente a
                      JOIN envios e ON e.solicitud_id=a.solicitud_id
                      JOIN solicitudes_guia s ON s.id=a.solicitud_id
                     WHERE e.cliente_id=%s AND e.estado='ACTIVO'
                       AND e.ambito=%s AND a.estado='APLICADO'
                       AND a.tipo='DEBITO'
                       AND (%s::date IS NULL OR a.aplicado_at::date >= %s::date)
                       AND (%s::date IS NULL OR a.aplicado_at::date <= %s::date)
                       AND NOT EXISTS (
                           SELECT 1 FROM facturas_cliente_items i
                           JOIN facturas_cliente f ON f.id=i.factura_id
                           WHERE i.ajuste_id=a.id AND f.estado='EMITIDA'
                       )
                    ORDER BY fecha, clase, origen_id
                    """,
                    (
                        cliente, ambito, desde_fecha, desde_fecha,
                        hasta_fecha, hasta_fecha,
                        cliente, ambito, desde_fecha, desde_fecha,
                        hasta_fecha, hasta_fecha,
                    ),
                )
            else:
                cur.execute(
                    """
                    SELECT 'A:' || a.id::text AS clave, 'AJUSTE' AS clase,
                           a.id AS origen_id, a.aplicado_at::date AS fecha,
                           ABS(a.monto_ars) AS monto,
                           COALESCE(NULLIF(BTRIM(s.tracking), ''),
                                    NULLIF(BTRIM(e.tracking), '')) AS tracking,
                           COALESCE(NULLIF(BTRIM(a.motivo), ''),
                                    'Crédito de envío') AS descripcion,
                           e.ambito
                      FROM ajustes_cliente a
                      JOIN envios e ON e.solicitud_id=a.solicitud_id
                      JOIN solicitudes_guia s ON s.id=a.solicitud_id
                     WHERE e.cliente_id=%s AND e.estado='ACTIVO'
                       AND e.ambito=%s AND a.estado='APLICADO'
                       AND a.tipo='CREDITO'
                       AND (%s::date IS NULL OR a.aplicado_at::date >= %s::date)
                       AND (%s::date IS NULL OR a.aplicado_at::date <= %s::date)
                       AND NOT EXISTS (
                           SELECT 1 FROM facturas_cliente_items i
                           JOIN facturas_cliente f ON f.id=i.factura_id
                           WHERE i.ajuste_id=a.id AND f.estado='EMITIDA'
                       )
                    ORDER BY fecha, origen_id
                    """,
                    (cliente, ambito, desde_fecha, desde_fecha,
                     hasta_fecha, hasta_fecha),
                )
            filas = [dict(fila) for fila in cur.fetchall()]
    for fila in filas:
        fila["monto"] = _dinero(fila["monto"], "Monto")
    return filas


def _cargar_partida(cur, *, cliente: str, tipo: str, clave: tuple[str, int]):
    clase, origen_id = clave
    if clase == "E":
        cur.execute(
            """
            SELECT e.id, e.cliente_id, e.fecha, e.monto_ars, e.estado,
                   e.ambito, e.nro_fc,
                   COALESCE(NULLIF(BTRIM(e.tracking), ''),
                            NULLIF(BTRIM(s.tracking), '')) AS tracking,
                   COALESCE(NULLIF(BTRIM(s.dest_nombre), ''),
                            NULLIF(BTRIM(e.descripcion), ''), 'Envío') AS detalle,
                   EXISTS (
                       SELECT 1 FROM facturas_cliente_items i
                       JOIN facturas_cliente f ON f.id=i.factura_id
                       WHERE i.envio_id=e.id AND f.estado='EMITIDA'
                   ) AS ya_facturado
              FROM envios e
         LEFT JOIN solicitudes_guia s ON s.id=e.solicitud_id
             WHERE e.id=%s FOR UPDATE OF e
            """,
            (origen_id,),
        )
        fila = cur.fetchone()
        if (
            not fila or fila["cliente_id"] != cliente or fila["estado"] != "ACTIVO"
            or fila.get("ambito") not in AMBITOS or fila.get("ya_facturado")
            or _texto(fila.get("nro_fc")) or tipo != "FC"
        ):
            raise FacturacionClienteError(
                f"El cargo {origen_id} ya no está disponible para facturar."
            )
        return {
            "envio_id": origen_id,
            "ajuste_id": None,
            "ambito": fila["ambito"],
            "monto": _dinero(fila["monto_ars"], "Monto del cargo"),
            "descripcion": _texto(
                f"Envío {fila.get('tracking') or origen_id} · {fila.get('detalle')}", 500
            ),
        }

    cur.execute(
        """
        SELECT a.id, a.tipo, a.estado, a.monto_ars, a.motivo,
               e.cliente_id, e.estado AS envio_estado, e.ambito,
               COALESCE(NULLIF(BTRIM(s.tracking), ''),
                        NULLIF(BTRIM(e.tracking), '')) AS tracking,
               EXISTS (
                   SELECT 1 FROM facturas_cliente_items i
                   JOIN facturas_cliente f ON f.id=i.factura_id
                   WHERE i.ajuste_id=a.id AND f.estado='EMITIDA'
               ) AS ya_facturado
          FROM ajustes_cliente a
          JOIN envios e ON e.solicitud_id=a.solicitud_id
          JOIN solicitudes_guia s ON s.id=a.solicitud_id
         WHERE a.id=%s FOR UPDATE OF a, e
        """,
        (origen_id,),
    )
    fila = cur.fetchone()
    tipo_esperado = "DEBITO" if tipo == "FC" else "CREDITO"
    if (
        not fila or fila["cliente_id"] != cliente or fila["envio_estado"] != "ACTIVO"
        or fila["estado"] != "APLICADO" or fila["tipo"] != tipo_esperado
        or fila.get("ambito") not in AMBITOS or fila.get("ya_facturado")
    ):
        raise FacturacionClienteError(
            f"El ajuste {origen_id} ya no está disponible para facturar."
        )
    return {
        "envio_id": None,
        "ajuste_id": origen_id,
        "ambito": fila["ambito"],
        "monto": _dinero(abs(fila["monto_ars"]), "Monto del ajuste"),
        "descripcion": _texto(
            f"Diferencia {fila.get('tracking') or origen_id} · "
            f"{fila.get('motivo') or tipo_esperado.title()}", 500
        ),
    }


def crear_factura_cliente(
    *,
    cliente_id: str,
    tipo: str,
    punto_venta: Any,
    numero: Any,
    cae: str,
    fecha_emision: Any,
    fecha_vencimiento: Any = None,
    periodo_desde: Any = None,
    periodo_hasta: Any = None,
    subtotal: Any,
    iva: Any,
    pdf: bytes,
    pdf_nombre: str,
    seleccion: Iterable[Any],
    created_by: str,
) -> dict[str, Any]:
    """Crea cabecera e ítems en una transacción; nunca toca ``envios.nro_fc``."""
    cliente = _texto(cliente_id, 80).upper()
    tipo = _texto(tipo, 2).upper()
    if tipo not in TIPOS:
        raise FacturacionClienteError("El tipo debe ser FC o NC.")
    pv = _entero_positivo(punto_venta, "Punto de venta")
    nro = _entero_positivo(numero, "Número")
    cae = _texto(cae, 30)
    if cae and not re.fullmatch(r"[0-9]{14}", cae):
        raise FacturacionClienteError("El CAE debe tener 14 dígitos.")
    emision = _fecha(fecha_emision, "Fecha de emisión")
    vencimiento = _fecha(fecha_vencimiento, "Fecha de vencimiento", opcional=True)
    desde = _fecha(periodo_desde, "Período desde", opcional=True)
    hasta = _fecha(periodo_hasta, "Período hasta", opcional=True)
    if vencimiento and vencimiento < emision:
        raise FacturacionClienteError("El vencimiento no puede ser anterior a la emisión.")
    if desde and hasta and hasta < desde:
        raise FacturacionClienteError("El período está invertido.")
    subtotal_d = _dinero(subtotal, "Subtotal", permite_cero=True)
    iva_d = _dinero(iva, "IVA", permite_cero=True)
    total_d = subtotal_d + iva_d
    contenido = bytes(pdf or b"")
    if validar_comprobante(contenido) != "application/pdf":
        raise FacturacionClienteError("La factura debe adjuntarse en formato PDF.")
    seleccion_normalizada = _normalizar_seleccion(seleccion)

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT cliente_id FROM clientes WHERE cliente_id=%s FOR UPDATE",
                    (cliente,),
                )
                if not cur.fetchone():
                    raise FacturacionClienteError("El cliente no existe.")
                partidas = [
                    _cargar_partida(cur, cliente=cliente, tipo=tipo, clave=clave)
                    for clave in seleccion_normalizada
                ]
                ambitos = {partida["ambito"] for partida in partidas}
                if len(ambitos) != 1:
                    raise FacturacionClienteError("Una factura no puede mezclar ámbitos.")
                suma_items = sum((p["monto"] for p in partidas), Decimal("0"))
                if abs(suma_items - total_d) > Decimal("0.02"):
                    raise FacturacionClienteError(
                        "Subtotal más IVA debe coincidir con la suma de las partidas."
                    )
                cur.execute(
                    """
                    INSERT INTO facturas_cliente (
                        cliente_id, tipo, punto_venta, numero, cae,
                        fecha_emision, fecha_vencimiento,
                        periodo_desde, periodo_hasta,
                        subtotal, iva, total, pdf, pdf_nombre,
                        estado, created_by
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        'EMITIDA',%s
                    ) RETURNING id
                    """,
                    (
                        cliente, tipo, pv, nro, cae or None, emision, vencimiento,
                        desde, hasta, subtotal_d, iva_d, total_d,
                        psycopg2.Binary(contenido), _texto(pdf_nombre, 160) or None,
                        _texto(created_by, 120) or "admin",
                    ),
                )
                factura_id = int(cur.fetchone()["id"])
                for partida in partidas:
                    cur.execute(
                        """
                        INSERT INTO facturas_cliente_items (
                            factura_id, envio_id, ajuste_id, descripcion, monto
                        ) VALUES (%s,%s,%s,%s,%s)
                        """,
                        (
                            factura_id, partida["envio_id"], partida["ajuste_id"],
                            partida["descripcion"], partida["monto"],
                        ),
                    )
                registrar_evento_con_cursor(
                    cur,
                    event="cuenta.factura_cliente_emitida",
                    actor_type="admin",
                    actor_ref=_texto(created_by, 120) or "admin",
                    ip=None, method=None, path=None, status_code=201,
                    success=True, request_id=None,
                    metadata={
                        "factura_id": factura_id,
                        "cliente_id": cliente,
                        "tipo": tipo,
                        "numero": numero_factura_visible(tipo, pv, nro),
                        "total_ars": str(total_d),
                        "items": len(partidas),
                        "ambito": next(iter(ambitos)),
                    },
                )
                return {
                    "id": factura_id,
                    "cliente_id": cliente,
                    "tipo": tipo,
                    "numero_visible": numero_factura_visible(tipo, pv, nro),
                    "total": total_d,
                    "items": len(partidas),
                    "ambito": next(iter(ambitos)),
                }
    except psycopg2.errors.UniqueViolation as exc:
        raise FacturacionClienteError(
            "Ya existe una factura con ese tipo, punto de venta y número."
        ) from exc


def listar_facturas_cliente(cliente_id: str) -> list[dict[str, Any]]:
    cliente = _texto(cliente_id, 80).upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.id, f.tipo, f.punto_venta, f.numero, f.cae,
                       f.fecha_emision, f.fecha_vencimiento,
                       f.periodo_desde, f.periodo_hasta,
                       f.subtotal, f.iva, f.total, f.estado,
                       f.pdf IS NOT NULL AS tiene_pdf,
                       COUNT(i.id) AS items,
                       COALESCE(
                           JSONB_AGG(JSONB_BUILD_OBJECT(
                               'descripcion', i.descripcion,
                               'monto', i.monto,
                               'envio_id', i.envio_id,
                               'ajuste_id', i.ajuste_id,
                               'tracking', COALESCE(
                                   NULLIF(BTRIM(e.tracking), ''),
                                   NULLIF(BTRIM(s.tracking), '')
                               )
                           ) ORDER BY i.id) FILTER (WHERE i.id IS NOT NULL),
                           '[]'::jsonb
                       ) AS detalle_items,
                       CASE WHEN f.tipo='FC' THEN LEAST(
                           f.total,
                           COALESCE((
                               SELECT SUM(pa.monto_ars)
                               FROM pagos_aplicaciones pa
                               WHERE pa.factura_id=f.id
                                 AND pa.estado='APLICADA'
                           ), 0) + COALESCE((
                               SELECT SUM(pa.monto_ars)
                               FROM pagos_aplicaciones pa
                               WHERE pa.envio_id IN (
                                   SELECT ii.envio_id
                                   FROM facturas_cliente_items ii
                                   WHERE ii.factura_id=f.id
                                     AND ii.envio_id IS NOT NULL
                               ) AND pa.estado='APLICADA'
                           ), 0)
                       ) ELSE 0 END AS pagado,
                       CASE WHEN f.tipo='FC' THEN GREATEST(
                           f.total - COALESCE((
                               SELECT SUM(pa.monto_ars)
                               FROM pagos_aplicaciones pa
                               WHERE pa.factura_id=f.id
                                 AND pa.estado='APLICADA'
                           ), 0) - COALESCE((
                               SELECT SUM(pa.monto_ars)
                               FROM pagos_aplicaciones pa
                               WHERE pa.envio_id IN (
                                   SELECT ii.envio_id
                                   FROM facturas_cliente_items ii
                                   WHERE ii.factura_id=f.id
                                     AND ii.envio_id IS NOT NULL
                               ) AND pa.estado='APLICADA'
                           ), 0), 0
                       ) ELSE 0 END AS saldo
                  FROM facturas_cliente f
             LEFT JOIN facturas_cliente_items i ON i.factura_id=f.id
             LEFT JOIN envios e ON e.id=i.envio_id
             LEFT JOIN ajustes_cliente a ON a.id=i.ajuste_id
             LEFT JOIN solicitudes_guia s
                    ON s.id=COALESCE(e.solicitud_id, a.solicitud_id)
                 WHERE f.cliente_id=%s
                 GROUP BY f.id
                 ORDER BY f.fecha_emision DESC, f.id DESC
                """,
                (cliente,),
            )
            filas = [dict(fila) for fila in cur.fetchall()]
    for fila in filas:
        fila["numero_visible"] = numero_factura_visible(
            fila["tipo"], fila["punto_venta"], fila["numero"]
        )
        for campo in ("subtotal", "iva", "total", "pagado", "saldo"):
            fila[campo] = _dinero(fila[campo], campo, permite_cero=True)
    return filas


def obtener_factura_cliente(
    factura_id: int,
    *,
    cliente_id: str | None = None,
) -> dict[str, Any] | None:
    parametros: list[Any] = [int(factura_id)]
    filtro_cliente = ""
    if cliente_id is not None:
        filtro_cliente = " AND f.cliente_id=%s"
        parametros.append(_texto(cliente_id, 80).upper())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.*,
                       CASE WHEN f.tipo='FC' THEN LEAST(f.total,
                           COALESCE((SELECT SUM(pa.monto_ars)
                             FROM pagos_aplicaciones pa
                             WHERE pa.factura_id=f.id AND pa.estado='APLICADA'),0)
                           + COALESCE((SELECT SUM(pa.monto_ars)
                             FROM pagos_aplicaciones pa
                             WHERE pa.envio_id IN (
                                 SELECT i.envio_id FROM facturas_cliente_items i
                                 WHERE i.factura_id=f.id AND i.envio_id IS NOT NULL
                             ) AND pa.estado='APLICADA'),0)
                       ) ELSE 0 END AS pagado,
                       CASE WHEN f.tipo='FC' THEN GREATEST(f.total
                           - COALESCE((SELECT SUM(pa.monto_ars)
                             FROM pagos_aplicaciones pa
                             WHERE pa.factura_id=f.id AND pa.estado='APLICADA'),0)
                           - COALESCE((SELECT SUM(pa.monto_ars)
                             FROM pagos_aplicaciones pa
                             WHERE pa.envio_id IN (
                                 SELECT i.envio_id FROM facturas_cliente_items i
                                 WHERE i.factura_id=f.id AND i.envio_id IS NOT NULL
                             ) AND pa.estado='APLICADA'),0), 0
                       ) ELSE 0 END AS saldo
                  FROM facturas_cliente f
                 WHERE f.id=%s
                """ + filtro_cliente,
                tuple(parametros),
            )
            factura = cur.fetchone()
            if not factura:
                return None
            cur.execute(
                """
                SELECT i.id, i.envio_id, i.ajuste_id, i.descripcion, i.monto,
                       COALESCE(NULLIF(BTRIM(e.tracking), ''),
                                NULLIF(BTRIM(s.tracking), '')) AS tracking,
                       COALESCE(e.fecha, a.aplicado_at::date) AS fecha,
                       COALESCE(e.ambito, ea.ambito) AS ambito
                  FROM facturas_cliente_items i
             LEFT JOIN envios e ON e.id=i.envio_id
             LEFT JOIN ajustes_cliente a ON a.id=i.ajuste_id
             LEFT JOIN envios ea ON ea.solicitud_id=a.solicitud_id
             LEFT JOIN solicitudes_guia s
                    ON s.id=COALESCE(e.solicitud_id, a.solicitud_id)
                 WHERE i.factura_id=%s
                 ORDER BY i.id
                """,
                (int(factura_id),),
            )
            items = [dict(fila) for fila in cur.fetchall()]
    resultado = dict(factura)
    resultado["numero_visible"] = numero_factura_visible(
        resultado["tipo"], resultado["punto_venta"], resultado["numero"]
    )
    resultado["items"] = items
    return resultado


def get_factura_cliente_pdf(
    factura_id: int,
    *,
    cliente_id: str | None = None,
) -> tuple[bytes, str] | None:
    parametros: list[Any] = [int(factura_id)]
    filtro = ""
    if cliente_id is not None:
        filtro = " AND cliente_id=%s"
        parametros.append(_texto(cliente_id, 80).upper())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pdf, pdf_nombre, tipo, punto_venta, numero "
                "FROM facturas_cliente WHERE id=%s" + filtro,
                tuple(parametros),
            )
            fila = cur.fetchone()
    if not fila or not fila.get("pdf"):
        return None
    nombre = _texto(fila.get("pdf_nombre"), 160) or (
        numero_factura_visible(fila["tipo"], fila["punto_venta"], fila["numero"])
        .replace(" ", "_") + ".pdf"
    )
    return bytes(fila["pdf"]), nombre
