from __future__ import annotations

from contextlib import contextmanager

from endpoints import portal_cliente
from servicios import catalogo, integraciones_tienda


def _conexion_falsa(*, filas=None, capturas=None):
    capturas = capturas if capturas is not None else []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, consulta, parametros=None):
            capturas.append((str(consulta), parametros))

        def fetchall(self):
            return filas or []

    class Conexion:
        def cursor(self):
            return Cursor()

    @contextmanager
    def abrir():
        yield Conexion()

    return abrir


def test_listar_pedidos_expone_tienda_dominio_sin_quitar_campos(monkeypatch):
    consultas = []
    filas = [{
        "id": 17,
        "cliente_id": "PESCA_JACKS",
        "plataforma": "shopify",
        "pedido_externo_id": "5001",
        "tienda_dominio": "pesca-jacks.myshopify.com",
    }]
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(
        integraciones_tienda,
        "get_conn",
        _conexion_falsa(filas=filas, capturas=consultas),
    )

    pedidos = integraciones_tienda.listar_pedidos(
        "PESCA_JACKS", "PENDIENTE", limite=25,
    )

    assert pedidos == filas
    consulta, parametros = consultas[0]
    assert "SELECT p.*, t.dominio AS tienda_dominio" in consulta
    assert "LEFT JOIN tiendas_conectadas" in consulta
    assert "UPPER(t.cliente_id) = UPPER(p.cliente_id)" in consulta
    assert parametros == ("PESCA_JACKS", "PENDIENTE", 25)


def test_enriquecer_items_filtra_catalogo_por_plataforma_y_tienda(monkeypatch):
    consultas = []
    producto = {
        "external_variant_id": "gid://shopify/ProductVariant/200",
        "alias_interno": "REEL-200",
        "sku_tienda": "REEL-200",
        "imagen_url": "https://cdn.shopify.com/pesca-jacks/reel.jpg",
        "titulo_tienda": "Reel 200",
        "variante_tienda": "Negro",
        "stock_controlado": True,
        "stock_disponible": 7,
        "sync_activo": True,
    }
    monkeypatch.setattr(
        catalogo,
        "get_conn",
        _conexion_falsa(filas=[producto], capturas=consultas),
    )

    items = catalogo.enriquecer_items_catalogo(
        "pesca_jacks",
        [{
            "variant_id": "gid://shopify/ProductVariant/200",
            "sku": "REEL-200",
            "nombre": "Reel",
        }],
        plataforma="SHOPIFY",
        tienda_dominio="PESCA-JACKS.MYSHOPIFY.COM",
    )

    consulta, parametros = consultas[0]
    assert "LOWER(COALESCE(plataforma, '')) = %s" in consulta
    assert "LOWER(COALESCE(tienda_dominio, '')) = %s" in consulta
    assert parametros[0:3] == (
        "PESCA_JACKS", "shopify", "pesca-jacks.myshopify.com",
    )
    assert items[0]["imagen_url"] == producto["imagen_url"]
    assert items[0]["stock_disponible"] == 7


def test_enriquecer_items_mantiene_compatibilidad_sin_filtros(monkeypatch):
    consultas = []
    monkeypatch.setattr(
        catalogo,
        "get_conn",
        _conexion_falsa(filas=[], capturas=consultas),
    )

    salida = catalogo.enriquecer_items_catalogo(
        "PESCA_JACKS", [{"sku": "SIN-IMAGEN"}],
    )

    consulta, parametros = consultas[0]
    assert "LOWER(COALESCE(plataforma" not in consulta
    assert "LOWER(COALESCE(tienda_dominio" not in consulta
    assert parametros[0] == "PESCA_JACKS"
    assert salida == [{"sku": "SIN-IMAGEN"}]


def test_tienda_view_pasa_identidad_de_tienda_al_enriquecimiento(monkeypatch):
    pedidos = [{
        "id": 17,
        "plataforma": "shopify",
        "tienda_dominio": "pesca-jacks.myshopify.com",
        "items": [{"sku": "REEL-200"}],
    }]
    llamadas = []

    monkeypatch.setattr(
        portal_cliente,
        "listar_tiendas",
        lambda _cliente: [{
            "id": 1,
            "plataforma": "shopify",
            "dominio": "pesca-jacks.myshopify.com",
            "activa": True,
        }],
    )
    monkeypatch.setattr(
        portal_cliente,
        "listar_pedidos",
        lambda _cliente, estado, limite=100: pedidos if estado == "PENDIENTE" else [],
    )
    monkeypatch.setattr(portal_cliente, "obtener_config", lambda _dominio: {})

    def enriquecer(cliente, items, **identidad):
        llamadas.append((cliente, items, identidad))
        return items

    monkeypatch.setattr(catalogo, "enriquecer_items_catalogo", enriquecer)
    monkeypatch.setattr(catalogo, "estado_sincronizacion_cliente", lambda _cliente: {})
    monkeypatch.setattr(catalogo, "resumen_stock_cliente", lambda _cliente: {})
    monkeypatch.setattr(
        portal_cliente.templates,
        "TemplateResponse",
        lambda **kwargs: kwargs["context"],
    )

    class Request:
        base_url = "https://taurosolutions.ar/"

    portal_cliente.tienda_view(Request(), cliente="PESCA_JACKS")

    assert llamadas == [(
        "PESCA_JACKS",
        [{"sku": "REEL-200"}],
        {
            "plataforma": "shopify",
            "tienda_dominio": "pesca-jacks.myshopify.com",
        },
    )]
