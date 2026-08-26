"""Contrato de ámbito del cotizador público (web, sin login)."""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WIDGET = ROOT / "web" / "components" / "02-quote-widget.jsx"
WEB_HTML = ROOT / "web" / "Tauro Solutions.html"


def test_widget_rechaza_ar_a_ar_antes_del_request():
    fuente = WIDGET.read_text(encoding="utf-8")
    guarda = 'if (origenIso === "AR" && destinoIso === "AR")'
    request = 'fetch(`${API_URL}/cotizar-web`'

    assert guarda in fuente
    assert fuente.index(guarda) < fuente.index(request)
    assert "OCA y Andreani" in fuente
    assert "Todavía no se pueden cotizar desde este formulario." in fuente


def test_widget_envia_isos_normalizados():
    fuente = WIDGET.read_text(encoding="utf-8")

    assert "const origenIso = normalizeCountry(origen);" in fuente
    assert "const destinoIso = normalizeCountry(destino);" in fuente
    assert "origen_pais: origenIso" in fuente
    assert "destino_pais: destinoIso" in fuente


def test_html_publico_referencia_el_bundle_actual():
    html = WEB_HTML.read_text(encoding="utf-8")

    assert '/static/js/app.js?v=16' in html
    assert '/static/js/app.js?v=5' not in html
