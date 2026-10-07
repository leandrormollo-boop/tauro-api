"""Los accesos cortos llevan a su panel sin saltear autenticación ni caché."""
import asyncio
from types import SimpleNamespace

import httpx2
import pytest
from fastapi.responses import HTMLResponse

from endpoints import admin, portal_cliente
from main import app


def _get(path, cookies=None, follow_redirects=False):
    async def request():
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="https://testserver",
            cookies=cookies or {},
            follow_redirects=follow_redirects,
        ) as client:
            return await client.get(path)

    return asyncio.run(request())


@pytest.mark.parametrize("root", ["/portal", "/admin"])
@pytest.mark.parametrize("slash", ["", "/"])
def test_acceso_corto_apunta_al_panel_propio_sin_cache(root, slash):
    response = _get(root + slash)
    assert response.status_code == 303
    assert response.headers["location"] == root + "/home"
    assert "set-cookie" not in response.headers
    for directive in ("no-store", "no-cache", "must-revalidate", "private"):
        assert directive in response.headers["cache-control"]
    assert response.headers["x-robots-tag"] == "noindex, nofollow"


@pytest.mark.parametrize("root", ["/portal", "/admin"])
@pytest.mark.parametrize("slash", ["", "/"])
def test_acceso_sin_sesion_termina_en_su_login(root, slash):
    response = _get(root + slash, follow_redirects=True)
    assert response.status_code == 200
    assert str(response.url) == "https://testserver" + root + "/login"
    assert [item.status_code for item in response.history] == [303, 303]


@pytest.mark.parametrize("root,cookie", [
    ("/portal", "token"), ("/admin", "admin_token"),
])
def test_cookie_invalida_se_valida_en_el_panel(monkeypatch, root, cookie):
    seen = []

    def reject(token):
        seen.append(token)
        return None

    if root == "/portal":
        monkeypatch.setattr(portal_cliente, "validar_token", reject)
    else:
        monkeypatch.setattr(admin, "_is_auth", reject)
    response = _get(root, cookies={cookie: "sesion-vencida"}, follow_redirects=True)
    assert response.status_code == 200
    assert response.url.path == root + "/login"
    assert seen and all(token == "sesion-vencida" for token in seen)


@pytest.mark.parametrize("root,other_cookie", [
    ("/portal", "admin_token"), ("/admin", "token"),
])
def test_sesion_del_otro_perfil_no_da_acceso(root, other_cookie):
    response = _get(root, cookies={other_cookie: "sesion-otro-perfil"}, follow_redirects=True)
    assert response.status_code == 200
    assert response.url.path == root + "/login"


@pytest.mark.parametrize("root", ["/portal", "/admin"])
@pytest.mark.parametrize("query", [
    "?next=https://example.com", "?redirect=//example.com", "?next=/admin/home",
])
def test_acceso_no_acepta_redirecciones_arbitrarias(root, query):
    response = _get(root + query)
    assert response.status_code == 303
    assert response.headers["location"] == root + "/home"


@pytest.mark.parametrize("root,cookie", [
    ("/portal", "token"), ("/admin", "admin_token"),
])
def test_acceso_no_modifica_sesion_existente(root, cookie):
    response = _get(root, cookies={cookie: "sesion-existente"})
    assert response.status_code == 303
    assert response.headers["location"] == root + "/home"
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("root, module, cookie", [
    ("/portal", portal_cliente, "token"), ("/admin", admin, "admin_token"),
])
def test_sesion_valida_llega_al_panel_sin_repetir_login(monkeypatch, root, module, cookie):
    seen = []

    def validate(token):
        seen.append(token)
        return "CLIENTE_QA" if token == "sesion-qa" else None

    # Sólo se simulan la sesión y las lecturas/render del panel; el routing,
    # la cookie y los controles de acceso son los de la aplicación real.
    if root == "/portal":
        monkeypatch.setattr(module, "validar_token", validate)
        monkeypatch.setattr(module, "get_facturado_real", lambda cliente: 0)
        monkeypatch.setattr(module, "saldo", lambda *args, **kwargs: {})
        monkeypatch.setattr(module, "listar_solicitudes_cliente", lambda *args, **kwargs: [])
        monkeypatch.setattr(module, "embudo_envios", lambda cliente: {})
        monkeypatch.setattr(module, "resumen_inicio_cliente", lambda *args: {})
    else:
        from servicios import control_negocio
        monkeypatch.setattr(module, "_is_auth", validate)
        monkeypatch.setattr(control_negocio, "obtener_control_negocio", lambda *args, **kwargs: {"stats": {}})

    def render(*, request, name, context):
        assert name == root.removeprefix("/") + "/home.html"
        if root == "/portal":
            assert context["cliente"] == "CLIENTE_QA"
        return HTMLResponse("Panel autenticado QA")

    monkeypatch.setattr(module, "templates", SimpleNamespace(TemplateResponse=render))
    response = _get(root, cookies={cookie: "sesion-qa"}, follow_redirects=True)
    assert response.status_code == 200
    assert response.url.path == root + "/home"
    assert len(response.history) == 1
    assert seen == ["sesion-qa"]
