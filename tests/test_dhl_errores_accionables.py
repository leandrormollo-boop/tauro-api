"""Casos oficiales y sintéticos de MyDHL, con HTTP simulado y sin emitir guías."""
from copy import deepcopy
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.requests import Request

from core.dhl_client import DHLClient
from servicios import solicitudes_guia as sg, api_b2b as b2b
from test_dhl_emision import ENVIO, _cliente, _emitir_capturando
from test_dhl_invoice_multiitems import caja
from test_numeros_operativos_fail_closed import _solicitud_con_bulto


def respuesta(data, status=422):
    resp = mock.Mock(status_code=status)
    resp.json.return_value = data
    return resp


@pytest.mark.parametrize("detalle,esperados", [
    ("#/customerDetails/receiverDetails/postalAddress/cityName: city not found",
     ["Ciudad del destinatario", "no encontró", "Revisá ciudad, código postal y país"]),
    ("#/customerDetails/shipperDetails/postalAddress/postalCode: Invalid postal code",
     ["Código postal del remitente", "Revisá el código postal"]),
    ("#/customerDetails/receiverDetails/postalAddress: city and postal code do not match",
     ["destinatario", "combinación de ciudad y código postal"]),
    # Ejemplo de schema validation en OpenAPI oficial 3.3.2.
    ("#/customerDetails/shipperDetails/postalAddress/cityName: expected maxLength: 45, actual: 49",
     ["Ciudad del remitente", "máximo de 45 caracteres"]),
    ("cityName: expected minLength 1", ["Ciudad", "mínimo de 1 caracteres"]),
    ("#/customerDetails/receiverDetails/contactInformation/phone: required key not found",
     ["Teléfono del destinatario", "Falta un dato obligatorio", "código de país"]),
    ("#/content/exportDeclaration/lineItems/1/commodityCodes/0/value: invalid HS code",
     ["HS code", "Artículo 2", "código arancelario"]),
    ("#/content/packages/2/weight: invalid weight",
     ["Peso", "Bulto 3", "peso declarado en kg"]),
    ("#/content/packages/0/dimensions/height: expected minimum: 0.001, actual: 0",
     ["Medidas", "Bulto 1", "mayor o igual a 0.001"]),
    ("7127: The sellerDetails/postalAddress/countryCode value provided is invalid.",
     ["País del vendedor", "DHL 7127"]),
    ("7008: The requested Special Service Code #/valueAddedServices/0/serviceCode 'EE' is not available between this origin and destination.",
     ["Servicio DHL", "Volvé a cotizar", "DHL 7008"]),
    ("7120: Required either #/outputImageProperties/imageOptions where typeCode=invoice or provide #/documentImages",
     ["Factura comercial", "DHL 7120"]),
    ("declaredValue does not match the sum of exportDeclaration lineItems",
     ["diferencia con la factura comercial", "unidades × valor unitario"]),
])
def test_rechazo_indica_campo_y_correccion(detalle, esperados):
    error = DHLClient._error_legible(respuesta({
        "detail": "Multiple problems found, see Additional Details",
        "additionalDetails": [detalle],
    }))
    for esperado in esperados:
        assert esperado in error
    assert "HTTP 422" in error
    assert "Multiple problems" not in error


def test_varios_errores_diccionarios_no_se_pierden_y_no_revelan_valores():
    error = DHLClient._error_legible(respuesta({"additionalDetails": [
        {"code": "1234", "path": "#/customerDetails/receiverDetails/postalAddress/cityName",
         "message": "City not found", "rejectedValue": "PRIVATE CITY"},
        {"field": "#/content/packages/0/weight", "message": "invalid weight"},
        {"code": "9876", "message": "Unknown rejection; password=PRIVATE-SECRET"},
    ]}))
    assert "Ciudad del destinatario" in error and "Bulto 1" in error
    assert "1234" in error and "9876" in error
    assert "portal pueda interpretar" in error
    assert "PRIVATE" not in error


@pytest.mark.parametrize("data", [None, [], "html", {}, {"detail": "Unknown rejection api_key=SECRET"}])
def test_respuesta_desconocida_no_inventa_ciudad_ni_expone_body(data):
    error = DHLClient._error_legible(respuesta(data, 400))
    assert "HTTP 400" in error and "Tauro" in error
    assert "Ciudad" not in error and "SECRET" not in error


def test_detail_con_prefijo_generico_conserva_la_causa_concreta():
    error = DHLClient._error_legible(respuesta({"detail": "Bad request: Invalid destination postal code"}))
    assert "Código postal del destinatario" in error
    assert "Revisá el código postal" in error


def test_datos_privados_en_mensaje_conocido_no_se_copian():
    error = DHLClient._error_legible(respuesta({"detail":
        "Invalid account number 123456789; Authorization: Basic dGVzdA==; api_secret=SECRET"}))
    assert "Cuenta DHL" in error
    assert all(s not in error for s in ("123456789", "dGVzdA", "SECRET"))


def test_observacion_desconocida_no_desaparece_junto_a_una_conocida():
    error = DHLClient._error_legible(respuesta({"additionalDetails": [
        "cityName: expected minLength 1", "unknown rejection PRIVATE",
    ]}))
    assert "Ciudad" in error and "portal pueda interpretar" in error
    assert "PRIVATE" not in error


def test_codigo_en_observaciones_de_dhl_se_conserva_en_logs_sin_datos_privados():
    data = {"code": "SECRET", "additionalDetails": [
        "7127: PRIVATE sellerDetails/postalAddress/countryCode value is invalid",
        {"code": "PRIVATE", "message": "private"},
    ]}
    assert DHLClient._codigos_error(respuesta(data)) == "7127"


def test_limite_de_longitud_sin_campo_no_se_confunde_con_medidas():
    error = DHLClient._error_legible(respuesta({"detail": "#/location: expected maxLength: 80, actual: 85"}))
    assert "Medidas" not in error
    assert "portal pueda interpretar" in error


def test_multiples_rechazos_de_emision_no_inventan_tracking_y_llegan_completos():
    _, result = _emitir_capturando(status=422, respuesta={"additionalDetails": [
        "#/customerDetails/receiverDetails/postalAddress/cityName: city not found",
        "#/content/exportDeclaration/lineItems/1/commodityCodes/0/value: invalid HS code",
    ]})
    assert not result["encontrado"] and "tracking" not in result
    assert "Ciudad del destinatario" in result["error"]
    assert "Artículo 2" in result["error"]


@pytest.mark.parametrize("exc,esperado", [
    (requests.Timeout("PRIVATE"), "no respondió a tiempo"),
    (requests.ConnectionError("SECRET"), "No pudimos conectar"),
])
@pytest.mark.parametrize("cantidad", [1, 2])
def test_error_de_tarifa_explicito_y_sin_detalles_de_la_conexion(exc, esperado, cantidad):
    with mock.patch("core.dhl_client.requests.post", side_effect=exc), \
            mock.patch("core.dhl_client.requests.get", side_effect=exc):
        result = _cliente().get_rates(
            {"country": "AR", "city": "CABA", "postal_code": "1000"},
            {"country": "US", "city": "Seattle", "postal_code": "98136"},
            paquetes=[{"peso_kg": 1, "largo_cm": 10, "ancho_cm": 10, "alto_cm": 10}] * cantidad,
        )
    assert esperado in result["error"]
    assert "PRIVATE" not in result["error"] and "SECRET" not in result["error"]


def test_desfase_conserva_segunda_caja_totales_y_diferencia_antes_de_dhl(monkeypatch):
    b = caja(2)
    b["valor_declarado_caja_usd"] = 80
    post = mock.Mock()
    monkeypatch.setattr("core.dhl_client.requests.post", post)
    _, _, error_api = b2b._piezas_del_catalogo("DEMO", [caja(), b])
    envio = deepcopy(ENVIO)
    envio["bultos"] = [caja(), b]
    error_emision = _cliente().create_shipment(envio)["error"]
    assert error_api.startswith("valor_declarado_no_coincide:")
    for texto in (error_api, error_emision):
        assert "Caja 2 (2 bultos)" in texto
        assert "USD 160.00" in texto and "USD 200.00" in texto
        assert "Diferencia: USD 40.00" in texto
        assert "contenido real" in texto
    post.assert_not_called()


def test_rechazo_dhl_via_tarifa_llega_al_cliente_sin_emitir(monkeypatch):
    import servicios.carriers as carriers
    sol = _solicitud_con_bulto()
    monkeypatch.setattr(sg, "obtener_solicitud", lambda *_: sol)
    monkeypatch.setattr(sg, "_reservar_credito_cliente", lambda *_: {"ok": True})
    liberar, emitir = mock.Mock(), mock.Mock()
    monkeypatch.setattr(sg, "_liberar_reserva", liberar)
    monkeypatch.setattr(sg, "generar_guia", emitir)
    monkeypatch.setattr(carriers, "CARRIERS", [{
        "id": "dhl", "nombre": "DHL", "logo": "", "servicio": "Express",
        "cliente": _cliente,
    }])
    monkeypatch.setattr(carriers, "carrier_activo", lambda *_: True)
    monkeypatch.setattr("servicios.cotizador.dolar_ars", lambda: 1000)
    monkeypatch.setattr("servicios.configuracion_couriers_cliente.configuracion_cotizacion", lambda *_: {
        "couriers_habilitados": ["dhl"], "pricing_general": {}, "pricing_por_courier": {},
    })
    post = mock.Mock(return_value=respuesta({"additionalDetails": [
        "#/customerDetails/receiverDetails/postalAddress/cityName: city not found",
    ]}))
    monkeypatch.setattr("core.dhl_client.requests.post", post)
    monkeypatch.setattr("core.dhl_client.requests.get", post)
    salida = sg.emitir_guia_como_cliente(44, "WAIMAO")
    assert not salida["ok"]
    assert "Ciudad del destinatario" in salida["error"] and "no encontró" in salida["error"]
    assert "No emitimos ni cobramos nada" in salida["error"]
    assert post.call_count == 1 and post.call_args.args[0].endswith("/rates")
    liberar.assert_called_once_with(44)
    emitir.assert_not_called()


def test_desfase_de_invoice_se_conserva_en_recotizacion(monkeypatch):
    sol = _solicitud_con_bulto()
    sol["bultos"] = [caja(), dict(caja(2), valor_declarado_caja_usd=80)]
    carrier = mock.Mock()
    monkeypatch.setattr("servicios.carriers.cotizar_carriers_cliente", carrier)
    result = sg._recotizar_dhl_antes_de_emitir(sol)
    assert not result["ok"] and "Caja 2" in result["error"]
    assert "USD 40.00" in result["error"]
    assert "valor_declarado_no_coincide:" not in result["error"]
    carrier.assert_not_called()


def test_bulto_corrupto_indica_el_campo_y_libera_reserva(monkeypatch):
    monkeypatch.setattr(sg, "obtener_solicitud", lambda *_: _solicitud_con_bulto(alto_cm=""))
    monkeypatch.setattr(sg, "_reservar_credito_cliente", lambda *_: {"ok": True})
    liberar, emitir = mock.Mock(), mock.Mock()
    monkeypatch.setattr(sg, "_liberar_reserva", liberar)
    monkeypatch.setattr(sg, "generar_guia", emitir)
    salida = sg.emitir_guia_como_cliente(44, "WAIMAO")
    assert "Bulto 1, alto: completá este valor" in salida["error"]
    liberar.assert_called_once_with(44)
    emitir.assert_not_called()


def test_admin_y_portal_no_recortan_el_motivo(monkeypatch):
    from endpoints import admin, portal_cliente as pc
    error = DHLClient._error_legible(respuesta({"additionalDetails": [
        "#/customerDetails/receiverDetails/postalAddress/cityName: city not found",
        "#/content/exportDeclaration/lineItems/1/commodityCodes/0/value: invalid HS code",
    ]}))
    assert len(error) > 200
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    monkeypatch.setattr(admin, "_is_auth", lambda *_: True)
    monkeypatch.setattr(admin, "generar_guia", lambda *_: {"ok": False, "error": error})
    monkeypatch.setattr(sg, "emitir_guia_como_cliente", lambda *_: {"ok": False, "error": error})
    for response, key in ((admin.admin_pedido_generar_guia(request, 44), "guia_error"),
                          (pc.emitir_guia_portal(request, 44, cliente="DEMO"), "error")):
        assert response.status_code == 303
        assert parse_qs(urlparse(response.headers["location"]).query)[key] == [error]


def test_mensaje_en_html_escapa_datos_y_separa_observaciones():
    env = Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"),
                      autoescape=select_autoescape())
    html = env.get_template("_error_operacion.html").render(
        mensaje_error="Ciudad del destinatario: revisá el código postal.\n<script>alert(1)</script>")
    assert 'role="alert"' in html and html.count("<p ") == 2
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_admin_muestra_el_rechazo_una_sola_vez():
    env = Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"),
                      autoescape=select_autoescape())
    env.globals.update(pendientes_admin=lambda: 0, alertas_guias_reemplazadas=lambda: 0)
    request = Request({"type": "http", "method": "GET", "path": "/admin/pedidos", "headers": [],
                       "state": {"csp_nonce": "test"}})
    html = env.get_template("admin/pedidos.html").render(
        request=request, solicitudes=[], estados=[], estados_ui={},
        flash_error="Ciudad del destinatario: ciudad no encontrada.\nRevisá el código postal.",
    )
    assert html.count("Ciudad del destinatario: ciudad no encontrada.") == 1
    assert html.count('role="alert"') == 1
