from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


SHOPIFY_SCOPES = (
    "read_orders,read_products,read_inventory,read_locations,"
    "write_merchant_managed_fulfillment_orders"
)
SHOPIFY_WEBHOOKS = [
    "app/uninstalled",
    "orders/create", "orders/updated", "orders/cancelled",
    "products/create", "products/update", "products/delete",
    "inventory_levels/update", "inventory_items/update",
]


def test_schema_readiness_es_idempotente_y_falla_cerrado():
    schema = (Path(__file__).resolve().parents[1] / "sql" / "schema.sql").read_text(
        encoding="utf-8",
    )

    assert "ADD COLUMN IF NOT EXISTS webhooks_ready" in schema
    assert "ADD COLUMN IF NOT EXISTS webhooks_verified_at" in schema
    assert "AND i.webhooks_ready = FALSE" in schema
    assert "SET activa = FALSE" in schema


class _CursorTienda:
    def __init__(self, propietario_inicial: str = "TEST_CLIENT"):
        self.propietario = propietario_inicial
        self.params = None
        self.query = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.query = query
        self.params = params or {}
        if isinstance(self.params, dict) and (
            self.propietario == self.params.get("cliente")
            or self.params.get("reasignar")
        ):
            self.propietario = self.params["cliente"]

    def fetchone(self):
        return {"id": 7, "cliente_id": self.propietario}


class _ConnTienda:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1


def test_scopes_shopify_incluyen_ubicaciones_para_nombres_de_deposito():
    from servicios.shopify_app import SCOPES

    assert set(SCOPES.split(",")) == set(SHOPIFY_SCOPES.split(","))
    assert "read_locations" in SCOPES.split(",")


def test_app_publica_recibe_oauth_y_conserva_webhooks_legados(monkeypatch):
    from servicios import shopify_app

    monkeypatch.setenv("SHOPIFY_PUBLIC_API_KEY", "client-publico")
    monkeypatch.setenv("SHOPIFY_PUBLIC_API_SECRET", "secret-publico")
    monkeypatch.setenv("SHOPIFY_API_KEY", "client-legado")
    monkeypatch.setenv("SHOPIFY_API_SECRET", "secret-legado")

    assert shopify_app.app_configurada()
    assert shopify_app.api_key_publica() == "client-publico"
    assert "client_id=client-publico" in shopify_app.url_instalacion(
        "tauro-qa.myshopify.com", "state-qa",
    )

    cuerpo = b'{"id":1}'
    firma_publica = base64.b64encode(
        hmac.new(b"secret-publico", cuerpo, hashlib.sha256).digest()
    ).decode()
    firma_legada = base64.b64encode(
        hmac.new(b"secret-legado", cuerpo, hashlib.sha256).digest()
    ).decode()

    assert shopify_app.cliente_app_para_webhook(cuerpo, firma_publica) == "client-publico"
    assert shopify_app.cliente_app_para_webhook(cuerpo, firma_legada) == "client-legado"
    assert not shopify_app.firma_valida_webhook_app(
        cuerpo, firma_legada, "client-publico",
    )


def test_credencial_publica_incompleta_no_se_mezcla_con_legada(monkeypatch):
    from servicios import shopify_app

    monkeypatch.setenv("SHOPIFY_PUBLIC_API_KEY", "client-publico")
    monkeypatch.delenv("SHOPIFY_PUBLIC_API_SECRET", raising=False)
    monkeypatch.setenv("SHOPIFY_API_KEY", "client-legado")
    monkeypatch.setenv("SHOPIFY_API_SECRET", "secret-legado")

    assert shopify_app.api_key_publica() == "client-publico"
    assert not shopify_app.app_configurada()


def test_fila_sin_client_id_se_trata_como_legada_y_no_como_wildcard(monkeypatch):
    from servicios import shopify_app

    monkeypatch.setenv("SHOPIFY_PUBLIC_API_KEY", "client-publico")
    monkeypatch.setenv("SHOPIFY_PUBLIC_API_SECRET", "secret-publico")
    monkeypatch.setenv("SHOPIFY_API_KEY", "client-legado")
    monkeypatch.setenv("SHOPIFY_API_SECRET", "secret-legado")

    assert shopify_app._client_id_instalacion_efectivo(None) == "client-legado"


def test_desinstalacion_legada_no_borra_token_de_app_publica(monkeypatch):
    from servicios import integraciones_tienda, shopify_app

    class _Cursor:
        borrado = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, _params=None):
            if query.lstrip().startswith("DELETE"):
                self.borrado = True

        def fetchone(self):
            return {"app_client_id": "client-publico"}

    cursor = _Cursor()
    conn = _ConnTienda(cursor)
    monkeypatch.setattr(shopify_app, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(shopify_app, "get_conn", lambda: conn)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)

    assert not shopify_app.desinstalar(
        "tauro-qa.myshopify.com", "client-legado",
    )
    assert not cursor.borrado
    assert conn.commits == 0


def test_shop_redact_legado_no_borra_datos_de_app_publica(monkeypatch):
    from endpoints import shopify
    from servicios import integraciones_tienda, shopify_app

    borrados = []
    desinstalaciones = []
    monkeypatch.setattr(
        shopify_app, "cliente_app_para_webhook", lambda *_args: "client-legado",
    )
    monkeypatch.setattr(
        shopify_app, "cliente_app_instalada", lambda _dominio: "client-publico",
    )
    monkeypatch.setattr(
        shopify_app, "registrar_shop_redact_pendiente", lambda *_args: True,
    )
    monkeypatch.setattr(shopify, "confirmar_shop_redact", lambda *_args: False)
    monkeypatch.setattr(
        integraciones_tienda, "borrar_datos_tienda", lambda *_args: borrados.append(True),
    )
    monkeypatch.setattr(
        shopify, "desinstalar", lambda *_args: desinstalaciones.append(True),
    )

    response = asyncio.run(shopify.gdpr_shop_redact(_Request(
        b'{"shop_id":1,"shop_domain":"tauro-qa.myshopify.com"}',
        {
            "x-shopify-shop-domain": "tauro-qa.myshopify.com",
            "x-shopify-hmac-sha256": "firma-legada",
            "x-shopify-topic": "shop/redact",
        },
    )))

    assert response == {"ok": True, "estado": "VERIFICAR_GENERACION"}
    assert borrados == []
    assert desinstalaciones == []


def test_shop_redact_fallido_devuelve_503_para_que_shopify_reintente(monkeypatch):
    from endpoints import shopify
    from servicios import shopify_app

    monkeypatch.setattr(
        shopify_app, "cliente_app_para_webhook", lambda *_args: "client-publico",
    )
    monkeypatch.setattr(
        shopify_app, "cliente_app_instalada",
        lambda _dominio: (_ for _ in ()).throw(RuntimeError("db caída")),
    )

    response = asyncio.run(shopify.gdpr_shop_redact(_Request(
        b'{"shop_id":1,"shop_domain":"tauro-qa.myshopify.com"}',
        {
            "x-shopify-shop-domain": "tauro-qa.myshopify.com",
            "x-shopify-hmac-sha256": "firma-publica",
            "x-shopify-topic": "shop/redact",
        },
    )))

    assert response.status_code == 503


def test_customer_redact_sin_ordenes_no_anonimiza_toda_la_tienda(monkeypatch):
    from servicios import integraciones_tienda

    monkeypatch.setattr(
        integraciones_tienda, "_ensure_tablas",
        lambda: (_ for _ in ()).throw(AssertionError("no debe tocar la base")),
    )

    assert integraciones_tienda.anonimizar_pedidos(
        "tauro-qa.myshopify.com", [],
    ) == 0


def test_customer_redact_tambien_elimina_pedido_huerfano(monkeypatch):
    from servicios import integraciones_tienda

    class _CursorRedact:
        def __init__(self):
            self.consultas = []
            self.rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            self.consultas.append((" ".join(query.split()), params))

        def fetchall(self):
            # Primera consulta: vínculo legado; segunda: linaje durable.
            return [{"solicitud_id": 77, "id": 88}]

    cursor = _CursorRedact()
    conn = _ConnTienda(cursor)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "get_conn", lambda: conn)

    dominio = "tauro-qa.myshopify.com"
    pedidos = ["101", "102"]
    assert integraciones_tienda.anonimizar_pedidos(dominio, pedidos) == 7
    sql = "\n".join(query for query, _params in cursor.consultas)

    assert "UPDATE pedidos_tienda" in sql
    assert "UPDATE solicitudes_guia" in sql
    assert "UPDATE envios" in sql
    assert "UPDATE recolecciones" in sql
    assert "tracking = NULL" in sql
    assert "descripcion = NULL" in sql
    assert "direccion = NULL" in sql
    assert "instrucciones = NULL" in sql
    assert "ubicacion = NULL" in sql
    assert "error_operativo = NULL" in sql
    assert "label_pdf = NULL" in sql
    assert "THEN 'CANCELADO'" in sql
    assert "origen_pedido_externo_id = ANY" in sql
    assert "DELETE FROM direcciones" in sql
    assert "DELETE FROM pedidos_huerfanos" in sql
    assert "DELETE FROM shopify_huerfanos_cancelados" in sql
    assert cursor.consultas[-1][1] == (dominio, pedidos)
    assert conn.commits == 1


def test_shop_redact_purga_todos_los_datos_del_dominio(monkeypatch):
    from servicios import integraciones_tienda

    class _CursorBorrado:
        def __init__(self):
            self.consultas = []
            self.rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            self.consultas.append((" ".join(query.split()), params))

        def fetchall(self):
            return [{"solicitud_id": 77, "id": 88}]

    cursor = _CursorBorrado()
    conn = _ConnTienda(cursor)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "get_conn", lambda: conn)

    dominio = "tauro-qa.myshopify.com"
    assert integraciones_tienda.borrar_datos_tienda(dominio) == 12
    sql = "\n".join(query for query, _params in cursor.consultas)

    for tabla in (
        "pedidos_huerfanos",
        "shopify_huerfanos_cancelados",
        "shopify_webhook_eventos",
        "shopify_sync_estado",
        "config_envio_tienda",
        "producto_inventario_ubicaciones",
        "productos",
        "tiendas_conectadas",
    ):
        assert f"DELETE FROM {tabla}" in sql
    assert "DELETE FROM shopify_gdpr_solicitudes" not in sql
    assert "UPDATE solicitudes_guia" in sql
    assert "UPDATE envios" in sql
    assert "UPDATE recolecciones" in sql
    assert "DELETE FROM direcciones" in sql
    assert sql.index("UPDATE solicitudes_guia") < sql.index("DELETE FROM tiendas_conectadas")
    assert all(
        params == (dominio,)
        for query, params in cursor.consultas
        if query.startswith("DELETE FROM")
    )
    assert conn.commits == 1


def test_conexion_manual_no_reasigna_tienda_de_otro_cliente(monkeypatch):
    from servicios import integraciones_tienda

    cursor = _CursorTienda("TEST_CLIENT")
    conn = _ConnTienda(cursor)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "get_conn", lambda: conn)

    resultado = integraciones_tienda.conectar_tienda(
        "PESCAJACKS", "shopify", "pesca.myshopify.com", "secreto-seguro",
    )

    assert resultado["ok"] is False
    assert cursor.propietario == "TEST_CLIENT"
    assert cursor.params["reasignar"] is False


def test_oauth_confirmado_reasigna_tienda_historica(monkeypatch):
    from servicios import integraciones_tienda

    cursor = _CursorTienda("TEST_CLIENT")
    conn = _ConnTienda(cursor)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "get_conn", lambda: conn)

    resultado = integraciones_tienda.conectar_tienda(
        "PESCAJACKS", "shopify", "pesca.myshopify.com", "oauth:shopify-app",
        reasignar_confirmado=True,
    )

    assert resultado == {"ok": True, "tienda_id": 7}
    assert cursor.propietario == "PESCAJACKS"
    assert cursor.params["reasignar"] is True


def test_migracion_base_crea_huerfanos_antes_de_leerlos(monkeypatch):
    from servicios import integraciones_tienda

    cursor = _CursorTienda()
    conn = _ConnTienda(cursor)
    monkeypatch.setattr(integraciones_tienda, "_tablas_listas", False)
    monkeypatch.setattr(integraciones_tienda, "get_conn", lambda: conn)

    integraciones_tienda._ensure_tablas()

    assert "CREATE TABLE IF NOT EXISTS pedidos_huerfanos" in cursor.query
    assert "CREATE TABLE IF NOT EXISTS config_envio_tienda" in cursor.query
    assert "ix_pedidos_huerfanos_dominio_fecha" in cursor.query
    assert conn.commits == 1


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

    monkeypatch.setattr(
        shopify_app, "_token_admin_vigente",
        lambda _dominio, token, **_kwargs: token,
    )
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
    monkeypatch.setattr(
        shopify_app, "_token_admin_vigente",
        lambda _dominio, token, **_kwargs: token,
    )
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
    monkeypatch.setattr(
        shopify_app, "_token_admin_vigente",
        lambda _dominio, token, **_kwargs: token,
    )
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


def test_canje_oauth_solicita_tokens_offline_expirables(monkeypatch):
    from servicios import shopify_app

    visto = {}
    monkeypatch.setattr(
        shopify_app, "_credenciales_publicas", lambda: ("client-id", "client-secret"),
    )

    def post(url, **kwargs):
        visto["url"] = url
        visto.update(kwargs)
        return _Respuesta(payload={
            "access_token": "access-nuevo",
            "refresh_token": "refresh-nuevo",
            "expires_in": 3600,
            "refresh_token_expires_in": 7776000,
        })

    monkeypatch.setattr(shopify_app.requests, "post", post)

    respuesta = shopify_app.canjear_token(
        "tauro-qa.myshopify.com", "codigo-un-solo-uso",
    )

    assert respuesta["refresh_token"] == "refresh-nuevo"
    assert visto["url"].endswith("/admin/oauth/access_token")
    assert visto["data"] == {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "code": "codigo-un-solo-uso",
        "expiring": "1",
    }


def test_canje_oauth_200_incompleto_se_rechaza_sin_filtrar_payload(monkeypatch, capsys):
    from servicios import shopify_app

    monkeypatch.setattr(
        shopify_app, "_credenciales_publicas", lambda: ("client-id", "client-secret"),
    )
    monkeypatch.setattr(
        shopify_app.requests,
        "post",
        lambda *_args, **_kwargs: _Respuesta(payload={
            "access_token": "access-super-secreto",
            "expires_in": 3600,
        }),
    )

    assert shopify_app.canjear_token(
        "tauro-qa.myshopify.com", "codigo",
    ) is None
    assert "super-secreto" not in capsys.readouterr().out


class _CursorTokens:
    def __init__(self, respuestas=()):
        self._respuestas = iter(respuestas)
        self.ejecutadas = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.ejecutadas.append((" ".join(str(sql).split()), params))

    def fetchone(self):
        return next(self._respuestas)


class _ConnTokens:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1


def test_guardar_instalacion_persiste_par_cifrado_y_vencimientos(monkeypatch):
    from servicios import integraciones_tienda, shopify_app

    cursor = _CursorTokens([{}, {}])
    conn = _ConnTokens(cursor)
    monkeypatch.setattr(shopify_app, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(shopify_app, "get_conn", lambda: conn)
    monkeypatch.setattr(shopify_app, "_cifrar_token", lambda token: f"enc:{token}")
    monkeypatch.setattr(
        shopify_app, "_credenciales_publicas", lambda: ("client-id", "client-secret"),
    )
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "_bloquear_dominio_shopify", lambda *_: None)

    antes = datetime.now(timezone.utc)
    shopify_app.guardar_instalacion(
        "tauro-qa.myshopify.com",
        "access-inicial",
        "read_orders",
        cliente_claim="",
        refresh_token="refresh-inicial",
        expires_in=3600,
        refresh_token_expires_in=7776000,
    )
    despues = datetime.now(timezone.utc)

    insert = next(
        (sql, params) for sql, params in cursor.ejecutadas
        if "INSERT INTO shopify_instalaciones" in sql
    )
    insert_sql, insert_params = insert
    assert insert_params[1:3] == ("enc:access-inicial", "enc:refresh-inicial")
    assert antes + timedelta(seconds=3600) <= insert_params[3] <= despues + timedelta(seconds=3600)
    assert antes + timedelta(seconds=7776000) <= insert_params[4] <= despues + timedelta(seconds=7776000)
    assert insert_params[-1] == ""
    assert "FALSE, NULL" in insert_sql
    assert "webhooks_ready = FALSE" in insert_sql
    assert conn.commits == 1


def test_refresh_expirado_se_rota_bajo_lock_y_se_persiste_atomicamente(monkeypatch):
    from servicios import integraciones_tienda, shopify_app

    ahora = datetime.now(timezone.utc)
    cursor = _CursorTokens([{
        "id": 9,
        "access_token": "enc:access-viejo",
        "refresh_token": "enc:refresh-viejo",
        "access_token_expires_at": ahora - timedelta(seconds=1),
        "refresh_token_expires_at": ahora + timedelta(days=30),
        "token_reauth_required": False,
        "app_client_id": "client-id",
        "install_generation": "gen-actual",
        "webhooks_ready": False,
    }])
    conn = _ConnTokens(cursor)
    eventos = []
    pedido = {}

    monkeypatch.setattr(shopify_app, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(shopify_app, "get_conn", lambda: conn)
    monkeypatch.setattr(
        integraciones_tienda, "_bloquear_dominio_shopify",
        lambda _cur, dominio: eventos.append(("lock", dominio)),
    )
    monkeypatch.setattr(
        shopify_app, "_credenciales_para_client_id",
        lambda _client_id: ("client-id", "client-secret"),
    )
    monkeypatch.setattr(
        shopify_app, "_descifrar_token", lambda token: token.removeprefix("enc:"),
    )
    monkeypatch.setattr(shopify_app, "_cifrar_token", lambda token: f"enc:{token}")

    def post(url, **kwargs):
        eventos.append(("post", url))
        pedido.update(kwargs)
        return _Respuesta(payload={
            "access_token": "access-rotado",
            "refresh_token": "refresh-rotado",
            "expires_in": 3600,
            "refresh_token_expires_in": 7776000,
        })

    monkeypatch.setattr(shopify_app.requests, "post", post)

    assert shopify_app._token_admin_vigente(
        "tauro-qa.myshopify.com",
        "access-obsoleto",
        permitir_pendiente_webhooks=True,
    ) == "access-rotado"
    assert [evento[0] for evento in eventos] == ["lock", "post"]
    assert pedido["data"] == {
        "grant_type": "refresh_token",
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "refresh-viejo",
    }
    update = next(
        params for sql, params in cursor.ejecutadas
        if "SET access_token = %s" in sql
    )
    assert update[0:2] == ("enc:access-rotado", "enc:refresh-rotado")
    assert update[-2:] == (9, "gen-actual")
    assert conn.commits == 1


def test_admin_api_normal_no_usa_token_de_generacion_pendiente(monkeypatch):
    from servicios import integraciones_tienda, shopify_app

    ahora = datetime.now(timezone.utc)
    cursor = _CursorTokens([{
        "id": 15,
        "access_token": "enc:access-pendiente",
        "refresh_token": "enc:refresh-pendiente",
        "access_token_expires_at": ahora + timedelta(hours=1),
        "refresh_token_expires_at": ahora + timedelta(days=30),
        "token_reauth_required": False,
        "app_client_id": "client-id",
        "install_generation": "gen-pendiente",
        "webhooks_ready": False,
    }])
    monkeypatch.setattr(shopify_app, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(shopify_app, "get_conn", lambda: _ConnTokens(cursor))
    monkeypatch.setattr(integraciones_tienda, "_bloquear_dominio_shopify", lambda *_: None)
    monkeypatch.setattr(
        shopify_app, "_descifrar_token",
        lambda _token: (_ for _ in ()).throw(AssertionError("no debe abrir el token")),
    )

    assert shopify_app._token_admin_vigente(
        "tauro-qa.myshopify.com",
    ) is None


def test_refresh_invalido_falla_cerrado_sin_filtrar_tokens(monkeypatch, capsys):
    from servicios import integraciones_tienda, shopify_app

    ahora = datetime.now(timezone.utc)
    cursor = _CursorTokens([{
        "id": 10,
        "access_token": "enc:access-super-secreto",
        "refresh_token": "enc:refresh-super-secreto",
        "access_token_expires_at": ahora - timedelta(seconds=1),
        "refresh_token_expires_at": ahora + timedelta(days=30),
        "token_reauth_required": False,
        "app_client_id": "client-id",
        "install_generation": "gen-segura",
        "webhooks_ready": True,
    }])
    conn = _ConnTokens(cursor)

    monkeypatch.setattr(shopify_app, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(shopify_app, "get_conn", lambda: conn)
    monkeypatch.setattr(integraciones_tienda, "_bloquear_dominio_shopify", lambda *_: None)
    monkeypatch.setattr(
        shopify_app, "_credenciales_para_client_id",
        lambda _client_id: ("client-id", "client-secret-super-secreto"),
    )
    monkeypatch.setattr(
        shopify_app, "_descifrar_token", lambda token: token.removeprefix("enc:"),
    )
    monkeypatch.setattr(
        shopify_app.requests, "post",
        lambda *_args, **_kwargs: _Respuesta(
            status_code=400,
            payload={"error_description": "refresh-super-secreto inválido"},
        ),
    )

    assert shopify_app._token_admin_vigente("tauro-qa.myshopify.com") is None
    invalida = next(
        (sql, params) for sql, params in cursor.ejecutadas
        if "token_reauth_required = TRUE" in sql
    )
    assert invalida[1] == (10, "gen-segura")
    assert "access_token = ''" in invalida[0]
    assert "refresh_token = NULL" in invalida[0]
    salida = capsys.readouterr().out
    assert "super-secreto" not in salida
    assert "HTTP 400" in salida
    assert conn.commits == 1


def test_instalacion_no_expone_refresh_y_reauth_bloquea_access(monkeypatch):
    from servicios import shopify_app

    cursor = _CursorTokens([{
        "id": 11,
        "dominio": "tauro-qa.myshopify.com",
        "access_token": "enc:access-no-debe-salir",
        "refresh_token": "enc:refresh-no-debe-salir",
        "token_reauth_required": True,
        "app_client_id": "client-id",
        "install_generation": "gen-1",
    }])
    monkeypatch.setattr(shopify_app, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(shopify_app, "get_conn", lambda: _ConnTokens(cursor))

    resultado = shopify_app.instalacion("tauro-qa.myshopify.com")

    assert resultado["access_token"] == ""
    assert "refresh_token" not in resultado
    assert "access_token_expires_at" not in resultado
    assert "refresh_token_expires_at" not in resultado
    assert resultado["token_rotativo"] is False
    assert resultado["install_generation"] == "gen-1"


def test_instalacion_pendiente_no_expone_access_a_consumidores(monkeypatch):
    from servicios import shopify_app

    ahora = datetime.now(timezone.utc)
    cursor = _CursorTokens([{
        "id": 14,
        "dominio": "tauro-qa.myshopify.com",
        "access_token": "enc:access-pendiente",
        "refresh_token": "enc:refresh-pendiente",
        "access_token_expires_at": ahora + timedelta(hours=1),
        "refresh_token_expires_at": ahora + timedelta(days=30),
        "token_reauth_required": False,
        "app_client_id": "client-id",
        "install_generation": "gen-pendiente",
        "webhooks_ready": False,
    }])
    monkeypatch.setattr(shopify_app, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(shopify_app, "get_conn", lambda: _ConnTokens(cursor))
    monkeypatch.setattr(
        shopify_app, "_descifrar_token",
        lambda _token: (_ for _ in ()).throw(AssertionError("no debe abrir el token")),
    )

    resultado = shopify_app.instalacion("tauro-qa.myshopify.com")

    assert resultado["webhooks_ready"] is False
    assert resultado["access_token"] == ""
    assert resultado["token_rotativo"] is True
    assert "refresh_token" not in resultado


def test_confirmar_webhooks_habilita_generacion_y_binding_atomicamente(monkeypatch):
    from servicios import integraciones_tienda, shopify_app

    cursor = _CursorTokens([
        {"id": 14, "cliente_id": "MELCIOR"},
        {"id": 7},
        {"id": 14},
    ])
    conn = _ConnTokens(cursor)
    monkeypatch.setattr(shopify_app, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(shopify_app, "get_conn", lambda: conn)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "_bloquear_dominio_shopify", lambda *_: None)

    assert shopify_app.confirmar_webhooks_verificados(
        "tauro-qa.myshopify.com", "gen-actual", SHOPIFY_WEBHOOKS,
    )

    sql = "\n".join(query for query, _params in cursor.ejecutadas)
    assert "UPDATE tiendas_conectadas SET activa = TRUE" in sql
    assert "SET webhooks_ready = TRUE" in sql
    assert ("tauro-qa.myshopify.com", "gen-actual") in [
        params for _query, params in cursor.ejecutadas
    ]
    assert conn.commits == 1


def test_confirmar_webhooks_rechaza_subset_sin_tocar_db(monkeypatch):
    from servicios import shopify_app

    monkeypatch.setattr(
        shopify_app, "get_conn",
        lambda: (_ for _ in ()).throw(AssertionError("no debe abrir la DB")),
    )

    assert not shopify_app.confirmar_webhooks_verificados(
        "tauro-qa.myshopify.com", "gen-actual", SHOPIFY_WEBHOOKS[:-1],
    )


def test_confirmar_webhooks_callback_viejo_no_habilita_generacion_nueva(monkeypatch):
    from servicios import integraciones_tienda, shopify_app

    cursor = _CursorTokens([None])
    conn = _ConnTokens(cursor)
    monkeypatch.setattr(shopify_app, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(shopify_app, "get_conn", lambda: conn)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "_bloquear_dominio_shopify", lambda *_: None)

    assert not shopify_app.confirmar_webhooks_verificados(
        "tauro-qa.myshopify.com", "gen-anterior", SHOPIFY_WEBHOOKS,
    )
    assert all(
        "SET webhooks_ready = TRUE" not in sql
        for sql, _params in cursor.ejecutadas
    )
    assert conn.commits == 0


def test_token_publico_sin_par_rotativo_exige_reautorizacion(monkeypatch):
    from servicios import integraciones_tienda, shopify_app

    cursor = _CursorTokens([{
        "id": 12,
        "access_token": "enc:access-publico",
        "refresh_token": None,
        "access_token_expires_at": None,
        "refresh_token_expires_at": None,
        "token_reauth_required": False,
        "app_client_id": "client-publico",
        "install_generation": "gen-publica",
        "webhooks_ready": True,
    }])
    conn = _ConnTokens(cursor)
    monkeypatch.setattr(shopify_app, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(shopify_app, "get_conn", lambda: conn)
    monkeypatch.setattr(shopify_app, "api_key_publica", lambda: "client-publico")
    monkeypatch.setattr(integraciones_tienda, "_bloquear_dominio_shopify", lambda *_: None)

    assert shopify_app._token_admin_vigente("tauro-qa.myshopify.com") is None
    assert any(
        "token_reauth_required = TRUE" in sql
        for sql, _params in cursor.ejecutadas
    )
    assert conn.commits == 1


def test_token_permanente_de_app_historica_sigue_operativo(monkeypatch):
    from servicios import integraciones_tienda, shopify_app

    cursor = _CursorTokens([{
        "id": 13,
        "access_token": "enc:access-legado",
        "refresh_token": None,
        "access_token_expires_at": None,
        "refresh_token_expires_at": None,
        "token_reauth_required": False,
        "app_client_id": "client-legado",
        "install_generation": "gen-legada",
        "webhooks_ready": True,
    }])
    monkeypatch.setattr(shopify_app, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(shopify_app, "get_conn", lambda: _ConnTokens(cursor))
    monkeypatch.setattr(shopify_app, "api_key_publica", lambda: "client-publico")
    monkeypatch.setattr(shopify_app, "_descifrar_token", lambda token: token.removeprefix("enc:"))
    monkeypatch.setattr(integraciones_tienda, "_bloquear_dominio_shopify", lambda *_: None)

    assert shopify_app._token_admin_vigente(
        "tauro-qa.myshopify.com",
    ) == "access-legado"


def test_admin_api_no_reutiliza_token_si_uninstall_borro_la_fila(monkeypatch):
    from servicios import integraciones_tienda, shopify_app

    cursor = _CursorTokens([None])
    monkeypatch.setattr(shopify_app, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(shopify_app, "get_conn", lambda: _ConnTokens(cursor))
    monkeypatch.setattr(integraciones_tienda, "_bloquear_dominio_shopify", lambda *_: None)

    assert shopify_app._token_admin_vigente(
        "tauro-qa.myshopify.com", "token-de-generacion-borrada",
    ) is None


def test_registro_webhooks_graphql_es_idempotente(monkeypatch):
    from servicios import shopify_app

    llamadas = []
    existentes = []

    bypasses = []

    def graphql(_dominio, _token, query, variables=None, **kwargs):
        llamadas.append((query, variables))
        bypasses.append(kwargs.get("permitir_pendiente_webhooks"))
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
        "APP_UNINSTALLED",
        "ORDERS_CREATE", "ORDERS_UPDATED", "ORDERS_CANCELLED",
        "PRODUCTS_CREATE", "PRODUCTS_UPDATE", "PRODUCTS_DELETE",
        "INVENTORY_LEVELS_UPDATE", "INVENTORY_ITEMS_UPDATE",
    ]
    assert all(bypasses)
    assert mutaciones[0]["subscription"] == {
        "uri": "https://taurosolutions.ar/shopify/webhook/desinstalada",
        "format": "JSON",
    }
    assert mutaciones[1]["subscription"] == {
        "uri": "https://taurosolutions.ar/integraciones/shopify/webhook/orders-create",
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


def test_callback_sin_cookie_state_rechaza_antes_del_canje(monkeypatch):
    from endpoints import shopify

    promociones = []
    canjes = []
    monkeypatch.setattr(shopify, "app_configurada", lambda: True)
    monkeypatch.setattr(shopify, "validar_hmac_query", lambda _params: True)
    monkeypatch.setattr(
        shopify, "canjear_token", lambda *_args: canjes.append(True),
    )
    monkeypatch.setattr(
        shopify, "guardar_instalacion",
        lambda *args, **kwargs: promociones.append((args, kwargs)),
    )
    monkeypatch.setattr(shopify, "registrar_webhooks", lambda *_args: SHOPIFY_WEBHOOKS)

    response = shopify.callback(_Request(
        query_params={
            "shop": "tauro-qa.myshopify.com",
            "code": "codigo-qa",
            "state": "state-atacante",
            "hmac": "firma-valida",
        },
        cookies={"token": "sesion-cliente-victima"},
    ))

    assert response.status_code == 403
    assert promociones == []
    assert canjes == []
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["pragma"] == "no-cache"
    assert "shopify_state=" in response.headers.get("set-cookie", "")


def test_abrir_app_instalada_no_consume_admin_api(monkeypatch):
    from endpoints import shopify
    from servicios import auth, shopify_app

    panel = []
    monkeypatch.setattr(shopify, "app_configurada", lambda: True)
    monkeypatch.setattr(shopify_app, "instalacion", lambda _dominio: {
        "access_token": "token-qa",
        "scopes": SHOPIFY_SCOPES,
        "cliente_id": "MELCIOR",
        "app_client_id": "client-publico",
        "token_rotativo": True,
        "webhooks_ready": True,
    })
    monkeypatch.setattr(shopify, "api_key_publica", lambda: "client-publico")
    monkeypatch.setattr(
        shopify, "registrar_webhooks",
        lambda *_args: (_ for _ in ()).throw(AssertionError("no debe llamar Shopify")),
    )
    monkeypatch.setattr(auth, "validar_token", lambda _token: "MELCIOR")
    monkeypatch.setattr(
        shopify, "_panel_tienda",
        lambda dominio, instalacion, cliente: panel.append(
            (dominio, instalacion, cliente),
        ) or "panel-local",
    )

    response = shopify.install(_Request(
        query_params={"shop": "tauro-qa.myshopify.com"},
        cookies={"token": "sesion-tauro"},
    ), shop="tauro-qa.myshopify.com")

    assert response == "panel-local"
    assert panel[0][0] == "tauro-qa.myshopify.com"
    assert panel[0][2] == "MELCIOR"


@pytest.mark.parametrize("cliente_sesion", ["", "OTRO_CLIENTE"])
def test_panel_shopify_no_expone_estadisticas_sin_sesion_tauro_coincidente(
    monkeypatch, cliente_sesion,
):
    from endpoints import shopify
    from servicios import catalogo, integraciones_tienda

    consultas = []
    monkeypatch.setattr(
        integraciones_tienda, "contar_pendientes",
        lambda cliente: consultas.append(("pendientes", cliente)) or 987654,
    )
    monkeypatch.setattr(
        catalogo, "estado_sincronizacion_cliente",
        lambda cliente: consultas.append(("sync", cliente)) or {"estado": "OK"},
    )
    monkeypatch.setattr(
        catalogo, "resumen_stock_cliente",
        lambda cliente: consultas.append(("stock", cliente)) or {
            "variantes": 876543,
            "unidades_disponibles": 765432,
        },
    )

    response = shopify._panel_tienda(
        "tienda-privada.myshopify.com",
        {"cliente_id": "MELCIOR"},
        cliente_sesion,
    )
    html = response.body.decode("utf-8")

    assert consultas == []
    assert "987654" not in html
    assert "876543" not in html
    assert "765432" not in html
    assert "tienda-privada.myshopify.com" not in html
    assert "MELCIOR" not in html
    assert "Iniciar sesión" in html
    assert "app-bridge" not in html.lower()
    assert response.headers["content-security-policy"] == "frame-ancestors 'none';"
    assert response.headers["x-frame-options"] == "DENY"


def test_panel_shopify_muestra_estadisticas_solo_a_su_cuenta_tauro(monkeypatch):
    from endpoints import shopify
    from servicios import catalogo, integraciones_tienda

    monkeypatch.setattr(integraciones_tienda, "contar_pendientes", lambda _cliente: 7)
    monkeypatch.setattr(
        catalogo, "estado_sincronizacion_cliente", lambda _cliente: {"estado": "OK"},
    )
    monkeypatch.setattr(
        catalogo, "resumen_stock_cliente",
        lambda _cliente: {"variantes": 11, "unidades_disponibles": 29},
    )

    response = shopify._panel_tienda(
        "tauro-qa.myshopify.com",
        {"cliente_id": "MELCIOR"},
        "melcior",
    )
    html = response.body.decode("utf-8")

    assert "Tenés <b>7 ventas</b>" in html
    assert "tauro-qa.myshopify.com" in html
    assert "Iniciar sesión" not in html


def test_app_externa_sin_shop_no_carga_app_bridge_ni_iframe(monkeypatch):
    from endpoints import shopify

    monkeypatch.setattr(shopify, "app_configurada", lambda: True)

    response = shopify.install(_Request(query_params={}), shop="")
    html = response.body.decode("utf-8")

    assert response.status_code == 200
    assert "app-bridge" not in html.lower()
    assert response.headers["content-security-policy"] == "frame-ancestors 'none';"
    assert response.headers["x-frame-options"] == "DENY"


def test_abrir_instalacion_legada_fuerza_oauth_publico(monkeypatch):
    from endpoints import shopify
    from servicios import shopify_app

    monkeypatch.setattr(shopify, "app_configurada", lambda: True)
    monkeypatch.setattr(shopify, "api_key_publica", lambda: "client-publico")
    monkeypatch.setattr(shopify_app, "instalacion", lambda _dominio: {
        "access_token": "token-legado",
        "scopes": SHOPIFY_SCOPES,
        "cliente_id": "PESCAJACKS",
        "app_client_id": None,
    })
    monkeypatch.setattr(shopify, "_redirect_oauth", lambda dominio: ("oauth", dominio))

    response = shopify.install(_Request(
        query_params={"shop": "pesca-jacks.myshopify.com"},
    ), shop="pesca-jacks.myshopify.com")

    assert response == ("oauth", "pesca-jacks.myshopify.com")


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


def test_abrir_instalacion_publica_no_rotativa_fuerza_oauth(monkeypatch):
    from endpoints import shopify
    from servicios import shopify_app

    monkeypatch.setattr(shopify, "app_configurada", lambda: True)
    monkeypatch.setattr(shopify, "api_key_publica", lambda: "client-publico")
    monkeypatch.setattr(shopify_app, "instalacion", lambda _dominio: {
        "access_token": "token-publico-anterior",
        "scopes": SHOPIFY_SCOPES,
        "cliente_id": "MELCIOR",
        "app_client_id": "client-publico",
        "token_rotativo": False,
    })
    monkeypatch.setattr(shopify, "_redirect_oauth", lambda dominio: ("oauth", dominio))

    response = shopify.install(_Request(
        query_params={"shop": "tauro-qa.myshopify.com"},
    ), shop="tauro-qa.myshopify.com")

    assert response == ("oauth", "tauro-qa.myshopify.com")


def test_abrir_instalacion_pendiente_reinicia_oauth_con_state(monkeypatch):
    from endpoints import shopify
    from servicios import shopify_app

    monkeypatch.setattr(shopify, "app_configurada", lambda: True)
    monkeypatch.setattr(shopify_app, "instalacion", lambda _dominio: {
        "access_token": "",
        "scopes": SHOPIFY_SCOPES,
        "cliente_id": "MELCIOR",
        "app_client_id": "client-publico",
        "token_rotativo": True,
        "webhooks_ready": False,
    })
    monkeypatch.setattr(shopify, "_redirect_oauth", lambda dominio: ("oauth", dominio))

    response = shopify.install(_Request(
        query_params={"shop": "tauro-qa.myshopify.com"},
    ), shop="tauro-qa.myshopify.com")

    assert response == ("oauth", "tauro-qa.myshopify.com")


def test_callback_con_state_verificado_puede_autovincular(monkeypatch):
    from endpoints import shopify
    from servicios import auth

    promociones = []
    confirmaciones = []
    monkeypatch.setattr(shopify, "app_configurada", lambda: True)
    monkeypatch.setattr(shopify, "validar_hmac_query", lambda _params: True)
    monkeypatch.setattr(shopify, "canjear_token", lambda *_args: {
        "access_token": "token-qa",
        "refresh_token": "refresh-qa",
        "expires_in": 3600,
        "refresh_token_expires_in": 7776000,
        "scope": SHOPIFY_SCOPES,
    })
    monkeypatch.setattr(
        shopify, "guardar_instalacion",
        lambda *args, **kwargs: promociones.append((args, kwargs)) or "gen-qa",
    )
    monkeypatch.setattr(shopify, "registrar_webhooks", lambda *_args: SHOPIFY_WEBHOOKS)
    monkeypatch.setattr(
        shopify, "confirmar_webhooks_verificados",
        lambda *args: confirmaciones.append(args) or True,
    )
    monkeypatch.setattr(
        "servicios.shopify_catalogo.lanzar_sincronizacion", lambda *_args: None,
    )
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

    assert response.status_code == 303
    assert response.headers["location"] == "/shopify/install?shop=tauro-qa.myshopify.com"
    assert promociones[0][1]["cliente_claim"] == "MELCIOR"
    assert promociones[0][1]["refresh_token"] == "refresh-qa"
    assert promociones[0][1]["expires_in"] == 3600
    assert promociones[0][1]["refresh_token_expires_in"] == 7776000
    assert confirmaciones == [(
        "tauro-qa.myshopify.com", "gen-qa", SHOPIFY_WEBHOOKS,
    )]
    assert "shopify_state=" in response.headers.get("set-cookie", "")


def test_callback_no_habilita_si_la_generacion_fue_reemplazada(monkeypatch):
    from endpoints import shopify

    sincronizaciones = []
    monkeypatch.setattr(shopify, "app_configurada", lambda: True)
    monkeypatch.setattr(shopify, "validar_hmac_query", lambda _params: True)
    monkeypatch.setattr(shopify, "canjear_token", lambda *_args: {
        "access_token": "token-qa",
        "refresh_token": "refresh-qa",
        "expires_in": 3600,
        "refresh_token_expires_in": 7776000,
        "scope": SHOPIFY_SCOPES,
    })
    monkeypatch.setattr(shopify, "guardar_instalacion", lambda *_a, **_k: "gen-vieja")
    monkeypatch.setattr(shopify, "registrar_webhooks", lambda *_args: SHOPIFY_WEBHOOKS)
    monkeypatch.setattr(shopify, "confirmar_webhooks_verificados", lambda *_args: False)
    monkeypatch.setattr(
        "servicios.shopify_catalogo.lanzar_sincronizacion",
        lambda *_args: sincronizaciones.append(True),
    )

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
    assert sincronizaciones == []


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
    guardadas = []
    monkeypatch.setattr(shopify, "app_configurada", lambda: True)
    monkeypatch.setattr(shopify, "validar_hmac_query", lambda _params: True)
    monkeypatch.setattr(shopify, "canjear_token", lambda *_args: {
        "access_token": "token-qa",
        "refresh_token": "refresh-qa",
        "expires_in": 3600,
        "refresh_token_expires_in": 7776000,
        "scope": SHOPIFY_SCOPES,
    })
    monkeypatch.setattr(
        shopify, "guardar_instalacion",
        lambda *_args, **kwargs: guardadas.append(kwargs["cliente_claim"]) or "gen-qa",
    )
    monkeypatch.setattr(shopify, "registrar_webhooks", lambda *_args: SHOPIFY_WEBHOOKS[:-1])
    monkeypatch.setattr(
        shopify, "confirmar_webhooks_verificados",
        lambda *_args: (_ for _ in ()).throw(AssertionError("no debe habilitar")),
    )
    monkeypatch.setattr(shopify, "desinstalar", desvinculadas.append)

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
    assert guardadas == [""]


def test_callback_timeout_verificando_webhooks_no_borra_instalacion(monkeypatch):
    from endpoints import shopify

    borradas = []
    guardadas = []
    monkeypatch.setattr(shopify, "app_configurada", lambda: True)
    monkeypatch.setattr(shopify, "validar_hmac_query", lambda _params: True)
    monkeypatch.setattr(shopify, "canjear_token", lambda *_args: {
        "access_token": "token-qa",
        "refresh_token": "refresh-qa",
        "expires_in": 3600,
        "refresh_token_expires_in": 7776000,
        "scope": SHOPIFY_SCOPES,
    })
    monkeypatch.setattr(
        shopify, "guardar_instalacion",
        lambda *_args, **kwargs: guardadas.append(kwargs["cliente_claim"]) or "gen-qa",
    )

    def falla_verificacion(*_args):
        raise shopify.ShopifyWebhookVerificationError("timeout")

    monkeypatch.setattr(shopify, "registrar_webhooks", falla_verificacion)
    monkeypatch.setattr(
        shopify, "confirmar_webhooks_verificados",
        lambda *_args: (_ for _ in ()).throw(AssertionError("no debe habilitar")),
    )
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
    assert guardadas == [""]


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

    assert response.status_code == 403
    assert canjes == []


def test_webhook_nuevo_y_repetido_dispara_armado_una_sola_vez(monkeypatch):
    from endpoints import integraciones
    from servicios import integraciones_tienda, shopify_app, solicitud_automatica

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
        "id": 7, "cliente_id": "MELCIOR", "plataforma": "shopify",
        "secreto": "oauth:shopify-app", "activa": True,
    })
    monkeypatch.setattr(shopify_app, "instalacion", lambda _dominio: {
        "access_token": "token", "app_client_id": "app", "cliente_id": "MELCIOR",
        "install_generation": "gen-1",
    })
    monkeypatch.setattr(shopify_app, "firma_valida_webhook_app", lambda *_args: True)
    monkeypatch.setattr(shopify_app, "clasificar_evento_instalacion", lambda *_args: "ACTUAL")
    monkeypatch.setattr(
        integraciones_tienda, "webhook_shopify_ya_procesado", lambda *_args: False,
    )
    monkeypatch.setattr(
        integraciones_tienda, "marcar_webhook_shopify_procesado", lambda *_args: None,
    )
    monkeypatch.setattr(
        integraciones, "guardar_pedido",
        lambda *_args, **_kwargs: next(resultados_guardar),
    )
    monkeypatch.setattr(integraciones_tienda, "id_de_pedido", lambda *_args: 99)
    monkeypatch.setattr(solicitud_automatica, "intentar_en_segundo_plano", armados.append)

    primera = asyncio.run(integraciones.shopify_orders_create(request))
    segunda = asyncio.run(integraciones.shopify_orders_create(request))

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
        "secreto": "oauth:shopify-app", "activa": True,
    })
    monkeypatch.setattr(integraciones, "verificar_hmac_shopify", lambda *_args: True)
    monkeypatch.setenv("SHOPIFY_API_SECRET", "secreto-app-qa")
    monkeypatch.setattr(shopify_app, "instalacion", lambda _dominio: {
        "access_token": "token", "app_client_id": "app", "cliente_id": "MELCIOR",
        "install_generation": "gen-1",
    })
    monkeypatch.setattr(shopify_app, "firma_valida_webhook_app", lambda *_args: True)
    monkeypatch.setattr(shopify_app, "clasificar_evento_instalacion", lambda *_args: "ACTUAL")

    def encolar(*args):
        recibidos.append(args)
        return next(resultados)

    monkeypatch.setattr(shopify_catalogo, "encolar_evento", encolar)
    monkeypatch.setattr(shopify_catalogo, "lanzar_procesamiento_eventos", lambda: lanzados.append(True))

    primera = asyncio.run(integraciones.shopify_inventory_levels_update(request))
    segunda = asyncio.run(integraciones.shopify_inventory_levels_update(request))

    assert primera == {"ok": True, "encolado": True, "duplicado": False}
    assert segunda == {"ok": True, "encolado": False, "duplicado": True}
    assert len(recibidos) == 2
    assert recibidos[0][:3] == ("wh-stock-1", "tauro-qa.myshopify.com", "inventory_levels/update")
    assert lanzados == [True]


def test_firma_invalida_no_parsea_ni_guarda(monkeypatch):
    from endpoints import integraciones
    from servicios import shopify_app

    guardados = []
    monkeypatch.setattr(integraciones, "tienda_por_dominio", lambda _dominio: {
        "id": 7, "cliente_id": "MELCIOR", "plataforma": "shopify",
        "secreto": "oauth:shopify-app", "activa": True,
    })
    monkeypatch.setattr(shopify_app, "instalacion", lambda _dominio: {
        "access_token": "token", "app_client_id": "app", "cliente_id": "MELCIOR",
        "install_generation": "gen-1",
    })
    monkeypatch.setattr(shopify_app, "firma_valida_webhook_app", lambda *_args: False)
    monkeypatch.setattr(integraciones, "guardar_pedido", lambda *_args: guardados.append(True))

    response = asyncio.run(integraciones.shopify_orders_create(_Request(
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
