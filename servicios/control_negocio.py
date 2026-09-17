"""Panel gerencial de sólo lectura. Importes NUMERIC/Decimal, sin asientos.

Rentabilidad = contribución del envío, no utilidad neta: cargo activo más
ajustes aplicados menos costo courier. Un costo final requiere la ÚLTIMA
conciliación cerrada con evidencia. Nunca se reemplaza un costo ausente por 0.
El período agrupa por fecha del cargo; la cobranza es el saldo actual completo.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from core.database import get_conn
from servicios.tracking_envios import horarios_tracking, proximo_control_vigilancia

AR = ZoneInfo("America/Argentina/Buenos_Aires")
CERO = Decimal("0.00")
METRICAS = ("envios", "cargos", "confirmados", "estimados", "sin_costo",
            "ingreso_confirmado", "costo_confirmado", "margen_confirmado",
            "margen_estimado", "perdidas")


def periodo_control(desde="", hasta="", *, hoy=None):
    hoy = hoy or datetime.now(AR).date()
    inicio, fin, error = hoy.replace(day=1), hoy, ""
    if desde or hasta:
        try:
            inicio = date.fromisoformat(desde) if desde else inicio
            fin = date.fromisoformat(hasta) if hasta else fin
            if inicio > fin or (fin - inicio).days > 366 or fin > hoy:
                raise ValueError
        except (ValueError, TypeError):
            inicio, fin = hoy.replace(day=1), hoy
            error = "Elegí un período de hasta un año, sin fechas futuras."
    return dict(desde=inicio, hasta=fin, error=error, hoy=hoy,
                ultimos_90_desde=hoy-timedelta(days=89))


_RENTABILIDAD_SQL = """
WITH ajustes AS (
    SELECT solicitud_id, SUM(monto_ars) AS monto
    FROM ajustes_cliente WHERE estado='APLICADO' GROUP BY solicitud_id
), base AS (
    SELECT e.cliente_id, c.nombre, date_trunc('month',e.fecha)::date AS mes,
           e.monto_ars + COALESCE(a.monto,0) AS cargo,
           CASE WHEN s.estado NOT IN ('CANCELADO','REEMPLAZADO')
                  AND co.estado='CERRADA' AND co.evidencia_completa
                THEN co.costo_courier_real_ars END AS costo_final,
           CASE WHEN s.estado NOT IN ('CANCELADO','REEMPLAZADO')
                THEN sn.costo_courier_estimado_ars END AS costo_estimado
    FROM envios e JOIN clientes c ON c.cliente_id=e.cliente_id
    LEFT JOIN solicitudes_guia s ON s.id=e.solicitud_id AND s.cliente_id=e.cliente_id
    LEFT JOIN ajustes a ON a.solicitud_id=s.id
    LEFT JOIN envio_cotizacion_snapshots sn ON sn.solicitud_id=s.id
    LEFT JOIN LATERAL (
        SELECT estado, evidencia_completa, costo_courier_real_ars
        FROM conciliaciones_envio WHERE solicitud_id=s.id
        ORDER BY version DESC LIMIT 1
    ) co ON TRUE
    WHERE c.test=FALSE AND e.estado='ACTIVO'
      AND (s.id IS NULL OR s.test=FALSE)
      AND e.fecha BETWEEN %s AND %s
)
SELECT cliente_id, nombre, mes, COUNT(*) AS envios, SUM(cargo) AS cargos,
       COUNT(costo_final) AS confirmados,
       COUNT(*) FILTER (WHERE costo_final IS NULL AND costo_estimado IS NOT NULL) AS estimados,
       COUNT(*) FILTER (WHERE costo_final IS NULL AND costo_estimado IS NULL) AS sin_costo,
       COALESCE(SUM(cargo) FILTER (WHERE costo_final IS NOT NULL),0) AS ingreso_confirmado,
       COALESCE(SUM(costo_final),0) AS costo_confirmado,
       COALESCE(SUM(cargo-costo_final),0) AS margen_confirmado,
       COALESCE(SUM(cargo-costo_estimado) FILTER (WHERE costo_final IS NULL),0) AS margen_estimado,
       COUNT(*) FILTER (WHERE cargo<costo_final) AS perdidas
FROM base GROUP BY cliente_id,nombre,mes ORDER BY mes,cliente_id
"""

_CUENTAS_SQL = """
WITH ajustes AS (
    SELECT solicitud_id, SUM(monto_ars) AS monto FROM ajustes_cliente
    WHERE estado='APLICADO' GROUP BY solicitud_id
), cargos AS (
    SELECT e.cliente_id, SUM(e.monto_ars + COALESCE(a.monto,0)) AS monto
    FROM envios e LEFT JOIN ajustes a ON a.solicitud_id=e.solicitud_id
    WHERE e.estado='ACTIVO' GROUP BY e.cliente_id
), pagos_cliente AS (
    SELECT cliente_id,
      SUM(monto_ars) FILTER (WHERE COALESCE(estado,'APROBADO')='APROBADO') AS aprobado,
      SUM(monto_ars) FILTER (WHERE estado='PENDIENTE') AS pendiente
    FROM pagos GROUP BY cliente_id
)
SELECT c.cliente_id,c.nombre,c.activo,
       ROUND(COALESCE(ca.monto,0),2) AS cargos,
       ROUND(COALESCE(p.aprobado,0),2) AS pagos,
       ROUND(COALESCE(ca.monto,0)-COALESCE(p.aprobado,0),2) AS saldo,
       ROUND(COALESCE(p.pendiente,0),2) AS pendiente
FROM clientes c LEFT JOIN cargos ca ON ca.cliente_id=c.cliente_id
LEFT JOIN pagos_cliente p ON p.cliente_id=c.cliente_id
WHERE c.test=FALSE
ORDER BY saldo DESC,c.cliente_id
"""

_OPERACION_BASE = """
FROM solicitudes_guia s JOIN clientes c ON c.cliente_id=s.cliente_id
LEFT JOIN envios e ON e.solicitud_id=s.id AND e.cliente_id=s.cliente_id
WHERE c.test=FALSE AND s.test=FALSE
"""
_ACTIVO = """s.estado NOT IN ('CANCELADO','REEMPLAZADO','ENTREGADO')
               AND s.tracking_estado IS DISTINCT FROM 'ENTREGADO'
               AND NULLIF(BTRIM(s.tracking),'') IS NOT NULL"""
_VIGILADO = "(s.tracking_vigilancia_desde IS NOT NULL OR s.tracking_estado='RETENIDO')"
_CANCELADO_CON_CARGO = "(s.estado IN ('CANCELADO','REEMPLAZADO') AND e.estado='ACTIVO' AND e.monto_ars<>0)"
_CARGO_PENDIENTE = """(s.estado NOT IN ('CANCELADO','REEMPLAZADO')
    AND NULLIF(BTRIM(s.tracking),'') IS NOT NULL
    AND (s.cargo_pendiente OR e.id IS NULL OR e.estado<>'ACTIVO'))"""
_OPERACION_SQL = """
SELECT COUNT(*) FILTER (WHERE """ + _ACTIVO + """) AS en_curso,
       COUNT(*) FILTER (WHERE """ + _ACTIVO + """ AND s.tracking_estado='RETENIDO') AS retenidos,
       COUNT(*) FILTER (WHERE """ + _ACTIVO + """ AND """ + _VIGILADO + """) AS vigilados,
       COUNT(*) FILTER (WHERE """ + _ACTIVO + """ AND UPPER(s.courier)='DHL'
         AND COALESCE(s.tracking_actualizado_at,s.guia_generada_at,s.created_at) < %s)
         AS sin_rastreo_reciente,
       COUNT(*) FILTER (WHERE s.estado IN ('CANCELADO','REEMPLAZADO')
         AND e.estado='ACTIVO' AND e.monto_ars<>0) AS cancelados_con_cargo,
       COUNT(*) FILTER (WHERE s.estado NOT IN ('CANCELADO','REEMPLAZADO')
         AND NULLIF(BTRIM(s.tracking),'') IS NOT NULL
         AND (s.cargo_pendiente OR e.id IS NULL OR e.estado<>'ACTIVO')) AS sin_cargo
""" + _OPERACION_BASE

_STATS_SQL = """
SELECT
 (SELECT COUNT(*) FROM clientes WHERE activo=TRUE AND test=FALSE) AS clientes_activos,
 (SELECT COUNT(*) FROM pagos p JOIN clientes c ON c.cliente_id=p.cliente_id
  WHERE p.estado='PENDIENTE' AND c.test=FALSE) AS total_pagos,
 (SELECT COUNT(*) FROM productos p JOIN clientes c ON c.cliente_id=p.cliente_id
  WHERE p.activo=FALSE AND c.test=FALSE) AS productos_pendientes,
 (SELECT COUNT(*) FROM solicitudes_guia s JOIN clientes c ON c.cliente_id=s.cliente_id
  WHERE s.estado IN ('SOLICITADO','EN_PROCESO','VERIFICAR_COURIER')
    AND s.test=FALSE AND c.test=FALSE) AS solicitudes_pendientes
"""


def _numero(valor):
    return Decimal(str(valor or 0))


def _completar(fila):
    for clave in METRICAS:
        fila.setdefault(clave, CERO)
    fila["porcentaje"] = ((_numero(fila["margen_confirmado"]) * 100
                           / _numero(fila["ingreso_confirmado"]))
                          .quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
                          if fila["ingreso_confirmado"] > 0 else None)
    fila["cobertura"] = (int(fila["confirmados"] * 100 / fila["envios"])
                         if fila["envios"] else 0)
    return fila


def resumir_rentabilidad(filas, desde, hasta):
    """Agrega cohortes sin multiplicar costos por pagos o ítems de factura."""
    clientes, meses, total = {}, {}, {}
    mes = desde.replace(day=1)
    while mes <= hasta:
        meses[mes] = dict(mes=mes)
        mes = (mes.replace(day=28) + timedelta(days=4)).replace(day=1)
    for fila in filas:
        cliente = clientes.setdefault(fila["cliente_id"], dict(
            cliente_id=fila["cliente_id"], nombre=fila.get("nombre")))
        for destino in (cliente, meses[fila["mes"]], total):
            for clave in METRICAS:
                destino[clave] = destino.get(clave, CERO) + _numero(fila[clave])
    for cliente in clientes.values():
        cliente["url"] = "/admin/clientes/" + quote(cliente["cliente_id"], safe="")
        cliente["control_url"] = "/admin/conciliacion-couriers?" + urlencode({
            "vista": "conciliacion", "cliente": cliente["cliente_id"]})
        _completar(cliente)
    ordenados = sorted(clientes.values(), key=lambda c: (-c["cargos"], c["cliente_id"]))
    max_cargos = max((c["cargos"] for c in ordenados), default=CERO)
    for cliente in ordenados:
        cliente["ancho"] = max(0, int(cliente["cargos"] * 100 / max_cargos)) if max_cargos > 0 else 0
    max_mes = max((abs(m.get("margen_confirmado", CERO)) for m in meses.values()), default=CERO)
    for m in meses.values():
        _completar(m)
        m["ancho"] = int(abs(m["margen_confirmado"]) * 100 / max_mes) if max_mes else 0
    return dict(totales=_completar(total), clientes=ordenados, meses=list(meses.values()))


def obtener_control_negocio(desde, hasta, *, pagina=1, vigilancia="todos",
                           pagina_cargos=1, cargos="todos", conexion=None):
    ahora = datetime.now(AR)
    try:
        pagina = max(1, min(int(pagina), 1000000))
    except (ValueError, TypeError):
        pagina = 1
    try:
        pagina_cargos = max(1, min(int(pagina_cargos), 1000000))
    except (ValueError, TypeError):
        pagina_cargos = 1
    filtros = {"todos": "TRUE", "retenidos": "s.tracking_estado='RETENIDO'",
               "liberados": "s.tracking_estado IS DISTINCT FROM 'RETENIDO'",
               "errores": "NULLIF(s.tracking_error,'') IS NOT NULL",
               "sin_actualizar": """UPPER(s.courier)='DHL' AND
                   COALESCE(s.tracking_actualizado_at,s.guia_generada_at,s.created_at)
                       < NOW()-INTERVAL '36 hours'"""}
    vigilancia = vigilancia if vigilancia in filtros else "todos"
    where_vigilancia = (_OPERACION_BASE + " AND " + _ACTIVO
                        + ("" if vigilancia == "sin_actualizar" else " AND " + _VIGILADO)
                        + " AND " + filtros[vigilancia])
    filtros_cargos = {"todos": f"({_CANCELADO_CON_CARGO} OR {_CARGO_PENDIENTE})",
                      "cancelados": _CANCELADO_CON_CARGO, "pendientes": _CARGO_PENDIENTE}
    cargos = cargos if cargos in filtros_cargos else "todos"
    where_cargos = _OPERACION_BASE + " AND " + filtros_cargos[cargos]
    with (conexion or get_conn)() as conn, conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        cur.execute(_STATS_SQL)
        stats = dict(cur.fetchone())
        cur.execute(_RENTABILIDAD_SQL, (desde, hasta))
        resultado = resumir_rentabilidad(cur.fetchall(), desde, hasta)
        cur.execute(_CUENTAS_SQL)
        cuentas = [dict(f) for f in cur.fetchall()]
        cur.execute(_OPERACION_SQL, (ahora-timedelta(hours=36),))
        operacion = dict(cur.fetchone())
        cur.execute("SELECT COUNT(*) AS total " + where_vigilancia)
        total = int(cur.fetchone()["total"])
        paginas = max(1, (total + 24) // 25)
        pagina = min(pagina, paginas)
        cur.execute("""SELECT s.id, s.cliente_id, s.dest_nombre, s.tracking,
                    s.numero_guia_tauro, s.courier, s.remitente_pais, s.destino_pais,
                    s.tracking_estado, s.tracking_descripcion, s.tracking_error,
                    s.tracking_consultado_at, s.tracking_actualizado_at,
                    s.tracking_vigilancia_desde
                    """ + where_vigilancia + """
                    ORDER BY (s.tracking_estado='RETENIDO') DESC NULLS LAST,
                             s.tracking_vigilancia_desde ASC NULLS FIRST,s.id
                    LIMIT 25 OFFSET %s""", ((pagina-1)*25,))
        vigilados = [dict(f) for f in cur.fetchall()]
        cur.execute("SELECT COUNT(*) AS total " + where_cargos)
        total_cargos = int(cur.fetchone()["total"])
        paginas_cargos = max(1, (total_cargos+24)//25)
        pagina_cargos = min(pagina_cargos, paginas_cargos)
        cur.execute("""SELECT s.id,s.cliente_id,s.dest_nombre,s.tracking,s.estado,
                         e.monto_ars,s.cargo_pendiente
                    """ + where_cargos + """
                    ORDER BY s.created_at,s.id LIMIT 25 OFFSET %s""", ((pagina_cargos-1)*25,))
        alertas = [dict(f) for f in cur.fetchall()]
    for c in cuentas:
        c["url"] = "/admin/clientes/" + quote(c["cliente_id"], safe="")
    for s in vigilados:
        fecha = s.get("tracking_vigilancia_desde")
        s["en_vigilancia"] = bool(fecha or s["tracking_estado"] == "RETENIDO")
        s["dias"] = max(0, (ahora.date()-fecha.astimezone(AR).date()).days) if fecha else None
        for campo in ("tracking_actualizado_at", "tracking_consultado_at"):
            s[campo + "_label"] = (s[campo].astimezone(AR).strftime("%d/%m %H:%M")
                                    if s[campo] else "Sin consulta")
        s["atrasado"] = (not s["tracking_actualizado_at"] or
                          s["tracking_actualizado_at"] < ahora-timedelta(hours=18))
    deuda = sum((max(c["saldo"], CERO) for c in cuentas), CERO)
    favor = sum((max(-c["saldo"], CERO) for c in cuentas), CERO)
    hora, minuto = horarios_tracking()
    resultado.update(stats=stats, cuentas=cuentas, operacion=operacion, alertas=alertas,
                     cargos_control=dict(total=total_cargos,pagina=pagina_cargos,
                         paginas=paginas_cargos,filtro=cargos),
                     cuentas_resumen=dict(deuda=deuda, favor=favor,
                         deudores=sum(c["saldo"]>0 for c in cuentas),
                         pendiente=sum((c["pendiente"] for c in cuentas), CERO)),
                     vigilancia=dict(items=vigilados,total=total,pagina=pagina,
                         paginas=paginas,filtro=vigilancia),
                     actualizado=ahora.strftime("%d/%m/%Y · %H:%M"),
                     proxima_ronda=proximo_control_vigilancia(ahora).strftime("%d/%m %H:%M"),
                     horarios=" y ".join(f"{h:02d}:{minuto:02d}" for h in sorted((hora,(hora+12)%24))))
    return resultado
