"""
Emisión de guías por UPS (Shipping API v2403).

Mismo criterio que DHL: se protege el PAYLOAD, porque un campo mal armado
no rompe en desarrollo — rompe el día de la primera guía real.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ups_client import UPSClient  # noqa: E402

ENVIO = {
    "shipper": {"nombre": "Prete Rosso", "empresa": "PRETE ROSSO S.A.",
                "telefono": "1145678900", "calle": "Nicasio Oroño 1680",
                "ciudad": "CABA", "estado": "C", "zip": "1416", "pais": "AR"},
    "recipient": {"nombre": "Michelle Bordelon", "telefono": "3103446337",
                  "calle": "5223 42nd Ave SW", "ciudad": "Seattle",
                  "estado": "WA", "zip": "98136", "pais": "US"},
    "bultos": [{"peso_kg": 1.4, "largo": 33, "ancho": 33, "alto": 22,
                "valor_unitario_usd": 100, "unidades": 2,
                "hs_code": "4202.21.00", "descripcion_en": "Leather handbag",
                "pais_origen": "AR"}],
}


def _cliente():
    c = UPSClient()
    c.client_id, c.client_secret, c.account_number = "id", "sec", "A1B2C3"
    c._token, c._token_expires_at = "tok", 9e18   # token cacheado: no pide OAuth
    return c


def _emitir(envio=None, respuesta=None, status=200):
    capturado = {}

    class FakeResp:
        status_code = status
        text = "error simulado"
        def json(self):
            return respuesta if respuesta is not None else {
                "ShipmentResponse": {"ShipmentResults": {
                    "ShipmentIdentificationNumber": "1Z999AA10123456784",
                    "PackageResults": [{"ShippingLabel": {"GraphicImage": "R0lGODlhAQ=="}}],
                }}
            }

    def fake_post(url, json=None, **kw):
        capturado["url"] = url
        capturado["body"] = json
        return FakeResp()

    with mock.patch("core.ups_client.requests.post", side_effect=fake_post):
        r = _cliente().create_shipment(envio or ENVIO)
    return capturado, r


def test_emite_y_devuelve_tracking_y_label():
    _, r = _emitir()
    assert r["encontrado"] and r["tracking"] == "1Z999AA10123456784"
    assert r["label_pdf"]


def test_una_pieza_por_unidad():
    cap, _ = _emitir()
    assert len(cap["body"]["ShipmentRequest"]["Shipment"]["Package"]) == 2


def test_lleva_factura_de_aduana():
    """Sin InternationalForms un envío con mercadería no despacha."""
    cap, _ = _emitir()
    sso = cap["body"]["ShipmentRequest"]["Shipment"]["ShipmentServiceOptions"]
    forms = sso["InternationalForms"]
    assert forms["FormType"] == "01" and forms["CurrencyCode"] == "USD"
    prod = forms["Product"][0]
    assert prod["CommodityCode"] == "42022100", "el HS viaja sin puntos"
    assert prod["Unit"]["Number"] == "2"


def test_cobra_a_la_cuenta_de_tauro():
    cap, _ = _emitir()
    pago = cap["body"]["ShipmentRequest"]["Shipment"]["PaymentInformation"]
    assert pago["ShipmentCharge"]["BillShipper"]["AccountNumber"] == "A1B2C3"


def test_sin_credenciales_no_intenta():
    c = UPSClient()
    c.client_id = c.client_secret = None
    r = c.create_shipment(ENVIO)
    assert not r["encontrado"] and "Credenciales" in r["error"]


def test_sin_numero_de_cuenta_no_emite():
    c = UPSClient()
    c.client_id, c.client_secret, c.account_number = "id", "sec", None
    r = c.create_shipment(ENVIO)
    assert not r["encontrado"] and "ACCOUNT" in r["error"]


def test_error_de_ups_se_reporta_sin_inventar_tracking():
    _, r = _emitir(respuesta={"response": {"errors": [{"message": "Invalid postal code"}]}},
                   status=400)
    assert not r["encontrado"] and "Invalid postal code" in r["error"]
    assert "tracking" not in r


def test_respuesta_sin_tracking_no_se_da_por_buena():
    _, r = _emitir(respuesta={"ShipmentResponse": {"ShipmentResults": {}}})
    assert not r["encontrado"]


def test_excepcion_de_red_avisa_que_hay_que_verificar():
    with mock.patch("core.ups_client.requests.post", side_effect=OSError("timeout")):
        r = _cliente().create_shipment(ENVIO)
    assert not r["encontrado"] and "Verificá en UPS" in r["error"]
