from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE = (ROOT / "templates/base.html").read_text(encoding="utf-8")
HOME = (ROOT / "templates/portal/home.html").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/tauro.css").read_text(encoding="utf-8")


def test_inicio_no_anida_landmark_main():
    assert '<main class="home-dashboard-main">' not in HOME
    assert '<div class="home-dashboard-main">' in HOME


def test_drawer_movil_expone_estado_y_control_semantico():
    assert '<aside class="sidebar" id="portal-sidebar" aria-label="Navegación y cuenta">' in BASE
    assert 'class="burger" aria-label="Abrir menú" aria-controls="portal-sidebar" aria-expanded="false"' in BASE
    assert 'class="side-overlay" aria-label="Cerrar menú"' in BASE
    assert 'burger.addEventListener("click"' in BASE
    assert 'event.key === "Escape"' in BASE
    assert 'sidebar.inert = mobile.matches && !open' in BASE
    assert 'if (main) main.inert = open' in BASE
    assert 'if (tabbar) tabbar.inert = open' in BASE


def test_drawer_cerrado_no_pinta_sombra_lateral():
    mobile = CSS[CSS.index("@media (max-width: 900px)"):CSS.index("@media (max-width: 640px)")]
    assert "visibility: hidden" in mobile
    assert "box-shadow: none !important" in mobile
    opened = mobile[mobile.index("#side-toggle:checked ~ .shell .sidebar"):]
    assert "visibility: visible" in opened
    assert "box-shadow: 20px 0 60px rgba(0,0,0,.5) !important" in opened


def test_versiona_css_del_shell():
    assert 'tauro.css?v=52' in BASE
