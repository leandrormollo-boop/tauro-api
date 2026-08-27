from __future__ import annotations

import asyncio
from datetime import datetime, timezone


DOMINIO = "pesca-jacks.myshopify.com"


class _Request:
    def __init__(self, body: bytes, headers: dict[str, str]):
        self._body = body
        self.headers = headers

    async def body(self) -> bytes:
        return self._body


class _Cursor:
    def __init__(self, respuestas=()):
        self.respuestas = iter(respuestas)
        self.ejecutadas: list[tuple[str, object]] = []
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.ejecutadas.append((" ".join(str(sql).split()), params))

    def fetchone(self):
        return next(self.respuestas)

    def fetchall(self):
        return next(self.respuestas)


class _Conn:
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


def test_uninstall_ata_dominio_del_body_firmado_al_header(monkeypatch):
    from endpoints import shopify
    from servicios import shopify_app

    llamadas = []
    monkeypatch.setattr(
        shopify_app, "cliente_app_para_webhook", lambda *_args: "app-publica",
    )
    monkeypatch.setattr(shopify, "desinstalar", lambda *_args: llamadas.append(True))

    respuesta = asyncio.run(shopify.desinstalada(_Request(
        b'{"id":123,"myshopify_domain":"pesca-jacks.myshopify.com"}',
        {
            "x-shopify-shop-domain": "otra-tienda.myshopify.com",
            "x-shopify-hmac-sha256": "firma-valida",
            "x-shopify-topic": "app/uninstalled",
        },
    )))

    assert respuesta.status_code == 400
    assert llamadas == []


def test_uninstall_atrasado_misma_app_no_purga_reinstalacion(monkeypatch):
    from servicios import integraciones_tienda, shopify_app

    cursor = _Cursor([{
        "id": 9,
        "app_client_id": "app-publica",
        "install_generation": "gen-nueva",
        "cliente_id": "MELCIOR",
        "instalada_en": datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc),
    }])
    conn = _Conn(cursor)
    purgas = []
    monkeypatch.setattr(shopify_app, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "_bloquear_dominio_shopify", lambda *_: None)
    monkeypatch.setattr(
        integraciones_tienda,
        "_borrar_datos_tienda_con_cursor",
        lambda *_: purgas.append(True),
    )
    monkeypatch.setattr(shopify_app, "get_conn", lambda: conn)

    assert not shopify_app.desinstalar(
        DOMINIO,
        "app-publica",
        "123",
        "2026-08-27T14:59:00Z",
    )
    assert purgas == []
    assert conn.commits == 0


def test_uninstall_actual_purga_tombstone_y_token_en_una_transaccion(monkeypatch):
    from servicios import integraciones_tienda, shopify_app

    cursor = _Cursor([
        {
            "id": 9,
            "app_client_id": "app-publica",
            "install_generation": "gen-1",
            "cliente_id": "MELCIOR",
            "instalada_en": datetime(2026, 8, 27, 14, 0, tzinfo=timezone.utc),
        },
        None,
    ])
    conn = _Conn(cursor)
    purgas = []
    monkeypatch.setattr(shopify_app, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "_bloquear_dominio_shopify", lambda *_: None)
    monkeypatch.setattr(
        integraciones_tienda,
        "_borrar_datos_tienda_con_cursor",
        lambda _cur, dominio: purgas.append(dominio) or 4,
    )
    monkeypatch.setattr(shopify_app, "get_conn", lambda: conn)

    assert shopify_app.desinstalar(
        DOMINIO,
        "app-publica",
        "123",
        "2026-08-27T15:00:00Z",
    )
    sql = "\n".join(q for q, _ in cursor.ejecutadas)
    assert purgas == [DOMINIO]
    assert "INSERT INTO shopify_desinstalaciones" in sql
    assert "DELETE FROM shopify_instalaciones" in sql
    assert "install_generation = %s" in sql
    assert conn.commits == 1


def test_shop_redact_tombstone_no_consulta_ni_borra_instalacion_nueva(monkeypatch):
    from endpoints import shopify
    from servicios import shopify_app

    monkeypatch.setattr(
        shopify_app, "cliente_app_para_webhook", lambda *_args: "app-publica",
    )
    monkeypatch.setattr(shopify, "confirmar_shop_redact", lambda *_args: True)
    monkeypatch.setattr(
        shopify_app,
        "cliente_app_instalada",
        lambda *_args: (_ for _ in ()).throw(AssertionError("no debe leer gen nueva")),
    )

    respuesta = asyncio.run(shopify.gdpr_shop_redact(_Request(
        b'{"shop_id":123,"shop_domain":"pesca-jacks.myshopify.com"}',
        {
            "x-shopify-shop-domain": DOMINIO,
            "x-shopify-hmac-sha256": "firma-valida",
            "x-shopify-topic": "shop/redact",
        },
    )))
    assert respuesta == {"ok": True}


def test_vincular_cliente_no_reasigna_owner_existente(monkeypatch):
    from servicios import integraciones_tienda, shopify_app

    cursor = _Cursor([{
        "id": 1, "cliente_id": "MELCIOR", "webhooks_ready": True,
    }])
    conn = _Conn(cursor)
    monkeypatch.setattr(shopify_app, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "_bloquear_dominio_shopify", lambda *_: None)
    monkeypatch.setattr(shopify_app, "get_conn", lambda: conn)

    try:
        shopify_app.vincular_cliente(DOMINIO, "GUAIMAO")
    except shopify_app.ShopifyOwnershipConflict:
        pass
    else:
        raise AssertionError("debía rechazar la reasignación")

    sql = "\n".join(q for q, _ in cursor.ejecutadas)
    assert "UPDATE shopify_instalaciones SET cliente_id" not in sql
    assert conn.commits == 0


def test_fallo_guardando_huerfano_devuelve_503(monkeypatch):
    from endpoints import integraciones
    from servicios import integraciones_tienda, shopify_app

    monkeypatch.setattr(integraciones, "tienda_por_dominio", lambda *_: None)
    monkeypatch.setattr(shopify_app, "instalacion", lambda *_: {
        "access_token": "token",
        "app_client_id": "app-publica",
        "install_generation": "gen-1",
        "cliente_id": None,
    })
    monkeypatch.setattr(shopify_app, "firma_valida_webhook_app", lambda *_: True)
    monkeypatch.setattr(shopify_app, "clasificar_evento_instalacion", lambda *_: "ACTUAL")
    monkeypatch.setattr(integraciones_tienda, "webhook_shopify_ya_procesado", lambda *_: False)
    monkeypatch.setattr(
        integraciones_tienda,
        "guardar_pedido_huerfano",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db")),
    )

    respuesta = asyncio.run(integraciones.shopify_orders_create(_Request(
        b'{"id":123}',
        {
            "x-shopify-shop-domain": DOMINIO,
            "x-shopify-hmac-sha256": "firma",
            "x-shopify-topic": "orders/create",
        },
    )))
    assert respuesta.status_code == 503


def test_webhook_pendiente_con_owner_no_ackea_y_shopify_lo_reintenta(monkeypatch):
    from endpoints import integraciones
    from servicios import integraciones_tienda, shopify_app

    procesados = []
    monkeypatch.setattr(integraciones, "tienda_por_dominio", lambda *_: {
        "id": 7,
        "cliente_id": "MELCIOR",
        "plataforma": "shopify",
        "secreto": "oauth:shopify-app",
        "activa": False,
    })
    monkeypatch.setattr(shopify_app, "instalacion", lambda *_: {
        "access_token": "",
        "app_client_id": "app-publica",
        "install_generation": "gen-pendiente",
        "cliente_id": "MELCIOR",
        "webhooks_ready": False,
    })
    monkeypatch.setattr(shopify_app, "firma_valida_webhook_app", lambda *_: True)
    monkeypatch.setattr(shopify_app, "clasificar_evento_instalacion", lambda *_: "ACTUAL")
    monkeypatch.setattr(integraciones_tienda, "webhook_shopify_ya_procesado", lambda *_: False)
    monkeypatch.setattr(
        integraciones_tienda,
        "guardar_pedido_huerfano",
        lambda *_a, **_k: (_ for _ in ()).throw(
            integraciones_tienda.TiendaNoOperativaError("pendiente"),
        ),
    )
    monkeypatch.setattr(
        integraciones_tienda,
        "marcar_webhook_shopify_procesado",
        lambda *_: procesados.append(True),
    )

    respuesta = asyncio.run(integraciones.shopify_orders_create(_Request(
        b'{"id":123}',
        {
            "x-shopify-shop-domain": DOMINIO,
            "x-shopify-hmac-sha256": "firma",
            "x-shopify-topic": "orders/create",
        },
    )))

    assert respuesta.status_code == 503
    assert procesados == []


def test_owner_incoherente_nunca_escribe_pedido_del_tenant(monkeypatch):
    from endpoints import integraciones
    from servicios import integraciones_tienda, shopify_app

    pedidos = []
    huerfanos = []
    monkeypatch.setattr(integraciones, "tienda_por_dominio", lambda *_: {
        "id": 7,
        "cliente_id": "MELCIOR",
        "plataforma": "shopify",
        "secreto": "oauth:shopify-app",
        "activa": True,
    })
    monkeypatch.setattr(shopify_app, "instalacion", lambda *_: {
        "access_token": "token",
        "app_client_id": "app-publica",
        "install_generation": "gen-1",
        "cliente_id": "GUAIMAO",
    })
    monkeypatch.setattr(shopify_app, "firma_valida_webhook_app", lambda *_: True)
    monkeypatch.setattr(shopify_app, "clasificar_evento_instalacion", lambda *_: "ACTUAL")
    monkeypatch.setattr(integraciones_tienda, "webhook_shopify_ya_procesado", lambda *_: False)
    monkeypatch.setattr(integraciones_tienda, "marcar_webhook_shopify_procesado", lambda *_: None)
    monkeypatch.setattr(integraciones, "guardar_pedido", lambda *_a, **_k: pedidos.append(True))
    monkeypatch.setattr(
        integraciones_tienda,
        "guardar_pedido_huerfano",
        lambda *_a, **_k: huerfanos.append(True),
    )

    respuesta = asyncio.run(integraciones.shopify_orders_create(_Request(
        b'{"id":123}',
        {
            "x-shopify-shop-domain": DOMINIO,
            "x-shopify-hmac-sha256": "firma",
            "x-shopify-topic": "orders/create",
        },
    )))
    assert respuesta == {"ok": True, "estado": "sin_vincular"}
    assert pedidos == []
    assert huerfanos == [True]


def test_guardar_pedido_revalida_owner_generacion_y_tombstone(monkeypatch):
    from servicios import integraciones_tienda

    cursor = _Cursor([{"id": 7}, {"es_nuevo": True}])
    conn = _Conn(cursor)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "get_conn", lambda: conn)

    assert integraciones_tienda.guardar_pedido(
        "MELCIOR",
        7,
        "shopify",
        {"pedido_externo_id": "123", "destinatario": {}, "items": []},
        dominio_verificado=DOMINIO,
        install_generation_verificada="gen-1",
    )
    sql = "\n".join(q for q, _ in cursor.ejecutadas)
    assert "t.cliente_id = %s" in sql
    assert "i.install_generation = %s" in sql
    assert "shopify_pedidos_redactados" in sql
    assert "pedidos_tienda.cliente_id = EXCLUDED.cliente_id" in sql
    assert conn.commits == 1


def test_huerfano_fallido_no_se_borra_del_retry(monkeypatch):
    from servicios import integraciones_tienda

    cursor = _Cursor([[
        {
            "pedido_externo_id": "123",
            "payload": {
                "id": 123,
                "shipping_address": {"address1": "Street", "country_code": "US"},
                "line_items": [],
            },
            "install_generation": "gen-1",
        }
    ]])
    conn = _Conn(cursor)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "get_conn", lambda: conn)
    monkeypatch.setattr(
        integraciones_tienda,
        "guardar_pedido",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("retry")),
    )

    assert integraciones_tienda.volcar_huerfanos("MELCIOR", 7, DOMINIO) == 0
    assert all("DELETE FROM pedidos_huerfanos" not in q for q, _ in cursor.ejecutadas)


def test_shopify_manual_se_rechaza_sin_escribir(monkeypatch):
    from endpoints import portal_cliente

    escrituras = []
    monkeypatch.setattr(
        portal_cliente,
        "conectar_tienda",
        lambda *_args, **_kwargs: escrituras.append(True),
    )
    respuesta = portal_cliente.tienda_conectar(
        plataforma="shopify",
        dominio=DOMINIO,
        secreto="secret-largo",
        cliente="MELCIOR",
    )
    assert respuesta.status_code == 303
    assert escrituras == []


def test_panel_privado_no_es_cacheable():
    from endpoints import shopify

    respuesta = shopify._panel_tienda(
        DOMINIO,
        {"cliente_id": ""},
        "",
    )
    assert respuesta.headers["cache-control"] == "private, no-store"
    assert respuesta.headers["pragma"] == "no-cache"
    assert respuesta.headers["vary"] == "Cookie"


def test_oauth_sin_state_nace_ownerless_y_desactiva_binding_anterior(monkeypatch):
    from servicios import integraciones_tienda, shopify_app

    cursor = _Cursor([
        {"owner_instalacion": "CLIENTE_A"},
        {"owner_mapping": "CLIENTE_A"},
    ])
    conn = _Conn(cursor)
    purgas = []
    monkeypatch.setattr(shopify_app, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(shopify_app, "_cifrar_token", lambda token: f"enc:{token}")
    monkeypatch.setattr(shopify_app, "_credenciales_publicas", lambda: ("app", "secret"))
    monkeypatch.setattr(shopify_app, "get_conn", lambda: conn)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "_bloquear_dominio_shopify", lambda *_: None)
    monkeypatch.setattr(
        integraciones_tienda, "_borrar_datos_tienda_con_cursor",
        lambda *_: purgas.append(True),
    )

    shopify_app.guardar_instalacion(
        DOMINIO,
        "token-nuevo",
        cliente_claim="",
        refresh_token="refresh-nuevo",
        expires_in=3600,
        refresh_token_expires_in=7776000,
    )

    instalacion = next(
        params for sql, params in cursor.ejecutadas
        if "INSERT INTO shopify_instalaciones" in sql
    )
    sql = "\n".join(query for query, _ in cursor.ejecutadas)
    assert instalacion[-1] == ""
    assert "cliente_id = EXCLUDED.cliente_id" in sql
    assert "UPDATE tiendas_conectadas SET activa = FALSE" in sql
    assert purgas == []
    assert conn.commits == 1


def test_oauth_cliente_b_transfiere_y_purga_cliente_a_atomicamente(monkeypatch):
    from servicios import integraciones_tienda, shopify_app

    cursor = _Cursor([
        {"owner_instalacion": "CLIENTE_A"},
        {"owner_mapping": "CLIENTE_A"},
        {"id": 7},
    ])
    conn = _Conn(cursor)
    purgas = []
    monkeypatch.setattr(shopify_app, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(shopify_app, "_cifrar_token", lambda token: f"enc:{token}")
    monkeypatch.setattr(shopify_app, "_credenciales_publicas", lambda: ("app", "secret"))
    monkeypatch.setattr(shopify_app, "get_conn", lambda: conn)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "_bloquear_dominio_shopify", lambda *_: None)
    monkeypatch.setattr(
        integraciones_tienda, "_borrar_datos_tienda_con_cursor",
        lambda _cur, dominio: purgas.append(dominio) or 3,
    )

    shopify_app.guardar_instalacion(
        DOMINIO,
        "token-b",
        cliente_claim="cliente_b",
        refresh_token="refresh-b",
        expires_in=3600,
        refresh_token_expires_in=7776000,
    )

    instalacion = next(
        params for sql, params in cursor.ejecutadas
        if "INSERT INTO shopify_instalaciones" in sql
    )
    binding = next(
        params for sql, params in cursor.ejecutadas
        if "INSERT INTO tiendas_conectadas" in sql
    )
    assert purgas == [DOMINIO]
    assert instalacion[-1] == "CLIENTE_B"
    assert binding[0] == "CLIENTE_B"
    assert "FALSE" in next(
        sql for sql, _params in cursor.ejecutadas
        if "INSERT INTO tiendas_conectadas" in sql
    )
    assert conn.commits == 1


def test_shop_redact_customer_payload_no_puede_replayarse_como_shop(monkeypatch):
    from endpoints import shopify
    from servicios import shopify_app

    confirmaciones = []
    monkeypatch.setattr(
        shopify_app, "cliente_app_para_webhook", lambda *_: "app-publica",
    )
    monkeypatch.setattr(
        shopify, "confirmar_shop_redact",
        lambda *_: confirmaciones.append(True),
    )
    cuerpo = (
        b'{"shop_id":123,"shop_domain":"pesca-jacks.myshopify.com",'
        b'"customer":{"id":7},"orders_to_redact":[99]}'
    )
    respuesta = asyncio.run(shopify.gdpr_shop_redact(_Request(cuerpo, {
        "x-shopify-shop-domain": DOMINIO,
        "x-shopify-hmac-sha256": "firma",
        "x-shopify-topic": "shop/redact",
    })))

    assert respuesta.status_code == 400
    assert confirmaciones == []


def test_shop_redact_no_puede_replayarse_como_uninstall(monkeypatch):
    from endpoints import shopify
    from servicios import shopify_app

    purgas = []
    monkeypatch.setattr(
        shopify_app, "cliente_app_para_webhook", lambda *_: "app-publica",
    )
    monkeypatch.setattr(
        shopify, "desinstalar", lambda *_args: purgas.append(True),
    )
    respuesta = asyncio.run(shopify.desinstalada(_Request(
        b'{"shop_id":123,"shop_domain":"pesca-jacks.myshopify.com"}',
        {
            "x-shopify-shop-domain": DOMINIO,
            "x-shopify-hmac-sha256": "firma",
            "x-shopify-topic": "app/uninstalled",
        },
    )))
    assert respuesta.status_code == 400
    assert purgas == []


def test_shop_redact_activo_persiste_obligacion_o_devuelve_503(monkeypatch):
    from endpoints import shopify
    from servicios import shopify_app

    monkeypatch.setattr(
        shopify_app, "cliente_app_para_webhook", lambda *_: "app-publica",
    )
    monkeypatch.setattr(shopify, "confirmar_shop_redact", lambda *_: False)
    monkeypatch.setattr(
        shopify_app, "cliente_app_instalada", lambda *_: "app-publica",
    )
    monkeypatch.setattr(
        shopify_app, "registrar_shop_redact_pendiente",
        lambda *_: (_ for _ in ()).throw(RuntimeError("commit")),
    )
    respuesta = asyncio.run(shopify.gdpr_shop_redact(_Request(
        b'{"shop_id":123,"shop_domain":"pesca-jacks.myshopify.com"}',
        {
            "x-shopify-shop-domain": DOMINIO,
            "x-shopify-hmac-sha256": "firma",
            "x-shopify-topic": "shop/redact",
        },
    )))
    assert respuesta.status_code == 503


def test_obligacion_shop_redact_activa_se_confirma_solo_despues_de_commit(monkeypatch):
    from servicios import integraciones_tienda, shopify_app

    cursor = _Cursor([{
        "install_generation": "gen-2",
        "app_client_id": "app-publica",
    }])
    conn = _Conn(cursor)
    monkeypatch.setattr(shopify_app, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(shopify_app, "get_conn", lambda: conn)
    monkeypatch.setattr(
        integraciones_tienda, "_bloquear_dominio_shopify", lambda *_: None,
    )

    assert shopify_app.registrar_shop_redact_pendiente(
        DOMINIO, "123", "app-publica",
    )
    sql = "\n".join(query for query, _ in cursor.ejecutadas)
    assert "INSERT INTO shopify_shop_redact_pendientes" in sql
    assert "VERIFICAR_GENERACION" in sql
    assert conn.commits == 1


def test_cancelacion_shopify_revalida_tenant_generacion_bajo_lock(monkeypatch):
    from servicios import integraciones_tienda

    cursor = _Cursor([{"id": 7}, {"id": 42}])
    conn = _Conn(cursor)
    locks = []
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "get_conn", lambda: conn)
    monkeypatch.setattr(
        integraciones_tienda, "_bloquear_dominio_shopify",
        lambda _cur, dominio: locks.append(dominio),
    )

    assert integraciones_tienda.cancelar_pedido_externo(
        7,
        "123",
        cliente_id="MELCIOR",
        dominio_verificado=DOMINIO,
        install_generation_verificada="gen-2",
    )
    sql = "\n".join(query for query, _ in cursor.ejecutadas)
    assert locks == [DOMINIO]
    assert "i.install_generation = %s" in sql
    assert "UPPER(t.cliente_id) = %s" in sql
    assert "UPPER(cliente_id) = %s" in sql
    assert conn.commits == 1


def test_catalogo_obsoleto_se_ackea_sin_reintento_infinito(monkeypatch):
    from endpoints import integraciones
    from servicios import shopify_app, shopify_catalogo

    monkeypatch.setattr(integraciones, "tienda_por_dominio", lambda *_: {
        "id": 7,
        "cliente_id": "MELCIOR",
        "plataforma": "shopify",
        "secreto": "oauth:shopify-app",
        "activa": True,
    })
    monkeypatch.setattr(shopify_app, "instalacion", lambda *_: {
        "access_token": "token",
        "app_client_id": "app",
        "install_generation": "gen-2",
        "cliente_id": "MELCIOR",
    })
    monkeypatch.setattr(shopify_app, "firma_valida_webhook_app", lambda *_: True)
    monkeypatch.setattr(
        shopify_app, "clasificar_evento_instalacion", lambda *_: "ACTUAL",
    )
    monkeypatch.setattr(
        shopify_catalogo,
        "encolar_evento",
        lambda *_: (_ for _ in ()).throw(
            shopify_catalogo.ShopifyCatalogError(
                "GENERACION_OBSOLETA", "stale",
            )
        ),
    )
    respuesta = asyncio.run(integraciones.shopify_products_create(_Request(
        b'{"id":123,"variants":[]}',
        {
            "x-shopify-shop-domain": DOMINIO,
            "x-shopify-hmac-sha256": "firma",
            "x-shopify-topic": "products/create",
        },
    )))
    assert respuesta == {"ok": True, "ignorado": "generacion_anterior"}


def test_fallback_dedupe_liga_dominio_topic_y_body():
    from endpoints import integraciones

    cuerpo = b'{"id":123}'
    request = _Request(cuerpo, {})
    primero = integraciones._webhook_id_shopify(
        request, DOMINIO, "orders/create", cuerpo,
    )
    segundo = integraciones._webhook_id_shopify(
        request, DOMINIO, "orders/create", cuerpo,
    )
    otro_topic = integraciones._webhook_id_shopify(
        request, DOMINIO, "orders/updated", cuerpo,
    )
    assert primero == segundo
    assert primero != otro_topic
    assert len(primero) == 64


def test_huerfano_no_se_inserta_si_claim_gano_la_carrera(monkeypatch):
    from servicios import integraciones_tienda

    cursor = _Cursor([None])
    conn = _Conn(cursor)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "get_conn", lambda: conn)
    monkeypatch.setattr(
        integraciones_tienda, "_bloquear_dominio_shopify", lambda *_: None,
    )

    try:
        integraciones_tienda.guardar_pedido_huerfano(
            DOMINIO,
            b'{"id":123}',
            app_client_id_verificado="app",
            install_generation_verificada="gen-2",
        )
    except integraciones_tienda.TiendaNoOperativaError:
        pass
    else:
        raise AssertionError("debía forzar retry sobre el binding recién claimado")

    sql = "\n".join(query for query, _ in cursor.ejecutadas)
    assert "COALESCE(cliente_id, '')" in sql
    assert "NOT EXISTS" in sql
    assert "INSERT INTO pedidos_huerfanos" not in sql
    assert conn.commits == 0


def test_create_huerfano_cancelado_no_se_vuelca_al_vincular(monkeypatch):
    from servicios import integraciones_tienda

    # La cancelación ownerless crea un tombstone monotónico y elimina el
    # payload (con PII) que hubiera llegado por orders/create.
    cursor_cancel = _Cursor([{"install_generation": "gen-2"}])
    conn_cancel = _Conn(cursor_cancel)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "get_conn", lambda: conn_cancel)
    monkeypatch.setattr(
        integraciones_tienda, "_bloquear_dominio_shopify", lambda *_: None,
    )
    assert integraciones_tienda.cancelar_pedido_huerfano(
        DOMINIO,
        "123",
        app_client_id_verificado="app",
        install_generation_verificada="gen-2",
    )
    sql_cancel = "\n".join(query for query, _ in cursor_cancel.ejecutadas)
    assert "INSERT INTO shopify_huerfanos_cancelados" in sql_cancel
    assert "GREATEST" in sql_cancel
    assert "DELETE FROM pedidos_huerfanos" in sql_cancel

    # Al vincular, la lectura excluye también cualquier payload seleccionado
    # antes de una limpieza/retry mediante el tombstone de la generación.
    cursor_volcado = _Cursor([[]])
    conn_volcado = _Conn(cursor_volcado)
    monkeypatch.setattr(integraciones_tienda, "get_conn", lambda: conn_volcado)
    assert integraciones_tienda.volcar_huerfanos(
        "MELCIOR", 7, DOMINIO,
    ) == 0
    sql_volcado = "\n".join(query for query, _ in cursor_volcado.ejecutadas)
    assert "LEFT JOIN shopify_huerfanos_cancelados" in sql_volcado
    assert "c.pedido_externo_id IS NULL" in sql_volcado


def test_create_tardio_no_reemplaza_cancelacion_ownerless(monkeypatch):
    from servicios import integraciones_tienda

    cursor = _Cursor([
        {"install_generation": "gen-2"},
        None,
        {"cancelado": True},
    ])
    conn = _Conn(cursor)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "get_conn", lambda: conn)
    monkeypatch.setattr(
        integraciones_tienda, "_bloquear_dominio_shopify", lambda *_: None,
    )
    assert not integraciones_tienda.guardar_pedido_huerfano(
        DOMINIO,
        b'{"id":123}',
        app_client_id_verificado="app",
        install_generation_verificada="gen-2",
    )
    sql = "\n".join(query for query, _ in cursor.ejecutadas)
    assert "FROM shopify_huerfanos_cancelados" in sql
    assert "INSERT INTO pedidos_huerfanos" not in sql
    assert conn.commits == 0


def test_endpoints_operacionales_exigen_topic_exacto():
    from endpoints import integraciones

    respuesta = asyncio.run(integraciones.shopify_orders_create(_Request(
        b'{"id":123}',
        {"x-shopify-topic": "orders/updated"},
    )))
    assert respuesta.status_code == 400


def test_replay_por_webhook_id_no_repite_efecto(monkeypatch):
    from endpoints import integraciones
    from servicios import integraciones_tienda, shopify_app

    efectos = []
    marcas = []
    vistos = iter([False, True])
    monkeypatch.setattr(integraciones, "tienda_por_dominio", lambda *_: {
        "id": 7,
        "cliente_id": "MELCIOR",
        "plataforma": "shopify",
        "secreto": "oauth:shopify-app",
        "activa": True,
    })
    monkeypatch.setattr(shopify_app, "instalacion", lambda *_: {
        "access_token": "token",
        "app_client_id": "app",
        "install_generation": "gen-2",
        "cliente_id": "MELCIOR",
    })
    monkeypatch.setattr(shopify_app, "firma_valida_webhook_app", lambda *_: True)
    monkeypatch.setattr(
        shopify_app, "clasificar_evento_instalacion", lambda *_: "ACTUAL",
    )
    monkeypatch.setattr(
        integraciones_tienda, "webhook_shopify_ya_procesado",
        lambda *_: next(vistos),
    )
    monkeypatch.setattr(
        integraciones_tienda, "marcar_webhook_shopify_procesado",
        lambda *args: marcas.append(args),
    )
    monkeypatch.setattr(
        integraciones, "guardar_pedido",
        lambda *_args, **_kwargs: efectos.append(True) or False,
    )
    request = _Request(
        b'{"id":123,"shipping_address":{"address1":"Test",'
        b'"country_code":"US"},"line_items":[]}',
        {
            "x-shopify-shop-domain": DOMINIO,
            "x-shopify-hmac-sha256": "firma",
            "x-shopify-topic": "orders/create",
            "x-shopify-webhook-id": "wh-123",
        },
    )
    primera = asyncio.run(integraciones.shopify_orders_create(request))
    segunda = asyncio.run(integraciones.shopify_orders_create(request))
    assert primera == {"ok": True, "nuevo": False}
    assert segunda == {"ok": True, "duplicado": True}
    assert efectos == [True]
    assert len(marcas) == 1


def test_create_activo_tardio_no_revive_pedido_cancelado(monkeypatch):
    from servicios import integraciones_tienda

    cursor = _Cursor([None, {"cancelado": True}])
    conn = _Conn(cursor)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "get_conn", lambda: conn)
    monkeypatch.setattr(
        integraciones_tienda, "_bloquear_dominio_shopify", lambda *_: None,
    )
    try:
        integraciones_tienda.guardar_pedido(
            "MELCIOR",
            7,
            "shopify",
            {"pedido_externo_id": "123", "destinatario": {}, "items": []},
            dominio_verificado=DOMINIO,
            install_generation_verificada="gen-2",
        )
    except integraciones_tienda.PedidoShopifyCanceladoError:
        pass
    else:
        raise AssertionError("un create tardío no debe revivir la cancelación")
    assert all(
        "INSERT INTO pedidos_tienda" not in query
        for query, _ in cursor.ejecutadas
    )
    assert conn.commits == 0


def test_reinicio_cliente_borra_solo_espejo_y_verifica_historia(monkeypatch):
    from servicios import integraciones_tienda

    cursor = _Cursor([
        {
            "id": 17,
            "cliente_id": "PESCAJACKS",
            "owner_instalacion": "PESCAJACKS",
        },
        {"n": 0},
        {"n": 229},
        {"n": 458},
        {"n": 1},
        {"n": 7},
        {"n": 3},
        {"n": 5},
        {"n": 7},
        {"n": 3},
        {"n": 5},
    ])
    conn = _Conn(cursor)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(
        integraciones_tienda, "_bloquear_dominio_shopify", lambda *_: None,
    )
    monkeypatch.setattr(integraciones_tienda, "get_conn", lambda: conn)

    resultado = integraciones_tienda.reiniciar_integracion_shopify_cliente(
        "pescajacks", "pescajacks-prueba.myshopify.com",
    )

    sql = "\n".join(query for query, _params in cursor.ejecutadas)
    assert resultado["productos"] == 229
    assert resultado["pedidos_importados"] == 1
    assert "DELETE FROM shopify_instalaciones" in sql
    assert "DELETE FROM producto_inventario_ubicaciones" in sql
    assert "DELETE FROM productos" in sql
    assert "DELETE FROM tiendas_conectadas" in sql
    assert "DELETE FROM envios" not in sql
    assert "DELETE FROM pagos" not in sql
    assert "DELETE FROM solicitudes_guia" not in sql
    assert "shopify.integration_reset" in str(cursor.ejecutadas[-1][1])
    assert conn.commits == 1


def test_reinicio_cliente_rechaza_tienda_de_otro_tenant(monkeypatch):
    from servicios import integraciones_tienda

    cursor = _Cursor([{
        "id": 17,
        "cliente_id": "OTRO_CLIENTE",
        "owner_instalacion": "OTRO_CLIENTE",
    }])
    conn = _Conn(cursor)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(
        integraciones_tienda, "_bloquear_dominio_shopify", lambda *_: None,
    )
    monkeypatch.setattr(integraciones_tienda, "get_conn", lambda: conn)

    try:
        integraciones_tienda.reiniciar_integracion_shopify_cliente(
            "pescajacks", "pescajacks-prueba.myshopify.com",
        )
    except ValueError as exc:
        assert "otra cuenta" in str(exc)
    else:
        raise AssertionError("debía rechazar el reset entre tenants")

    assert all(
        "DELETE FROM" not in query for query, _params in cursor.ejecutadas
    )
    assert conn.commits == 0


def test_limpieza_huerfana_retira_shopify_y_preserva_historia(monkeypatch):
    from servicios import integraciones_tienda

    cursor = _Cursor([
        {
            "bindings_activos": 0,
            "bindings_inactivos": 1,
            "instalaciones": 1,
        },
        {"n": 229},
        {"n": 458},
        {"n": 7},
        {"n": 3},
        {"n": 5},
        {"n": 7},
        {"n": 3},
        {"n": 5},
    ])
    conn = _Conn(cursor)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "get_conn", lambda: conn)

    resultado = integraciones_tienda.limpiar_espejo_shopify_huerfano_cliente(
        "pescajacks",
    )

    sql = "\n".join(query for query, _params in cursor.ejecutadas)
    assert resultado["productos"] == 229
    assert resultado["bindings_inactivos_retirados"] == 1
    assert resultado["instalaciones_retiradas"] == 1
    assert "DELETE FROM tiendas_conectadas" in sql
    assert "DELETE FROM shopify_instalaciones" in sql
    assert "DELETE FROM producto_inventario_ubicaciones" in sql
    assert "DELETE FROM productos" in sql
    assert "DELETE FROM envios" not in sql
    assert "DELETE FROM pagos" not in sql
    assert "DELETE FROM solicitudes_guia" not in sql
    assert "shopify.orphan_mirror_cleanup" in str(cursor.ejecutadas[-1][1])
    assert conn.commits == 1
