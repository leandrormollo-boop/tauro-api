from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock
import json

import pytest

from servicios.pricing_rangos import validar_rangos, calcular_rangos
from servicios.pricing import aplicar_pricing


def rangos():
    return [dict(desde="0", hasta="100000", tipo="FIJO_ARS", valor="14000", minimo="0"),
            dict(desde="100000", hasta=None, tipo="PCT", valor="12", minimo="20000")]


@pytest.mark.parametrize("costo,precio", [(0,14000),(99999.99,113999.99),(100000,120000),(250000,280000)])
def test_limites_y_minimo(costo, precio):
    assert calcular_rangos(costo, rangos())["precio"] == Decimal(str(precio))


@pytest.mark.parametrize("campo,valor", [("desde", "1"), ("hasta", "0"), ("valor", "-1"),
    ("valor", "NaN"), ("valor", "Infinity"), ("valor", True), ("tipo", "OTRO"), ("minimo", "10")])
def test_invalidos(campo, valor):
    filas = rangos()
    filas[0][campo] = valor
    with pytest.raises(ValueError):
        validar_rangos(filas)


@pytest.mark.parametrize("desde", [99999,100001])
def test_no_huecos_ni_superposiciones(desde):
    filas = rangos(); filas[1]["desde"] = desde
    with pytest.raises(ValueError): validar_rangos(filas)


def test_ultimo_abierto_y_sin_fallback():
    filas = rangos(); filas[1]["hasta"] = 300000
    with pytest.raises(ValueError): validar_rangos(filas)
    with pytest.raises(ValueError): calcular_rangos(100, [])
    with pytest.raises(ValueError): calcular_rangos(-1, rangos())
    assert validar_rangos("[]") == []


def test_integracion_conserva_importes_snapshot():
    resultado = aplicar_pricing(costo_usd=100, costo_ars=100000, dolar=1000,
        pricing={"tipo":"RANGOS", "valor":0, "rangos_ars":rangos()})
    assert resultado == dict(precio_final_ars=120000, precio_final_usd=120,
        markup_pct_equivalente=20, markup_tipo="FIJO_ARS", markup_valor=20000)


def test_separacion_nacional_internacional(monkeypatch):
    from servicios import pricing
    row = {"pricing_rangos_internacional":rangos(), "pricing_rangos_nacional":[
        dict(desde=0,hasta=None,tipo="FIJO_ARS",valor=2000,minimo=0)]}
    conn = MagicMock(); conn.__enter__.return_value = conn
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = row
    monkeypatch.setattr(pricing, "get_conn", lambda:conn)
    for ambito,expected in [("nacional",12000),("internacional",24000)]:
        config = pricing.get_pricing_config("TEST", ambito=ambito)
        assert aplicar_pricing(costo_ars=10000,costo_usd=10,dolar=1000,pricing=config)["precio_final_ars"] == expected
    assert pricing.get_pricing_nacional_estricto("TEST")["rangos_ars"][0]["valor"] == "2000"


def test_courier_especifico_tiene_prioridad():
    from servicios.configuracion_couriers_cliente import _armar_matriz
    cliente = dict(cliente_id="TEST",activo=True,markup_tipo="FIJO_ARS",markup_valor=9000,
        pricing_rangos_internacional=rangos())
    fila = dict(courier="dhl",markup_tipo="PCT",markup_valor=30,
        puede_cotizar=False,puede_emitir=False,puede_recolectar=False)
    matriz = _armar_matriz(cliente,[fila])
    assert matriz["pricing_general"]["tipo"] == "RANGOS"
    assert next(c for c in matriz["couriers"] if c["id"] == "dhl")["pricing"]["tipo"] == "PCT"


def test_admin_simular_no_escribe_y_guardar_audita(monkeypatch):
    from endpoints import admin
    from servicios import auditoria
    monkeypatch.setattr(admin,"_is_auth",lambda _:True)
    monkeypatch.setattr(admin.templates,"TemplateResponse",lambda **kwargs:kwargs)
    conn = MagicMock(); conn.__enter__.return_value = conn
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = {"pricing_rangos_internacional":[],"pricing_rangos_nacional":[],"perfil_comercial":""}
    get_conn = MagicMock(return_value=conn); monkeypatch.setattr(admin,"get_conn",get_conn)
    audit = MagicMock(); monkeypatch.setattr(auditoria,"registrar_desde_request_con_cursor",audit)
    args = dict(request=SimpleNamespace(),cliente_id="TEST",internacional=json.dumps(rangos()),
        nacional="[]",perfil="ECOMMERCE",accion="simular",costo_prueba="100000",
        ambito_prueba="internacional",admin_token="test")
    result = admin.admin_tarifas_guardar(**args)
    assert result["context"]["simulacion"]["precio"] == 120000
    get_conn.assert_not_called()
    args["accion"] = "guardar"
    assert admin.admin_tarifas_guardar(**args).status_code == 303
    assert cur.execute.call_count == 2
    audit.assert_called_once()
    assert "UPDATE clientes" in cur.execute.call_args[0][0]


def test_admin_invalido_no_escribe(monkeypatch):
    from endpoints import admin
    monkeypatch.setattr(admin,"_is_auth",lambda _:True)
    monkeypatch.setattr(admin.templates,"TemplateResponse",lambda **kwargs:kwargs)
    db = MagicMock(); monkeypatch.setattr(admin,"get_conn",db)
    result = admin.admin_tarifas_guardar(request=SimpleNamespace(),cliente_id="TEST",
        internacional='[{"desde":5}]',nacional="[]",perfil="",accion="guardar",
        costo_prueba="",ambito_prueba="internacional",admin_token="test")
    assert result["status_code"] == 422
    db.assert_not_called()


def test_template_render_y_nonce():
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    from pathlib import Path
    env = Environment(loader=FileSystemLoader(Path(__file__).parents[1] / "templates"), autoescape=select_autoescape())
    env.globals.update(pendientes_admin=lambda:0, alertas_guias_reemplazadas=lambda:0)
    html = env.get_template("admin/cliente_tarifas.html").render(
        cliente={"cliente_id":"TEST", "perfil_comercial":"", "pricing_rangos_internacional":rangos()},
        request=SimpleNamespace(state=SimpleNamespace(csp_nonce="test-nonce")))
    assert 'data-ambito="nacional"' in html
    assert 'data-ambito="internacional"' in html
    assert '<script nonce="test-nonce">' in html
