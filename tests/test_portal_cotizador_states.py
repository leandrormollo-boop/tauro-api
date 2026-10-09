from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from test_cotizador_unificado import render_quote
from test_portal_tanda1 import browser


ROOT = Path(__file__).resolve().parents[1]


def _option(carrier, price):
    return {
        "carrier_id": carrier.lower(),
        "carrier_nombre": carrier,
        "servicio": "Express",
        "precio_final_ars": Decimal(price),
        "dias_estimados": 3,
        "portal_quote_id": None,
        "reseller_quote_id": None,
        "continuar_url": "/portal/oca/nuevo",
    }


def _serve(page, html):
    def respond(route):
        path = urlsplit(route.request.url).path
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
    page.evaluate("""() => {
      window.__quotePending = [];
      window.fetch = (_url, options = {}) => {
        if (options.method !== 'POST') {
          return Promise.resolve(new Response('{"suggestions":[]}', {
            status: 200, headers: {'content-type': 'application/json'}
          }));
        }
        return new Promise((resolve, reject) => {
          window.__quotePending.push({resolve, reject, signal: options.signal});
        });
      };
      window.__resolveQuote = (index, html, status = 200) => {
        window.__quotePending[index].resolve(new Response(html, {
          status,
          headers: {'content-type': 'text/html'}
        }));
      };
    }""")


def _fill_complete(page):
    page.evaluate("""() => {
      const form = document.querySelector('[data-unified-form="internacional"]');
      const values = {
        origen_pais: 'AR', origen_ciudad: 'Buenos Aires', origen_cp_internacional: '1001',
        destino_pais: 'US', destino_ciudad_internacional: 'Miami', destino_cp_internacional: '33101',
        bulto_cantidad: '1', bulto_peso: '5', bulto_largo: '30', bulto_ancho: '20',
        bulto_alto: '10', valor_declarado_usd: '100'
      };
      Object.entries(values).forEach(([name, value]) => { form.elements[name].value = value; });
      form.dispatchEvent(new Event('input', {bubbles: true}));
    }""")


def test_estados_reintento_y_respuesta_obsoleta_conservan_los_datos(browser):
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    stale_html = render_quote(opciones=[_option("DHL", "999999")])
    old_option = _option("DHL", "236077")
    old_option["portal_quote_id"] = "old-quote"
    old_html = render_quote(opciones=[old_option])
    new_option = _option("DHL", "240500")
    new_option["portal_quote_id"] = "new-quote"
    new_html = render_quote(opciones=[new_option])
    retry_html = render_quote(opciones=[_option("DHL", "241000")])
    try:
        _serve(page, render_quote())
        panel = page.locator('[data-quote-panel="internacional"]')
        results = panel.locator("[data-quote-results]")
        assert panel.locator(".uq-results").get_attribute("data-quote-state") == "idle"

        _fill_complete(page)
        page.wait_for_function("window.__quotePending.length === 1")
        assert panel.locator(".uq-results").get_attribute("data-quote-state") == "loading"
        assert results.get_attribute("aria-busy") == "true"
        page.evaluate("html => window.__resolveQuote(0, html)", old_html)
        panel.locator(".uq-price").wait_for()
        assert panel.locator(".uq-results").get_attribute("data-quote-state") == "ready"
        assert results.locator(".uq-choose").get_attribute("href") is not None
        assert results.locator(".uq-client-download button").is_enabled()

        page.evaluate("""() => {
          const form = document.querySelector('[data-unified-form="internacional"]');
          form.elements.destino_ciudad_internacional.value = 'Nueva York';
          form.dispatchEvent(new Event('input', {bubbles: true}));
        }""")
        page.wait_for_function("window.__quotePending.length === 2")
        assert results.locator("[data-quote-stale]").is_visible()
        assert results.locator(".uq-price").is_visible()
        stale_link = results.locator(".uq-choose")
        assert stale_link.get_attribute("href") is None
        assert stale_link.get_attribute("aria-disabled") == "true"
        assert results.locator(".uq-client-download button").is_disabled()
        current_url = page.url
        stale_link.click()
        assert page.url == current_url
        assert results.locator(".uq-client-download").evaluate(
            "form => form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}))"
        ) is False

        page.evaluate("""() => {
          const form = document.querySelector('[data-unified-form="internacional"]');
          form.elements.destino_cp_internacional.value = '10001';
          form.dispatchEvent(new Event('input', {bubbles: true}));
        }""")
        page.wait_for_function("window.__quotePending.length === 3")
        assert page.evaluate("window.__quotePending[1].signal.aborted") is True
        page.evaluate("html => window.__resolveQuote(2, html)", new_html)
        results.get_by_text("$ 240.500,00").wait_for()
        assert results.locator(".uq-choose").get_attribute("href") is not None
        assert results.locator(".uq-client-download button").is_enabled()
        page.evaluate("html => window.__resolveQuote(1, html)", stale_html)
        page.wait_for_timeout(20)
        assert results.get_by_text("$ 240.500,00").is_visible()
        assert results.get_by_text("$ 999.999,00").count() == 0

        page.evaluate("""() => {
          const form = document.querySelector('[data-unified-form="internacional"]');
          form.elements.destino_ciudad_internacional.value = 'Boston';
          form.dispatchEvent(new Event('input', {bubbles: true}));
        }""")
        page.wait_for_function("window.__quotePending.length === 4")
        page.evaluate("window.__resolveQuote(3, '', 500)")
        panel.locator('[data-quote-state="error"]').wait_for()
        assert results.locator(".uq-price").is_visible()
        assert results.locator("[data-quote-stale]").is_visible()
        assert results.locator("[data-quote-retry]").is_visible()
        assert page.locator('[name="destino_ciudad_internacional"]').input_value() == "Boston"

        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(50)
        panel.locator('[data-quote-step="3"]').click()
        assert panel.locator("[data-quote-status]").get_attribute("aria-live") == "off"
        assert panel.locator("[data-quote-results-status]").get_attribute("aria-live") == "polite"
        assert panel.locator("[data-quote-results-status]").text_content().startswith(
            "No pudimos actualizar"
        )
        results.locator("[data-quote-retry]").click()
        page.wait_for_function("window.__quotePending.length === 5")
        page.evaluate("html => window.__resolveQuote(4, html)", retry_html)
        results.get_by_text("$ 241.000,00").wait_for()
        assert panel.locator(".uq-results").get_attribute("data-quote-state") == "ready"
        assert results.locator("[data-quote-stale]").count() == 0
        assert results.get_by_text("Precio estimado", exact=True).is_visible()
        assert page.locator('[name="destino_ciudad_internacional"]').input_value() == "Boston"
        assert panel.locator("[data-quote-status]").text_content() == "Tarifas actualizadas."
    finally:
        page.close()


def test_resultado_oca_distingue_error_por_operador_y_precio_estimado():
    unavailable = render_quote(
        ambito="nacional",
        opciones=[],
        no_disponibles=[{"id": "oca", "nombre": "OCA", "motivo": "No pudimos consultar su tarifa."}],
    )
    success = render_quote(ambito="nacional", opciones=[_option("OCA", "18500")])

    assert "OCA" in unavailable and "No pudimos consultar su tarifa." in unavailable
    assert "OCA" in success and "Precio estimado" in success and "$ 18.500,00" in success
