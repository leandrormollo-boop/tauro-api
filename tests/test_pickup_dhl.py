"""
Recolecciones DHL: el payload y el despachador por courier.

Hasta el 06/08 servicios/recolecciones.py tenía FedExClient cableado: una
recolección para guías DHL se agendaba en FedEx y el chofer equivocado tocaba
el timbre. Ahora hay registro por courier, y el payload de DHL está calcado
del ejemplo oficial que mandó DHL (Pickup.txt, 09/2025).

OJO: el payload se valida acá contra el ejemplo; la PRIMERA recolección viva
hay que mirarla igual (mismo criterio que la primera guía).
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.dhl_client import DHLClient  # noqa: E402

ORIGEN = {"nombre": "WAIMAO", "empresa": "Waimao SRL", "telefono": "1133779002",
          "calle": "Av. Corrientes 1234", "ciudad": "CABA", "estado": "C",
          "zip": "1043", "pais": "AR"}

DATOS = {"origen": ORIGEN, "fecha": "2026-08-10", "ready_time": "09:30",
         "close_time": "17:00", "peso_kg": 4.5, "bultos": 3,
         "instrucciones": "tocar timbre 2B"}


def _cliente():
    c = DHLClient()
    c.api_key, c.api_secret = "k", "s"
    c.account_number = "741622792"
    c.account_import = "730089966"
    return c


def _capturar_post(respuesta_json=None, status=200):
    r = mock.Mock()
    r.status_code = status
    r.text = "cuerpo crudo para el log"
    r.json.return_value = respuesta_json or {"dispatchConfirmationNumbers": ["CBJ250100001"]}
    return mock.patch("core.dhl_client.requests.post", return_value=r)


def test_payload_calcado_del_ejemplo_oficial():
    with _capturar_post() as post:
        out = _cliente().create_pickup(DATOS)

    assert out["encontrado"] is True
    assert out["confirmation_code"] == "CBJ250100001"

    body = post.call_args.kwargs["json"]
    # Fecha con OFFSET (-03:00), no el "GMT-03:00" de /shipments: así viene
    # en el ejemplo oficial y los dos endpoints usan formatos distintos.
    assert body["plannedPickupDateAndTime"] == "2026-08-10T09:30:00-03:00"
    assert body["closeTime"] == "17:00"
    assert body["accounts"] == [{"typeCode": "shipper", "number": "741622792"}]
    ship = body["customerDetails"]["shipperDetails"]
    assert ship["postalAddress"]["cityName"] == "CABA"
    assert ship["postalAddress"]["countryCode"] == "AR"
    assert ship["contactInformation"]["companyName"] == "Waimao SRL"
    # 3 bultos = 3 packages, no 1 con quantity.
    assert len(body["shipmentDetails"][0]["packages"]) == 3
    assert body["shipmentDetails"][0]["declaredValue"] == 1.0
    assert body["shipmentDetails"][0]["declaredValueCurrency"] == "USD"
    assert body["specialInstructions"][0]["value"] == "tocar timbre 2B"


def test_pickup_desde_guia_conserva_valor_declarado_y_cpa():
    datos = dict(
        DATOS,
        origen=dict(ORIGEN, zip="C1043ABC"),
        paquetes=[{
            "peso_kg": 2, "largo_cm": 10, "ancho_cm": 10, "alto_cm": 10,
            "cantidad": 1, "unidades_aduana": 8, "valor_unitario_usd": 15,
        }],
    )
    with _capturar_post() as post:
        _cliente().create_pickup(datos)
    body = post.call_args.kwargs["json"]
    assert body["customerDetails"]["shipperDetails"]["postalAddress"]["postalCode"] == "1043"
    assert body["shipmentDetails"][0]["declaredValue"] == 120.0


def test_origen_no_ar_usa_cuenta_impo():
    datos = dict(DATOS, origen=dict(ORIGEN, pais="CN"))
    with _capturar_post() as post:
        _cliente().create_pickup(datos)
    assert post.call_args.kwargs["json"]["accounts"][0]["number"] == "730089966"


def test_error_de_dhl_llega_legible():
    rechazo = {"detail": "Multiple problems found, see Additional Details",
               "additionalDetails": ["cityName: expected minLength 1"]}
    with _capturar_post(respuesta_json=rechazo, status=422):
        out = _cliente().create_pickup(DATOS)
    assert out["encontrado"] is False
    assert "cityName" in out["error"]          # el detalle real, no el genérico


def test_sin_confirmacion_no_es_exito():
    with _capturar_post(respuesta_json={"dispatchConfirmationNumbers": []}):
        out = _cliente().create_pickup(DATOS)
    assert out["encontrado"] is False


def test_despachador_elige_por_courier():
    from servicios.recolecciones import _cliente_pickup
    from core.fedex_client import FedExClient

    assert isinstance(_cliente_pickup("FEDEX"), FedExClient)
    assert isinstance(_cliente_pickup("dhl"), DHLClient)
    assert isinstance(_cliente_pickup(""), FedExClient)   # legacy: default FedEx
    # Courier desconocido → None → error claro. NUNCA caer a FedEx por
    # descarte: es el mismo principio que el despachador de emisión.
    assert _cliente_pickup("UPS") is None


def test_cancelar_usa_solo_el_codigo():
    r = mock.Mock(); r.status_code = 200
    with mock.patch("core.dhl_client.requests.delete", return_value=r) as d:
        out = _cliente().cancel_pickup("CBJ250100001", "2026-08-10", "")
    assert out["ok"] is True
    assert "CBJ250100001" in d.call_args.args[0]
    assert d.call_args.kwargs["params"]["requestorName"]
