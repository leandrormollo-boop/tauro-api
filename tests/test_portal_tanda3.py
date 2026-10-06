"""Cotización comercial, fila móvil y contactos de presentación del portal."""
import asyncio
import io
import json
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock

import pytest
from pypdf import PdfReader

from endpoints import portal_cliente as pc, portal_oca as po
from servicios import cotizaciones_portal as quotes, cotizaciones_reseller as reseller
from servicios.estados_envio import presentar_estados_envio
from servicios.presentacion import condiciones_cotizacion
from test_cotizacion_portal_handoff import _preparar_wizard, _request, _snapshot
from test_portal_home_panorama import _action_portal
from test_portal_tanda1 import abrir_local, browser  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def render_quote(op=None, *, ambito="internacional", tax="DESTINATARIO", reseller=False):
    op = op or dict(carrier_id="dhl", carrier_nombre="DHL", servicio="Express",
                    precio_final_ars=123456.78, dias_estimados="3", peso_usado_kg=8,
                    peso_real_kg=4, portal_quote_id="RQ-demo", reseller_quote_id="RQ-demo")
    return pc.templates.TemplateResponse(request=_request(), name="portal/_quote_results.html",
        context=dict(opciones=[op], ambito=ambito, tax_paga_cotizacion=tax,
                     es_reseller=reseller, form=dict(origen_pais="AR", destino_pais="US",
                     origen_ciudad="Buenos Aires", destino_ciudad_internacional="Miami",
                     valor_declarado_usd="50"), cajas_cotizadas="[]")).body.decode()


@pytest.mark.parametrize("tax,texto", [("CLIENTE", "Los pagás vos; se suman a tu cuenta"),
                                      ("DESTINATARIO", "Los paga quien recibe"),
                                      (None, "Se confirma al emitir")])
def test_7a_condiciones_reales_de_cada_opcion_y_configuracion(tax, texto):
    html = render_quote(tax=tax)
    for esperado in ("Moneda", "ARS", "8,00 kg", "Cobro por volumen", "Sí",
                     "3 días hábiles", texto):
        assert esperado in html
    assert "Puerta a puerta" not in html
    assert "costo_courier" not in html and "markup" not in html


def test_7a_no_usa_peso_de_otra_opcion_ni_inventa_peso_nacional():
    op = dict(carrier_id="oca", carrier_nombre="OCA", servicio="OCA Express",
              precio_final_ars=500, dias_estimados=None, continuar_url="/portal/oca/nuevo")
    datos = dict(condiciones_cotizacion(op, nacional=True))
    assert datos["Peso facturable usado"] == datos["Cobro por volumen"] == "Se confirma al emitir"
    assert datos["Plazo estimado"] == "Se confirma al emitir"
    assert "Incluye" not in datos
    assert "Impuestos y aranceles en destino" not in datos
    assert dict(condiciones_cotizacion(dict(peso_usado_kg=1.25, peso_real_kg=1.25)))["Cobro por volumen"] == "No"
    assert "1,25 kg" in dict(condiciones_cotizacion(dict(peso_usado_kg=1.25))).values()
    assert "Se confirma al emitir" in render_quote(op, ambito="nacional")


@pytest.mark.parametrize("actual,extra", [(100, None), (100.004, None),
                                         (125.55, "+ $ 25,55"), (90.25, "− $ 9,75")])
def test_7b_compara_con_centavos_sin_aviso_si_no_cambio(actual, extra):
    resultado = quotes.comparar({"precio_ars": "100.00"}, actual)
    if extra is None:
        assert resultado is None
    else:
        assert extra in resultado["diferencia"]
        assert resultado["texto"].startswith("Cotizaste $ 100,00 · con los datos completos: $ ")
        assert resultado["motivo"] == "Motivo: Se confirma al emitir"
        assert quotes.comparar({"precio_ars": 100}, actual, "Recargo registrado")['motivo'] == "Recargo registrado"


def test_7b_wizard_conserva_id_original_web_y_portal(monkeypatch):
    _preparar_wizard(monkeypatch, render_real=True)
    monkeypatch.setattr("servicios.leads.obtener_cotizacion", lambda *a, **k: _snapshot())
    web = pc.envio_nuevo_form(_request(), ambito="internacional", cliente="DEMO", quote_id=_snapshot()["quote_id"])
    assert web.context["form"]["cotizacion_origen_id"] == _snapshot()["quote_id"]
    monkeypatch.setattr(quotes, "obtener", lambda *a: {"precio_base_ars": "100"})
    portal = pc.envio_nuevo_form(_request(), ambito="internacional", cliente="DEMO",
        cotizacion_origen_id="RQ-demo", cajas='[{"cantidad":1,"peso_kg":2,"largo_cm":20,"ancho_cm":20,"alto_cm":20}]')
    assert portal.context["form"]["cotizacion_origen_id"] == "RQ-demo"
    assert 'name="cotizacion_origen_id" id="cotizacion_origen_id" value="RQ-demo"' in portal.body.decode()
    # No toma un importe arbitrario del query string como referencia.
    assert "precio_cotizado_ars" not in portal.context["form"]
    monkeypatch.setattr(quotes, "obtener", lambda *a: None)
    ajena = pc.envio_nuevo_form(_request(), ambito="internacional", cliente="DEMO", cotizacion_origen_id="RQ-ajena")
    assert "cotizacion_origen_id" not in ajena.context["form"]
    assert "no pertenece" in ajena.context["error"]


def test_7b_api_compara_snapshot_y_sigue_sin_filtrar_costos(monkeypatch):
    monkeypatch.setattr(quotes, "obtener", lambda cliente, qid: {"precio_base_ars": 100} if (cliente, qid) == ("DEMO", "RQ-demo") else None)
    monkeypatch.setattr(pc, "cotizar_couriers_cliente", lambda *a, **k: dict(encontrado=True,
        opciones=[dict(id="dhl", nombre="DHL", precio_ars=125, precio_usd=1,
                       costo_courier=999, markup_pct=20, _base_interna={"secreto": True})]))
    def consultar(qid):
        req = _request()
        req._body = json.dumps(dict(cotizacion_origen_id=qid, precio_original=1,
            destino="US", origen_pais="AR", bultos=[dict(peso_kg=2, cantidad=1)])).encode()
        return json.loads(asyncio.run(pc.api_precio_envio_multi(req, cliente="DEMO")).body)
    data = consultar("RQ-demo")
    assert data["opciones"][0]["comparacion_cotizacion"]["diferencia"] == "Diferencia: + $ 25,00"
    assert all(secret not in json.dumps(data) for secret in ("costo_courier", "markup", "secreto", "_base_interna"))
    assert consultar("RQ-ajena")["opciones"][0]["comparacion_cotizacion"] is None


def test_7b_oca_ultimo_paso_tambien_compara_y_no_recibe_costo(monkeypatch):
    _action_portal(monkeypatch, [])
    monkeypatch.setattr(po.oca, "obtener", lambda *a: dict(id="demo", precio_ars=125,
        costo_ars=999, expires_at=datetime.now(timezone.utc), payload=dict(_cotizacion_origen_id="RQ-demo",
        origin={}, destination={}, packages=[dict(quantity=1, weight_kg=2, length_cm=20, width_cm=20, height_cm=20)], declared_value=50)))
    monkeypatch.setattr(quotes, "obtener", lambda *a: {"precio_base_ars": 100})
    respuesta = po.cotizacion(_request(), "demo", cliente="DEMO")
    assert "Cotizaste $ 100,00 · con los datos completos: $ 125,00" in respuesta.body.decode()
    assert "costo_ars" not in respuesta.context["tarifa"]


class Inputs(HTMLParser):
    def __init__(self, html):
        super().__init__(); self.forms = []; self.actual = None; self.feed(html)
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.actual = {"action": attrs.get("action"), "inputs": []}; self.forms.append(self.actual)
        if tag == "input" and self.actual is not None:
            self.actual["inputs"].append(attrs.get("name"))
    def handle_endtag(self, tag):
        if tag == "form": self.actual = None


def test_7c_cliente_comun_descarga_precio_fijo_reseller_conserva_permiso():
    forms = Inputs(render_quote()).forms
    assert [f['inputs'] for f in forms if f['action'] == '/portal/cotizaciones/cliente.pdf'] == [["quote_id"]]
    assert not any(f['action'] == '/portal/cotizaciones/reseller.pdf' for f in forms)
    forms = Inputs(render_quote(reseller=True)).forms
    assert [f['inputs'] for f in forms if f['action'] == '/portal/cotizaciones/reseller.pdf'] == [["quote_id", "precio"]]
    assert "cotizacion_origen_id=RQ-demo" in render_quote()


def test_7c_pdf_comun_solo_precio_guardado(monkeypatch):
    row = dict(quote_id="RQ-demo", ruta="AR → US", courier="DHL", servicio="Express",
        precio_base_ars=Decimal("123456.78"), peso_facturable_kg=None, tiempo_estimado="A confirmar",
        bultos=[dict(cantidad=1, peso_kg=2, largo_cm=20, ancho_cm=20, alto_cm=20)],
        costo_courier=666666, margen_tauro=555555)
    monkeypatch.setattr(quotes, "obtener", lambda *a: row)
    response = pc.descargar_cotizacion_cliente("RQ-demo", "DEMO")
    texto = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(response.body)).pages)
    assert "$ 123.456,78 ARS" in texto and "Se confirma al emitir" in texto
    assert all(secret not in texto.lower() for secret in ("costo", "margen", "666666", "555555"))
    assert response.headers["cache-control"] == "private, no-store"
    monkeypatch.setattr(quotes, "obtener", lambda *a: None)
    assert pc.descargar_cotizacion_cliente("RQ-ajena", "DEMO").status_code == 422


def test_7c_consulta_exige_dueno_activo_y_vigencia(monkeypatch):
    cursor = Mock(); cursor.fetchone.return_value = None
    cursor.__enter__ = Mock(return_value=cursor); cursor.__exit__ = Mock(return_value=False)
    conn = Mock(); conn.cursor.return_value = cursor
    conn.__enter__ = Mock(return_value=conn); conn.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(quotes, "get_conn", lambda: conn)
    assert quotes.obtener("demo", "RQ-ajena") is None
    sql, params = cursor.execute.call_args.args
    assert params == ("RQ-ajena", "DEMO")
    for restriccion in ("q.cliente_id=%s", "q.vigente_hasta >= NOW()", "c.activo=TRUE"):
        assert restriccion in sql
    assert "c.es_reseller" not in sql


def test_7c_persistencia_elige_campos_sin_costos_y_admite_peso_desconocido(monkeypatch):
    cursor = Mock(); cursor.__enter__ = Mock(return_value=cursor); cursor.__exit__ = Mock(return_value=False)
    conn = Mock(); conn.cursor.return_value = cursor
    conn.__enter__ = Mock(return_value=conn); conn.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(reseller, "get_conn", lambda: conn)
    op = dict(precio_final_ars=125, carrier_nombre="OCA", servicio="Express", costo_courier=777, margen_tauro=999)
    salida = quotes.guardar_opciones("DEMO", ruta="AR → AR", bultos=[], peso_facturable_kg=None, opciones=[op])
    assert salida[0]["portal_quote_id"].startswith("RQ-")
    sql, params = cursor.execute.call_args.args
    assert "costo" not in sql and "margen" not in sql
    assert params[1] == "DEMO" and params[4] is None and params[6] == 125
    assert 777 not in params and 999 not in params
    schema = (ROOT/'sql/schema.sql').read_text()
    assert 'ALTER TABLE cotizaciones_reseller ALTER COLUMN peso_facturable_kg DROP NOT NULL' in schema


def test_7c_falla_snapshot_no_rompe_tarifa_ni_habilita_descarga(monkeypatch):
    monkeypatch.setattr(quotes, "_guardar_opciones", Mock(side_effect=RuntimeError("db")))
    op = dict(carrier_id="dhl", carrier_nombre="DHL", servicio="Express", precio_final_ars=125)
    salida = quotes.guardar_opciones("DEMO", ruta="AR → US", bultos=[], peso_facturable_kg=2, opciones=[op])
    assert salida == [op]
    assert '/portal/cotizaciones/cliente.pdf' not in render_quote(salida[0])


def envios_html(monkeypatch):
    envios = [presentar_estados_envio(dict(id=n, estado=estado, tracking_estado=tracking_estado,
        courier="DHL", remitente_pais="CN", destino_pais="AR", remitente_ciudad="Shanghai",
        dest_ciudad="Buenos Aires", dest_nombre="Destinatario con un nombre muy largo para probar elipsis " * 2,
        tracking="1234567890123456789012345678901234567890" if n != 3 else "",
        tiene_label=n == 1, numero_guia_tauro="TAURO-2026-0123456789",
        precio_tauro_ars=123456.78, precio_inicial_cliente_ars=100000,
        precio_final_cliente_ars=123456.78, diferencia_flete_ars=23456.78,
        bultos=[], cantidad=1, resumen_pesos={}, created_at=datetime(2026,10,6)))
        for n, (estado, tracking_estado) in enumerate([("GUIA_LISTA", None), ("DESPACHADO", "RETENIDO"), ("SOLICITADO", None)], 1)]
    portal, req = _action_portal(monkeypatch, envios)
    return portal.envios_view(req("/portal/envios"), cliente="DEMO").body.decode()


def test_8_todos_los_datos_y_acciones_se_conservan_en_mas_datos(monkeypatch):
    html = envios_html(monkeypatch)
    assert html.count('<details class="shipment-more">') == 3
    assert html.count('<summary>Más datos</summary>') == 3
    assert "Fecha: 06/10/2026" in html and "Recorrido: Shanghai" in html
    assert "TAURO TAURO-2026-0123456789" in html
    assert "Ver envío</a>" in html and "Ver guía</a>" in html
    assert "Ver desglose" in html and "Opciones" in html


def test_8_fila_390_no_supera_180_px_y_despliega_datos(browser, monkeypatch):
    page = browser.new_page(viewport={"width":390,"height":800})
    try:
        abrir_local(page, envios_html(monkeypatch))
        filas = page.locator('.envios-table .shipment-row')
        assert filas.count() == 3
        for fila in filas.all():
            assert fila.bounding_box()['height'] <= 180
            assert fila.locator('.shipment-recipient').evaluate("el => getComputedStyle(el).textOverflow") == 'ellipsis'
            assert fila.locator('.envio-status-cell .badge').is_visible()
            assert fila.locator('.shipment-actions-cell > .btn').is_visible()
            assert not fila.locator('.shipment-date-cell').is_visible()
            assert not fila.locator('.shipment-desktop-options').is_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        fila = filas.first
        fila.locator('.shipment-more > summary').click()
        assert fila.locator('.shipment-more-content').is_visible()
        assert "06/10/2026" in fila.locator('.shipment-more-content').inner_text()
        fila.locator('.shipment-more-content .shipment-actions > summary').click()
        assert fila.locator('.shipment-more-content .shipment-action-primary').is_visible()
        fila.locator('.shipment-more > summary').click()
        assert fila.bounding_box()['height'] <= 180
    finally:
        page.close()


def test_7b_presentacion_js_conserva_referencia_y_actualiza_diferencia():
    result = subprocess.run(['node','--test','tests/js/quote-comparison.test.cjs'],cwd=ROOT,capture_output=True,text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_7a_post_usa_tax_de_la_cuenta_y_guarda_precio_por_opcion(monkeypatch):
    import inspect
    from fastapi.params import Form
    _action_portal(monkeypatch, [])
    monkeypatch.setattr('servicios.rate_limit.check_rate', lambda *a, **k: True)
    monkeypatch.setattr(reseller, 'cliente_es_reseller', lambda *a: False)
    monkeypatch.setattr(pc, 'tax_paga_cliente', lambda *a: 'CLIENTE')
    monkeypatch.setattr(pc, 'obtener_rutas_frecuentes', lambda *a: [])
    monkeypatch.setattr(pc, '_paises_con_nacional', lambda: [])
    monkeypatch.setattr(pc, '_operadores_cliente', lambda *a: [])
    op = dict(carrier_id='dhl',carrier_nombre='DHL',servicio='Express',precio_final_ars=150,
              peso_usado_kg=8,peso_real_kg=4,dias_estimados=3)
    monkeypatch.setattr(pc, 'cotizar_referencia_couriers', lambda **k: dict(encontrado=True,
        opciones=[op],no_disponibles=[],resumen=dict(ruta='AR → US',peso_usado_kg=8)))
    guardar = Mock(return_value=[{**op,'portal_quote_id':'RQ-demo'}])
    monkeypatch.setattr(quotes, 'guardar_opciones', guardar)
    kwargs = {k:v.default.default for k,v in inspect.signature(pc.cotizar_post).parameters.items() if isinstance(v.default,Form)}
    kwargs.update(origen_pais='AR',destino_pais='US',peso_kg='4',largo_cm='40',ancho_cm='40',alto_cm='25',valor_declarado_usd='50')
    response = pc.cotizar_post(_request(), cliente='DEMO', **kwargs)
    assert 'Los pagás vos; se suman a tu cuenta' in response.body.decode()
    assert 'cotizacion_origen_id=RQ-demo' in response.body.decode()
    assert guardar.call_args.kwargs['opciones'][0]['precio_final_ars'] == 150
    assert guardar.call_args.args == ('DEMO',)
