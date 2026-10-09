from pathlib import Path
from urllib.parse import urlsplit

from test_cotizador_unificado import render_quote
from test_portal_tanda1 import browser


ROOT = Path(__file__).resolve().parents[1]


def _serve(page, html, block_scripts=False):
    def respond(route):
        path = urlsplit(route.request.url).path
        if block_scripts and path.endswith(".js"):
            route.abort()
            return
        if path.startswith("/static/"):
            asset = ROOT / path.lstrip("/")
            if asset.is_file():
                route.fulfill(path=str(asset))
                return
        if route.request.resource_type == "document":
            route.fulfill(body=html, content_type="text/html")
        else:
            route.abort()

    page.route("**/*", respond)
    page.goto("https://portal.test/portal/cotizar?ambito=internacional")


def test_sin_javascript_conserva_ayudas_y_formulario_completo(browser):
    context = browser.new_context(
        viewport={"width": 390, "height": 844}, java_script_enabled=False
    )
    page = context.new_page()
    try:
        _serve(page, render_quote())
        panel = page.locator('[data-quote-panel="internacional"]')
        assert panel.get_by_text("Ingresá el peso y las medidas", exact=False).is_visible()
        assert panel.locator('[name="origen_ciudad"]').is_visible()
        assert panel.locator('[name="bulto_peso"]').is_visible()
        assert panel.locator("[data-quote-results]").is_visible()
        assert panel.locator(".uq-step-nav").is_hidden()
        assert panel.locator("[data-quote-draft-slot]").is_hidden()
    finally:
        context.close()


def test_scripts_bloqueados_no_impiden_usar_los_campos(browser):
    page = browser.new_page(viewport={"width": 390, "height": 844})
    try:
        _serve(page, render_quote(), block_scripts=True)
        panel = page.locator('[data-quote-panel="internacional"]')
        city = panel.locator('[name="origen_ciudad"]')
        package_weight = panel.locator('[name="bulto_peso"]')
        assert city.is_visible() and city.is_enabled()
        assert package_weight.is_visible() and package_weight.is_enabled()
        city.fill("Buenos Aires")
        package_weight.fill("5")
        assert city.input_value() == "Buenos Aires"
        assert package_weight.input_value() == "5"
    finally:
        page.close()
