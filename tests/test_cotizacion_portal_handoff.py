"""La copia enviada por mail debe continuar en el portal sin rehacer datos."""

from pathlib import Path
from decimal import Decimal
from html.parser import HTMLParser
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


def _preparar_wizard(monkeypatch, *, render_real=False):
    if not render_real:
        monkeypatch.setattr(pc.templates, "TemplateResponse", lambda **kw: kw)
    monkeypatch.setitem(pc.templates.env.globals, "saldo_menu", lambda *_a: None)
    monkeypatch.setitem(pc.templates.env.globals, "pendientes_menu", lambda *_a: {"envios": 0, "tienda": 0})
    monkeypatch.setitem(pc.templates.env.globals, "ayuda", lambda: {})
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


def test_cotizador_traslada_ciudad_y_cp_sin_mezclar_otro_domicilio(monkeypatch):
    _preparar_wizard(monkeypatch)
    monkeypatch.setattr(pc,'obtener_remitente_para_envio',lambda _:dict(pais='AR',ciudad='Buenos Aires',cp='1000',direccion='Calle 1'))
    cajas=json.dumps([dict(cantidad=1,peso_kg=2,largo_cm=20,ancho_cm=20,alto_cm=20)])
    contexto=pc.envio_nuevo_form(_request(),ambito='internacional',cajas=cajas,origen='AR',destino='US',
        origen_ciudad='Córdoba',origen_cp='5000',destino_ciudad='Miami',destino_cp='33101',cliente='DEMO')['context']
    assert contexto['form']['rem_ciudad']=='Córdoba'
    assert contexto['form']['rem_zip']=='5000'
    assert contexto['form']['dest_ciudad']=='Miami'
    assert contexto['form']['dest_zip']=='33101'
    assert contexto['remitente'] is None
    assert contexto['remitente_por_completar'] is True


class _Inputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.campos = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('name'):
            self.campos.setdefault(attrs['name'], []).append(attrs)


@pytest.mark.parametrize('valor', ['100', '100.50', '100,50'])
def test_continuar_tarifa_dhl_renderiza_valor_total_sin_inventar_unidades(monkeypatch, valor):
    _preparar_wizard(monkeypatch, render_real=True)
    cajas = json.dumps([dict(cantidad=1, peso_kg=2, largo_cm=30, ancho_cm=20, alto_cm=10)])
    respuesta = pc.envio_nuevo_form(
        _request(), ambito='internacional', cliente='DEMO', courier='dhl',
        origen='AR', destino='US', origen_ciudad='', origen_cp='',
        destino_ciudad='Miami', destino_cp='33101', cajas=cajas,
        valor_cotizado=valor,
    )

    assert respuesta.status_code == 200
    html = respuesta.body.decode('utf-8')
    assert 'Nuevo envío internacional' in html
    campos = _Inputs(html).campos
    esperado = Decimal(valor.replace(',', '.'))
    assert Decimal(campos['bulto_valor_caja_usd'][0]['value']) == esperado
    assert Decimal(campos['bulto_total_usd'][0]['value']) == esperado
    assert Decimal(campos['bulto_peso'][0]['value']) == Decimal('2')
    assert campos['bulto_cantidad'][0]['value'] == '1'
    # Cotizar cajas no declara cantidades ni descripciones de mercadería.
    for nombre in ('bulto_unidades_aduana', 'bulto_desc_en'):
        assert campos[nombre][0]['value'] == ''
        assert 'required' in campos[nombre][0]
    assert 'precio_cotizado_ars' not in respuesta.context['form']


def test_varias_cajas_renderizan_sin_repartir_el_total_de_cotizacion(monkeypatch):
    _preparar_wizard(monkeypatch, render_real=True)
    cajas = json.dumps([
        dict(cantidad=2, peso_kg=2, largo_cm=30, ancho_cm=20, alto_cm=10),
        dict(cantidad=1, peso_kg=1, largo_cm=20, ancho_cm=15, alto_cm=10),
    ])
    respuesta = pc.envio_nuevo_form(
        _request(), ambito='internacional', cliente='DEMO', courier='dhl',
        cajas=cajas, valor_cotizado='300',
    )
    assert respuesta.status_code == 200
    campos = _Inputs(respuesta.body.decode('utf-8')).campos
    assert [c['value'] for c in campos['bulto_cantidad']] == ['2', '1']
    for nombre in ('bulto_valor_caja_usd', 'bulto_total_usd', 'bulto_unidades_aduana'):
        assert [c['value'] for c in campos[nombre]] == ['', '']
    assert respuesta.context['form']['valor_total_cotizado_usd'] == 300


@pytest.mark.parametrize('valor', ['texto', '0', '-1', 'NaN'])
def test_valor_invalido_muestra_error_sin_romper_el_formulario(monkeypatch, valor):
    _preparar_wizard(monkeypatch, render_real=True)
    cajas = json.dumps([dict(cantidad=1, peso_kg=2, largo_cm=30, ancho_cm=20, alto_cm=10)])
    respuesta = pc.envio_nuevo_form(
        _request(), ambito='internacional', cliente='DEMO', courier='dhl',
        cajas=cajas, valor_cotizado=valor,
    )
    assert respuesta.status_code == 200
    assert respuesta.context['error']
    campos = _Inputs(respuesta.body.decode('utf-8')).campos
    assert campos['bulto_peso'][0]['value'] == '2.0'
    assert campos['bulto_total_usd'][0]['value'] == ''


def test_snapshot_web_sigue_renderizando_el_valor_declarado(monkeypatch):
    _preparar_wizard(monkeypatch, render_real=True)
    monkeypatch.setattr(leads, 'obtener_cotizacion', lambda *a, **k: _snapshot())
    respuesta = pc.envio_nuevo_form(
        _request(), ambito='internacional', cliente='DEMO',
        quote_id='Q-abcdefghijklmnopqrstuvwxyz123456',
    )
    assert respuesta.status_code == 200
    campos = _Inputs(respuesta.body.decode('utf-8')).campos
    assert Decimal(campos['bulto_total_usd'][0]['value']) == Decimal('100')
