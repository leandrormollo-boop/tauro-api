"""Cotizaciones comerciales por cuenta, sin costos ni márgenes en la salida."""
from decimal import Decimal, InvalidOperation

from core.database import get_conn
from servicios.cotizaciones_reseller import _guardar_opciones, _render_pdf
from servicios.presentacion import dinero_ars


def guardar_opciones(cliente, *, ruta, bultos, peso_facturable_kg, opciones):
    if not opciones:
        return opciones or []
    try:
        guardadas = _guardar_opciones(cliente.strip().upper(), ruta=ruta, bultos=bultos,
                                     peso_facturable_kg=peso_facturable_kg, opciones=opciones)
    except Exception as exc:
        # Una falla de almacenamiento no oculta tarifas válidas. Sin snapshot
        # no hay descarga ni comparación a partir de importes del navegador.
        print(f"[cotizacion-portal] snapshot no disponible: {type(exc).__name__}")
        return opciones
    return [{**op, "portal_quote_id": op["reseller_quote_id"]} for op in guardadas]


def obtener(cliente, quote_id):
    if not isinstance(quote_id, str) or not quote_id.startswith("RQ-") or len(quote_id) > 80:
        return None
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT q.* FROM cotizaciones_reseller q
                       JOIN clientes c ON c.cliente_id=q.cliente_id
                       WHERE q.quote_id=%s AND q.cliente_id=%s
                         AND q.vigente_hasta >= NOW() AND c.activo=TRUE""",
                    (quote_id, cliente.strip().upper()))
        row = cur.fetchone()
    return dict(row) if row else None


def referencia(cliente, quote_id):
    """Un precio original comprobado en servidor, independiente del aceptado."""
    if not isinstance(quote_id, str) or not quote_id:
        return None
    try:
        if quote_id.startswith("RQ-"):
            row = obtener(cliente, quote_id)
            return {"precio_ars": row["precio_base_ars"]} if row else None
        from servicios.leads import obtener_cotizacion
        row = obtener_cotizacion(quote_id, exigir_vigente=True)
        if row:
            op = next((o for o in row["opciones"] if o.get("recomendada")), row["opciones"][0])
            return {"precio_ars": op["precio_ars"]}
    except Exception:
        return None
    return None


def comparar(original, actual, motivo=None):
    if not original:
        return None
    try:
        anterior = Decimal(str(original["precio_ars"])).quantize(Decimal("0.01"))
        completo = Decimal(str(actual)).quantize(Decimal("0.01"))
        if not anterior.is_finite() or not completo.is_finite() or anterior <= 0:
            return None
    except (InvalidOperation, TypeError, ValueError, KeyError):
        return None
    diferencia = completo - anterior
    if not diferencia:
        return None
    return {"texto": f"Cotizaste {dinero_ars(anterior)} · con los datos completos: {dinero_ars(completo)}",
            "diferencia": f"Diferencia: {'+' if diferencia > 0 else '−'} {dinero_ars(abs(diferencia))}",
            "motivo": motivo or "Motivo: Se confirma al emitir"}


def generar_pdf(cliente, quote_id):
    row = obtener(cliente, quote_id)
    if not row:
        raise ValueError("La cotización no existe, venció o no pertenece a tu cuenta.")
    # Precio fijo del snapshot. No se recibe un importe editable del navegador.
    return _render_pdf(row, Decimal(str(row["precio_base_ars"])))
