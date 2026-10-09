"""Contacto accesible y URL canónica, sin llamadas a operadores."""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from test_portal_tanda1 import browser  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def abrir_web(page, root=ROOT):
    def responder(route):
        path = urlsplit(route.request.url).path
        if route.request.method != "GET":
            raise AssertionError("La prueba de contacto no debe enviar datos")
        archivo = root / path.lstrip("/")
        if path == "/web":
            archivo = root / "web/Tauro Solutions.html"
        elif path == "/styles.css":
            archivo = root / "web/styles.css"
        if urlsplit(route.request.url).hostname == "localhost" and archivo.is_file():
            route.fulfill(path=str(archivo))
        elif path in ("/partners", "/operadores", "/paises"):
            route.fulfill(json=[])
        else:
            route.abort()
    page.route("**/*", responder)
    page.goto("http://localhost/web#contacto")
    page.locator("#contacto").scroll_into_view_if_needed()


def test_canonical_no_redirige_y_coincide_con_sitemap(monkeypatch):
    import main
    monkeypatch.setattr(main, "obtener_meta_pixel_id", lambda: None)
    client = TestClient(main.app, base_url="https://taurosolutions.ar")
    # Sin lifespan: no arrancar jobs ni conectar servicios reales.
    entrada = client.get("/", follow_redirects=False)
    assert entrada.headers["location"] == "/web"
    pagina = client.get("/web", follow_redirects=False)
    assert pagina.status_code == 200
    canonical = re.search(r'rel="canonical" href="([^"]+)"', pagina.text).group(1)
    assert canonical == str(pagina.url)
    assert f'property="og:url" content="{canonical}"' in pagina.text
    structured = json.loads(re.search(r'type="application/ld\+json">\s*(.*?)</script>', pagina.text, re.S).group(1))
    assert structured["url"] == canonical
    assert f"<loc>{canonical}</loc>" in client.get("/sitemap.xml").text


@pytest.mark.parametrize("width", [390, 1440])
@pytest.mark.parametrize("rechazar", [False, True])
def test_copiar_correo_teclado_y_permiso_denegado(browser, width, rechazar):
    context = browser.new_context(viewport={"width": width, "height": 900})
    page = context.new_page()
    page.add_init_script("""Object.defineProperty(navigator, 'clipboard', {value: {
      writeText: async text => { window.correoCopiado = text; %s }
    }});""" % ("throw new Error('Permiso denegado');" if rechazar else ""))
    abrir_web(page)
    boton = page.get_by_role("button", name="Copiar correo", exact=True)
    boton.focus()
    boton.press("Enter")
    status = page.locator("#contacto [role=status]")
    from playwright.sync_api import expect
    expect(status).to_have_text(
        "No pudimos copiarlo. Seleccioná el correo y copialo manualmente."
        if rechazar else "Correo copiado."
    )
    assert page.evaluate("window.correoCopiado") == "cotizaciones@taurosolutions.ar"
    expect(page.locator("#contacto").get_by_role("link", name="cotizaciones@taurosolutions.ar")).to_be_visible()
    context.close()
