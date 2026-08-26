"""Costuras UX de cotización, recupero y configuración de correo."""

from pathlib import Path

from servicios import password_reset_queue


ROOT = Path(__file__).resolve().parents[1]


def test_web_no_promete_operacion_nacional_antes_de_oca_andreani():
    fuentes = "\n".join(
        (ROOT / ruta).read_text(encoding="utf-8")
        for ruta in (
            "web/components/01-nav-hero.jsx",
            "web/components/03-services-tracking.jsx",
        )
    )
    assert "Logística nacional e internacional conectada" not in fuentes
    assert "Operación nacional e internacional" not in fuentes
    assert "Nacional en preparación" in fuentes


def test_recupero_preserva_quote_id_hasta_volver_al_login():
    login = (ROOT / "templates/portal/login.html").read_text(encoding="utf-8")
    reset = (ROOT / "templates/portal/password_reset.html").read_text(encoding="utf-8")
    endpoint = (ROOT / "endpoints/portal_cliente.py").read_text(encoding="utf-8")

    bloque_forgot = login[login.index('id="password-forgot-form"') :]
    bloque_forgot = bloque_forgot[: bloque_forgot.index("</form>")]
    assert 'name="quote_id"' in bloque_forgot
    assert 'id="password-reset-quote" name="quote_id"' in reset
    assert 'fragmentParams.get("quote_id")' in reset
    assert 'query["quote_id"] = quote_id' in endpoint

    quote_id = "Q-abcdefghijklmnopqrstuvwxyz123456"
    link = password_reset_queue._password_reset_link("x" * 43, quote_id)
    assert f"&quote_id={quote_id}" in link
    assert "?token=" not in link


def test_admin_mobile_no_comprime_contenido_y_config_tiene_labels():
    base = (ROOT / "templates/admin/base_admin.html").read_text(encoding="utf-8")
    config = (ROOT / "templates/admin/config.html").read_text(encoding="utf-8")

    assert "@media (max-width: 720px)" in base
    assert ".admin-layout { display: block" in base
    assert ".sidebar {" in base and "overflow-x: auto" in base
    assert ".admin-main { width: 100%; padding: 18px 14px 80px" in base
    assert 'label for="config-item-{{ loop.index }}"' in config
    assert 'id="config-nuevo-parametro"' in config
    assert 'for="config-nuevo-valor"' in config
