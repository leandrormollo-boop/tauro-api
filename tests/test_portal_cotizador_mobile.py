from pathlib import Path
from urllib.parse import urlsplit

import pytest

from test_cotizador_unificado import render_quote
from test_portal_tanda1 import browser


ROOT = Path(__file__).resolve().parents[1]


def _open_quote(page, html):
    def respond(route):
        path = urlsplit(route.request.url).path
        if path.startswith("/static/"):
            asset = ROOT / path.lstrip("/")
            if asset.is_file():
                route.fulfill(path=str(asset))
                return
        if route.request.resource_type == "document" or path == "/portal/cotizar":
            route.fulfill(body=html, content_type="text/html")
        else:
            route.abort()

    page.route("**/*", respond)
    page.goto("https://portal.test/portal/home")
    page.evaluate("""() => {
      const opener = document.createElement('a');
      opener.id = 'quote-test-opener';
      opener.href = '/portal/cotizar?ambito=internacional';
      opener.dataset.cotizarVentana = '';
      opener.textContent = 'Cotizar';
      document.body.appendChild(opener);
    }""")
    page.locator("#quote-test-opener").evaluate("opener => { opener.focus(); opener.click(); }")
    page.locator("#quote-window-dialog[open] .unified-quote").wait_for()


@pytest.mark.parametrize("width", [320, 390, 430])
def test_cotizador_movil_no_superpone_acciones_y_restituye_foco(browser, width):
    page = browser.new_page(viewport={"width": width, "height": 844})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        _open_quote(page, render_quote())
        panel = page.locator(
            '#quote-window-dialog [data-quote-panel="internacional"]'
        )
        destination = panel.locator('[data-location-side="destino"]').bounding_box()
        actions = panel.locator(".uq-mobile-actions").bounding_box()

        assert actions["y"] >= destination["y"] + destination["height"] - 1
        assert panel.locator(".uq-mobile-actions").evaluate(
            "el => getComputedStyle(el).position"
        ) == "static"
        scroll_needed = page.locator("[data-cotizar-contenido]").evaluate(
            "el => el.scrollHeight - el.clientHeight"
        )
        assert scroll_needed <= 120
        page.locator("[data-cotizar-contenido]").evaluate(
            "el => { el.scrollTop = el.scrollHeight; }"
        )
        assert panel.locator("[data-quote-next]").is_visible()

        destination_postal = panel.locator('[name="destino_cp_internacional"]')
        destination_postal.focus()
        page.set_viewport_size({"width": width, "height": 480})
        page.wait_for_function("""() => {
          const field = document.activeElement;
          const viewport = window.visualViewport;
          return field && viewport && field.getBoundingClientRect().bottom + 11 <=
            viewport.offsetTop + viewport.height;
        }""")
        page.set_viewport_size({"width": width, "height": 844})

        panel.locator("form").evaluate(
            "form => form.querySelectorAll('[required]').forEach(field => field.required = false)"
        )
        panel.locator("[data-quote-next]").click()
        assert page.evaluate("document.activeElement.textContent.trim()") == "Paquetes"

        page.keyboard.press("Escape")
        assert not page.locator("#quote-window-dialog").evaluate("dialog => dialog.open")
        assert page.evaluate("document.activeElement.id") == "quote-test-opener"
        assert errors == []
    finally:
        page.close()
