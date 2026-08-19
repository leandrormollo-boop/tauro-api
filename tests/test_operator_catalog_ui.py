from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_web_y_portal_consumen_catalogo_publico_sin_habilitar_operaciones():
    main = (ROOT / "main.py").read_text()
    endpoint = (ROOT / "endpoints/portal_cliente.py").read_text()
    portal = (ROOT / "templates/portal/cotizar.html").read_text()
    web = (ROOT / "web/components/02-quote-widget.jsx").read_text()
    assert '@app.get("/operadores"' in main
    assert "_operadores_cliente(" in endpoint
    assert "operadores_internacionales" in portal
    assert "fetch(`${API_URL}/operadores`)" in web
    assert "disponible_segun_cuenta" in web
    assert "integracion_preparada" in web
    assert "operator.estado_corto || operator.estado_label" in web


def test_catalogo_visible_no_contiene_nombres_de_secretos():
    portal = (ROOT / "templates/portal/cotizar.html").read_text()
    web = (ROOT / "web/components/02-quote-widget.jsx").read_text()
    visible = portal + web
    assert "DHL_API_SECRET" not in visible
    assert "FEDEX_SECRET_KEY" not in visible
    assert "OCA_PASSWORD" not in visible
