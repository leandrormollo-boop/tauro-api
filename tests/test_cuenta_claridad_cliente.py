"""La cuenta corriente le dice al cliente qué debe, qué falta facturar y qué
acción tiene a mano, sin inventar vencimientos que no existen."""

from pathlib import Path

HTML = (Path(__file__).resolve().parents[1] / "templates/portal/cuenta.html").read_text(encoding="utf-8")


def test_acciones_de_pago_no_se_confunden():
    assert "Imputar un pago" not in HTML
    assert "Asignar un pago a envíos" in HTML
    assert "Informar un pago" in HTML


def test_saldo_explica_cuanto_falta_facturar():
    assert "account-balance-unbilled" in HTML
    assert "envíos que todavía no facturamos" in HTML


def test_sin_facturas_no_promete_un_vencimiento_inventado():
    assert "No hay facturas emitidas pendientes." not in HTML
    assert "Todavía no tenés facturas con vencimiento." in HTML
    assert "cuando emitamos la factura vas a ver acá el importe y la fecha de vencimiento" in HTML
