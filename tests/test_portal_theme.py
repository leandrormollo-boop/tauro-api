"""Contrato del tema claro/oscuro del portal de clientes."""
from pathlib import Path


RAIZ = Path(__file__).resolve().parent.parent
BASE = (RAIZ / "templates/base.html").read_text(encoding="utf-8")
ADMIN = (RAIZ / "templates/admin/base_admin.html").read_text(encoding="utf-8")
CSS = (RAIZ / "static/css/tauro.css").read_text(encoding="utf-8")
JS = (RAIZ / "static/js/portal-theme.js").read_text(encoding="utf-8")


def test_tema_se_aplica_antes_del_css_sin_destello():
    assert '<html lang="es" data-theme="light">' in BASE
    assert 'id="portal-theme-color"' in BASE
    assert 'localStorage.getItem("tauro.portal.theme")' in BASE
    assert 'if (tema !== "light" && tema !== "dark")' in BASE
    assert 'tema = "light";' in BASE
    assert BASE.index('localStorage.getItem("tauro.portal.theme")') < BASE.index("tauro.css?v=49")


def test_portal_ofrece_controles_en_shell_mobile_y_login():
    assert BASE.count("data-theme-toggle") == 3
    assert "theme-toggle-sidebar" in BASE
    assert "theme-toggle-compact" in BASE
    assert "theme-toggle-floating" in BASE
    assert 'src="/static/js/portal-theme.js?v=2"' in BASE


def test_preferencia_persiste_con_claro_predeterminado():
    assert 'localStorage.setItem(STORAGE_KEY, theme)' in JS
    assert 'prefers-color-scheme' not in JS
    assert 'aria-pressed' in JS
    assert 'metaColor.content' in JS


def test_paleta_clara_cubre_operacion_y_cotizador():
    assert 'html[data-theme="light"] {' in CSS
    for selector in (
        ".shell > .sidebar", ".mobile-bar", ".tabbar",
        ".quote-form-block", ".quote-package-row", ".quote-price-card",
        ".quote-form-card input",
    ):
        assert f'html[data-theme="light"] {selector}' in CSS
    assert 'html[data-theme="light"] .side-logo .mark img' in CSS
    assert 'html[data-theme="light"] .home-hero h1 { color: #fff; }' in CSS


def test_web_publica_y_admin_no_activan_el_tema_del_portal():
    web_html = (RAIZ / "web/Tauro Solutions.html").read_text(encoding="utf-8")
    assert "portal-theme.js" not in web_html
    assert "portal-theme.js" not in ADMIN
    assert 'data-theme-scope="admin"' in ADMIN
    assert 'src="/static/js/admin-theme.js?v=1"' in ADMIN
    assert 'tauro.admin.theme' in (RAIZ / "static/js/admin-theme.js").read_text()
