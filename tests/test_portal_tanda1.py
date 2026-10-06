"""Estados, acceso de operadores, formatos y espacio de trabajo del portal."""
import asyncio
import json
import subprocess
import socket
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock

import pytest
from starlette.requests import Request

from endpoints import portal_cliente as pc
from servicios.estados_envio import presentar_estados_envio
from servicios.panel_cliente import resumen_inicio_cliente
from servicios.presentacion import medida_cm
from test_portal_home_panorama import _action_portal

ROOT = Path(__file__).resolve().parents[1]


def request(query="", token=""):
    return Request(dict(type="http", method="GET", path="/portal/envios/nuevo",
                        query_string=query.encode(), server=("testserver", 80),
                        headers=[(b"cookie", f"admin_token={token}".encode())] if token else [],
                        state={"csp_nonce": "test"}))


@pytest.fixture
def wizard(monkeypatch):
    _action_portal(monkeypatch, [])
    monkeypatch.setattr(pc, "get_productos", lambda _: [])
    monkeypatch.setattr(pc, "listar_direcciones", lambda _: [])
    monkeypatch.setattr(pc, "obtener_remitente_para_envio", lambda _: None)
    monkeypatch.setattr(pc, "_paises_con_nacional", lambda: [("AR", "Argentina"), ("US", "Estados Unidos")])
    monkeypatch.setattr(pc, "tax_paga_cliente", lambda _: "DESTINATARIO")
    monkeypatch.setattr(pc, "courier_default_cliente", lambda _: "")
    monkeypatch.setitem(pc.templates.env.globals, "invoice_asistente_habilitado", lambda: False)
    return lambda token="": pc.envio_nuevo_form(request(token=token), ambito="internacional", cliente="DEMO").body.decode()


@pytest.mark.parametrize("courier,rutas,automatico", [
    ("dhl", [], True), ("fedex", [("AR", "AR")], False), ("dhl", [("CN", "MX")], True),
    ("", [("CN", "AR")], True), ("", [("AR", "US")], True),
    ("", [("AR", "AR"), ("CN", "AR")], False),
    ("", [("AR", "AR")], False), ("", [], False),
    ("", [("", "")], False),
])
def test_entrada_internacional_conserva_parametros(wizard, monkeypatch, courier, rutas, automatico):
    monkeypatch.setattr(pc, "courier_default_cliente", lambda _: courier)
    monkeypatch.setattr(pc, "listar_solicitudes_cliente", lambda *_, **__: [
        dict(remitente_pais=o, destino_pais=d) for o, d in rutas])
    response = pc.envio_nuevo_form(request("remitente_id=7&destinatario_id=8&ventana=1"), cliente="DEMO")
    assert response.status_code == (303 if automatico else 200)
    if automatico:
        assert parse_qs(urlsplit(response.headers["location"]).query) == {
            "remitente_id": ["7"], "destinatario_id": ["8"], "ventana": ["1"],
            "ambito": ["internacional"],
        }


def test_ambito_explicito_prevalece_y_hay_link_nacional(wizard, monkeypatch):
    monkeypatch.setattr(pc, "courier_default_cliente", lambda _: "dhl")
    response = pc.envio_nuevo_form(request("ambito=nacional"), ambito="nacional", cliente="DEMO")
    assert response.headers["location"] == "/portal/oca/nuevo"
    assert 'href="/portal/envios/nuevo?ambito=nacional">¿Es un envío nacional?</a>' in wizard()


@pytest.mark.parametrize("token,permitido", [("", False), ("invalido", False), ("vigente", True)])
def test_pegar_pedido_solo_admin_en_form_y_api(wizard, monkeypatch, token, permitido):
    from endpoints import admin
    from servicios import parser_pedidos
    auth = Mock(side_effect=lambda token: token == "vigente")
    monkeypatch.setattr(admin, "_is_auth", auth)
    parser = Mock(return_value={"campos": {"dest_nombre": "Prueba"}})
    monkeypatch.setattr(parser_pedidos, "parsear_pedido", parser)
    html = wizard(token)
    assert ('id="pedido-pegado"' in html) == permitido
    assert ('Pegar pedido recibido por mail (solo TAURO)' in html) == permitido
    if permitido:
        assert '<section class="card shipment-admin-paste"' in html
    api_request = request(token=token)
    api_request._body = json.dumps({"texto": "Pedido de prueba"}).encode()
    response = asyncio.run(pc.api_parsear_pedido(api_request))
    assert response.status_code == (200 if permitido else 403)
    assert parser.call_count == int(permitido)
    if token:
        assert auth.call_count == 2
    else:
        auth.assert_not_called()


class EstadoActual(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.estados = []
        self.clases = []
        self.actual = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "span":
            attrs = dict(attrs)
            self.actual = attrs.get("title") == "Estado actual del envío"
            if self.actual:
                self.clases.append(attrs["class"])

    def handle_data(self, data):
        if self.actual:
            self.estados.append(data)
            self.actual = False


@pytest.mark.parametrize("estado,tracking,label", [
    ("SOLICITADO", None, "Solicitado"), ("GUIA_LISTA", None, "Guía lista"),
    ("DESPACHADO", None, "En tránsito"), ("GUIA_LISTA", "PROCESO_ENTREGA", "En tránsito"),
    ("DESPACHADO", "RETENIDO", "Retenido"), ("DESPACHADO", "ENTREGADO", "Entregado"),
    ("CANCELADO", "ENTREGADO", "Cancelado"), ("REEMPLAZADO", "RETENIDO", "Reemplazado"),
])
def test_mismo_envio_mismo_estado_en_tres_handlers(monkeypatch, estado, tracking, label):
    envio = presentar_estados_envio(dict(id=1, estado=estado, tracking_estado=tracking,
        remitente_pais="CN", destino_pais="AR", courier="DHL", dest_nombre="Prueba",
        bultos=[], cantidad=1, precio_tauro_ars=0, resumen_pesos={}))
    portal, req = _action_portal(monkeypatch, [envio])
    monkeypatch.setattr(portal, "obtener_solicitud_de_cliente", lambda *_: dict(envio))
    monkeypatch.setattr(portal, "_cliente_puede_emitir_courier", lambda *_: False)
    monkeypatch.setattr(portal, "validar_cancelacion_cliente", lambda *_: {"ok": False})
    monkeypatch.setattr(portal, "validar_reemision_cliente", lambda *_: {"ok": False})
    paso = {"CANCELADO": "canceladas", "REEMPLAZADO": "modificados"}.get(estado, "")
    paginas = [portal.home(req(), cliente="DEMO"),
               portal.envios_view(req("/portal/envios"), cliente="DEMO", paso=paso),
               portal.envio_detalle(req("/portal/envios/1"), 1, cliente="DEMO")]
    for indice, pagina in enumerate(paginas):
        parsed = EstadoActual(pagina.body.decode())
        assert parsed.estados == [label]
        # En el listado las bajas conservan su estilo tachado propio.
        clase = ("shipment-cancelled-badge" if indice == 1 and estado in {"CANCELADO", "REEMPLAZADO"}
                 else envio["estado_cliente_ui"]["clase"])
        assert parsed.clases == ["badge " + clase]
    assert paginas[1].context["solicitudes"][0]["estado_cliente_ui"] == paginas[2].context["s"]["estado_cliente_ui"]


def test_destinos_cuenta_importacion_a_argentina():
    resumen = resumen_inicio_cliente([
        dict(estado="GUIA_LISTA", remitente_pais="CN", destino_pais="AR"),
        dict(estado="GUIA_LISTA", remitente_pais="AR", destino_pais="US"),
    ], [], hoy=date(2026, 10, 6))
    assert {d["codigo"]: d["cantidad"] for d in resumen["destinos_frecuentes"]} == {"AR": 1, "US": 1}


@pytest.mark.parametrize("valor,esperado", [(45.0, "45"), (45.25, "45,25"), (0, "0")])
def test_medidas_conservan_fracciones_sin_ceros_sobrantes(valor, esperado):
    assert medida_cm(valor) == esperado


def test_espacio_de_navegacion_y_foco_responsive():
    dock = (ROOT / "static/css/portal-dock.css").read_text()
    css = (ROOT / "static/css/tauro.css").read_text()
    assert "@media (min-width: 901px)" in dock
    assert ".portal-operacion .tabbar { display: none; }" in dock
    assert "padding-right" not in dock
    assert "padding-bottom: 96px" in css
    assert "scroll-margin-bottom: calc(96px + var(--tabbar-h, 62px)" in css
    result = subprocess.run(["node", "--test", "tests/js/shipment-focus.test.cjs"],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_grafico_un_color_y_textos_sin_decoracion_ni_truncado():
    css = (ROOT / "static/css/portal-operacion.css").read_text()
    barra = css[css.index(".portal-operacion .home-bar-fill {"):css.index(".portal-operacion .home-monthly-bar:hover .home-bar-fill")]
    assert barra.count("background: var(--accent-soft)") == 3
    assert "linear-gradient" not in barra
    assert "opacity" not in barra
    cotizador = (ROOT / "static/css/portal-cotizador.css").read_text()
    assert ".uq-route-tools .draft-status{white-space:normal;overflow-wrap:anywhere}" in cotizador
    assert ".uq-route-tools .draft-status{display:none}" not in cotizador
    assert ".uq-route-tools .draft-status{max-width:170px}" not in cotizador
    for path in ("home.html", "estadisticas.html"):
        html = (ROOT / "templates/portal" / path).read_text()
        assert "Destinos de tus envíos" in html
        assert "Incluye importaciones con destino Argentina." in html
    assert '>Escritorio</span>' not in (ROOT / "templates/portal/home.html").read_text()
    assert '>Administración</div>' not in (ROOT / "templates/portal/cuenta.html").read_text()


def test_detalle_y_saldo_conservan_centavos_y_coma_decimal(monkeypatch):
    envio = presentar_estados_envio(dict(id=1, estado="DESPACHADO", courier="DHL",
        remitente_pais="CN", destino_pais="AR", cantidad=1,
        valor_declarado_usd=26.0, resumen_pesos=dict(real_total_kg=3.8,
        volumetrico_total_kg=7.992, facturable_total_kg=8, divisor=5000),
        bultos=[dict(cantidad=1, largo_cm=45.0, ancho_cm=37.0, alto_cm=24.0,
                     peso_kg=3.8, valor_declarado_caja_usd=26.0)]))
    portal, req = _action_portal(monkeypatch, [envio])
    monkeypatch.setitem(portal.templates.env.globals, "saldo_menu", lambda *_: dict(pendiente_ars=3448638.70))
    html = portal.templates.TemplateResponse(request=req("/portal/envios/1"), name="portal/envio_detalle.html",
        context=dict(cliente="DEMO", s=envio, cancelar_bloqueo="Solo se cancelan guías que todavía no salieron.")).body.decode()
    for texto in ("$ 3.448.638,70", "45 × 37 × 24 cm", "USD 26,00 por caja", "USD 26,00",
                  "3,80 kg", "Peso por volumen (largo × ancho × alto ÷ 5000)",
                  "← Mis envíos internacionales", "No se puede cancelar. Solo se cancelan guías que todavía no salieron."):
        assert texto in html
    assert "USD 26.0" not in html


@pytest.fixture(scope="module")
def browser():
    # Chromium requiere SO_PASSCRED para su canal local de procesos.
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as canal:
            canal.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    except PermissionError:
        pytest.skip("El sandbox bloquea sockets de Chromium")
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip("Chromium no está instalado")
        # Algunos sandboxes no permiten sockets de Chromium. Detectarlo con
        # un proceso acotado antes del transporte persistente de Playwright.
        probe = subprocess.run([p.chromium.executable_path, "--headless", "--no-sandbox",
                                "--disable-dev-shm-usage", "--dump-dom", "about:blank"],
                               capture_output=True, timeout=15)
        if b"Operation not permitted" in probe.stderr:
            pytest.skip("El sandbox bloquea sockets de Chromium")
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"], timeout=10000)
        yield browser
        browser.close()


def abrir_local(page, html):
    """Recorrido con assets reales; sin red, base de datos ni couriers."""
    def responder(route):
        path = urlsplit(route.request.url).path
        if path.startswith("/static/"):
            archivo = ROOT / path.lstrip("/")
            if archivo.is_file():
                route.fulfill(path=str(archivo))
                return
        if route.request.resource_type == "document":
            route.fulfill(body=html, content_type="text/html")
        elif path.startswith("/portal/api/"):
            route.fulfill(body='{"opciones":[],"no_disponibles":[]}', content_type="application/json")
        else:
            route.abort()
    page.route("**/*", responder)
    page.goto("https://portal.test/portal/envios/nuevo?ambito=internacional")


@pytest.mark.parametrize("ancho", [1100, 390])
def test_dock_y_pais_enfocado_no_tapan_contenido(browser, wizard, ancho):
    page = browser.new_page(viewport={"width": ancho, "height": 700})
    try:
        abrir_local(page, wizard())
        assert page.locator(".tabbar").is_visible() == (ancho == 390)
        # Abrir Destinatario; la validación del remitente pertenece a otros tests.
        page.locator(".shipment-step-sender").evaluate(
            "sec => sec.querySelectorAll('input, select, textarea').forEach(el => {el.required = false; el.setCustomValidity('');})")
        page.locator("#stepper-envio .step").nth(1).click()
        page.wait_for_function("document.querySelector('#shipment-wizard').dataset.step === '2'")
        campo = page.locator("#destino_pais").evaluate_handle("el => el.closest('.tselect')?.querySelector('.tselect-btn') || el")
        page.evaluate("el => {window.scrollTo(0, 0); el.focus({preventScroll:true});}", campo)
        page.wait_for_function("""() => {
          const el = document.activeElement;
          const nav = document.querySelector('.shipment-step-recipient .paso-nav');
          return el.getBoundingClientRect().bottom + 15 <= nav.getBoundingClientRect().top;
        }""")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.locator(".shipment-step-recipient input:visible").first.focus()
        page.wait_for_function("""() => document.activeElement.getBoundingClientRect().bottom + 15 <=
            document.querySelector('.shipment-step-recipient .paso-nav').getBoundingClientRect().top""")
    finally:
        page.close()


@pytest.mark.parametrize("ancho", [1100, 390])
def test_cotizar_muestra_aviso_completo_sin_truncar(browser, wizard, ancho):
    from test_cotizador_unificado import render_quote
    page = browser.new_page(viewport={"width": ancho, "height": 700})
    try:
        # El aviso lo agrega form-draft.js en la pantalla real de Cotizar.
        abrir_local(page, render_quote())
        status = page.locator('[data-quote-panel="internacional"] .uq-route-tools .draft-status')
        assert status.is_visible()
        assert "en esta pestaña" in status.inner_text()
        assert status.evaluate("el => el.scrollWidth <= el.clientWidth && getComputedStyle(el).textOverflow !== 'ellipsis'")
    finally:
        page.close()
