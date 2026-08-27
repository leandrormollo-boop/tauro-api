from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path

import pytest


SHOPIFY_SCOPES = (
    "read_orders,read_products,read_inventory,"
    "write_merchant_managed_fulfillment_orders"
)
SHOPIFY_WEBHOOKS = [
    "orders/create", "orders/updated",
    "products/create", "products/update", "products/delete",
    "inventory_levels/update", "inventory_items/update",
    "app/uninstalled",
]


class _Respuesta:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _Request:
    def __init__(self, body: bytes = b"", headers: dict | None = None,
                 query_params: dict | None = None, cookies: dict | None = None):
        self._body = body
        self.headers = headers or {}
        self.query_params = query_params or {}
        self.cookies = cookies or {}

    async def body(self):
        return self._body


def test_hmac_shopify_coincide_con_contrato_oficial():
    from servicios.integraciones_tienda import verificar_hmac_shopify

    cuerpo = b'{"id":990000000001,"name":"#TAURO-QA"}'
    secreto = "secreto-local-de-prueba"
    firma = base64.b64encode(
        hmac.new(secreto.encode(), cuerpo, hashlib.sha256).digest()
    ).decode()

    assert verificar_hmac_shopify(secreto, cuerpo, firma)
    assert not verificar_hmac_shopify(secreto, cuerpo + b" ", firma)
    assert not verificar_hmac_shopify(secreto, cuerpo, "")


def test_parser_shopify_conserva_datos_de_envio_y_flete():
    from servicios.integraciones_tienda import parsear_pedido_shopify

    pedido = parsear_pedido_shopify({
        "id": 990000000001,
        "name": "#QA-1",
        "email": "comprador@invalid.example",
        "financial_status": "paid",
        "currency": "USD",
        "total_price": "25.00",
        "shipping_address": {
            "name": "Cliente QA",
            "company": "Empresa QA",
            "address1": "1 Test Street",
            "address2": "Piso 2",
            "city": "Miami",
            "province": "Florida",
            "province_code": "FL",
            "zip": "33101",
            "country_code": "US",
        },
        "line_items": [{
            "title": "Producto QA", "quantity": 2, "price": "10.00",
            "sku": "SKU-QA", "grams": 500,
            "product_id": 321, "variant_id": 654, "variant_title": "Azul",
        }],
        "shipping_lines": [
            {"title": "Envío", "code": "TAURO_QA", "price": "4.00"},
            {"title": "Seguro", "code": "INSURANCE", "price": "1.00"},
        ],
    })

    assert pedido is not None
    assert pedido["pedido_externo_id"] == "990000000001"
    assert pedido["destinatario"]["direccion2"] == "Piso 2"
    assert pedido["destinatario"]["estado"] == "FL"
    assert pedido["destinatario"]["pais"] == "US"
    assert pedido["items"][0]["cantidad"] == 2
    assert pedido["items"][0]["external_product_id"] == "gid://shopify/Product/321"
    assert pedido["items"][0]["external_variant_id"] == "gid://shopify/ProductVariant/654"
    assert pedido["items"][0]["variante"] == "Azul"
    assert pedido["flete_cobrado"] == 5.0
    assert [f["codigo"] for f in pedido["flete_detalle"]] == ["TAURO_QA", "INSURANCE"]


def test_graphql_usa_version_token_y_no_rest(monkeypatch):
    from servicios import shopify_app

    visto = {}

    def post(url, **kwargs):
        visto["url"] = url
        visto.update(kwargs)
        return _Respuesta(payload={"data": {"shop": {"name": "QA"}}})

    monkeypatch.setattr(shopify_app.requests, "post", post)
    data = shopify_app._graphql(
        "tauro-qa.myshopify.com", "token-secreto", "query { shop { name } }",
    )

    assert data == {"shop": {"name": "QA"}}
    assert visto["url"].endswith("/admin/api/2026-07/graphql.json")
    assert visto["headers"]["X-Shopify-Access-Token"] == "token-secreto"
    assert visto["json"]["variables"] == {}
    assert visto["timeout"] == 25


def test_graphql_reintenta_un_throttle(monkeypatch):
    from servicios import shopify_app

    respuestas = iter([
        _Respuesta(payload={
            "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
        }),
        _Respuesta(payload={"data": {"shop": {"name": "QA"}}}),
    ])
    pausas = []
    monkeypatch.setattr(shopify_app.requests, "post", lambda *_args, **_kwargs: next(respuestas))
    monkeypatch.setattr(shopify_app.time, "sleep", pausas.append)

    assert shopify_app._graphql(
        "tauro-qa.myshopify.com", "token", "query { shop { name } }",
    ) == {"shop": {"name": "QA"}}
    assert pausas == [1.25]


def test_graphql_respeta_el_presupuesto_de_costo_shopify(monkeypatch):
    from servicios import shopify_app

    respuestas = iter([
        _Respuesta(payload={
            "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
            "extensions": {"cost": {
                "requestedQueryCost": 100,
                "throttleStatus": {"currentlyAvailable": 0, "restoreRate": 50},
            }},
        }),
        _Respuesta(payload={"data": {"shop": {"name": "QA"}}}),
    ])
    pausas = []
    monkeypatch.setattr(shopify_app.requests, "post", lambda *_args, **_kwargs: next(respuestas))
    monkeypatch.setattr(shopify_app.time, "sleep", pausas.append)

    assert shopify_app._graphql(
        "tauro-qa.myshopify.com", "token", "query { shop { name } }",
    ) == {"shop": {"name": "QA"}}
    assert pausas == [2.25]


def test_token_shopify_se_guarda_cifrado_y_se_puede_migrar(monkeypatch):
    from servicios import shopify_app

    monkeypatch.setenv("SHOPIFY_TOKEN_ENCRYPTION_KEY", "clave-qa-independiente")
    cifrado = shopify_app._cifrar_token("shpat_token-privado")

    assert cifrado.startswith("enc:v1:")
    assert "shpat_token-privado" not in cifrado
    assert shopify_app._descifrar_token(cifrado) == "shpat_token-privado"
    assert shopify_app._descifrar_token("token-legacy-plaintext") == "token-legacy-plaintext"


def test_token_cifrado_falla_cerrado_con_otra_clave(monkeypatch):
    from servicios import shopify_app

    monkeypatch.setenv("SHOPIFY_TOKEN_ENCRYPTION_KEY", "clave-original")
    cifrado = shopify_app._cifrar_token("token-qa")
    monkeypatch.setenv("SHOPIFY_TOKEN_ENCRYPTION_KEY", "clave-equivocada")
    with pytest.raises(RuntimeError, match="no se pudo abrir"):
        shopify_app._descifrar_token(cifrado)


def test_token_sigue_abriendo_al_agregar_clave_exclusiva(monkeypatch):
    from servicios import shopify_app

    monkeypatch.delenv("SHOPIFY_TOKEN_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("SHOPIFY_API_SECRET", "api-secret-estable")
    cifrado_anterior = shopify_app._cifrar_token("token-previo")

    monkeypatch.setenv("SHOPIFY_TOKEN_ENCRYPTION_KEY", "clave-exclusiva-nueva")
    assert shopify_app._descifrar_token(cifrado_anterior) == "token-previo"

    cifrado_nuevo = shopify_app._cifrar_token("token-nuevo")
    monkeypatch.setenv("SHOPIFY_TOKEN_ENCRYPTION_KEY", "clave-exclusiva-nueva")
    assert shopify_app._descifrar_token(cifrado_nuevo) == "token-nuevo"


def test_registro_webhooks_graphql_es_idempotente(monkeypatch):
    from servicios import shopify_app

    llamadas = []
    existentes = []

    def graphql(_dominio, _token, query, variables=None):
        llamadas.append((query, variables))
        if "TauroWebhookSubscriptions" in query:
            return {
                "webhookSubscriptions": {
                    "nodes": [
                        {"id": f"gid://shopify/WebhookSubscription/{i}",
                         "topic": topic, "uri": uri}
                        for i, (topic, uri) in enumerate(existentes, 1)
                    ]
                }
            }
        existentes.append((variables["topic"], variables["subscription"]["uri"]))
        return {
            "webhookSubscriptionCreate": {
                "webhookSubscription": {"id": "gid://shopify/WebhookSubscription/1"},
                "userErrors": [],
            }
        }

    monkeypatch.setattr(shopify_app, "_graphql", graphql)
    monkeypatch.setenv("BASE_URL", "https://taurosolutions.ar")

    assert shopify_app.registrar_webhooks(
        "tauro-qa.myshopify.com", "token"
    ) == SHOPIFY_WEBHOOKS
    mutaciones = [v for q, v in llamadas if "TauroWebhookCreate" in q]
    assert [v["topic"] for v in mutaciones] == [
        "ORDERS_CREATE", "ORDERS_UPDATED",
        "PRODUCTS_CREATE", "PRODUCTS_UPDATE", "PRODUCTS_DELETE",
        "INVENTORY_LEVELS_UPDATE", "INVENTORY_ITEMS_UPDATE",
        "APP_UNINSTALLED",
    ]
    assert mutaciones[0]["subscription"] == {
        "uri": "https://taurosolutions.ar/integraciones/shopify/webhook",
        "format": "JSON",
    }
    assert sum("TauroWebhookSubscriptions" in query for query, _ in llamadas) == 2


def test_tracking_vuelve_a_shopify_por_graphql(monkeypatch):
    from servicios import shopify_app

    llamadas = []
    monkeypatch.setattr(shopify_app, "instalacion", lambda _dominio: {
        "access_token": "token-qa",
    })

    def graphql(_dominio, _token, query, variables=None):
        llamadas.append((query, variables))
        if "TauroFulfillmentOrders" in query:
            return {
                "order": {
                    "fulfillments": [],
                    "fulfillmentOrders": {
                        "nodes": [
                            {"id": "gid://shopify/FulfillmentOrder/7", "status": "OPEN"},
                            {"id": "gid://shopify/FulfillmentOrder/8", "status": "CLOSED"},
                        ]
                    }
                }
            }
        return {
            "fulfillmentCreate": {
                "fulfillment": {"id": "gid://shopify/Fulfillment/9", "status": "SUCCESS"},
                "userErrors": [],
            }
        }

    monkeypatch.setattr(shopify_app, "_graphql", graphql)

    assert shopify_app.marcar_enviado(
        "tauro-qa.myshopify.com", "123", "TRACK-123", "DHL Express",
    )
    assert llamadas[0][1] == {"orderId": "gid://shopify/Order/123"}
    fulfillment = llamadas[1][1]["fulfillment"]
    assert fulfillment["lineItemsByFulfillmentOrder"] == [
        {"fulfillmentOrderId": "gid://shopify/FulfillmentOrder/7"}
    ]
    assert fulfillment["trackingInfo"] == {
        "number": "TRACK-123",
        "company": "DHL Express",
        "url": "https://www.dhl.com/global-en/home/tracking.html?tracking-id=TRACK-123",
    }
    assert fulfillment["notifyCustomer"] is True


def test_tracking_repetido_no_crea_otro_fulfillment(monkeypatch):
    from servicios import shopify_app

    llamadas = []
    monkeypatch.setattr(shopify_app, "instalacion", lambda _dominio: {
        "access_token": "token-qa",
    })

    def graphql(_dominio, _token, query, variables=None):
        llamadas.append((query, variables))
        return {
            "order": {
                "fulfillments": [{
                    "id": "gid://shopify/Fulfillment/9",
                    "status": "SUCCESS",
                    "trackingInfo": [{"number": "TRACK-123"}],
                }],
                "fulfillmentOrders": {
                    "nodes": [{"id": "gid://shopify/FulfillmentOrder/7", "status": "OPEN"}],
                },
            }
        }

    monkeypatch.setattr(shopify_app, "_graphql", graphql)

    assert shopify_app.marcar_enviado(
        "tauro-qa.myshopify.com", "123", "TRACK-123", "DHL",
    )
    assert len(llamadas) == 1


def test_tracking_rechaza_id_de_orden_malformado_antes_de_shopify(monkeypatch):
    from servicios import shopify_app

    llamadas = []
    monkeypatch.setattr(shopify_app, "instalacion", lambda _dominio: {
        "access_token": "token-qa",
    })
    monkeypatch.setattr(shopify_app, "_graphql", lambda *_args, **_kwargs: llamadas.append(True))

    assert not shopify_app.marcar_enviado(
        "tauro-qa.myshopify.com", "gid://shopify/Product/123", "TRACK-123", "DHL",
    )
    assert llamadas == []


def test_callback_sin_state_instala_pero_no_autovincula(monkeypatch):
    from endpoints import shopify

    vinculados = []
    monkeypatch.setattr(shopify, "app_configurada", lambda: True)
    monkeypatch.setattr(shopify, "validar_hmac_query", lambda _params: True)
    monkeypatch.setattr(shopify, "canjear_token", lambda *_args: {
        "access_token": "token-qa", "scope": SHOPIFY_SCOPES,
    })
    monkeypatch.setattr(shopify, "guardar_instalacion", lambda *_args: None)
    monkeypatch.setattr(shopify, "registrar_webhooks", lambda *_args: SHOPIFY_WEBHOOKS)
    monkeypatch.setattr(shopify, "vincular_cliente", lambda dominio, cliente: vinculados.append((dominio, cliente)))

    response = shopify.callback(_Request(
        query_params={
            "shop": "tauro-qa.myshopify.com",
            "code": "codigo-qa",
            "state": "state-atacante",
            "hmac": "firma-valida",
        },
        cookies={"token": "sesion-cliente-victima"},
    ))

    assert response.status_code == 200
    assert vinculados == []
    assert "shopify_state=" in response.headers.get("set-cookie", "")


def test_abrir_app_instalada_no_consume_admin_api(monkeypatch):
    from endpoints import shopify
    from servicios import shopify_app

    monkeypatch.setattr(shopify, "app_configurada", lambda: True)
    monkeypatch.setattr(shopify_app, "instalacion", lambda _dominio: {
        "access_token": "token-qa",
        "scopes": SHOPIFY_SCOPES,
        "cliente_id": "MELCIOR",
    })
    monkeypatch.setattr(
        shopify, "registrar_webhooks",
        lambda *_args: (_ for _ in ()).throw(AssertionError("no debe llamar Shopify")),
    )
    monkeypatch.setattr(shopify, "_panel_tienda", lambda *_args: "panel-local")

    response = shopify.install(_Request(
        query_params={"shop": "tauro-qa.myshopify.com"},
    ), shop="tauro-qa.myshopify.com")

    assert response == "panel-local"


def test_reautorizar_inicia_oauth_sin_consultar_admin_api(monkeypatch):
    from endpoints import shopify
    from servicios import shopify_app

    monkeypatch.setattr(shopify, "app_configurada", lambda: True)
    monkeypatch.setattr(
        shopify_app, "instalacion",
        lambda *_args: (_ for _ in ()).throw(AssertionError("no debe leer instalación")),
    )
    monkeypatch.setattr(shopify, "_redirect_oauth", lambda dominio: ("oauth", dominio))

    response = shopify.install(_Request(
        query_params={
            "shop": "tauro-qa.myshopify.com",
            "reautorizar": "1",
        },
    ), shop="tauro-qa.myshopify.com")

    assert response == ("oauth", "tauro-qa.myshopify.com")


def test_callback_con_state_verificado_puede_autovincular(monkeypatch):
    from endpoints import shopify
    from servicios import auth

    vinculados = []
    monkeypatch.setattr(shopify, "app_configurada", lambda: True)
    monkeypatch.setattr(shopify, "validar_hmac_query", lambda _params: True)
    monkeypatch.setattr(shopify, "canjear_token", lambda *_args: {
        "access_token": "token-qa", "scope": SHOPIFY_SCOPES,
    })
    monkeypatch.setattr(shopify, "guardar_instalacion", lambda *_args: None)
    monkeypatch.setattr(shopify, "registrar_webhooks", lambda *_args: SHOPIFY_WEBHOOKS)
    monkeypatch.setattr(
        "servicios.shopify_catalogo.lanzar_sincronizacion", lambda *_args: None,
    )
    monkeypatch.setattr(shopify, "vincular_cliente", lambda dominio, cliente: vinculados.append((dominio, cliente)))
    monkeypatch.setattr(auth, "validar_token", lambda _token: "MELCIOR")

    response = shopify.callback(_Request(
        query_params={
            "shop": "tauro-qa.myshopify.com",
            "code": "codigo-qa",
            "state": "state-propio",
            "hmac": "firma-valida",
        },
        cookies={"shopify_state": "state-propio", "token": "sesion-cliente"},
    ))

    assert response.status_code == 200
    assert vinculados == [("tauro-qa.myshopify.com", "MELCIOR")]
    assert "shopify_state=" in response.headers.get("set-cookie", "")


def test_callback_rechaza_scopes_incompletos_sin_guardar(monkeypatch):
    from endpoints import shopify

    guardadas = []
    monkeypatch.setattr(shopify, "app_configurada", lambda: True)
    monkeypatch.setattr(shopify, "validar_hmac_query", lambda _params: True)
    monkeypatch.setattr(shopify, "canjear_token", lambda *_args: {
        "access_token": "token-qa", "scope": "read_orders",
    })
    monkeypatch.setattr(shopify, "guardar_instalacion", lambda *_args: guardadas.append(True))

    response = shopify.callback(_Request(
        query_params={
            "shop": "tauro-qa.myshopify.com",
            "code": "codigo-qa",
            "state": "state-propio",
            "hmac": "firma-valida",
        },
        cookies={"shopify_state": "state-propio"},
    ))

    assert response.status_code == 502
    assert guardadas == []


def test_callback_no_declara_exito_si_falta_un_webhook(monkeypatch):
    from endpoints import shopify

    desvinculadas = []
    vinculadas = []
    monkeypatch.setattr(shopify, "app_configurada", lambda: True)
    monkeypatch.setattr(shopify, "validar_hmac_query", lambda _params: True)
    monkeypatch.setattr(shopify, "canjear_token", lambda *_args: {
        "access_token": "token-qa", "scope": SHOPIFY_SCOPES,
    })
    monkeypatch.setattr(shopify, "guardar_instalacion", lambda *_args: None)
    monkeypatch.setattr(shopify, "registrar_webhooks", lambda *_args: SHOPIFY_WEBHOOKS[:-1])
    monkeypatch.setattr(shopify, "desinstalar", desvinculadas.append)
    monkeypatch.setattr(shopify, "vincular_cliente", lambda *_args: vinculadas.append(True))

    response = shopify.callback(_Request(
        query_params={
            "shop": "tauro-qa.myshopify.com",
            "code": "codigo-qa",
            "state": "state-propio",
            "hmac": "firma-valida",
        },
        cookies={"shopify_state": "state-propio", "token": "sesion-cliente"},
    ))

    assert response.status_code == 503
    assert desvinculadas == []
    assert vinculadas == []


def test_callback_timeout_verificando_webhooks_no_borra_instalacion(monkeypatch):
    from endpoints import shopify

    borradas = []
    monkeypatch.setattr(shopify, "app_configurada", lambda: True)
    monkeypatch.setattr(shopify, "validar_hmac_query", lambda _params: True)
    monkeypatch.setattr(shopify, "canjear_token", lambda *_args: {
        "access_token": "token-qa", "scope": SHOPIFY_SCOPES,
    })
    monkeypatch.setattr(shopify, "guardar_instalacion", lambda *_args: None)

    def falla_verificacion(*_args):
        raise shopify.ShopifyWebhookVerificationError("timeout")

    monkeypatch.setattr(shopify, "registrar_webhooks", falla_verificacion)
    monkeypatch.setattr(shopify, "desinstalar", borradas.append)

    response = shopify.callback(_Request(
        query_params={
            "shop": "tauro-qa.myshopify.com",
            "code": "codigo-qa",
            "state": "state-propio",
            "hmac": "firma-valida",
        },
        cookies={"shopify_state": "state-propio"},
    ))

    assert response.status_code == 503
    assert borradas == []


def test_callback_state_distinto_no_canjea_token(monkeypatch):
    from endpoints import shopify

    canjes = []
    monkeypatch.setattr(shopify, "app_configurada", lambda: True)
    monkeypatch.setattr(shopify, "validar_hmac_query", lambda _params: True)
    monkeypatch.setattr(shopify, "canjear_token", lambda *_args: canjes.append(True))

    response = shopify.callback(_Request(
        query_params={
            "shop": "tauro-qa.myshopify.com",
            "code": "codigo-qa",
            "state": "state-ajeno",
            "hmac": "firma-valida",
        },
        cookies={"shopify_state": "state-propio"},
    ))

    assert response.status_code == 400
    assert canjes == []


def test_webhook_nuevo_y_repetido_dispara_armado_una_sola_vez(monkeypatch):
    from endpoints import integraciones
    from servicios import integraciones_tienda, solicitud_automatica

    cuerpo = json.dumps({
        "id": 990000000001,
        "name": "#QA-1",
        "financial_status": "paid",
        "shipping_address": {"address1": "1 Test", "country_code": "US"},
        "line_items": [{"title": "QA", "quantity": 1, "sku": "SKU-QA"}],
    }).encode()
    request = _Request(cuerpo, {
        "x-shopify-shop-domain": "tauro-qa.myshopify.com",
        "x-shopify-hmac-sha256": "firma-valida",
        "x-shopify-topic": "orders/create",
    })
    resultados_guardar = iter([True, False])
    armados = []

    monkeypatch.setattr(integraciones, "tienda_por_dominio", lambda _dominio: {
        "id": 7, "cliente_id": "MELCIOR", "plataforma": "shopify", "secreto": "secret",
    })
    monkeypatch.setattr(integraciones, "verificar_hmac_shopify", lambda *_args: True)
    monkeypatch.setattr(integraciones, "guardar_pedido", lambda *_args: next(resultados_guardar))
    monkeypatch.setattr(integraciones_tienda, "id_de_pedido", lambda *_args: 99)
    monkeypatch.setattr(solicitud_automatica, "intentar_en_segundo_plano", armados.append)

    primera = asyncio.run(integraciones.shopify_webhook(request))
    segunda = asyncio.run(integraciones.shopify_webhook(request))

    assert primera == {"ok": True, "nuevo": True}
    assert segunda == {"ok": True, "nuevo": False}
    assert armados == [99]


def test_webhook_inventario_se_encola_idempotente_y_responde_rapido(monkeypatch):
    from endpoints import integraciones
    from servicios import shopify_app, shopify_catalogo

    cuerpo = json.dumps({
        "inventory_item_id": 300000000001,
        "location_id": 400000000001,
        "available": 7,
    }).encode()
    request = _Request(cuerpo, {
        "x-shopify-shop-domain": "tauro-qa.myshopify.com",
        "x-shopify-hmac-sha256": "firma-valida",
        "x-shopify-topic": "inventory_levels/update",
        "x-shopify-webhook-id": "wh-stock-1",
    })
    resultados = iter([True, False])
    lanzados = []
    recibidos = []

    monkeypatch.setattr(integraciones, "tienda_por_dominio", lambda _dominio: {
        "id": 7, "cliente_id": "MELCIOR", "plataforma": "shopify",
        "secreto": "oauth:shopify-app",
    })
    monkeypatch.setattr(integraciones, "verificar_hmac_shopify", lambda *_args: True)
    monkeypatch.setenv("SHOPIFY_API_SECRET", "secreto-app-qa")
    monkeypatch.setattr(shopify_app, "instalacion", lambda _dominio: {"access_token": "token"})

    def encolar(*args):
        recibidos.append(args)
        return next(resultados)

    monkeypatch.setattr(shopify_catalogo, "encolar_evento", encolar)
    monkeypatch.setattr(shopify_catalogo, "lanzar_procesamiento_eventos", lambda: lanzados.append(True))

    primera = asyncio.run(integraciones.shopify_webhook(request))
    segunda = asyncio.run(integraciones.shopify_webhook(request))

    assert primera == {"ok": True, "encolado": True, "duplicado": False}
    assert segunda == {"ok": True, "encolado": False, "duplicado": True}
    assert len(recibidos) == 2
    assert recibidos[0][:3] == ("wh-stock-1", "tauro-qa.myshopify.com", "inventory_levels/update")
    assert lanzados == [True]


def test_firma_invalida_no_parsea_ni_guarda(monkeypatch):
    from endpoints import integraciones

    guardados = []
    monkeypatch.setattr(integraciones, "tienda_por_dominio", lambda _dominio: {
        "id": 7, "cliente_id": "MELCIOR", "plataforma": "shopify", "secreto": "secret",
    })
    monkeypatch.setattr(integraciones, "verificar_hmac_shopify", lambda *_args: False)
    monkeypatch.setattr(integraciones, "guardar_pedido", lambda *_args: guardados.append(True))

    response = asyncio.run(integraciones.shopify_webhook(_Request(
        b'{"id":1}',
        {
            "x-shopify-shop-domain": "tauro-qa.myshopify.com",
            "x-shopify-hmac-sha256": "falsa",
            "x-shopify-topic": "orders/create",
        },
    )))

    assert response.status_code == 401
    assert guardados == []


def test_coleccion_postman_es_segura_y_no_contiene_secretos():
    raiz = Path(__file__).resolve().parents[1]
    coleccion_path = raiz / "docs/postman/TAURO-Shopify.postman_collection.json"
    entorno_path = raiz / "docs/postman/TAURO-Shopify.local.postman_environment.json"
    coleccion = json.loads(coleccion_path.read_text())
    entorno = json.loads(entorno_path.read_text())

    serializado = json.dumps({"coleccion": coleccion, "entorno": entorno})
    assert "shpat_" not in serializado
    assert "@gmail.com" not in serializado
    assert "allow_tauro_webhook_writes') !== 'true'" in serializado
    assert next(v for v in entorno["values"] if v["key"] == "allow_tauro_webhook_writes")["value"] == "false"
    assert next(v for v in entorno["values"] if v["key"] == "shopify_access_token")["value"] == ""
    assert next(v for v in entorno["values"] if v["key"] == "tauro_api_key")["value"] == ""
    assert not any(v["key"] == "shopify_api_secret" for v in entorno["values"])

    diagnosticos = coleccion["item"][0]["item"]
    graphql = next(
        item["request"] for item in diagnosticos
        if item["name"] == "Shopify · Probar token con GraphQL"
    )
    assert graphql["method"] == "POST"
    assert graphql["url"].endswith("/graphql.json")
    assert "X-Shopify-Access-Token" in {h["key"] for h in graphql["header"]}

    webhooks = next(
        carpeta["item"] for carpeta in coleccion["item"]
        if carpeta["name"] == "10 · Webhooks controlados"
    )
    validos = [w for w in webhooks if "firma válida" in w["name"] or "idempotencia" in w["name"]]
    assert validos
    for request in validos:
        scripts = "\n".join(
            linea
            for evento in request["event"] if evento["listen"] == "prerequest"
            for linea in evento["script"]["exec"]
        )
        assert "pm.execution.skipRequest()" in scripts
        assert "pm.vault.get('tauro-shopify-dev-app-secret')" in scripts
        assert "HmacSHA256" in scripts
