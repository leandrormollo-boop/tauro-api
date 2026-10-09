from pathlib import Path

from endpoints import portal_cliente


ROOT = Path(__file__).resolve().parents[1]


def _leer(ruta: str) -> str:
    return (ROOT / ruta).read_text(encoding="utf-8")


def test_catalogo_y_pedidos_comparten_miniatura_de_producto():
    catalogo = _leer("templates/portal/catalogo.html")
    tienda = _leer("templates/portal/tienda.html")
    macro = _leer("templates/portal/_product_thumbnail.html")

    assert "product_thumbnail(p.imagen_url" in catalogo
    assert "product_thumbnail(it.imagen_url" in tienda
    assert 'alt=""' in macro
    assert "Sin foto" in macro
    assert "data-product-thumb-key" in macro
    assert "'catalog:' ~ p.alias_interno" in catalogo
    assert "'order:' ~ p.id ~ ':' ~ loop.index0" in tienda
    assert "📦" not in macro


def test_templates_de_catalogo_compilan_con_la_macro_compartida():
    entorno = portal_cliente.templates.env

    entorno.get_template("portal/catalogo.html")
    entorno.get_template("portal/tienda.html")
    entorno.get_template("portal/_product_thumbnail.html")


def test_miniatura_no_recorta_y_es_legible_en_mobile():
    css = _leer("static/css/product-thumbnails.css")

    assert "object-fit: contain" in css
    assert "--product-thumb-size: 72px" in css
    assert "--product-thumb-size: 54px" in css
    assert "@media (max-width: 480px)" in css
    assert "overflow-wrap: anywhere" in css
    assert "button.product-thumb--missing:hover" in css
    assert ".catalog-out-of-stock.is-active" in css


def test_imagen_rota_muestra_respaldo_y_se_puede_ampliar():
    javascript = _leer("static/js/product-thumbnails.js")

    assert 'document.addEventListener("error"' in javascript
    assert 'event.target.matches("[data-product-image]")' in javascript
    assert 'event.target.closest("[data-product-image-open]")' in javascript
    assert "showModal()" in javascript


def test_sincronizacion_del_catalogo_no_recarga_la_pagina():
    javascript = _leer("static/js/catalog-sync.js")
    catalogo = _leer("templates/portal/catalogo.html")
    tienda = _leer("templates/portal/tienda.html")

    assert 'event.preventDefault()' in javascript
    assert 'fetch(formulario.action' in javascript
    assert 'fetch("/portal/api/catalogo/sync-estado"' in javascript
    assert "sincronizacion.sync_intento_id !== sincronizacionEsperada" in javascript
    assert "Date.now() - 5000" not in javascript
    assert "data-catalog-sync-out-of-stock" in tienda
    assert 'classList.toggle("is-active", agotadas > 0)' in javascript
    assert 'role="status"' in catalogo
    assert 'aria-live="polite"' in tienda
    assert "var nuevas = new Map()" in javascript
    assert "actual.dataset.productThumbKey" in javascript
    assert "location.reload" not in javascript
    assert "location.assign" not in javascript
    assert "data-catalog-sync-form" in catalogo
    assert "data-catalog-sync-form" in tienda
    assert "data-catalog-results" in catalogo
    assert 'resultadosActuales.replaceWith' in javascript
    assert 'destinoNuevo.dispatchEvent(new Event("change"' in javascript
