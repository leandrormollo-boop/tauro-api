"""Navegación, sesión, formatos compartidos y recorrido móvil de la ronda 4."""
import re
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException

from endpoints import admin, portal_cliente as pc, portal_oca_qa
from servicios import presentacion as fmt
from servicios.estados_envio import presentar_estados_envio
from test_portal_home_panorama import _action_portal
from test_portal_tanda1 import abrir_local, browser, wizard, request  # noqa: F401
from test_portal_tanda3 import envios_html

ROOT = Path(__file__).resolve().parents[1]
# Estos tres templates los actualiza Cerebro en paralelo.
CUENTA_EN_PARALELO = {"cuenta.html", "cuenta_movimientos.html", "pago_imputar.html"}


@pytest.mark.parametrize("token", [None, "invalido", "vigente"])
def test_parser_exige_sesion_de_cliente_aunque_haya_admin(monkeypatch, token):
    route = next(r for r in pc.router.routes if r.path == "/portal/api/parsear-pedido")
    deps = route.dependant.dependencies
    assert [d.call for d in deps] == [pc.cliente_actual]
    assert not hasattr(pc, "_es_admin_request")
    monkeypatch.setattr(pc, "validar_token", lambda t: "DEMO" if t == "vigente" else None)
    if token == "vigente":
        assert pc.cliente_actual(token) == "DEMO"
    else:
        with pytest.raises(HTTPException) as exc:
            pc.cliente_actual(token)
        assert exc.value.status_code == 303 and exc.value.headers["Location"] == "/portal/login"


def test_inicio_sin_botonera_superior_y_con_menu_movil(monkeypatch):
    portal, req = _action_portal(monkeypatch, [])
    html = portal.home(req(), cliente="DEMO").body.decode()
    header = html.split('<header class="home-workspace-head">')[1].split('</header>')[0]
    assert '<nav' not in header and 'home-primary-actions' not in html
    assert 'class="burger"' in html and 'class="tabbar"' in html


@pytest.mark.parametrize("func,valor,esperado", [
    (fmt.dinero_ars, Decimal('1234.56'), '$ 1.234,56'),
    (fmt.dinero_usd, Decimal('1234.56'), 'USD 1.234,56'),
    (fmt.dinero_ars, Decimal('-0.57'), '$ -0,57'),
    (fmt.numero_ars, Decimal('1000.01'), '1.000,01'),
    (fmt.kg, Decimal('3.8'), '3,80 kg'), (fmt.kg, '3,8', '3,80 kg'), (fmt.kg, None, '—'),
    (fmt.medida_cm, Decimal('45.0'), '45'), (fmt.medida_cm, Decimal('37.25'), '37,25'),
    (fmt.numero_maquina, Decimal('1234.5678'), '1234.5678'),
    (fmt.numero_maquina, Decimal('45.0'), '45'),
])
def test_formatos_y_precision_de_valores_para_calculos(func, valor, esperado):
    assert func(valor) == esperado


@pytest.mark.parametrize("env", [pc.templates.env, admin.templates.env, portal_oca_qa.templates.env])
def test_filtros_globales_y_templates_compilan(env):
    assert env.from_string('{{ precio|dinero_ars }} · {{ precio|dinero_usd }} · {{ peso|kg }} · '
        '{{ largo|medida_cm }} × {{ ancho|medida_cm }} × {{ alto|medida_cm }} cm').render(
        precio=Decimal('1234.56'), peso=3.8, largo=45.0, ancho=37.0, alto=24.0
        ) == '$ 1.234,56 · USD 1.234,56 · 3,80 kg · 45 × 37 × 24 cm'
    for folder in ('portal', 'admin'):
        for path in (ROOT/'templates'/folder).glob('*.html'):
            env.get_template(str(path.relative_to(ROOT/'templates')))


def test_pantallas_no_reintroducen_formatos_locales():
    rutas = [ROOT/'templates/base.html', * (ROOT/'templates/portal').glob('*.html'),
             * (ROOT/'templates/admin').glob('*.html')]
    prohibidos = re.compile(r'\{:,\.0f\}|[\'"]%\.2f[\'"]\s*\|\s*format')
    errores = [str(p.relative_to(ROOT)) for p in rutas if prohibidos.search(p.read_text())]
    # La limpieza adicional de los formatos de dos decimales excluye sólo
    # los archivos que Cerebro está trabajando; la regresión pedida los cubre.
    errores += [str(p.relative_to(ROOT)) for p in rutas if p.name not in CUENTA_EN_PARALELO
                and '{:,.2f}' in p.read_text()]
    assert errores == []


def test_precios_y_pesos_admin_no_pierden_centavos(monkeypatch):
    monkeypatch.setitem(admin.templates.env.globals, 'admin_user', lambda *_: None)
    p = dict(id=1, cliente_id='DEMO', alias_interno='Artículo', nombre_invoice='Article', hs_code='1234',
        peso_kg=Decimal('3.8'), valor_usd_default=Decimal('1234.56'), largo_cm=45, ancho_cm=37,
        alto_cm=24, activo=True)
    html = admin.templates.TemplateResponse(request=request(), name='admin/productos.html',
        context=dict(pendientes=[p], todos=[p])).body.decode()
    assert 'USD 1.234,56' in html and '3,80 kg' in html and '45 × 37 × 24 cm' in html
    assert '$ 1.234,56' not in html
    macros = admin.templates.env.get_template('admin/operador_macros.html').module
    assert str(macros.dinero(Decimal('1234.56'))) == '1.234,56'
    assert fmt.simbolo_moneda('ARS') == '$' and fmt.simbolo_moneda('USD') == 'USD'


def _chequear_celular(page, pantalla):
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), pantalla
    assert page.locator('.mobile-bar .burger').is_visible()
    assert page.locator('.tabbar').is_visible()
    chicos = page.locator('button:visible, .btn:visible, [role="button"]:visible, summary:visible').evaluate_all('''els =>
      els.filter(el => {const r = el.getBoundingClientRect(); return r.width < 43.5 || r.height < 43.5;})
         .map(el => ({texto:el.textContent.trim(), clase:el.className, ancho:el.getBoundingClientRect().width,
                     alto:el.getBoundingClientRect().height}))''')
    assert chicos == [], (pantalla, chicos)


def _pantallas_celular(wizard, monkeypatch):
    from test_cotizador_unificado import render_quote
    envio = presentar_estados_envio(dict(id=1, estado='GUIA_LISTA', courier='DHL', remitente_pais='CN',
        destino_pais='AR', dest_nombre='Destinatario con nombre largo para revisar en el celular',
        remitente_ciudad='Shanghai', dest_ciudad='Buenos Aires', tracking='1234567890123456789012345678901234567890',
        bultos=[dict(cantidad=1,peso_kg=3.8,largo_cm=45,ancho_cm=37,alto_cm=24)],
        cantidad=1, precio_tauro_ars=123456.78, valor_declarado_usd=1234.56, resumen_pesos={}))
    portal, req = _action_portal(monkeypatch, [envio])
    monkeypatch.setattr(portal, 'obtener_solicitud_de_cliente', lambda *_: dict(envio))
    monkeypatch.setattr(portal, '_cliente_puede_emitir_courier', lambda *_: False)
    monkeypatch.setattr(portal, 'validar_cancelacion_cliente', lambda *_: {'ok':False})
    monkeypatch.setattr(portal, 'validar_reemision_cliente', lambda *_: {'ok':False})
    pantallas = [('/portal/home', portal.home(req(), cliente='DEMO').body.decode()),
                ('/portal/envios', envios_html(monkeypatch)),
                ('/portal/envios/1', portal.envio_detalle(req('/portal/envios/1'), 1, cliente='DEMO').body.decode()),
                ('/portal/envios/nuevo', portal.envio_nuevo_form(req('/portal/envios/nuevo'), cliente='DEMO').body.decode()),
                ('/portal/envios/nuevo?ambito=internacional', wizard()),
                ('/portal/cotizar', render_quote(opciones=[dict(carrier_id='dhl',carrier_nombre='DHL',servicio='Express con un nombre largo para probar el celular',precio_final_ars=123456.78,dias_estimados='3',peso_usado_kg=3.8)]))]
    return pantallas


def test_fixture_movil_renderiza_las_cinco_pantallas_y_ambitos(wizard, monkeypatch):
    pantallas = _pantallas_celular(wizard, monkeypatch)
    assert len(pantallas) == 6  # Nuevo envío incluye selector y wizard.
    for ruta, html in pantallas:
        assert '<!DOCTYPE html>' in html and 'class="burger"' in html, ruta
    assert 'Envío nacional' in pantallas[3][1] and 'Envío internacional' in pantallas[3][1]
    assert '3,80 kg' in pantallas[2][1]
    assert '$ 123.456,78' in pantallas[5][1]


def test_recorrido_cinco_pantallas_a_390_sin_desborde_y_botones_44(browser, wizard, monkeypatch):
    pantallas = _pantallas_celular(wizard, monkeypatch)
    page = browser.new_page(viewport={'width':390,'height':800})
    try:
        for ruta, html in pantallas:
            page.unroute('**/*')
            abrir_local(page, html, ruta)
            _chequear_celular(page, ruta)
            if ruta == '/portal/envios':
                for i in range(page.locator('.shipment-more').count()):
                    page.locator('.shipment-more > summary').nth(i).click()
                _chequear_celular(page, ruta + ' / Más datos')
            elif 'ambito=internacional' in ruta:
                page.locator('.shipment-paste > summary').click()
                _chequear_celular(page, ruta + ' / Pegar pedido')
                for i in range(1, 4):
                    page.locator('#shipment-wizard').evaluate("sec => sec.querySelectorAll('input, select, textarea').forEach(el => {el.required=false;el.setCustomValidity('');})")
                    page.locator('#stepper-envio .step').nth(i).click()
                    page.wait_for_function(f"document.querySelector('#shipment-wizard').dataset.step === '{i+1}'")
                    _chequear_celular(page, ruta + f' / paso {i+1}')
            elif ruta == '/portal/cotizar':
                # La navegación visual no depende de las consultas de tarifas.
                for paso in (2, 3):
                    page.locator('.uq-panel:visible').evaluate("el => el.querySelectorAll('[required]').forEach(input => {input.required=false;input.setCustomValidity('');})")
                    page.locator('.uq-panel:visible [data-quote-step]').nth(paso-1).click()
                    page.wait_for_function(f"document.querySelector('.uq-panel:not([hidden])').dataset.mobileStep === '{paso}'")
                    _chequear_celular(page, ruta + f' / paso {paso}')
    finally:
        page.close()


def test_javascript_formatea_importes_pesos_y_medidas_sin_modificar_datos():
    result = subprocess.run(['node', '--test', 'tests/js/presentacion-portal.test.cjs'],
        cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
