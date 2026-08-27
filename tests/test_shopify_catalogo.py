from __future__ import annotations

import pytest

from servicios import shopify_catalogo as sc


def _variante(*, sku="REEL-PJ-200", tracked=True):
    return {
        "id": "gid://shopify/ProductVariant/200",
        "title": "Negro",
        "sku": sku,
        "price": "44.50",
        "updatedAt": "2026-08-27T01:00:00Z",
        "image": {"url": "https://cdn.shopify.com/reel-negro.jpg"},
        "product": {
            "id": "gid://shopify/Product/100",
            "title": "Reel Pesca Jacks 200",
            "status": "ACTIVE",
            "updatedAt": "2026-08-27T00:59:00Z",
            "featuredMedia": None,
        },
        "inventoryItem": {
            "id": "gid://shopify/InventoryItem/300",
            "tracked": tracked,
            "harmonizedSystemCode": "950710",
            "countryCodeOfOrigin": "CN",
            "measurement": {"weight": {"value": 1200, "unit": "GRAMS"}},
            "inventoryLevels": {
                "nodes": [
                    {
                        "updatedAt": "2026-08-27T01:02:00Z",
                        "location": {"id": "gid://shopify/Location/1", "name": "Depósito"},
                        "quantities": [
                            {"name": "available", "quantity": 7},
                            {"name": "committed", "quantity": 2},
                            {"name": "on_hand", "quantity": 9},
                            {"name": "incoming", "quantity": 4},
                        ],
                    },
                    {
                        "updatedAt": "2026-08-27T01:03:00Z",
                        "location": {"id": "gid://shopify/Location/2", "name": "Local"},
                        "quantities": [
                            {"name": "available", "quantity": 3},
                            {"name": "committed", "quantity": 1},
                            {"name": "on_hand", "quantity": 4},
                            {"name": "incoming", "quantity": 0},
                        ],
                    },
                ]
            },
        },
    }


def test_graphql_mapea_variante_imagen_peso_hs_y_stock(monkeypatch):
    monkeypatch.setattr(sc, "_graphql", lambda *_args, **_kwargs: {
        "shop": {"currencyCode": "ARS"},
        "productVariants": {
            "nodes": [_variante()],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        },
    })

    filas = sc.traer_variantes("pesca-jacks.myshopify.com", "token")
    assert len(filas) == 1
    fila = filas[0]
    assert fila["external_variant_id"] == "gid://shopify/ProductVariant/200"
    assert fila["peso_kg"] == 1.2
    assert fila["imagen_src"].endswith("reel-negro.jpg")
    assert fila["hs_code_tienda"] == "950710"
    assert fila["stock_disponible"] == 10
    assert fila["stock_comprometido"] == 3
    assert fila["stock_fisico"] == 13
    assert fila["stock_entrante"] == 4
    assert [u["ubicacion_nombre"] for u in fila["ubicaciones"]] == ["Depósito", "Local"]


def test_variante_sin_sku_no_se_descarta(monkeypatch):
    monkeypatch.setattr(sc, "_graphql", lambda *_args, **_kwargs: {
        "shop": {"currencyCode": "USD"},
        "productVariants": {
            "nodes": [_variante(sku="")],
            "pageInfo": {"hasNextPage": False},
        },
    })
    fila = sc.traer_variantes("x.myshopify.com", "token")[0]
    assert fila["sku"] == ""
    assert fila["external_variant_id"].endswith("/200")


def test_inventario_no_controlado_no_inventa_cero(monkeypatch):
    monkeypatch.setattr(sc, "_graphql", lambda *_args, **_kwargs: {
        "shop": {"currencyCode": "ARS"},
        "productVariants": {
            "nodes": [_variante(tracked=False)],
            "pageInfo": {"hasNextPage": False},
        },
    })
    fila = sc.traer_variantes("x.myshopify.com", "token")[0]
    assert fila["stock_controlado"] is False
    assert fila["stock_disponible"] is None


def test_no_publica_stock_parcial_si_hay_mas_ubicaciones(monkeypatch):
    variante = _variante()
    variante["inventoryItem"]["inventoryLevels"]["pageInfo"] = {"hasNextPage": True}
    monkeypatch.setattr(sc, "_graphql", lambda *_args, **_kwargs: {
        "shop": {"currencyCode": "ARS"},
        "productVariants": {
            "nodes": [variante],
            "pageInfo": {"hasNextPage": False},
        },
    })

    with pytest.raises(sc.ShopifyCatalogError) as exc:
        sc.traer_variantes("x.myshopify.com", "token")
    assert exc.value.codigo == "DEMASIADAS_UBICACIONES"


def test_graphql_pagina_por_cursor(monkeypatch):
    cursores = []

    def graphql(_dominio, _token, _query, variables):
        cursores.append(variables["after"])
        if variables["after"] is None:
            return {
                "shop": {"currencyCode": "ARS"},
                "productVariants": {
                    "nodes": [_variante()],
                    "pageInfo": {"hasNextPage": True, "endCursor": "cursor-2"},
                },
            }
        segundo = _variante(sku="CANA-39")
        segundo["id"] = "gid://shopify/ProductVariant/201"
        return {
            "shop": {"currencyCode": "ARS"},
            "productVariants": {
                "nodes": [segundo],
                "pageInfo": {"hasNextPage": False},
            },
        }

    monkeypatch.setattr(sc, "_graphql", graphql)
    assert len(sc.traer_variantes("x.myshopify.com", "token")) == 2
    assert cursores == [None, "cursor-2"]


def test_api_caida_no_se_confunde_con_catalogo_vacio(monkeypatch):
    monkeypatch.setattr(sc, "_graphql", lambda *_args, **_kwargs: None)
    with pytest.raises(sc.ShopifyCatalogError) as exc:
        sc.traer_variantes("x.myshopify.com", "token")
    assert exc.value.codigo == "SHOPIFY_NO_RESPONDE"


def test_webhook_inventario_consulta_la_variante_singular(monkeypatch):
    variante = _variante()
    inventario = variante.pop("inventoryItem")
    inventario["variant"] = variante
    consultas = []
    guardadas = []

    monkeypatch.setattr(sc, "instalacion", lambda _dominio: {"access_token": "token"})

    def graphql(_dominio, _token, query, variables):
        consultas.append((query, variables))
        return {"shop": {"currencyCode": "ARS"}, "inventoryItem": inventario}

    monkeypatch.setattr(sc, "_graphql", graphql)
    monkeypatch.setattr(
        sc, "_guardar_variantes",
        lambda dominio, cliente, filas, run_id: guardadas.append(
            (dominio, cliente, filas, run_id)
        ) or (0, len(filas)),
    )

    resultado = sc.sincronizar_inventory_item(
        "pesca-jacks.myshopify.com", "PESCA_JACKS", 300
    )

    assert resultado == {"ok": True, "creados": 0, "actualizados": 1}
    assert "variant {" in consultas[0][0]
    assert "variants(first:" not in consultas[0][0]
    assert consultas[0][1] == {"id": "gid://shopify/InventoryItem/300"}
    assert guardadas[0][2][0]["stock_disponible"] == 10


def test_sync_fallida_no_archiva_el_catalogo_existente(monkeypatch):
    estados = []
    archivados = []
    monkeypatch.setattr(sc, "instalacion", lambda _dominio: {
        "access_token": "token",
        "scopes": "read_products,read_inventory",
    })
    monkeypatch.setattr(
        sc, "traer_variantes",
        lambda *_args: (_ for _ in ()).throw(
            sc.ShopifyCatalogError("SHOPIFY_NO_RESPONDE", "caída controlada")
        ),
    )
    monkeypatch.setattr(sc, "_actualizar_estado", lambda *args, **kwargs: estados.append((args, kwargs)))
    monkeypatch.setattr(sc, "desactivar_ausentes_shopify", lambda *_args: archivados.append(True))

    resultado = sc.importar_catalogo("pesca-jacks.myshopify.com", "PESCA_JACKS")

    assert resultado["ok"] is False
    assert resultado["codigo"] == "SHOPIFY_NO_RESPONDE"
    assert archivados == []
    assert estados[-1][0][2] == "ERROR"


@pytest.mark.parametrize(
    ("value", "unit", "esperado"),
    [(1000, "GRAMS", 1.0), (2.20462262, "POUNDS", 1.0), (1, "KILOGRAMS", 1.0)],
)
def test_conversion_peso(value, unit, esperado):
    assert sc._peso_kg({"value": value, "unit": unit}) == pytest.approx(esperado, abs=1e-6)
