from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "static/css/tauro.css").read_text(encoding="utf-8")
HOME = (ROOT / "templates/portal/home.html").read_text(encoding="utf-8")


def test_authenticated_primary_actions_use_calm_tauro_glass():
    start = CSS.index(".shell .btn-primary:not(.is-loading),")
    end = CSS.index(".shell .btn-primary:not(.is-loading)::before", start)
    block = CSS[start:end]

    assert "background-color: rgba(77,50,132,.2)" in block
    assert "backdrop-filter: blur(12px) saturate(1.08)" in block
    assert "border: 1px solid rgba(190,168,255,.46)" in block
    assert "animation: none" in block
    assert "background-color: #5e3fad" not in block


def test_light_theme_has_glass_for_cards_and_dark_glass_on_home_hero():
    assert 'html[data-theme="light"] .shell .btn-primary:not(.is-loading)' in CSS
    assert "background-color: rgba(111,79,197,.075)" in CSS
    assert 'html[data-theme="light"] .home-hero .btn-primary:not(.is-loading)' in CSS
    assert "background-color: rgba(77,50,132,.24)" in CSS


def test_home_keeps_primary_semantics_for_main_actions():
    assert 'href="/portal/envios/nuevo" class="btn btn-primary"' in HOME
    assert 'class="btn btn-primary">Rastrear</button>' in HOME
