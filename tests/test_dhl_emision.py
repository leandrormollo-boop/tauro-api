"""
Emisión de guías por DHL (MyDHL API POST /shipments).

Lo que se protege acá: que el payload que se le manda a DHL sea el que la
API exige. Un campo mal armado no rompe en desarrollo — rompe el día que se
emite una guía real, con el cliente esperando.
"""
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.dhl_client import DHLClient  # noqa: E402

ENVIO = {
    "shipper": {"nombre": "Prete Rosso", "empresa": "PRETE ROSSO S.A.",
                "telefono": "1145678900", "email": "envios@prete.com",
                "calle": "Nicasio Oroño 1680", "ciudad": "CABA",
                "estado": "C", "zip": "1416", "pais": "AR",
                "documento": "20111111112"},
    "recipient": {"nombre": "Michelle Bordelon", "telefono": "3103446337",
                  "calle": "5223 42nd Ave SW", "ciudad": "Seattle",
                  "estado": "WA", "zip": "98136", "pais": "US"},
    "bultos": [{"peso_kg": 1.4, "largo": 33, "ancho": 33, "alto": 22,
                "valor_unitario_usd": 100, "unidades": 2,
                "hs_code": "4202.21.00", "descripcion_en": "Leather handbag",
                "pais_origen": "AR"}],
}

PICKUP_PACKAGES = [{
    "cantidad": 1, "unidades_aduana": 1, "peso_kg": 1.4,
    "largo_cm": 33, "ancho_cm": 33, "alto_cm": 22,
    "valor_unitario_usd": 100,
}]


def _cliente():
    c = DHLClient()
    c.api_key, c.api_secret = "k", "s"
    c.account_number, c.account_import = "741622792", "730089966"
    return c


def _emitir_capturando(envio=None, respuesta=None, status=200):
    """Emite interceptando el POST: devuelve (payload_enviado, resultado)."""
    capturado = {}

    class FakeResp:
        status_code = status
        text = "error simulado"
        def json(self):
            if respuesta is not None:
                return respuesta
            pdf = "JVBERi0xLjQK"
            return {
                "shipmentTrackingNumber": "1234567890",
                "documents": [
                    {"typeCode": "label", "content": pdf},
                    {"typeCode": "invoice", "content": pdf},
                ],
            }

    def fake_post(url, json=None, **kw):
        capturado["url"] = url
        capturado["body"] = json
        capturado["headers"] = kw.get("headers", {})
        return FakeResp()

    with mock.patch("core.dhl_client.requests.post", side_effect=fake_post):
        r = _cliente().create_shipment(envio or ENVIO)
    return capturado, r


def test_emite_y_devuelve_tracking_y_label():
    _, r = _emitir_capturando()
    assert r["encontrado"] and r["tracking"] == "1234567890"
    assert r["label_pdf"] and r["label_pdf"].startswith(b"%PDF")
    assert r["invoice_pdf"] and r["invoice_pdf"].startswith(b"%PDF")


def test_solicita_data_staging_guia_y_factura_comercial():
    cap, _ = _emitir_capturando()
    body = cap["body"]
    assert body["valueAddedServices"] == [{"serviceCode": "PV"}]
    assert body["outputImageProperties"] == {
        "encodingFormat": "pdf",
        "imageOptions": [
            {"typeCode": "label", "templateName": "ECOM26_84_A4_001"},
            {
                "typeCode": "invoice",
                "templateName": "COMMERCIAL_INVOICE_P_10",
                "isRequested": True,
                "invoiceType": "commercial",
            },
        ],
    }


def test_exportador_argentino_sin_cuit_valido_no_llega_a_dhl():
    envio = {**ENVIO, "shipper": {**ENVIO["shipper"], "documento": "20-1"}}
    with mock.patch("core.dhl_client.requests.post") as post:
        r = _cliente().create_shipment(envio)
    assert not r["encontrado"] and "CUIT argentino válido" in r["error"]
    post.assert_not_called()


def test_usa_la_referencia_que_el_servicio_persistio_antes_del_post():
    referencia = "tauro-dhl-ship-81-abcd1234"
    cap, r = _emitir_capturando({**ENVIO, "message_reference": referencia})

    assert cap["headers"]["Message-Reference"] == referencia
    assert r["message_reference"] == referencia


def test_las_direcciones_llegan_completas_a_dhl():
    """
    Regresión de la guía #4 (06/08): el despachador arma las direcciones con
    el contrato en CASTELLANO ({ciudad, zip, pais} — ver el docstring de
    FedExClient.create_shipment) y `_direccion` leía sólo inglés ({city,
    postal_code, country}). Resultado: cityName y countryCode viajaban VACÍOS
    y DHL rechazaba con "expected minLength 1, actual 0".

    Este fixture siempre usó castellano, pero nadie miraba qué llegaba en el
    payload: el test pasaba con las direcciones vacías. Nunca más.
    """
    cap, _ = _emitir_capturando()
    ship = cap["body"]["customerDetails"]["shipperDetails"]["postalAddress"]
    recv = cap["body"]["customerDetails"]["receiverDetails"]["postalAddress"]

    assert ship["cityName"] == "CABA"
    assert ship["countryCode"] == "AR"
    assert ship["postalCode"] == "1416"
    assert recv["cityName"] == "Seattle"
    assert recv["countryCode"] == "US"
    assert recv["postalCode"] == "98136"

    # minLength 1 / pattern .*\S+.* del schema: nada puede ir vacío o espacios.
    for bloque in (ship, recv):
        for clave in ("cityName", "countryCode"):
            assert bloque[clave].strip(), f"{clave} viajó vacío"


def test_el_camino_ingles_de_cotizacion_sigue_andando():
    """La cotización pasa {city, postal_code, country}: no romperla al biling."""
    envio = {
        **ENVIO,
        "shipper": {"nombre": "X", "telefono": "1", "calle": "Calle 1",
                    "city": "CABA", "postal_code": "1414", "country": "AR",
                    "documento": "20111111112"},
        "recipient": {"nombre": "Y", "telefono": "2", "calle": "5th Av 1",
                      "city": "New York", "postal_code": "10001", "country": "US"},
    }
    cap, r = _emitir_capturando(envio)
    assert r["encontrado"]
    ship = cap["body"]["customerDetails"]["shipperDetails"]["postalAddress"]
    assert ship["cityName"] == "CABA" and ship["countryCode"] == "AR"


def test_una_pieza_por_unidad():
    """2 unidades = 2 piezas: cada caja viaja con su etiqueta."""
    cap, _ = _emitir_capturando()
    assert len(cap["body"]["content"]["packages"]) == 2


def test_declara_aduana_con_hs_code_sin_puntos():
    """DHL rechaza el HS con puntos: 4202.21.00 tiene que viajar 42022100."""
    cap, _ = _emitir_capturando()
    contenido = cap["body"]["content"]
    assert contenido["isCustomsDeclarable"] is True
    item = contenido["exportDeclaration"]["lineItems"][0]
    assert item["commodityCodes"][0]["value"] == "42022100"
    # valor declarado = unitario × unidades
    assert contenido["declaredValue"] == 200


def test_hs_code_opcional_no_bloquea_la_emision():
    envio = {**ENVIO, "bultos": [{**ENVIO["bultos"][0], "hs_code": ""}]}
    cap, resultado = _emitir_capturando(envio)
    item = cap["body"]["content"]["exportDeclaration"]["lineItems"][0]
    assert resultado["encontrado"]
    assert item["commodityCodes"] == []
    assert item["price"] == 100
    assert item["quantity"]["value"] == 2
    assert cap["body"]["content"]["declaredValue"] == 200


def test_usa_la_cuenta_de_exportacion_para_salidas():
    cap, _ = _emitir_capturando()
    assert cap["body"]["accounts"][0]["number"] == "741622792"


def test_importacion_sin_cuenta_no_emite():
    """Emitir una importación con la cuenta de expo factura contra la grilla
    equivocada: mejor no emitir."""
    c = DHLClient()
    c.api_key, c.api_secret, c.account_number = "k", "s", "741622792"
    c.account_import = None
    envio = {**ENVIO,
             "shipper": {**ENVIO["shipper"], "pais": "US"},
             "recipient": {**ENVIO["recipient"], "pais": "AR"}}
    r = c.create_shipment(envio)
    assert not r["encontrado"] and "importación" in r["error"]


def test_sin_credenciales_no_intenta():
    c = DHLClient()
    c.api_key = c.api_secret = None
    r = c.create_shipment(ENVIO)
    assert not r["encontrado"] and "credenciales" in r["error"]


def test_telefono_vacio_bloquea_antes_de_inventar_un_contacto():
    """Un dato falso puede invalidar la guía: se pide el teléfono real."""
    envio = {**ENVIO, "recipient": {**ENVIO["recipient"], "telefono": ""}}
    with mock.patch("core.dhl_client.requests.post") as post:
        r = _cliente().create_shipment(envio)
    assert not r["encontrado"] and "teléfono" in r["error"]
    post.assert_not_called()


def test_error_de_dhl_se_reporta_sin_inventar_tracking():
    cap, r = _emitir_capturando(respuesta={"detail": "Invalid postal code"}, status=400)
    assert not r["encontrado"] and "Invalid postal code" in r["error"]
    assert "tracking" not in r


def test_401_y_403_de_dhl_informan_acceso_productivo_sin_exponer_respuesta():
    for status in (401, 403):
        respuesta = mock.Mock(status_code=status, text='{"detail":"dato interno"}')
        respuesta.json.return_value = {"detail": "dato interno"}

        error = DHLClient._error_legible(respuesta)

        assert f"HTTP {status}" in error
        assert "dato interno" not in error


def test_respuesta_sin_tracking_no_se_da_por_buena():
    _, r = _emitir_capturando(respuesta={"documents": []})
    assert not r["encontrado"]


def test_excepcion_de_red_avisa_que_hay_que_verificar():
    """Sin respuesta NO sabemos si DHL emitió: el mensaje tiene que decirlo."""
    with mock.patch("core.dhl_client.requests.post", side_effect=OSError("timeout")):
        r = _cliente().create_shipment(ENVIO)
    assert not r["encontrado"] and "Verificá en MyDHL" in r["error"]


@pytest.mark.parametrize("status", [408, 503])
def test_http_transitorio_de_emision_es_incierto_y_conserva_referencia(status):
    """Un 408/5xx puede ocurrir después de que DHL ya persistió la guía."""
    _, r = _emitir_capturando(status=status)
    assert not r["encontrado"]
    assert r["incierto"] is True
    assert r["message_reference"].startswith("tauro-ship-")
    assert "Verificá en MyDHL" in r["error"]


@pytest.mark.parametrize("status", [408, 503])
def test_http_transitorio_de_pickup_es_incierto_y_conserva_referencia(status):
    """No se debe agendar otro retiro si DHL no confirma el resultado."""
    respuesta = mock.Mock(status_code=status, text="Error transitorio")
    datos = {
        "origen": {
            "nombre": "Prete Rosso", "calle": "Nicasio Oroño 1680",
            "telefono": "1145678900", "ciudad": "CABA", "zip": "1416", "pais": "AR",
        },
        "fecha": "2026-08-11", "ready_time": "09:00", "close_time": "17:00",
        "paquetes": PICKUP_PACKAGES,
    }
    with mock.patch("core.dhl_client.requests.post", return_value=respuesta):
        r = _cliente().create_pickup(datos)

    assert not r["encontrado"]
    assert r["incierto"] is True
    assert r["message_reference"].startswith("tauro-pickup-")
    assert "Verificá en MyDHL" in r["error"]


def test_pickup_usa_la_referencia_persistida_antes_del_post():
    respuesta = mock.Mock(status_code=201, text="ok")
    respuesta.json.return_value = {"dispatchConfirmationNumbers": ["PU-1"]}
    datos = {
        "origen": {"nombre": "WAIMAO", "telefono": "1145678900",
                   "calle": "Calle 1", "ciudad": "CABA",
                   "zip": "1000", "pais": "AR"},
        "fecha": "2026-08-11", "ready_time": "09:00", "close_time": "17:00",
        "paquetes": PICKUP_PACKAGES,
        "message_reference": "tauro-dhl-pick-abcd1234",
    }
    with mock.patch("core.dhl_client.requests.post", return_value=respuesta) as post:
        r = _cliente().create_pickup(datos)

    assert post.call_args.kwargs["headers"]["Message-Reference"] == datos["message_reference"]
    assert r["message_reference"] == datos["message_reference"]
