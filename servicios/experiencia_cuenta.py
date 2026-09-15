"""Lecturas acotadas para la cuenta del cliente; nunca modifica el libro contable.

Los vencimientos son documentales, no un segundo saldo de cuenta. Un pago
informado sigue siendo deuda hasta su aprobación. No se publican notas internas
ni se deducen fechas de acreditación que el esquema no registra.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from core.database import get_conn
from servicios.cuenta_corriente import _decimal_monto
from servicios.facturacion_clientes import numero_factura_visible


_AR = timezone(timedelta(hours=-3), "Argentina")
_CERO = Decimal("0.00")
_MESES = ("Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic")

# EXISTS evita multiplicar las aplicaciones a un envío si hay varios ítems.
# También valida al dueño del pago y del envío: nunca confiar sólo en la FK.
_VENCIMIENTOS_SQL = """
WITH facturas AS (
    SELECT f.id, f.tipo, f.punto_venta, f.numero, f.fecha_vencimiento,
           f.fecha_emision, f.total,
           COALESCE(a.pagado, 0) AS pagado,
           COALESCE(a.solicitado, 0) AS solicitado
    FROM facturas_cliente f
    LEFT JOIN LATERAL (
        SELECT SUM(pa.monto_ars) FILTER (
                   WHERE pa.estado='APLICADA'
                     AND COALESCE(p.estado,'APROBADO')='APROBADO'
               ) AS pagado,
               SUM(pa.monto_ars) FILTER (
                   WHERE pa.estado='SOLICITADA' AND p.estado='PENDIENTE'
               ) AS solicitado
        FROM pagos_aplicaciones pa
        JOIN pagos p ON p.id=pa.pago_id AND p.cliente_id=f.cliente_id
        WHERE pa.factura_id=f.id OR EXISTS (
            SELECT 1 FROM facturas_cliente_items i
            JOIN envios e ON e.id=i.envio_id AND e.cliente_id=f.cliente_id
            WHERE i.factura_id=f.id AND i.envio_id=pa.envio_id
        )
    ) a ON TRUE
    WHERE f.cliente_id=%s AND f.tipo='FC' AND f.estado='EMITIDA'
), saldos AS (
    SELECT *, GREATEST(total-pagado,0) AS saldo
    FROM facturas WHERE total>pagado
)
SELECT *, COUNT(*) OVER () AS cantidad,
       SUM(saldo) OVER () AS total_pendiente_ars,
       COALESCE(SUM(saldo) FILTER (WHERE fecha_vencimiento<%s) OVER (),0)
           AS total_vencido_ars,
       COUNT(*) FILTER (WHERE fecha_vencimiento<%s) OVER () AS cantidad_vencida,
       SUM(LEAST(saldo,solicitado)) OVER () AS total_en_revision_ars
FROM saldos
ORDER BY fecha_vencimiento ASC NULLS LAST, fecha_emision, id
LIMIT 6
"""

_PAGOS_SQL = """
WITH recientes AS (
    SELECT p.id, p.cliente_id, p.fecha, p.created_at, p.monto_ars,
           p.metodo, p.referencia, COALESCE(p.estado,'APROBADO') AS estado,
           p.comprobante IS NOT NULL AS tiene_comprobante
    FROM pagos p WHERE p.cliente_id=%s
    ORDER BY p.created_at DESC, p.id DESC LIMIT 8
)
SELECT p.*, COALESCE(a.detalle,'[]'::jsonb) AS aplicaciones,
       COALESCE(a.cantidad,0) AS cantidad_aplicaciones
FROM recientes p
LEFT JOIN LATERAL (
    SELECT JSONB_AGG(JSONB_BUILD_OBJECT(
               'documento', d.documento, 'tracking', d.tracking,
               'monto_ars', d.monto_ars::text, 'estado', d.estado,
               'ambito', d.ambito
           ) ORDER BY d.id) AS detalle, MAX(d.cantidad) AS cantidad
    FROM (
        SELECT pa.id, pa.monto_ars, pa.estado, pa.ambito,
               COUNT(*) OVER () AS cantidad,
               CASE WHEN f.id IS NOT NULL THEN
                   f.tipo || ' ' || LPAD(f.punto_venta::text,4,'0')
                       || '-' || LPAD(f.numero::text,8,'0')
               WHEN e.id IS NOT NULL THEN 'Envío ' || COALESCE(
                   NULLIF(BTRIM(e.tracking),''), NULLIF(BTRIM(s.tracking),''), e.id::text
               ) ELSE 'Aplicación a ' || LOWER(pa.ambito) END AS documento,
               COALESCE(NULLIF(BTRIM(e.tracking),''),
                        NULLIF(BTRIM(s.tracking),'')) AS tracking
        FROM pagos_aplicaciones pa
        LEFT JOIN facturas_cliente f
               ON f.id=pa.factura_id AND f.cliente_id=p.cliente_id
        LEFT JOIN envios e ON e.id=pa.envio_id AND e.cliente_id=p.cliente_id
        LEFT JOIN solicitudes_guia s
               ON s.id=e.solicitud_id AND s.cliente_id=p.cliente_id
        WHERE pa.pago_id=p.id
          AND (pa.factura_id IS NULL OR f.id IS NOT NULL)
          AND (pa.envio_id IS NULL OR e.id IS NOT NULL)
          AND ((p.estado='APROBADO' AND pa.estado='APLICADA')
               OR (p.estado='PENDIENTE' AND pa.estado='SOLICITADA'))
        ORDER BY pa.id LIMIT 24
    ) d
) a ON TRUE
ORDER BY p.created_at DESC, p.id DESC
"""

# Espejo de la lectura en solicitudes_guia._reservar_credito_cliente.
# El panel no tiene una guía candidata: incluye todas las reservas vigentes.
# No usar el resumen con ajustes para sustituir este criterio de emisión.
_CUPO_SQL = """
SELECT c.tope_deuda_ars,
       COALESCE((SELECT SUM(e.monto_ars) FROM envios e
                 WHERE e.cliente_id=c.cliente_id
                   AND e.estado NOT IN ('CANCELADO','NC')),0)
       - COALESCE((SELECT SUM(p.monto_ars) FROM pagos p
                   WHERE p.cliente_id=c.cliente_id
                     AND COALESCE(p.estado,'APROBADO')='APROBADO'),0) AS deuda,
       COALESCE((SELECT SUM(s2.precio_tauro_ars) FROM solicitudes_guia s2
                 WHERE s2.cliente_id=c.cliente_id AND (
                     (s2.tracking IS NULL AND s2.estado IN
                         ('EMITIENDO','VERIFICAR_COURIER'))
                     OR (s2.cargo_pendiente=TRUE AND NOT EXISTS (
                         SELECT 1 FROM envios e2 WHERE e2.solicitud_id=s2.id
                     ))
                 )),0) AS reservado
FROM clientes c WHERE c.cliente_id=%s
"""

_COSTOS_SQL = """
WITH cargos AS (
    SELECT e.fecha, e.ambito, e.monto_ars
    FROM envios e
    WHERE e.cliente_id=%s AND e.estado NOT IN ('CANCELADO','NC')
      AND e.monto_ars>0 AND e.fecha>=%s AND e.fecha<%s
    UNION ALL
    SELECT (a.aplicado_at AT TIME ZONE 'America/Argentina/Buenos_Aires')::date,
           e.ambito, ROUND(a.monto_ars,2)
    FROM ajustes_cliente a
    JOIN envios e ON e.solicitud_id=a.solicitud_id
    WHERE e.cliente_id=%s AND e.estado='ACTIVO' AND a.estado='APLICADO'
      AND a.aplicado_at >= (%s::timestamp AT TIME ZONE 'America/Argentina/Buenos_Aires')
      AND a.aplicado_at < (%s::timestamp AT TIME ZONE 'America/Argentina/Buenos_Aires')
)
SELECT DATE_TRUNC('month',fecha)::date AS mes,
       COALESCE(SUM(monto_ars) FILTER (WHERE ambito='NACIONAL'),0)
           AS nacional_ars,
       COALESCE(SUM(monto_ars) FILTER (WHERE ambito='INTERNACIONAL'),0)
           AS internacional_ars,
       COALESCE(SUM(monto_ars) FILTER (
           WHERE ambito IS NULL OR ambito NOT IN ('NACIONAL','INTERNACIONAL')
       ),0) AS sin_clasificar_ars
FROM cargos
GROUP BY DATE_TRUNC('month',fecha)::date
ORDER BY mes LIMIT 6
"""


def _hoy_argentina() -> date:
    return datetime.now(_AR).date()


def _dinero_firmado(valor: Any) -> Decimal:
    """Un mes con sólo notas de crédito puede tener costo neto negativo."""
    try:
        monto = Decimal(str(valor or 0))
        if not monto.is_finite():
            raise ValueError
        return monto.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("El monto no es válido.") from None


def _fecha(valor: Any) -> date | None:
    if isinstance(valor, datetime):
        return valor.astimezone(_AR).date() if valor.tzinfo else valor.date()
    if isinstance(valor, date):
        return valor
    return date.fromisoformat(str(valor)[:10]) if valor else None


def _fecha_visible(valor: Any) -> str:
    fecha = _fecha(valor)
    return fecha.strftime("%d/%m/%Y") if fecha else ""


def _registro_visible(valor: Any) -> str:
    if not valor:
        return ""
    if not isinstance(valor, datetime):
        valor = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
    # TIMESTAMPTZ llega con zona. Un legado sin zona no justifica inventarla.
    if valor.tzinfo:
        return valor.astimezone(_AR).strftime("%d/%m/%Y · %H:%M (AR)")
    return valor.strftime("%d/%m/%Y · %H:%M")


def _presentar_factura(fila: dict, hoy: date) -> dict:
    total = _decimal_monto(fila.get("total"))
    pagado = min(total, _decimal_monto(fila.get("pagado")))
    saldo = total - pagado
    revision = min(saldo, _decimal_monto(fila.get("solicitado")))
    vencimiento = _fecha(fila.get("fecha_vencimiento"))
    if not saldo:
        estado, etiqueta = "PAGADA", "Pagada"
    elif vencimiento and vencimiento < hoy:
        estado, etiqueta = "VENCIDA", "Vencida"
    elif vencimiento == hoy:
        estado, etiqueta = "VENCE_HOY", "Vence hoy"
    elif vencimiento:
        estado, etiqueta = "POR_VENCER", "Próximo vencimiento"
    else:
        estado, etiqueta = "SIN_FECHA", "Sin vencimiento informado"
    return {
        "id": fila["id"],
        "numero_visible": numero_factura_visible("FC", fila["punto_venta"], fila["numero"]),
        "fecha_vencimiento": _fecha_visible(vencimiento),
        "fecha_vencimiento_iso": vencimiento.isoformat() if vencimiento else "",
        "total_ars": total, "pagado_ars": pagado, "saldo_ars": saldo,
        "en_revision_ars": revision, "disponible_pago_ars": saldo - revision,
        "parcial": _CERO < pagado < total, "estado": estado, "estado_label": etiqueta,
        "dias_vencida": max(0, (hoy - vencimiento).days) if vencimiento and saldo else 0,
        "destino_pago": f"F:{fila['id']}",
    }


def _presentar_vencimientos(filas: list[dict], hoy: date) -> dict:
    totales = filas[0] if filas else {}
    return {
        "items": [_presentar_factura(dict(f), hoy) for f in filas],
        "cantidad": int(totales.get("cantidad") or 0),
        "cantidad_vencida": int(totales.get("cantidad_vencida") or 0),
        **{k: _decimal_monto(totales.get(k)) for k in (
            "total_pendiente_ars", "total_vencido_ars", "total_en_revision_ars"
        )},
    }


def _presentar_pago(fila: dict) -> dict:
    estado = str(fila.get("estado") or "APROBADO")
    etiqueta = {"PENDIENTE": "En revisión", "APROBADO": "Acreditado", "RECHAZADO": "Rechazado"}[estado]
    registro = _registro_visible(fila.get("created_at"))
    aplicaciones = [{
        "documento": str(a.get("documento") or ""),
        "tracking": str(a.get("tracking") or ""),
        "monto_ars": _decimal_monto(a.get("monto_ars")),
        "estado": str(a.get("estado") or ""),
        "ambito": str(a.get("ambito") or ""),
    } for a in fila.get("aplicaciones") or []]
    return {
        "id": fila["id"], "fecha": _fecha_visible(fila.get("fecha")),
        "registrado_at": registro, "estado": estado, "estado_label": etiqueta,
        "monto_ars": _decimal_monto(fila.get("monto_ars")),
        "referencia": str(fila.get("referencia") or ""),
        "metodo": str(fila.get("metodo") or ""),
        "tiene_comprobante": bool(fila.get("tiene_comprobante")),
        "aplicaciones": aplicaciones,
        "cantidad_aplicaciones": int(fila.get("cantidad_aplicaciones") or len(aplicaciones)),
        "pasos": [
            {"label": "Comprobante recibido" if fila.get("tiene_comprobante") else "Pago registrado",
             "fecha": registro, "completo": True, "actual": False},
            {"label": etiqueta, "fecha": "", "completo": estado != "PENDIENTE", "actual": True},
        ],
    }


def _presentar_cupo(fila: dict) -> dict:
    tope = fila.get("tope_deuda_ars")
    configurado = tope is not None and Decimal(str(tope)) >= 0
    deuda = Decimal(str(fila.get("deuda") or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    reservado = _decimal_monto(fila.get("reservado"))
    comprometido = max(_CERO, deuda) + reservado
    tope_decimal = _decimal_monto(tope) if configurado else None
    return {
        "configurado": configurado, "tope_ars": tope_decimal,
        "deuda_ars": deuda, "reservado_ars": reservado,
        "comprometido_ars": comprometido,
        "disponible_ars": max(_CERO, tope_decimal - comprometido) if configurado else None,
    }


def _meses(hoy: date) -> list[date]:
    indice = hoy.year * 12 + hoy.month - 1
    return [date(n // 12, n % 12 + 1, 1) for n in range(indice - 5, indice + 1)]


def _presentar_costos(filas: list[dict], hoy: date) -> dict:
    por_mes = {_fecha(f["mes"]): f for f in filas}
    meses = []
    for inicio in _meses(hoy):
        siguiente = (inicio.replace(day=28) + timedelta(days=4)).replace(day=1)
        fila = por_mes.get(inicio, {})
        importes = {k: _dinero_firmado(fila.get(k)) for k in (
            "nacional_ars", "internacional_ars", "sin_clasificar_ars"
        )}
        meses.append({
            "clave": inicio.strftime("%Y-%m"), "label": _MESES[inicio.month - 1],
            "label_completo": f"{_MESES[inicio.month - 1]} {inicio.year}",
            "desde": inicio.isoformat(), "hasta": min(hoy, siguiente - timedelta(days=1)).isoformat(),
            **importes, "total_ars": sum(importes.values(), _CERO),
            "positivo_ars": sum((m for m in importes.values() if m>0), _CERO),
            "negativo_ars": -sum((m for m in importes.values() if m<0), _CERO),
        })
    return {
        "meses": meses, "total_ars": sum((m["total_ars"] for m in meses), _CERO),
        "maximo_ars": max((m["total_ars"] for m in meses), default=_CERO),
        "maximo_positivo_ars": max((m["positivo_ars"] for m in meses), default=_CERO),
        "maximo_negativo_ars": max((m["negativo_ars"] for m in meses), default=_CERO),
        "hay_negativos": any(m["negativo_ars"] for m in meses),
        "criterio_fecha": "Cargos y ajustes según su fecha · ARS",
        "hay_sin_clasificar": any(m["sin_clasificar_ars"] for m in meses),
    }


def obtener_experiencia_cuenta(cliente: str, resumen: dict) -> dict:
    """Complementa un resumen ya calculado con hechos del cliente autenticado.

    Cuatro lecturas acotadas en la misma foto de datos. El disponible es
    informativo: emitir vuelve a validar el límite de manera atómica.
    """
    cliente = str(cliente or "").strip().upper()
    if not cliente or len(cliente) > 80:
        raise ValueError("El cliente no es válido.")
    hoy = _hoy_argentina()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            cur.execute(_VENCIMIENTOS_SQL, (cliente, hoy, hoy))
            vencimientos = _presentar_vencimientos(cur.fetchall(), hoy)
            cur.execute(_PAGOS_SQL, (cliente,))
            pagos = [_presentar_pago(dict(f)) for f in cur.fetchall()]
            cur.execute(_CUPO_SQL, (cliente,))
            cupo = _presentar_cupo(dict(cur.fetchone() or {}))
            periodo = (cliente, _meses(hoy)[0], hoy + timedelta(days=1))
            cur.execute(_COSTOS_SQL, periodo + periodo)
            costos = _presentar_costos(cur.fetchall(), hoy)
    return {
        "vencimientos": vencimientos, "pagos": pagos, "cupo": cupo, "costos": costos,
        "actualizado_fecha": _fecha_visible(hoy),
        "pagos_en_revision_ars": _decimal_monto(resumen.get("pagos_pendientes_ars")),
        "credito_sin_imputar_ars": _decimal_monto(resumen.get("credito_sin_imputar_ars")),
    }
