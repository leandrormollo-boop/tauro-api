"""Imputa sólo el remanente de un pago propio, sin volver a acreditarlo."""
from decimal import Decimal

from core.database import get_conn
from servicios.auditoria import registrar_evento_con_cursor
from servicios.cuenta_corriente import (
    _armar_aplicaciones_documentales, _conflictos_como_valueerror,
    _decimal_monto, _normalizar_destinos_documentales,
)


@_conflictos_como_valueerror
def imputar_pago_cliente(cliente_id, pago_id, destinos, disponible_esperado):
    cliente = str(cliente_id or "").strip().upper()
    seleccion = _normalizar_destinos_documentales(destinos)
    esperado = _decimal_monto(disponible_esperado, permitir_cero=False)
    if not cliente or pago_id <= 0 or not seleccion or len(seleccion) > 200:
        raise ValueError("Elegí entre 1 y 200 envíos o facturas.")
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Serializa imputaciones y la aprobación del administrador.
            cur.execute("""SELECT id, monto_ars, COALESCE(estado,'APROBADO') AS estado
                FROM pagos WHERE id=%s AND cliente_id=%s FOR UPDATE""", (pago_id, cliente))
            pago = cur.fetchone()
            if not pago or pago["estado"] not in ("APROBADO", "PENDIENTE"):
                raise ValueError("Este pago no está disponible para imputar.")
            cur.execute("SELECT factura_id,envio_id,monto_ars FROM pagos_aplicaciones WHERE pago_id=%s", (pago_id,))
            previas = list(cur.fetchall())
            disponible = _decimal_monto(pago["monto_ars"]) - sum(
                (_decimal_monto(f["monto_ars"]) for f in previas), Decimal("0.00"))
            # Una doble pulsación o pestaña desactualizada nunca imputa dos veces.
            if disponible <= 0 or disponible != esperado:
                raise ValueError("El pago cambió o ya fue imputado. Revisá su detalle antes de continuar.")
            existentes = {("F", f["factura_id"]) if f["factura_id"] else ("E", f["envio_id"])
                          for f in previas if f["factura_id"] or f["envio_id"]}
            if existentes.intersection(seleccion):
                raise ValueError("Un documento elegido ya está vinculado a este pago.")
            aplicaciones = _armar_aplicaciones_documentales(
                cur, cliente_id=cliente, monto_pago=disponible, destinos=seleccion)
            if len(aplicaciones) != len(seleccion):
                raise ValueError("El importe no alcanza para todos los envíos elegidos. Quitá los que quedan en cero.")
            estado = "APLICADA" if pago["estado"] == "APROBADO" else "SOLICITADA"
            for a in aplicaciones:
                cur.execute("""INSERT INTO pagos_aplicaciones
                    (pago_id,ambito,monto_ars,estado,factura_id,envio_id,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,NOW())""",
                    (pago_id,a["ambito"],a["monto"],estado,a["factura_id"],a["envio_id"]))
            registrar_evento_con_cursor(cur, event="cuenta.imputar_pago_cliente",
                actor_type="cliente", actor_ref=cliente, ip=None, method="POST",
                path="/portal/pagos/{id}/imputar", status_code=200, success=True,
                request_id=None, metadata={"pago_id":pago_id,"estado":estado,
                    "documentos":[{"factura_id":a["factura_id"],"envio_id":a["envio_id"],
                                   "monto_ars":str(a["monto"])} for a in aplicaciones]})
            return estado
