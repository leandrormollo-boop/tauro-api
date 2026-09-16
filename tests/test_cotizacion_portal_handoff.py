"""La copia enviada por mail debe continuar en el portal sin rehacer datos."""

from pathlib import Path
import json
import pytest

import endpoints.portal_cliente as pc
from servicios import leads


ROOT = Path(__file__).resolve().parents[1]


def _request():
    from starlette.requests import Request

    request = Request({
        "type": "http", "method": "GET", "path": "/portal/envios/nuevo",
        "raw_path": b"/portal/envios/nuevo", "query_string": b"",
        "headers": [], "scheme": "https", "server": ("testserver", 443),
        "client": ("127.0.0.1", 1234), "root_path": "",
    })
    request.state.csp_nonce = "test"
    return request


def _snapshot():
    return {
        "quote_id": "Q-abcdefghijklmnopqrstuvwxyz123456",
        "referencia": "TW-20260818-ABC123",
        "origen": "CN", "destino": "US",
        "peso_kg": "5.500", "largo_cm": "30.00", "ancho_cm": "20.00",
        "alto_cm": "10.00", "valor_declarado_usd": "100.00",
        "opciones": [{
            "id": "dhl", "recomendada": True, "precio_ars": "95000.00",
        }],
    }


def _preparar_wizard(monkeypatch):
    monkeypatch.setattr(pc.templates, "TemplateResponse", lambda **kw: kw)
    monkeypatch.setattr(pc, "get_productos", lambda _cliente: [])
    monkeypatch.setattr(pc, "_paises_con_nacional", lambda: [("CN", "China"), ("US", "Estados Unidos")])
    monkeypatch.setattr(pc, "obtener_remitente_para_envio", lambda _cliente: None)
    monkeypatch.setattr(pc, "listar_direcciones", lambda *_a: [])
    monkeypatch.setattr(pc, "tax_paga_cliente", lambda _cliente: "DESTINATARIO")
    monkeypatch.setattr(pc, "courier_default_cliente", lambda _cliente: "fedex")


def test_login_solo_redirige_a_snapshot_vigente_y_opaco(monkeypatch):
    qid = "Q-abcdefghijklmnopqrstuvwxyz123456"
    monkeypatch.setattr(leads, "obtener_cotizacion", lambda *a, **k: _snapshot())

    destino = pc._destino_post_login(qid)

    assert destino == f"/portal/envios/nuevo?ambito=internacional&quote_id={qid}"
    assert pc._destino_post_login("https://evil.example") == "/portal/home"


def test_wizard_precarga_snapshot_y_revalida_sin_confiar_en_query(monkeypatch):
    _preparar_wizard(monkeypatch)
    monkeypatch.setattr(leads, "obtener_cotizacion", lambda *a, **k: _snapshot())

    respuesta = pc.envio_nuevo_form(
        _request(), ambito="internacional",
        quote_id="Q-abcdefghijklmnopqrstuvwxyz123456", cliente="MELCIOR",
    )
    contexto = respuesta["context"]
    form = contexto["form"]

    assert contexto["cotizacion_web"]["referencia"] == "TW-20260818-ABC123"
    assert form["rem_pais"] == "CN"
    assert form["destino_pais"] == "US"
    assert form["intl_courier"] == "dhl"
    assert form["precio_cotizado_ars"] == "95000.00"
    assert form["bultos"][0]["peso_kg"] == "5.500"
    assert form["bultos"][0]["valor_unitario_usd"] == "100.00"
    assert "Cotización web TW-20260818-ABC123" == form["observaciones"]


def test_ctas_conservan_quote_id_sin_mandar_precio():
    publica = (ROOT / "templates/public/cotizacion.html").read_text(encoding="utf-8")
    widget = (ROOT / "web/components/02-quote-widget.jsx").read_text(encoding="utf-8")

    assert "/portal/login?quote_id=" in publica
    assert "encodeURIComponent(result.quote_id)" in widget
    assert "precio_ars" not in publica.split("/portal/login?quote_id=", 1)[1].split('"', 1)[0]


@pytest.mark.parametrize('origen,conservar', [('AR', True), ('CN', False)])
def test_ruta_no_mezcla_pais_cotizado_con_domicilio_habitual(monkeypatch, origen, conservar):
    _preparar_wizard(monkeypatch)
    domicilio = {'pais': 'AR', 'nombre': 'Origen demo', 'direccion': 'Calle demo 1', 'ciudad': 'Buenos Aires', 'cp': '1000'}
    monkeypatch.setattr(pc, 'obtener_remitente_para_envio', lambda _cliente: domicilio)
    contexto = pc.envio_nuevo_form(_request(), ambito='internacional', origen=origen, destino='US', cliente='DEMO')['context']
    assert contexto['form']['rem_pais'] == origen
    assert contexto['remitente'] == (domicilio if conservar else None)
    assert contexto['remitente_por_completar'] is (not conservar)


def test_cajas_cotizadas_se_precargan_sin_transferir_precio_o_datos_aduaneros(monkeypatch):
    _preparar_wizard(monkeypatch)
    filas = [dict(cantidad=2, peso_kg=2.5, largo_cm=30, ancho_cm=20, alto_cm=10),
             dict(cantidad=1, peso_kg=1, largo_cm=15, ancho_cm=12, alto_cm=10)]
    payload = [dict(f, precio_cotizado_ars=1, descripcion_en='No debe copiarse') for f in filas]
    contexto = pc.envio_nuevo_form(_request(), ambito='internacional', cajas=json.dumps(payload), origen='CN', destino='US', courier='dhl', cliente='DEMO')['context']
    assert contexto['form']['bultos'] == filas
    assert 'precio_cotizado_ars' not in contexto['form']
    assert contexto['form']['intl_courier'] == 'dhl'


@pytest.mark.parametrize('payload', ['no json', '{}', '[]', '[null]', '[{"cantidad":21}]', '[{"cantidad":1,"peso_kg":"NaN","largo_cm":20,"ancho_cm":20,"alto_cm":20}]'])
def test_cajas_manipuladas_no_rompen_el_wizard(monkeypatch, payload):
    _preparar_wizard(monkeypatch)
    contexto = pc.envio_nuevo_form(_request(), ambito='internacional', cajas=payload, cliente='DEMO')['context']
    assert 'Volvé a cotizar' in contexto['error']
    assert 'bultos' not in contexto['form']


@pytest.mark.parametrize('cantidad', [1, 2])
def test_valor_de_cotizacion_se_conserva_sin_inventar_reparto_aduanero(monkeypatch, cantidad):
    _preparar_wizard(monkeypatch)
    payload = json.dumps([dict(cantidad=cantidad, peso_kg=2, largo_cm=20, ancho_cm=20, alto_cm=20)])
    form = pc.envio_nuevo_form(_request(), ambito='internacional', cajas=payload, valor_cotizado='100,50', cliente='DEMO')['context']['form']
    assert form['valor_total_cotizado_usd'] == 100.50
    if cantidad == 1:
        assert form['bultos'][0]['valor_declarado_caja_usd'] == 100.50
    else:
        assert 'valor_declarado_caja_usd' not in form['bultos'][0]
