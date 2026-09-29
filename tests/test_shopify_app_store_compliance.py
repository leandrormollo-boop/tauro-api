from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_shopify_admin_api_es_exclusivamente_graphql():
    fuente = (ROOT / "servicios" / "shopify_app.py").read_text(encoding="utf-8")

    assert "/graphql.json" in fuente
    assert "carrier_services.json" not in fuente
    assert "fulfillment_orders.json" not in fuente
    assert "fulfillments.json" not in fuente
    assert "requests.request(" not in fuente


def test_portal_no_pide_dominio_para_instalar_shopify():
    plantilla = (ROOT / "templates" / "portal" / "tienda.html").read_text(
        encoding="utf-8"
    )

    assert 'action="/shopify/install"' not in plantilla
    assert 'name="shop"' not in plantilla
    assert "En tu administrador de Shopify, entrá en Apps" in plantilla
    assert "https://admin.shopify.com/" in plantilla


def test_portal_acota_shopify_a_pedidos_y_tracking():
    plantilla = (ROOT / "templates" / "portal" / "tienda.html").read_text(
        encoding="utf-8"
    )
    paquetes = (ROOT / "templates" / "portal" / "paquetes.html").read_text(
        encoding="utf-8"
    )
    javascript = (ROOT / "static" / "js" / "portal-paquetes.js").read_text(
        encoding="utf-8"
    )

    assert 'action="/portal/tienda/politica"' not in plantilla
    assert "Shopify importa pedidos y recibe tracking" in plantilla
    assert "no publica tarifas TAURO en checkout" in plantilla
    assert "Las cotizaciones se hacen en el portal TAURO" in paquetes
    assert "Autorizar tarifas en Shopify" not in javascript
    assert "Conectar servicio" not in javascript
    assert "esta integración no publica tarifas en checkout" in javascript
