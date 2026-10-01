"""Contrato de ciudad/CP entre el cotizador y los formularios de envío.

Estas pruebas no consultan couriers ni una base real. Los links sí se
renderizan y se recorren por ASGI para cubrir el handoff que usa el cliente.
"""

from html.parser import HTMLParser
from decimal import Decimal
import json
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlparse
from unittest.mock import Mock

import pytest
from fastapi import FastAPI

import endpoints.portal_cliente as pc
import endpoints.portal_oca as po
from servicios import direcciones as direcciones_servicio
from servicios import cotizador_portal_nacional as nacional
from servicios import oca_portal
from test_cotizador_unificado import TestClient, configurar, datos, render_quote
from test_dhl_invoice_multiitems import portal, submit  # fixtures/helpers reales


class _HTML(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.inputs = {}
        self.links = []
        self.options = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name"):
            self.inputs.setdefault(attrs["name"], []).append(attrs)
        elif tag == "a":
            self.links.append(attrs)
        elif tag == "option":
            self.options.append(attrs)


def _continuar(html, courier):
    for attrs in _HTML(html).links:
        if attrs.get("aria-label") == f"Continuar con {courier}":
            return attrs["href"]
    raise AssertionError(f"No se renderizó el link para {courier}")


def _input(parser, name):
    assert name in parser.inputs, f"Falta el control {name}"
    return parser.inputs[name][0]


def _contacto(ident, tipo, *, ciudad, cp, pais="AR"):
    prefijo = "Origen" if tipo == "REMITENTE" else "Destino"
    fields = {
        "nombre": f"{prefijo} guardado",
        "apellido": "Persona" if tipo == "DESTINATARIO" else "",
        "provincia": "B",
        "localidad": ciudad,
        "calle": "Calle guardada",
        "numero": "123",
        "cp": cp,
        "piso": "",
        "depto": "",
        "email": "agenda@example.invalid",
        "telefono": "+54 11 5555 5555",
    }
    return {
        "id": ident,
        "tipo": tipo,
        "label": f"{prefijo} agenda",
        "fields": fields,
        "nombre": fields["nombre"],
        "documento": "",
        "email": fields["email"],
        "telefono": fields["telefono"],
        "direccion": "Calle guardada 123",
        "ciudad": ciudad,
        "estado": "Buenos Aires",
        "cp": cp,
        "pais": pais,
        "predeterminada": False,
    }


@pytest.fixture
def web(monkeypatch):
    """Portal completo, con menús, agenda, DB y operadores aislados."""
    contactos = {
        7: _contacto(7, "REMITENTE", ciudad="Azul", cp="B7300ABC"),
        8: _contacto(8, "DESTINATARIO", ciudad="Tandil", cp="B7000XYZ"),
    }
    agenda = list(contactos.values())

    monkeypatch.setitem(pc.templates.env.globals, "saldo_menu", lambda *_a: None)
    monkeypatch.setitem(
        pc.templates.env.globals,
        "pendientes_menu",
        lambda *_a: {"envios": 0, "tienda": 0},
    )
    monkeypatch.setitem(pc.templates.env.globals, "ayuda", lambda: {})
    monkeypatch.setattr(pc, "_operadores_cliente", lambda *_a: [])
    monkeypatch.setattr(pc, "get_productos", lambda *_a: [])
    monkeypatch.setattr(
        pc,
        "_paises_con_nacional",
        lambda: [("AR", "Argentina"), ("CO", "Colombia"), ("US", "Estados Unidos")],
    )
    monkeypatch.setattr(pc, "obtener_remitente_para_envio", lambda *_a: None)
    monkeypatch.setattr(pc, "listar_direcciones", lambda *_a: agenda)
    monkeypatch.setattr(
        pc,
        "obtener_direccion",
        lambda cliente, ident, tipo=None: contactos.get(int(ident))
        if cliente == "CLIENTE-UBICACIONES" and (not tipo or contactos.get(int(ident), {}).get("tipo") == tipo) else None,
    )
    monkeypatch.setattr(pc, "tax_paga_cliente", lambda *_a: "DESTINATARIO")
    monkeypatch.setattr(pc, "courier_default_cliente", lambda *_a: "dhl")

    monkeypatch.setattr(po, "listar_direcciones", lambda *_a: agenda)
    monkeypatch.setattr(po, "proyectar", lambda row: row)
    monkeypatch.setattr(
        direcciones_servicio,
        "obtener_direccion",
        lambda cliente, ident, tipo=None: contactos.get(int(ident))
        if cliente == "CLIENTE-UBICACIONES" and (not tipo or contactos.get(int(ident), {}).get("tipo") == tipo) else None,
    )
    monkeypatch.setattr(po, "check_rate", lambda *_a, **_k: True)
    adapter_oca = Mock()
    monkeypatch.setattr(
        po.oca,
        "adapter_cliente",
        lambda *_a: (SimpleNamespace(insured_operation=True), adapter_oca),
    )

    # Una credencial de la máquina no puede convertir estas pruebas en una
    # llamada real si en el futuro cambia el orden de las validaciones.
    for name in (
        "OCA_USUARIO", "OCA_PASSWORD", "OCA_CUIT", "OCA_CUENTA",
        "OCA_OPERATIVA", "DHL_API_KEY", "DHL_API_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)

    app = FastAPI()
    app.include_router(pc.router)
    app.include_router(po.router)
    app.dependency_overrides[pc.cliente_actual] = lambda: "CLIENTE-UBICACIONES"
    with TestClient(app) as client:
        yield client, adapter_oca, contactos


@pytest.mark.parametrize("invertir", [False, True])
def test_link_oca_conserva_cpa_y_manda_cp4_al_courier(monkeypatch, invertir):
    _, adapter = configurar(monkeypatch)
    origen = {
        "origen_provincia": "B", "origen_localidad": "Bahía Blanca",
        "origen_cp": "B8000ABC", "origen_referencia": True,
    }
    destino = {
        "destino_provincia": "X", "destino_localidad": "Río Cuarto",
        "destino_cp": "X5800DEF", "destino_referencia": False,
    }
    if invertir:
        origen, destino = (
            {
                "origen_provincia": "X", "origen_localidad": "Río Cuarto",
                "origen_cp": "X5800DEF", "origen_referencia": False,
            },
            {
                "destino_provincia": "B", "destino_localidad": "Bahía Blanca",
                "destino_cp": "B8000ABC", "destino_referencia": True,
            },
        )

    entrada = {**datos(), **origen, **destino}
    refs = {
        "origen_referencia": entrada.pop("origen_referencia"),
        "destino_referencia": entrada.pop("destino_referencia"),
    }
    resultado = nacional.cotizar_referencia_nacional(
        "CLIENTE-UBICACIONES", **refs, **entrada,
    )
    query = parse_qs(urlparse(resultado["opciones"][0]["continuar_url"]).query)

    assert query["origen_localidad"] == [entrada["origen_localidad"]]
    assert query["destino_localidad"] == [entrada["destino_localidad"]]
    assert query["origen_cp"] == [entrada["origen_cp"]]
    assert query["destino_cp"] == [entrada["destino_cp"]]
    assert (query.get("origen_referencia") == ["1"]) is refs["origen_referencia"]
    assert (query.get("destino_referencia") == ["1"]) is refs["destino_referencia"]
    request = adapter.quote.call_args.args[0]
    assert request.origin["codigo_postal"] == entrada["origen_cp"][1:5]
    assert request.destination["codigo_postal"] == entrada["destino_cp"][1:5]


@pytest.mark.parametrize('path,prefijos', [
    ('/portal/envios/nuevo?ambito=internacional', ('rem_ciudad', 'dest_ciudad')),
    ('/portal/oca/nuevo?ambito=nacional', ('origen_localidad', 'destino_localidad')),
])
def test_formularios_aceptan_contactos_con_roles_invertidos(web, path, prefijos):
    client, adapter, _ = web
    response = client.get(path + '&remitente_id=8&destinatario_id=7')
    assert response.status_code == 200
    parser = _HTML(response.text)
    assert _input(parser, prefijos[0])['value'] == 'Tandil'
    assert _input(parser, prefijos[1])['value'] == 'Azul'
    assert not adapter.mock_calls


@pytest.mark.parametrize(
    "origen_referencia,destino_referencia,origen_ciudad,origen_cp,destino_ciudad,destino_cp",
    [
        ("1", "", "Bogotá D.C.", "110111", "Mérida", "97000"),
        ("", "1", "Mérida", "97000", "Bogotá D.C.", "110111"),
    ],
)
def test_link_dhl_conserva_ubicaciones_y_flags_por_lado(
    origen_referencia,
    destino_referencia,
    origen_ciudad,
    origen_cp,
    destino_ciudad,
    destino_cp,
):
    cajas = json.dumps([
        {"cantidad": 2, "peso_kg": 3.5, "largo_cm": 40, "ancho_cm": 30, "alto_cm": 20},
    ])
    form = {
        "origen_pais": "CO",
        "destino_pais": "US",
        "origen_ciudad": origen_ciudad,
        "origen_cp_internacional": origen_cp,
        "destino_ciudad_internacional": destino_ciudad,
        "destino_cp_internacional": destino_cp,
        "origen_referencia": origen_referencia,
        "destino_referencia": destino_referencia,
        "valor_declarado_usd": "250,50",
        "bultos": [{}],
    }
    html = render_quote(
        form=form,
        opciones=[{
            "carrier_id": "dhl", "carrier_nombre": "DHL", "servicio": "Express",
            "carrier_logo": "/dhl.svg", "precio_final_ars": 123456,
            "dias_estimados": 3,
        }],
        cajas_cotizadas=cajas,
    )
    query = parse_qs(urlparse(_continuar(html, "DHL")).query, keep_blank_values=True)

    assert query["origen_ciudad"] == [origen_ciudad]
    assert query["origen_cp"] == [origen_cp]
    assert query["destino_ciudad"] == [destino_ciudad]
    assert query["destino_cp"] == [destino_cp]
    assert query.get("origen_referencia", [""])[0] == origen_referencia
    assert query.get("destino_referencia", [""])[0] == destino_referencia


def test_roundtrip_link_dhl_precarga_geometria_y_confirmaciones(web):
    client, _, _ = web
    cajas = json.dumps([
        {"cantidad": 2, "peso_kg": 3.5, "largo_cm": 40, "ancho_cm": 30, "alto_cm": 20},
    ])
    html = render_quote(
        form={
            "origen_pais": "CO", "destino_pais": "US",
            "origen_ciudad": "Bogotá D.C.", "origen_cp_internacional": "110111",
            "destino_ciudad_internacional": "Miami", "destino_cp_internacional": "33101",
            "origen_referencia": "1", "destino_referencia": "1",
            "valor_declarado_usd": "250", "bultos": [{}],
        },
        opciones=[{
            "carrier_id": "dhl", "carrier_nombre": "DHL", "servicio": "Express",
            "carrier_logo": "/dhl.svg", "precio_final_ars": 123456,
            "dias_estimados": 3,
        }],
        cajas_cotizadas=cajas,
    )

    response = client.get(_continuar(html, "DHL"))
    assert response.status_code == 200
    parsed = _HTML(response.text)
    assert _input(parsed, "rem_ciudad")["value"] == "Bogotá D.C."
    assert _input(parsed, "rem_zip")["value"] == "110111"
    assert _input(parsed, "dest_ciudad")["value"] == "Miami"
    assert _input(parsed, "dest_zip")["value"] == "33101"
    assert _input(parsed, "bulto_cantidad")["value"] == "2"
    assert Decimal(_input(parsed, "bulto_peso")["value"]) == Decimal("3.5")
    assert Decimal(_input(parsed, "bulto_largo")["value"]) == Decimal("40")
    assert Decimal(_input(parsed, "bulto_ancho")["value"]) == Decimal("30")
    assert Decimal(_input(parsed, "bulto_alto")["value"]) == Decimal("20")
    for lado in ("origen", "destino"):
        assert _input(parsed, f"{lado}_referencia")["value"] == "1"
        checkbox = _input(parsed, f"{lado}_ubicacion_confirmada")
        assert checkbox["type"] == "checkbox" and "required" in checkbox
        assert "checked" not in checkbox
    assert re.search(
        r"confirm[aá].{0,80}ciudad.{0,100}c[oó]digo postal.{0,100}domicilio",
        response.text,
        re.I | re.S,
    )


@pytest.mark.parametrize(
    "parametro,ident,lado_guardado,lado_cotizado",
    [
        ("remitente_id", 7, "origen", "destino"),
        ("destinatario_id", 8, "destino", "origen"),
    ],
)
def test_contacto_internacional_explicito_solo_anula_flag_de_su_lado(
    web, parametro, ident, lado_guardado, lado_cotizado,
):
    client, _, _ = web
    cajas = json.dumps([
        {"cantidad": 1, "peso_kg": 2, "largo_cm": 30, "ancho_cm": 20, "alto_cm": 10},
    ])
    query = {
        "ambito": "internacional", "courier": "dhl", "origen": "AR", "destino": "US",
        "origen_ciudad": "Ciudad origen cotizada", "origen_cp": "1000",
        "destino_ciudad": "Ciudad destino cotizada", "destino_cp": "33101",
        "origen_referencia": "1", "destino_referencia": "1", "cajas": cajas,
        "valor_cotizado": "100", parametro: str(ident),
    }
    response = client.get("/portal/envios/nuevo?" + urlencode(query))
    parsed = _HTML(response.text)

    assert _input(parsed, f"{lado_guardado}_referencia")["value"] == ""
    assert "disabled" in _input(parsed, f"{lado_guardado}_ubicacion_confirmada")
    assert _input(parsed, f"{lado_cotizado}_referencia")["value"] == "1"
    assert Decimal(_input(parsed, "bulto_peso")["value"]) == Decimal("2")
    if lado_guardado == "origen":
        assert _input(parsed, "rem_ciudad")["value"] == "Azul"
        assert _input(parsed, "dest_ciudad")["value"] == "Ciudad destino cotizada"
    else:
        assert _input(parsed, "dest_ciudad")["value"] == "Tandil"
        assert _input(parsed, "rem_ciudad")["value"] == "Ciudad origen cotizada"


def test_roundtrip_link_oca_y_contacto_explicito_conservan_cpa_por_lado(web, monkeypatch):
    client, adapter_oca, _ = web
    configurar(monkeypatch)
    resultado = nacional.cotizar_referencia_nacional(
        "CLIENTE-UBICACIONES",
        **{
            **datos(),
            "origen_provincia": "B", "origen_localidad": "Bahía Blanca",
            "origen_cp": "B8000ABC", "destino_provincia": "X",
            "destino_localidad": "Río Cuarto", "destino_cp": "X5800DEF",
        },
        origen_referencia=True,
        destino_referencia=True,
    )
    link = resultado["opciones"][0]["continuar_url"]
    monkeypatch.setattr(
        po.oca,
        "adapter_cliente",
        lambda *_a: (SimpleNamespace(insured_operation=True), adapter_oca),
    )
    response = client.get(link)
    parsed = _HTML(response.text)
    assert _input(parsed, "origen_localidad")["value"] == "Bahía Blanca"
    assert _input(parsed, "origen_cp")["value"] == "B8000ABC"
    assert _input(parsed, "destino_localidad")["value"] == "Río Cuarto"
    assert _input(parsed, "destino_cp")["value"] == "X5800DEF"
    for lado in ("origen", "destino"):
        assert _input(parsed, f"{lado}_referencia")["value"] == "1"
        assert "required" in _input(parsed, f"{lado}_ubicacion_confirmada")

    reemplazo = client.get(link + "&remitente_id=7")
    reemplazo_parsed = _HTML(reemplazo.text)
    assert _input(reemplazo_parsed, "origen_referencia")["value"] == ""
    assert "disabled" in _input(reemplazo_parsed, "origen_ubicacion_confirmada")
    assert _input(reemplazo_parsed, "destino_referencia")["value"] == "1"
    assert _input(reemplazo_parsed, "origen_localidad")["value"] == "Azul"
    assert _input(reemplazo_parsed, "destino_localidad")["value"] == "Río Cuarto"


@pytest.mark.parametrize(
    "faltante,esperado,paso",
    [("origen", "origen", 1), ("destino", "destino", 2)],
)
def test_post_internacional_bloquea_referencia_sin_confirmar_y_conserva_form(
    portal, monkeypatch, faltante, esperado, paso,
):
    created, quotes = portal
    agenda = [
        _contacto(7, "REMITENTE", ciudad="Azul", cp="B7300ABC"),
        _contacto(8, "DESTINATARIO", ciudad="Tandil", cp="B7000XYZ"),
    ]
    monkeypatch.setattr(pc, "listar_direcciones", lambda *_a: agenda)
    confirmaciones = {
        "origen_ubicacion_confirmada": "" if faltante == "origen" else "1",
        "destino_ubicacion_confirmada": "" if faltante == "destino" else "1",
    }
    response = submit(
        remitente_id="7", destinatario_id="8",
        origen_referencia="1", destino_referencia="1",
        **confirmaciones,
    )
    contexto = response["context"]

    assert esperado in contexto["error"].lower()
    assert "ciudad" in contexto["error"].lower()
    assert "código postal" in contexto["error"].lower()
    assert contexto["form"]["initial_step"] == paso
    assert contexto["form"]["remitente_id"] == "7"
    assert contexto["form"]["destinatario_id"] == "8"
    assert contexto["form"]["origen_referencia"] == "1"
    assert contexto["form"]["destino_referencia"] == "1"
    assert contexto["form"]["origen_ubicacion_confirmada"] == confirmaciones["origen_ubicacion_confirmada"]
    assert contexto["form"]["destino_ubicacion_confirmada"] == confirmaciones["destino_ubicacion_confirmada"]
    assert Decimal(str(contexto["form"]["bultos"][0]["peso_kg"])) == Decimal("4")
    assert contexto["remitentes"] == agenda and contexto["destinatarios"] == agenda
    quotes.assert_not_called()
    created.assert_not_called()


def test_post_internacional_confirmado_cotiza_y_crea(portal):
    created, quotes = portal

    response = submit(
        origen_referencia="1",
        destino_referencia="1",
        origen_ubicacion_confirmada="1",
        destino_ubicacion_confirmada="1",
    )

    assert response.status_code == 303
    quotes.assert_called_once()
    created.assert_called_once()


def test_oca_preparar_confirmado_conserva_domicilios_y_normaliza_cp4(monkeypatch):
    validar_xml = Mock()
    monkeypatch.setattr(oca_portal, "_shipment_xml", validar_xml)
    form = {
        "origen_referencia": "1", "destino_referencia": "1",
        "origen_ubicacion_confirmada": "1", "destino_ubicacion_confirmada": "1",
        "origen_provincia": "B", "origen_localidad": "Bahía Blanca",
        "origen_cp": "B8000ABC", "origen_nombre": "Origen confirmado",
        "origen_calle": "Brown", "origen_numero": "10",
        "origen_email": "origen@example.invalid", "origen_telefono": "2915550000",
        "destino_provincia": "X", "destino_localidad": "Río Cuarto",
        "destino_cp": "X5800DEF", "destino_nombre": "Destino",
        "destino_apellido": "Persona", "destino_calle": "Sobremonte",
        "destino_numero": "20", "destino_email": "destino@example.invalid",
        "destino_telefono": "3585550000", "cantidad_bultos": "2",
        "peso_kg": "3.5", "largo_cm": "40", "ancho_cm": "30",
        "alto_cm": "20", "valor_declarado_ars": "100000",
    }

    payload = oca_portal.preparar(form, SimpleNamespace())

    assert payload["origin"]["cp"] == "8000"
    assert payload["origin"]["localidad"] == "Bahía Blanca"
    assert payload["destination"]["cp"] == "5800"
    assert payload["destination"]["localidad"] == "Río Cuarto"
    assert payload["packages"] == [{
        "quantity": 2,
        "weight_kg": "3.5",
        "length_cm": "40",
        "width_cm": "30",
        "height_cm": "20",
    }]
    validar_xml.assert_called_once()


@pytest.mark.parametrize("faltante", ["origen", "destino"])
def test_post_oca_bloquea_referencia_sin_confirmar_y_conserva_agenda(web, faltante):
    client, adapter_oca, _ = web
    form = {
        "origen_agenda_id": "7", "destino_agenda_id": "8",
        "origen_nombre": "Origen editado", "origen_provincia": "B",
        "origen_localidad": "Bahía Blanca", "origen_calle": "Brown",
        "origen_numero": "10", "origen_cp": "B8000ABC",
        "origen_email": "origen@example.invalid",
        "destino_nombre": "Destino", "destino_apellido": "Persona",
        "destino_provincia": "X", "destino_localidad": "Río Cuarto",
        "destino_calle": "Sobremonte", "destino_numero": "20",
        "destino_cp": "X5800DEF", "cantidad_bultos": "2", "peso_kg": "3.5",
        "largo_cm": "40", "ancho_cm": "30", "alto_cm": "20",
        "valor_declarado_ars": "100000", "origen_referencia": "1",
        "destino_referencia": "1",
        "origen_ubicacion_confirmada": "" if faltante == "origen" else "1",
        "destino_ubicacion_confirmada": "" if faltante == "destino" else "1",
    }
    response = client.post("/portal/oca/cotizar", data=form)
    assert response.status_code == 200
    assert faltante in response.text.lower()
    assert "código postal" in response.text.lower()
    parsed = _HTML(response.text)
    assert _input(parsed, "origen_referencia")["value"] == "1"
    assert _input(parsed, "destino_referencia")["value"] == "1"
    assert _input(parsed, "origen_localidad")["value"] == "Bahía Blanca"
    assert _input(parsed, "destino_cp")["value"] == "X5800DEF"
    assert _input(parsed, "cantidad_bultos")["value"] == "2"
    assert any(o.get("value") == "7" and "selected" in o for o in parsed.options)
    assert any(o.get("value") == "8" and "selected" in o for o in parsed.options)
    for lado in ("origen", "destino"):
        checkbox = _input(parsed, f"{lado}_ubicacion_confirmada")
        assert "required" in checkbox
        assert ("checked" in checkbox) is (form[f"{lado}_ubicacion_confirmada"] == "1")
    adapter_oca.quote.assert_not_called()
