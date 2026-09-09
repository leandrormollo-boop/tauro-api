"""WAIMAO: varios artículos en una caja, sin emitir ni tocar cuentas reales."""
from copy import deepcopy
from decimal import Decimal
import inspect
import json
from pathlib import Path
from unittest import mock

import pytest
from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request

import endpoints.portal_cliente as pc
from servicios import api_b2b as b2b, solicitudes_guia as sg
from servicios.invoice_comercial import normalizar_items_invoice, total_items_invoice
from test_dhl_client import _emitir, SHIPPER_CN, RECEIVER_AR
from test_waimao_dhl_pilot import _dhl


def caja(cantidad=1):
    items = [
        {"descripcion_en": "Cotton shirts", "unidades_aduana": 4,
         "valor_unitario_usd": 25, "peso_neto_kg": 1.5,
         "hs_code": "6109.10", "pais_origen": "CN"},
        {"descripcion_en": "Polyester trousers", "unidades_aduana": 2,
         "valor_unitario_usd": 50, "peso_neto_kg": 2,
         "hs_code": "6204.63", "pais_origen": "VN"},
    ]
    return {"producto": "", "cantidad": cantidad, "peso_kg": 4,
            "largo_cm": 40, "ancho_cm": 30, "alto_cm": 20,
            **{k: v for k, v in items[0].items() if k != "peso_neto_kg"},
            "valor_declarado_caja_usd": 200 / cantidad, "items_invoice": items}


def test_una_caja_dos_items_sin_duplicar_flete_o_invoice():
    piezas, detalle, error = b2b._piezas_del_catalogo("WAIMAO", [caja()])
    assert error is None
    assert len(piezas) == 1 and piezas[0]["peso_kg"] == 4
    assert total_items_invoice(detalle[0]["items_invoice"]) == Decimal("200.00")
    payload = _emitir({"bultos": detalle, "asegurar_carga": True})
    assert len(payload["content"]["packages"]) == 1
    lineas = payload["content"]["exportDeclaration"]["lineItems"]
    assert [i["number"] for i in lineas] == [1, 2]
    assert [i["description"] for i in lineas] == ["Cotton shirts", "Polyester trousers"]
    assert [i["quantity"]["value"] for i in lineas] == [4, 2]
    assert [i["price"] for i in lineas] == [25, 50]
    assert [i["manufacturerCountry"] for i in lineas] == ["CN", "VN"]
    assert [i["weight"]["netValue"] for i in lineas] == [1.5, 2]
    assert payload["content"]["declaredValue"] == 200
    assert next(s for s in payload["valueAddedServices"] if s["serviceCode"] == "II")["value"] == 200


def test_dos_cajas_iguales_no_multiplican_los_items():
    piezas, detalle, error = b2b._piezas_del_catalogo("WAIMAO", [caja(2), caja()])
    assert error is None and len(piezas) == 3
    payload = _emitir({"bultos": detalle})
    assert len(payload["content"]["packages"]) == 3
    assert payload["content"]["declaredValue"] == 400
    lineas = payload["content"]["exportDeclaration"]["lineItems"]
    assert [i["number"] for i in lineas] == [1, 2, 3, 4]
    assert [i["quantity"]["value"] for i in lineas] == [4, 2, 4, 2]


@pytest.mark.parametrize("cambio", [
    {"unidades_aduana": "1.5"}, {"unidades_aduana": 0},
    {"unidades_aduana": 10000}, {"valor_unitario_usd": "NaN"},
    {"valor_unitario_usd": "-1"}, {"valor_unitario_usd": "1.2345"},
    {"descripcion_en": ""}, {"descripcion_en": "x" * 76},
    {"peso_neto_kg": ""}, {"peso_neto_kg": "Infinity"},
    {"peso_neto_kg": 8}, {"pais_origen": "ZZ"}, {"hs_code": "abc"},
])
def test_dato_invalido_en_segundo_item_no_se_omite(cambio):
    b = caja()
    b["items_invoice"][1].update(cambio)
    with pytest.raises(ValueError):
        normalizar_items_invoice(b["items_invoice"], peso_total_kg=4)
    assert b2b._piezas_del_catalogo("WAIMAO", [b])[2].startswith("invoice_invalida:")


def test_decimales_espanoles_y_suma_de_centavos():
    items = caja()["items_invoice"]
    items[0].update(valor_unitario_usd="0,10", peso_neto_kg="1,5")
    items[1].update(valor_unitario_usd="0,20")
    normalizados = normalizar_items_invoice(items, peso_total_kg=4)
    assert total_items_invoice(normalizados) == Decimal("0.80")


def test_limite_es_por_envio_y_no_por_caja():
    b = caja()
    b["items_invoice"] = [dict(b["items_invoice"][0], peso_neto_kg=0.001)] * 51
    b["valor_declarado_caja_usd"] = 5100
    assert "máximo 100" in b2b._piezas_del_catalogo("WAIMAO", [b, deepcopy(b)])[2]


def test_total_de_todos_los_items_debe_coincidir_con_cajas():
    b = caja()
    b["valor_declarado_caja_usd"] = 100  # Sólo el primer artículo: inválido.
    assert b2b._piezas_del_catalogo("WAIMAO", [b])[2].startswith("valor_declarado_no_coincide:")


def test_dhl_revalida_items_guardados_antes_de_llamar_a_la_api():
    b = caja()
    b["items_invoice"][1]["peso_neto_kg"] = -1
    with mock.patch("core.dhl_client.requests.post") as post:
        result = _dhl().create_shipment({"shipper": SHIPPER_CN, "recipient": RECEIVER_AR, "bultos": [b]})
    assert not result["encontrado"] and "peso neto" in result["error"]
    post.assert_not_called()


def test_despachador_no_pierde_items_entre_snapshot_y_dhl(monkeypatch):
    sol = {"id": 999, "cliente_id": "WAIMAO", "estado": "SOLICITADO",
           "courier": "DHL", "bultos": [caja()], "remitente_pais": "CN",
           "destino_pais": "AR", "producto_alias": "CARGA"}
    monkeypatch.setattr(sg, "obtener_solicitud", lambda *a: sol)
    monkeypatch.setattr(sg, "_reservar_para_emitir", lambda *a: True)
    monkeypatch.setattr(sg, "_persistir_referencia_courier", lambda *a: True)
    monkeypatch.setattr("servicios.catalogo.get_producto", lambda *a: None)
    saved = mock.Mock()
    monkeypatch.setattr(sg, "guardar_guia_generada", saved)
    api = mock.Mock()
    api.create_shipment.return_value = {"encontrado": True, "tracking": "TEST", "label_pdf": b"%PDF"}
    with mock.patch("core.dhl_client.DHLClient", return_value=api):
        result = sg.generar_guia_internacional(999, courier="DHL")
    assert result["ok"]
    assert api.create_shipment.call_args.args[0]["bultos"][0]["items_invoice"] == caja()["items_invoice"]
    api.create_shipment.assert_called_once()
    saved.assert_called_once()


@pytest.mark.parametrize("courier,clase", [
    ("FEDEX", "core.fedex_client.FedExClient"), ("UPS", "core.ups_client.UPSClient"),
])
def test_otro_courier_no_emite_una_invoice_incompleta(monkeypatch, courier, clase):
    sol = {"id": 999, "cliente_id": "WAIMAO", "estado": "SOLICITADO",
           "bultos": [caja()], "remitente_pais": "CN", "destino_pais": "AR"}
    monkeypatch.setattr(sg, "obtener_solicitud", lambda *a: sol)
    monkeypatch.setattr(sg, "_reservar_para_emitir", lambda *a: True)
    monkeypatch.setattr("servicios.catalogo.get_producto", lambda *a: None)
    liberar = mock.Mock()
    monkeypatch.setattr(sg, "_liberar_reserva", liberar)
    with mock.patch(clase) as api:
        result = sg.generar_guia_internacional(999, courier=courier)
    assert not result["ok"] and "requiere DHL" in result["error"]
    api.assert_not_called()
    liberar.assert_called_once_with(999)


@pytest.fixture
def portal(monkeypatch):
    monkeypatch.setattr(pc.templates, "TemplateResponse", lambda **kw: kw)
    monkeypatch.setattr(pc, "get_productos", lambda *a: [])
    monkeypatch.setattr(pc, "listar_direcciones", lambda *a: [])
    monkeypatch.setattr(pc, "obtener_remitente_para_envio", lambda *a: None)
    monkeypatch.setattr(pc, "tax_paga_cliente", lambda *a: "DESTINATARIO")
    monkeypatch.setattr(pc, "courier_default_cliente", lambda *a: "dhl")
    monkeypatch.setattr("servicios.cotizador.dolar_ars", lambda: 1000)
    monkeypatch.setattr("servicios.configuracion_couriers_cliente.configuracion_cotizacion", lambda *a: {
        "pricing_general": {}, "pricing_por_courier": {}, "couriers_habilitados": {"dhl", "fedex"},
    })
    quotes = mock.Mock(return_value=[{
        "id": "dhl", "nombre": "DHL Express", "estado": "cotizado",
        "precio_ars": 195000, "precio_usd": 195,
    }])
    monkeypatch.setattr("servicios.carriers.cotizar_carriers_cliente", quotes)
    created = mock.Mock(return_value={"id": 99})
    monkeypatch.setattr(pc, "crear_solicitud_guia", created)
    return created, quotes


def submit(**overrides):
    request = Request({"type": "http", "method": "POST", "path": "/portal/envios/nuevo",
                       "headers": [], "query_string": b"", "server": ("testserver", 80)})
    request.state.csp_nonce = "test"
    fields = {name: param.default.default for name, param in inspect.signature(pc.envio_nuevo_post).parameters.items()
              if hasattr(param.default, "default")}
    fields.update(
        cliente="WAIMAO", ambito="internacional", destino_pais="AR", intl_courier="dhl",
        rem_nombre="Proveedor prueba", rem_contacto="Contacto prueba", rem_email="shipper@example.com",
        rem_telefono="+86 12345678", rem_direccion="Test Street", rem_ciudad="SHENZHEN",
        rem_zip="518000", rem_pais="CN", dest_nombre="WAIMAO",
        dest_telefono="+54 1112345678", dest_direccion="Direccion de prueba",
        dest_ciudad="CABA", dest_zip="1000", precio_cotizado_ars="195000",
        bulto_producto=[""], bulto_cantidad=["1"], bulto_peso=["4"],
        bulto_largo=["40"], bulto_ancho=["30"], bulto_alto=["20"],
        bulto_valor_caja_usd=["200"], bulto_desc_en=["Cotton shirts"],
        bulto_unidades_aduana=["4"], bulto_valor_usd=["25"],
        bulto_hs=["6109.10"], bulto_pais_fab=["CN"], bulto_peso_neto=["1.5"],
        bulto_items_extra=[json.dumps(caja()["items_invoice"][1:])],
    )
    fields.update(overrides)
    return pc.envio_nuevo_post(request, **fields)


def test_submit_persiste_ambos_items_y_precio_una_caja(portal):
    created, quotes = portal
    response = submit()
    assert response.status_code == 303
    saved = created.call_args.kwargs
    assert saved["cliente_id"] == "WAIMAO" and saved["cantidad"] == 1
    assert saved["valor_declarado_usd"] == 200 and saved["peso_kg"] == 4
    assert saved["bultos"][0]["items_invoice"] == caja()["items_invoice"]
    assert quotes.call_args.kwargs["couriers_habilitados"] == ["dhl"]
    assert len(quotes.call_args.kwargs["paquetes"]) == 1


def test_error_conserva_articulos_y_abre_invoice(portal):
    created, _ = portal
    response = submit(bulto_valor_caja_usd=["100"])
    assert "no_coincide" in response["context"]["error"]
    assert response["context"]["form"]["initial_step"] == 4
    assert len(response["context"]["form"]["bultos"][0]["items_invoice"]) == 2
    created.assert_not_called()


@pytest.mark.parametrize("raw", ["oops", "null", "{}", '["invalid"]'])
def test_json_manipulado_no_crea_solicitud(portal, raw):
    created, quotes = portal
    response = submit(bulto_items_extra=[raw])
    assert response["context"]["error"]
    created.assert_not_called()
    quotes.assert_not_called()


def test_repetir_y_recotizar_conservan_todos_los_articulos(portal):
    sol = {"cliente_id": "WAIMAO", "bultos": [caja()], "courier": "DHL",
           "remitente_pais": "CN", "destino_pais": "AR", "precio_tauro_ars": 195000}
    form, _ = pc._precargar_envio_existente(sol)
    assert form["bultos"][0]["items_invoice"] == caja()["items_invoice"]
    captured = mock.Mock(return_value={"encontrado": False, "motivo": "test"})
    with mock.patch.object(b2b, "cotizar_couriers_cliente", captured):
        sg._recotizar_dhl_antes_de_emitir(sol)
    assert captured.call_args.args[2][0]["items_invoice"] == caja()["items_invoice"]


def test_resumen_de_verificacion_muestra_todos_y_escapa_texto():
    env = Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"), autoescape=True)
    b = caja()
    b["items_invoice"][1]["descripcion_en"] = '<img src=x onerror="alert(1)">'
    html = env.get_template("portal/_envio_verificacion.html").render(s={"bultos": [b]})
    assert "Cotton shirts" in html and "Polyester" not in html
    assert "&lt;img" in html and '<img src=x' not in html
