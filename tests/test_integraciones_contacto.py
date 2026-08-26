"""Los CTA de integración usan la casilla corporativa específica."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_cta_integraciones_apunta_a_casilla_corporativa():
    hero = (ROOT / "web/components/01-nav-hero.jsx").read_text(encoding="utf-8")
    servicios = (ROOT / "web/components/03-services-tracking.jsx").read_text(encoding="utf-8")
    contacto = (ROOT / "web/components/04-stats-contact-footer.jsx").read_text(encoding="utf-8")

    assert 'INTEGRACIONES_EMAIL = "integraciones@taurosolutions.ar"' in hero
    assert "Quiero integrar mi tienda con TAURO" in hero
    assert hero.count("href={INTEGRACIONES_MAILTO}") == 4
    assert "href: INTEGRACIONES_MAILTO" in servicios
    assert contacto.count("href={INTEGRACIONES_MAILTO}") == 2


def test_bundle_publico_publica_el_mailto_y_cache_nuevo():
    bundle = (ROOT / "static/js/app.js").read_text(encoding="utf-8")
    html = (ROOT / "web/Tauro Solutions.html").read_text(encoding="utf-8")

    assert "integraciones@taurosolutions.ar" in bundle
    assert "Quiero integrar mi tienda con TAURO" in bundle
    assert 'src="/static/js/app.js?v=16"' in html
