from __future__ import annotations

import inspect
from datetime import datetime, timezone

import main
from modelos.producto import Producto
from servicios import catalogo


def _producto_stock() -> Producto:
    return Producto.model_construct(
        cliente="PESCA_JACKS",
        alias_interno="REEL-PJ-200",
        nombre_invoice="Fishing reel",
        hs_code="9507.10.00",
        largo_cm=0,
        ancho_cm=0,
        alto_cm=0,
        peso_kg=1.2,
        valor_usd_default=0,
        activo=False,
        imagen_url="https://cdn.shopify.com/reel.jpg",
        plataforma="shopify",
        tienda_dominio="pesca-jacks.myshopify.com",
        external_product_id="gid://shopify/Product/100",
        external_variant_id="gid://shopify/ProductVariant/200",
        external_inventory_item_id="gid://shopify/InventoryItem/300",
        sku_tienda="REEL-PJ-200",
        titulo_tienda="Reel Pesca Jacks 200",
        variante_tienda="Negro",
        precio_tienda=44500.0,
        moneda_tienda="ARS",
        stock_controlado=True,
        stock_disponible=7,
        stock_comprometido=2,
        stock_fisico=9,
        stock_entrante=4,
        stock_actualizado_at=datetime(2026, 8, 27, 4, 0, tzinfo=timezone.utc),
        source_updated_at=None,
        sync_activo=True,
        ubicaciones=[{
            "external_location_id": "gid://shopify/Location/1",
            "ubicacion_nombre": "Depósito",
            "disponible": 7,
            "comprometido": 2,
            "fisico": 9,
            "entrante": 4,
            "source_updated_at": datetime(2026, 8, 27, 3, 59, tzinfo=timezone.utc),
        }],
    )


def test_stock_api_es_del_cliente_y_no_filtra_costos(monkeypatch):
    monkeypatch.setattr(
        main, "autenticar", lambda clave: {
            "encontrado": True, "cliente_id": "PESCA_JACKS",
        } if clave == "tauro-qa" else None,
    )
    consultas = []

    def listar(cliente, limite, offset):
        consultas.append((cliente, limite, offset))
        return [_producto_stock()], 1

    monkeypatch.setattr(catalogo, "listar_stock_cliente", listar)
    monkeypatch.setattr(catalogo, "estado_sincronizacion_cliente", lambda cliente: {
        "estado": "COMPLETADO",
        "ultima_sincronizacion_at": datetime(2026, 8, 27, 4, 1, tzinfo=timezone.utc),
        "ultimo_error_codigo": None,
    })

    respuesta = main.stock_cliente("tauro-qa", limite=50, offset=0)

    assert consultas == [("PESCA_JACKS", 50, 0)]
    assert respuesta["total"] == 1
    assert respuesta["productos"][0]["stock_disponible"] == 7
    assert respuesta["productos"][0]["ubicaciones"][0]["nombre"] == "Depósito"
    assert respuesta["productos"][0]["listo_para_envio"] is False
    assert respuesta["sincronizacion"]["estado"] == "COMPLETADO"
    serializado = str(respuesta).lower()
    assert "costo" not in serializado
    assert "margen" not in serializado
    assert "markup" not in serializado


def test_listar_stock_sql_siempre_filtra_cliente():
    fuente = inspect.getsource(catalogo.listar_stock_cliente)
    assert "WHERE cliente_id=%s" in fuente
    assert "COALESCE(sync_activo, TRUE)=TRUE" in fuente
