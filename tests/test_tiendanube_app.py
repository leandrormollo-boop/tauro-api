from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path

import pytest
from starlette.requests import Request


class _Response:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = data
        self.text = text

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class _Request:
    def __init__(self, body=b"", headers=None, cookies=None):
        self._body = body
        self.headers = headers or {}
        self.cookies = cookies or {}

    async def body(self):
        return self._body


class _Cursor:
    def __init__(self, respuestas=()):
        self.respuestas = iter(respuestas)
        self.ejecutadas = []
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


def test_oauth_incluye_redirect_uri_canonica(monkeypatch):
    from servicios import tiendanube_app

    enviados = []
    monkeypatch.setenv("BASE_URL", "https://nacionales.taurosolutions.ar/")
    monkeypatch.setenv("TIENDANUBE_CLIENT_ID", "client-id")
    monkeypatch.setenv("TIENDANUBE_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(
        tiendanube_app.requests,
        "post",
        lambda *args, **kwargs: enviados.append((args, kwargs))
        or _Response(200, {"access_token": "token", "user_id": 123}),
    )

    assert tiendanube_app.canjear_token("codigo")["user_id"] == 123
    payload = enviados[0][1]["json"]
    assert payload["redirect_uri"] == (
        "https://nacionales.taurosolutions.ar/integraciones/tiendanube/callback"
    )
    assert payload["grant_type"] == "authorization_code"


def test_token_se_cifra_y_admite_rotacion(monkeypatch):
    from servicios import tiendanube_app

    monkeypatch.setenv("TIENDANUBE_CLIENT_SECRET", "fallback-estable")
    monkeypatch.setenv("TIENDANUBE_TOKEN_ENCRYPTION_KEY", "clave-anterior")
    cifrado = tiendanube_app._cifrar_token("token-privado")
    assert cifrado.startswith("enc:v1:")
    assert "token-privado" not in cifrado

    monkeypatch.setenv("TIENDANUBE_TOKEN_ENCRYPTION_KEY", "clave-nueva")
    monkeypatch.setenv("TIENDANUBE_TOKEN_ENCRYPTION_KEY_PREVIOUS", "clave-anterior")
    assert tiendanube_app._descifrar_token(cifrado) == "token-privado"


def test_api_envia_authorization_actual_y_header_legacy(monkeypatch):
    from servicios import tiendanube_app

    llamadas = []
    monkeypatch.setattr(
        tiendanube_app.requests, "request",
        lambda *args, **kwargs: llamadas.append((args, kwargs)) or _Response(200, {}),
    )
    tiendanube_app._api("123", "token", "GET", "store")
    headers = llamadas[0][1]["headers"]
    assert headers["Authorization"] == "Bearer token"
    assert headers["Authentication"] == "bearer token"
    assert llamadas[0][0][1].startswith(
        "https://api.tiendanube.com/2025-03/123/"
    )


def test_cookie_oauth_esta_firmada_y_no_admite_cambiar_owner(monkeypatch):
    from servicios import tiendanube_app

    monkeypatch.setenv("TIENDANUBE_CLIENT_SECRET", "secret-app")
    cookie = tiendanube_app.firmar_oauth_cookie("state-1", "MELCIOR")
    assert tiendanube_app.validar_oauth_cookie(cookie, "state-1") == "MELCIOR"
    adulterada = cookie.replace(":MELCIOR:", ":OTRO:")
    assert tiendanube_app.validar_oauth_cookie(adulterada, "state-1") == ""
    assert tiendanube_app.validar_oauth_cookie(cookie, "state-2") == ""


def test_webhooks_requeridos_cubren_lifecycle_pedidos_y_privacidad():
    from servicios.tiendanube_app import WEBHOOKS_REQUERIDOS

    assert set(WEBHOOKS_REQUERIDOS) == {
        "order/created", "order/updated", "order/cancelled",
        "app/uninstalled", "app/suspended", "app/resumed",
        "store/redact", "customers/redact", "customers/data_request",
    }


def test_registro_webhooks_es_idempotente_por_listado_existente(monkeypatch):
    from servicios import tiendanube_app

    destino = "https://taurosolutions.ar/integraciones/tiendanube/webhook"
    existentes = [
        {"event": evento, "url": destino}
        for evento in tiendanube_app.WEBHOOKS_REQUERIDOS
    ]
    llamadas = []

    def api(_store, _token, metodo, path, payload=None, **_kwargs):
        llamadas.append((metodo, path, payload))
        return _Response(200, existentes)

    monkeypatch.setattr(tiendanube_app, "_api", api)
    assert tiendanube_app.registrar_webhooks("123", "token") == list(
        tiendanube_app.WEBHOOKS_REQUERIDOS
    )
    assert [x for x in llamadas if x[0] == "POST"] == []
    assert len([x for x in llamadas if x[0] == "GET"]) == 2


def test_registro_crea_solo_faltantes_y_verifica_despues(monkeypatch):
    from servicios import tiendanube_app

    destino = "https://taurosolutions.ar/integraciones/tiendanube/webhook"
    filas = [{"event": "order/created", "url": destino}]
    posts = []

    def api(_store, _token, metodo, _path, payload=None, **_kwargs):
        if metodo == "GET":
            return _Response(200, list(filas))
        posts.append(payload["event"])
        filas.append({"event": payload["event"], "url": payload["url"]})
        return _Response(201, {})

    monkeypatch.setattr(tiendanube_app, "_api", api)
    assert len(tiendanube_app.registrar_webhooks("123", "token")) == 9
    assert "order/created" not in posts
    assert set(posts) == set(tiendanube_app.WEBHOOKS_REQUERIDOS) - {"order/created"}


def test_422_no_declara_exito_si_get_no_confirma(monkeypatch):
    from servicios import tiendanube_app

    monkeypatch.setattr(
        tiendanube_app, "_api",
        lambda *_args, **_kwargs: _Response(200, [])
        if _args[2] == "GET" else _Response(422, {}),
    )
    with pytest.raises(tiendanube_app.TiendanubeWebhookError):
        tiendanube_app.registrar_webhooks("123", "token")


def test_payload_privacidad_no_persiste_pii():
    from servicios.tiendanube_app import sanitizar_payload_webhook

    limpio = sanitizar_payload_webhook({
        "store_id": 123,
        "event": "customers/data_request",
        "customer": {
            "id": 9, "email": "persona@example.com", "phone": "+54",
            "identification": "DNI",
        },
        "orders_requested": [1, 2],
        "checkouts_requested": [3],
        "data_request": {"id": 77},
    })

    serializado = json.dumps(limpio)
    assert "persona@example.com" not in serializado
    assert "+54" not in serializado
    assert "DNI" not in serializado
    assert limpio["customer_id"] == "9"
    assert limpio["request_id"] == "77"


def test_idempotencia_usa_header_y_sin_id_no_suprime_actualizaciones_futuras():
    from servicios.tiendanube_app import webhook_evento_id

    datos_a = {"event": "order/created", "store_id": 123, "id": 88}
    datos_b = {"id": 88, "store_id": 123, "event": "order/created"}
    assert webhook_evento_id(datos_a, b"a", "delivery-1") == "delivery-1"
    primero = webhook_evento_id(datos_a, b"a")
    segundo = webhook_evento_id(datos_b, b"b")
    assert primero.startswith("generated-")
    assert segundo.startswith("generated-")
    assert primero != segundo


def test_claim_valido_vincula_sin_reinstalar(monkeypatch):
    from servicios import tiendanube_app

    secreto = "claim-seguro"
    cursor = _Cursor([{
        "cliente_id": None,
        "estado": "ACTIVA",
        "claim_token_hash": hashlib.sha256(secreto.encode()).hexdigest(),
        "claim_vigente": True,
    }])
    conn = _Conn(cursor)
    vinculaciones = []
    monkeypatch.setattr(tiendanube_app, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(tiendanube_app, "get_conn", lambda: conn)
    monkeypatch.setattr(
        tiendanube_app, "vincular_cliente",
        lambda store_id, cliente: vinculaciones.append((store_id, cliente)),
    )

    assert tiendanube_app.reclamar_con_token(
        f"123.{secreto}", "melcior",
    ) == "123"
    assert vinculaciones == [("123", "melcior")]


def test_claim_invalido_no_vincula(monkeypatch):
    from servicios import tiendanube_app

    cursor = _Cursor([{
        "cliente_id": None,
        "estado": "ACTIVA",
        "claim_token_hash": hashlib.sha256(b"otro").hexdigest(),
        "claim_vigente": True,
    }])
    monkeypatch.setattr(tiendanube_app, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(tiendanube_app, "get_conn", lambda: _Conn(cursor))
    monkeypatch.setattr(
        tiendanube_app, "vincular_cliente",
        lambda *_args: pytest.fail("no debía vincular"),
    )
    with pytest.raises(tiendanube_app.TiendanubeClaimError):
        tiendanube_app.reclamar_con_token("123.falso", "MELCIOR")


def test_callback_ownerless_entrega_claim_y_no_pide_reinstalar(monkeypatch):
    from endpoints import integraciones
    from servicios import tiendanube_app, tiendanube_shipping

    monkeypatch.setattr(tiendanube_app, "app_configurada", lambda: True)
    monkeypatch.setattr(
        tiendanube_app, "canjear_token",
        lambda _code: {"access_token": "token", "user_id": 123},
    )
    monkeypatch.setattr(tiendanube_app, "datos_tienda", lambda *_: {"name": {"es": "Tienda"}})
    monkeypatch.setattr(tiendanube_app, "guardar_instalacion", lambda *_: "123.claim")
    monkeypatch.setattr(
        tiendanube_app, "registrar_webhooks",
        lambda *_: list(tiendanube_app.WEBHOOKS_REQUERIDOS),
    )
    monkeypatch.setattr(tiendanube_app, "confirmar_webhooks", lambda *_: True)
    monkeypatch.setattr(
        tiendanube_shipping, "registrar_shipping_carrier",
        lambda *_: {"ready": True},
    )
    monkeypatch.setattr(
        tiendanube_app, "vincular_cliente",
        lambda *_: pytest.fail("un callback externo no debe adivinar owner"),
    )

    respuesta = integraciones.tiendanube_callback(_Request(), code="code")
    cuerpo = respuesta.body.decode("utf-8")
    assert respuesta.status_code == 200
    assert "sin pedirte que reinstales" in cuerpo
    cookies = b" ".join(
        valor for clave, valor in respuesta.raw_headers
        if clave.lower() == b"set-cookie"
    )
    assert b"tn_claim=" in cookies


def test_callback_no_declara_exito_si_fallan_webhooks(monkeypatch):
    from endpoints import integraciones
    from servicios import tiendanube_app

    monkeypatch.setattr(tiendanube_app, "app_configurada", lambda: True)
    monkeypatch.setattr(
        tiendanube_app, "canjear_token",
        lambda _code: {"access_token": "token", "user_id": 123},
    )
    monkeypatch.setattr(tiendanube_app, "datos_tienda", lambda *_: {})
    monkeypatch.setattr(tiendanube_app, "guardar_instalacion", lambda *_: "123.claim")
    monkeypatch.setattr(
        tiendanube_app, "registrar_webhooks",
        lambda *_: (_ for _ in ()).throw(tiendanube_app.TiendanubeWebhookError("falló")),
    )

    respuesta = integraciones.tiendanube_callback(_Request(), code="code")
    assert respuesta.status_code == 502
    assert "Instalación pendiente" in respuesta.body.decode("utf-8")


def test_callback_no_declara_exito_si_shipping_no_esta_listo(monkeypatch):
    from endpoints import integraciones
    from servicios import tiendanube_app, tiendanube_shipping

    monkeypatch.setattr(tiendanube_app, "app_configurada", lambda: True)
    monkeypatch.setattr(
        tiendanube_app, "canjear_token",
        lambda _code: {"access_token": "token", "user_id": 123},
    )
    monkeypatch.setattr(tiendanube_app, "datos_tienda", lambda *_: {})
    monkeypatch.setattr(tiendanube_app, "guardar_instalacion", lambda *_: "123.claim")
    monkeypatch.setattr(
        tiendanube_app, "registrar_webhooks",
        lambda *_: list(tiendanube_app.WEBHOOKS_REQUERIDOS),
    )
    monkeypatch.setattr(
        tiendanube_shipping, "registrar_shipping_carrier",
        lambda *_: {"ready": False, "reason": "tarifas_nacionales_no_habilitadas"},
    )
    monkeypatch.setattr(
        tiendanube_app, "confirmar_webhooks",
        lambda *_: pytest.fail("no debe marcar readiness sin Shipping"),
    )

    respuesta = integraciones.tiendanube_callback(_Request(), code="code")
    assert respuesta.status_code == 502
    assert "Instalación pendiente" in respuesta.body.decode("utf-8")


def _webhook_firmado(secret: str, datos: dict, **headers):
    cuerpo = json.dumps(datos, separators=(",", ":")).encode()
    firma = hmac.new(secret.encode(), cuerpo, hashlib.sha256).hexdigest()
    return _Request(
        cuerpo,
        {"x-linkedstore-hmac-sha256": firma, **headers},
    )


def test_webhook_ackea_solo_despues_de_encolar(monkeypatch):
    from endpoints import integraciones
    from servicios import integraciones_tienda, tiendanube_app

    monkeypatch.setenv("TIENDANUBE_CLIENT_SECRET", "secret")
    monkeypatch.setattr(tiendanube_app, "app_configurada", lambda: True)
    monkeypatch.setattr(integraciones_tienda, "verificar_hmac_tiendanube", lambda *_: True)
    encolados = []
    lanzados = []
    monkeypatch.setattr(
        tiendanube_app, "encolar_webhook",
        lambda evento_id, datos: encolados.append((evento_id, datos)) or True,
    )
    monkeypatch.setattr(
        tiendanube_app, "lanzar_procesamiento_eventos",
        lambda: lanzados.append(True),
    )
    req = _webhook_firmado(
        "secret", {"store_id": 123, "event": "order/created", "id": 8},
        **{"x-linkedstore-event-id": "delivery-8"},
    )

    respuesta = asyncio.run(integraciones.tiendanube_webhook(req))
    assert respuesta == {"ok": True, "encolado": True, "duplicado": False}
    assert encolados[0][0] == "delivery-8"
    assert lanzados == [True]


def test_webhook_devuelve_503_si_no_hay_commit_durable(monkeypatch):
    from endpoints import integraciones
    from servicios import integraciones_tienda, tiendanube_app

    monkeypatch.setenv("TIENDANUBE_CLIENT_SECRET", "secret")
    monkeypatch.setattr(tiendanube_app, "app_configurada", lambda: True)
    monkeypatch.setattr(integraciones_tienda, "verificar_hmac_tiendanube", lambda *_: True)
    monkeypatch.setattr(
        tiendanube_app, "encolar_webhook",
        lambda *_: (_ for _ in ()).throw(RuntimeError("db caída")),
    )
    req = _webhook_firmado(
        "secret", {"store_id": 123, "event": "order/updated", "id": 8},
    )
    respuesta = asyncio.run(integraciones.tiendanube_webhook(req))
    assert respuesta.status_code == 503


def test_webhook_rechaza_firma_invalida(monkeypatch):
    from endpoints import integraciones
    from servicios import tiendanube_app

    monkeypatch.setattr(tiendanube_app, "app_configurada", lambda: True)
    respuesta = asyncio.run(integraciones.tiendanube_webhook(_Request(
        b'{"store_id":123,"event":"order/created","id":8}',
        {"x-linkedstore-hmac-sha256": "incorrecta"},
    )))
    assert respuesta.status_code == 401


def test_worker_reintenta_fallo_transitorio_con_backoff(monkeypatch):
    from servicios import tiendanube_app

    evento = {"evento_id": "e1", "intentos": 2}
    cursor = _Cursor([])
    conn = _Conn(cursor)
    tomas = iter([evento, None])
    monkeypatch.setattr(tiendanube_app, "_tomar_evento", lambda: next(tomas))
    monkeypatch.setattr(
        tiendanube_app, "_procesar_evento",
        lambda _evento: (_ for _ in ()).throw(
            tiendanube_app.TiendanubeRetryableError("API_503")
        ),
    )
    monkeypatch.setattr(tiendanube_app, "get_conn", lambda: conn)

    assert tiendanube_app.procesar_cola_eventos() == {"procesados": 0, "errores": 1}
    sql, params = cursor.ejecutadas[-1]
    assert "proximo_intento_at" in sql
    assert params[0] == "PENDIENTE"
    assert params[2] > 0


def test_reconciliacion_completa_instalacion_parcial_sin_reinstall(monkeypatch):
    from servicios import tiendanube_app, tiendanube_shipping

    cursor = _Cursor([[{"store_id": "123", "access_token": "enc:v1:token"}]])
    monkeypatch.setattr(tiendanube_app, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(tiendanube_app, "get_conn", lambda: _Conn(cursor))
    monkeypatch.setattr(tiendanube_app, "_descifrar_token", lambda _token: "token")
    monkeypatch.setattr(
        tiendanube_app, "registrar_webhooks",
        lambda *_: list(tiendanube_app.WEBHOOKS_REQUERIDOS),
    )
    confirmadas = []
    monkeypatch.setattr(
        tiendanube_app, "confirmar_webhooks",
        lambda store_id, _eventos: confirmadas.append(store_id) or True,
    )
    monkeypatch.setattr(
        tiendanube_shipping, "registrar_shipping_carrier",
        lambda *_: {"ready": True},
    )

    assert tiendanube_app.reconciliar_instalaciones_pendientes() == {
        "completadas": 1, "errores": 0,
    }
    assert confirmadas == ["123"]


def test_evento_resumed_reactiva_misma_generacion(monkeypatch):
    from servicios import tiendanube_app

    llamadas = []
    monkeypatch.setattr(
        tiendanube_app,
        "reactivar",
        lambda store_id, generation: llamadas.append((store_id, generation)) or True,
    )

    assert tiendanube_app._procesar_evento({
        "evento": "app/resumed",
        "store_id": "123",
        "install_generation": "gen-1",
    }) == "REACTIVADA"
    assert llamadas == [("123", "gen-1")]


def test_reactivar_rehabilita_binding_y_mismo_shipping(monkeypatch):
    from servicios import tiendanube_app, tiendanube_shipping

    cursor = _Cursor([{
        "install_generation": "gen-1",
        "cliente_id": "melcior",
    }])
    conn = _Conn(cursor)
    shipping = []
    monkeypatch.setattr(tiendanube_app, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(tiendanube_app, "get_conn", lambda: conn)
    monkeypatch.setattr(
        tiendanube_shipping, "reactivar", lambda store_id: shipping.append(store_id)
    )

    assert tiendanube_app.reactivar("123", "gen-1") is True
    assert shipping == ["123"]
    assert any("SET activa = TRUE" in sql for sql, _ in cursor.ejecutadas)
    assert any("APP_RESUMED" in str(params) for _, params in cursor.ejecutadas)
    assert conn.commits == 1


def test_admin_link_solo_acepta_tienda_del_cliente(monkeypatch):
    from endpoints import portal_cliente
    from servicios import tiendanube_app

    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/portal/tienda/tiendanube/pedidos",
        "query_string": b"store=123&id=pedido-1&id=pedido-2",
        "headers": [],
    })
    monkeypatch.setattr(tiendanube_app, "instalacion", lambda _store: {
        "cliente_id": "MELCIOR",
        "estado": "ACTIVA",
        "webhooks_ready": True,
    })
    monkeypatch.setattr(
        portal_cliente,
        "listar_pedidos_tiendanube_seleccionados",
        lambda *_args, **_kwargs: [
            {
                "id": 7,
                "plataforma": "tiendanube",
                "pedido_externo_id": "pedido-1",
                "destinatario": {},
                "items": [],
            },
        ],
    )

    response = portal_cliente.tienda_tiendanube_admin_link(
        request, cliente="MELCIOR"
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.context["pedidos"]] == [7]
    assert response.context["solicitados"] == 2
    assert response.context["faltantes"] == 1


def test_consulta_admin_link_acota_cliente_tienda_plataforma_y_estado(monkeypatch):
    from servicios import integraciones_tienda

    cursor = _Cursor([[]])
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(
        integraciones_tienda, "get_conn", lambda: _Conn(cursor),
    )

    assert integraciones_tienda.listar_pedidos_tiendanube_seleccionados(
        "MELCIOR", "123", ["pedido-1", "pedido-1", "pedido-2"],
    ) == []
    sql, params = cursor.ejecutadas[0]
    normalized = " ".join(sql.split())
    assert "UPPER(p.cliente_id) = UPPER(%s)" in normalized
    assert "UPPER(t.cliente_id) = UPPER(%s)" in normalized
    assert "p.estado = 'PENDIENTE'" in normalized
    assert "p.plataforma = 'tiendanube'" in normalized
    assert "t.plataforma = 'tiendanube'" in normalized
    assert "LOWER(t.dominio) = %s" in normalized
    assert params == (
        "MELCIOR",
        "MELCIOR",
        "123.tiendanube",
        ["pedido-1", "pedido-2"],
        100,
    )


def test_admin_link_rechaza_tienda_ajena(monkeypatch):
    from endpoints import portal_cliente
    from servicios import tiendanube_app

    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/portal/tienda/tiendanube/pedidos",
        "query_string": b"store=123&id=pedido-1",
        "headers": [],
    })
    monkeypatch.setattr(tiendanube_app, "instalacion", lambda _store: {
        "cliente_id": "OTRO",
        "estado": "ACTIVA",
        "webhooks_ready": True,
    })

    response = portal_cliente.tienda_tiendanube_admin_link(
        request, cliente="MELCIOR"
    )

    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_customers_redact_anonimiza_copias_derivadas_y_crea_tombstone(
    monkeypatch,
):
    from servicios import integraciones_tienda, tiendanube_app

    cursor = _Cursor([
        [{"solicitud_id": 77}],
        [{"id": 88}],
    ])
    conn = _Conn(cursor)
    monkeypatch.setattr(tiendanube_app, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(tiendanube_app, "get_conn", lambda: conn)

    result = tiendanube_app._procesar_customers_redact({
        "store_id": "123",
        "install_generation": "gen-1",
        "payload": {"orders_to_redact": ["pedido-1"]},
    })
    sql = "\n".join(query for query, _params in cursor.ejecutadas)

    assert result == "REDACTADO"
    assert "INSERT INTO tiendanube_pedidos_redactados" in sql
    assert "UPDATE pedidos_tienda" in sql
    assert "UPDATE solicitudes_guia" in sql
    assert "UPDATE envios" in sql
    assert "UPDATE recolecciones" in sql
    assert "DELETE FROM direcciones" in sql
    assert "DELETE FROM pedidos_huerfanos" in sql
    assert "label_pdf = NULL" in sql
    assert conn.commits == 1


def test_guardar_pedido_tiendanube_no_reintroduce_orden_redactada(monkeypatch):
    from servicios import integraciones_tienda

    cursor = _Cursor([None])
    conn = _Conn(cursor)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "get_conn", lambda: conn)

    with pytest.raises(
        integraciones_tienda.TiendaNoOperativaError,
        match="Tiendanube ya no está operativa",
    ):
        integraciones_tienda.guardar_pedido(
            "MELCIOR",
            1,
            "tiendanube",
            {"pedido_externo_id": "pedido-1"},
            dominio_verificado="123.tiendanube",
            install_generation_verificada="gen-1",
        )

    sql = "\n".join(query for query, _params in cursor.ejecutadas)
    assert "pg_advisory_xact_lock" in sql
    assert "tiendanube_pedidos_redactados" in sql
    assert "INSERT INTO pedidos_tienda" not in sql
    assert conn.commits == 0


def test_marcar_enviado_actualiza_solo_fulfillment_tauro(monkeypatch):
    from servicios import tiendanube_app, tiendanube_shipping

    monkeypatch.setattr(tiendanube_app, "instalacion", lambda _store: {
        "access_token": "token",
        "estado": "ACTIVA",
        "webhooks_ready": True,
    })
    monkeypatch.setattr(tiendanube_shipping, "configuracion", lambda _store: {
        "carrier_id": "carrier-tauro",
    })
    llamadas = []

    def api(_store, _token, method, path, payload=None, **_kwargs):
        llamadas.append((method, path, payload))
        if method == "GET":
            return _Response(200, [
                {
                    "id": "ffo-tauro",
                    "status": "PACKED",
                    "shipping": {
                        "type": "ship",
                        "carrier": {"carrier_id": "carrier-tauro"},
                        "option": {"code": "tauro_nacional_domicilio"},
                    },
                },
                {
                    "id": "ffo-otro",
                    "status": "PACKED",
                    "shipping": {
                        "type": "ship",
                        "carrier": {"carrier_id": "otro"},
                        "option": {"code": "otro"},
                    },
                },
            ])
        return _Response(200, {})

    monkeypatch.setattr(tiendanube_app, "_api", api)

    assert tiendanube_app.marcar_enviado(
        "123", "pedido-1", "TRACK-1", "https://tracking.example/TRACK-1"
    ) is True
    patches = [call for call in llamadas if call[0] == "PATCH"]
    assert len(patches) == 1
    assert patches[0][1].endswith("/fulfillment-orders/ffo-tauro")
    assert patches[0][2] == {
        "status": "DISPATCHED",
        "tracking_info": {
            "code": "TRACK-1",
            "url": "https://tracking.example/TRACK-1",
            "notify_customer": True,
        },
    }
    assert not [call for call in llamadas if call[1].endswith("/fulfill")]


def test_marcar_enviado_legacy_usa_endpoint_fulfill(monkeypatch):
    from servicios import tiendanube_app, tiendanube_shipping

    monkeypatch.setattr(tiendanube_app, "instalacion", lambda _store: {
        "access_token": "token",
        "estado": "ACTIVA",
        "webhooks_ready": True,
    })
    monkeypatch.setattr(
        tiendanube_shipping, "configuracion", lambda _store: {"carrier_id": "carrier"}
    )
    llamadas = []

    def api(_store, _token, method, path, payload=None, **_kwargs):
        llamadas.append((method, path, payload))
        return _Response(404, {}) if method == "GET" else _Response(200, {})

    monkeypatch.setattr(tiendanube_app, "_api", api)

    assert tiendanube_app.marcar_enviado("123", "pedido-1", "TRACK-1") is True
    assert llamadas[-1] == (
        "POST",
        "orders/pedido-1/fulfill",
        {"shipping_tracking_number": "TRACK-1", "notify_customer": True},
    )


def test_no_hace_fallback_legacy_si_fulfillment_es_de_otro_carrier(monkeypatch):
    from servicios import tiendanube_app, tiendanube_shipping

    monkeypatch.setattr(tiendanube_app, "instalacion", lambda _store: {
        "access_token": "token",
        "estado": "ACTIVA",
        "webhooks_ready": True,
    })
    monkeypatch.setattr(
        tiendanube_shipping, "configuracion", lambda _store: {"carrier_id": "tauro"}
    )
    llamadas = []

    def api(_store, _token, method, path, payload=None, **_kwargs):
        llamadas.append((method, path, payload))
        return _Response(200, [{
            "id": "ffo-otro",
            "shipping": {
                "type": "ship",
                "carrier": {"carrier_id": "otro"},
                "option": {"code": "otro"},
            },
        }])

    monkeypatch.setattr(tiendanube_app, "_api", api)

    assert tiendanube_app.marcar_enviado("123", "pedido-1", "TRACK-1") is False
    assert [call[0] for call in llamadas] == ["GET"]


def test_filtra_fulfillments_de_otro_carrier():
    from servicios.tiendanube_app import fulfillment_orders_tauro

    order = {"fulfillments": [
        {
            "id": "tauro",
            "shipping": {
                "type": "ship",
                "carrier": {"id": "carrier-tauro"},
                "option": {"code": "codigo-legacy-distinto"},
            },
        },
        {
            "id": "otro",
            "shipping": {
                "type": "ship",
                "carrier": {"carrier_id": "otro"},
                "option": {"code": "otro"},
            },
        },
    ]}

    assert [item["id"] for item in fulfillment_orders_tauro(
        order, "carrier-tauro"
    )] == ["tauro"]


def test_pedido_sin_ffo_ni_option_code_se_reintenta_en_vez_de_descartarse(
    monkeypatch,
):
    from servicios import tiendanube_app, tiendanube_shipping

    monkeypatch.setattr(
        tiendanube_app,
        "_resolver_instalacion_operativa",
        lambda _evento: {
            "access_token": "token",
            "cliente_id": "MELCIOR",
            "install_generation": "gen-1",
        },
    )
    monkeypatch.setattr(
        tiendanube_app,
        "_api",
        lambda *_args, **_kwargs: _Response(
            200,
            {"id": 9, "fulfillments": [], "shipping": "carrier-id"},
        ),
    )
    monkeypatch.setattr(
        tiendanube_shipping,
        "configuracion",
        lambda _store: {"carrier_id": "carrier-id"},
    )

    with pytest.raises(
        tiendanube_app.TiendanubeRetryableError,
        match="FULFILLMENT_PENDIENTE",
    ):
        tiendanube_app._procesar_pedido_evento({
            "store_id": "123",
            "evento": "order/created",
            "payload": {"id": "9"},
        })


def test_pedido_con_ffo_tauro_y_otro_carrier_bloquea_multi_cd(monkeypatch):
    from servicios import tiendanube_app, tiendanube_shipping

    monkeypatch.setattr(
        tiendanube_app,
        "_resolver_instalacion_operativa",
        lambda _evento: {
            "access_token": "token",
            "cliente_id": "MELCIOR",
            "install_generation": "gen-1",
        },
    )
    order = {
        "id": 9,
        "fulfillments": [
            {
                "id": "tauro",
                "shipping": {
                    "type": "ship",
                    "option": {"code": "tauro_nacional_domicilio"},
                    "carrier": {"carrier_id": "carrier-tauro"},
                },
            },
            {
                "id": "otro",
                "shipping": {
                    "type": "ship",
                    "option": {"code": "otro"},
                    "carrier": {"carrier_id": "carrier-otro"},
                },
            },
        ],
    }
    monkeypatch.setattr(
        tiendanube_app,
        "_api",
        lambda *_args, **_kwargs: _Response(200, order),
    )
    monkeypatch.setattr(
        tiendanube_shipping,
        "configuracion",
        lambda _store: {"carrier_id": "carrier-tauro"},
    )

    with pytest.raises(
        tiendanube_app.TiendanubeRetryableError,
        match="MULTI_CD_NO_SOPORTADO",
    ):
        tiendanube_app._procesar_pedido_evento({
            "store_id": "123",
            "evento": "order/created",
            "payload": {"id": "9"},
        })


def test_fallo_db_de_privacidad_se_convierte_en_reintento(monkeypatch):
    from servicios import tiendanube_app

    monkeypatch.setattr(
        tiendanube_app,
        "_procesar_customers_redact",
        lambda _evento: (_ for _ in ()).throw(RuntimeError("db caída")),
    )

    with pytest.raises(
        tiendanube_app.TiendanubeRetryableError,
        match="EVENTO_TRANSITORIO_CUSTOMERS_REDACT",
    ):
        tiendanube_app._procesar_evento({
            "evento": "customers/redact",
            "store_id": "123",
        })


def test_fallo_db_de_pedido_se_convierte_en_reintento(monkeypatch):
    from servicios import tiendanube_app

    monkeypatch.setattr(
        tiendanube_app,
        "_procesar_pedido_evento",
        lambda _evento: (_ for _ in ()).throw(RuntimeError("db caída")),
    )

    with pytest.raises(
        tiendanube_app.TiendanubeRetryableError,
        match="EVENTO_TRANSITORIO_ORDER_CREATED",
    ):
        tiendanube_app._procesar_evento({
            "evento": "order/created",
            "store_id": "123",
        })


def test_uninstall_atrasado_no_desactiva_reinstalacion_con_token_vigente(
    monkeypatch,
):
    from servicios import tiendanube_app

    monkeypatch.setattr(
        tiendanube_app,
        "instalacion",
        lambda _store: {"estado": "ACTIVA", "access_token": "token-g2"},
    )
    monkeypatch.setattr(
        tiendanube_app,
        "_api",
        lambda *_args, **_kwargs: _Response(200, {"id": 123}),
    )
    monkeypatch.setattr(
        tiendanube_app,
        "desinstalar",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("no debe desinstalar G2")
        ),
    )

    with pytest.raises(
        tiendanube_app.TiendanubeQuarantineError,
        match="TOKEN_ACTUAL_VIGENTE",
    ):
        tiendanube_app._procesar_evento({
            "evento": "app/uninstalled",
            "store_id": "123",
            "install_generation": "gen-2",
        })


def test_uninstall_con_token_revocado_aplica_lifecycle(monkeypatch):
    from servicios import tiendanube_app

    monkeypatch.setattr(
        tiendanube_app,
        "instalacion",
        lambda _store: {"estado": "ACTIVA", "access_token": "token-revocado"},
    )
    monkeypatch.setattr(
        tiendanube_app,
        "_api",
        lambda *_args, **_kwargs: _Response(401),
    )
    applied = []
    monkeypatch.setattr(
        tiendanube_app,
        "desinstalar",
        lambda store, generation: applied.append((store, generation)) or True,
    )

    assert tiendanube_app._procesar_evento({
        "evento": "app/uninstalled",
        "store_id": "123",
        "install_generation": "gen-2",
    }) == "DESINSTALADA"
    assert applied == [("123", "gen-2")]


def test_suspended_con_api_402_aplica_suspension(monkeypatch):
    from servicios import tiendanube_app

    monkeypatch.setattr(
        tiendanube_app,
        "instalacion",
        lambda _store: {"estado": "ACTIVA", "access_token": "token-suspendido"},
    )
    monkeypatch.setattr(
        tiendanube_app,
        "_api",
        lambda *_args, **_kwargs: _Response(402),
    )
    applied = []
    monkeypatch.setattr(
        tiendanube_app,
        "suspender",
        lambda store, generation: applied.append((store, generation)) or True,
    )

    assert tiendanube_app._procesar_evento({
        "evento": "app/suspended",
        "store_id": "123",
        "install_generation": "gen-2",
    }) == "SUSPENDIDA"
    assert applied == [("123", "gen-2")]


def test_worker_persiste_cuarentena_visible(monkeypatch):
    from servicios import tiendanube_app

    evento = {
        "evento_id": "evt-stale-1",
        "evento": "store/redact",
        "store_id": "123",
        "intentos": 1,
    }
    entregados = iter((evento, None))
    monkeypatch.setattr(
        tiendanube_app, "_tomar_evento", lambda: next(entregados)
    )
    monkeypatch.setattr(
        tiendanube_app,
        "_procesar_evento",
        lambda _evento: (_ for _ in ()).throw(
            tiendanube_app.TiendanubeQuarantineError("TOKEN_ACTUAL_VIGENTE")
        ),
    )
    cursor = _Cursor()
    conn = _Conn(cursor)
    monkeypatch.setattr(tiendanube_app, "get_conn", lambda: conn)

    assert tiendanube_app.procesar_cola_eventos(limite=1) == {
        "procesados": 0,
        "errores": 1,
    }
    sql = "\n".join(query for query, _params in cursor.ejecutadas)
    assert "SET estado = %s" in sql
    assert "INSERT INTO tiendanube_privacidad_solicitudes" in sql
    assert any(
        params == ("evt-stale-1", "123", "store/redact")
        for _query, params in cursor.ejecutadas
    )


def test_store_redact_tardio_no_borra_reinstalacion_activa(monkeypatch):
    from servicios import integraciones_tienda, tiendanube_app

    cursor = _Cursor([{
        "estado": "ACTIVA",
        "install_generation": "gen-nueva",
    }])
    conn = _Conn(cursor)
    monkeypatch.setattr(tiendanube_app, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(tiendanube_app, "get_conn", lambda: conn)

    with pytest.raises(
        tiendanube_app.TiendanubeError,
        match="STORE_REDACT_INSTALACION_ACTIVA",
    ):
        tiendanube_app._procesar_store_redact({
            "store_id": "123",
            "install_generation": "gen-nueva",
        })

    sql = "\n".join(query for query, _params in cursor.ejecutadas)
    assert "FOR UPDATE" in sql
    assert "DELETE FROM tiendas_conectadas" not in sql
    assert conn.commits == 0


@pytest.mark.parametrize("accion", ["desinstalar", "suspender"])
def test_lifecycle_desactiva_binding_y_shipping(monkeypatch, accion):
    from servicios import tiendanube_app

    cursor = _Cursor([{"install_generation": "gen-1"}])
    conn = _Conn(cursor)
    shipping = []
    monkeypatch.setattr(tiendanube_app, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(tiendanube_app, "get_conn", lambda: conn)
    monkeypatch.setattr(
        tiendanube_app, "_desactivar_shipping",
        lambda store_id: shipping.append(store_id),
    )

    assert getattr(tiendanube_app, accion)("123", "gen-1") is True
    assert shipping == ["123"]
    assert any("SET activa = FALSE" in sql for sql, _ in cursor.ejecutadas)
    assert conn.commits == 1


def test_schema_declara_cola_lifecycle_claim_y_privacidad():
    schema = (Path(__file__).resolve().parents[1] / "sql" / "schema.sql").read_text(
        encoding="utf-8",
    )
    for nombre in (
        "tiendanube_instalaciones",
        "tiendanube_lifecycle_eventos",
        "tiendanube_webhook_eventos",
        "tiendanube_privacidad_solicitudes",
        "tiendanube_pedidos_redactados",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {nombre}" in schema
    assert "claim_token_hash" in schema
    assert "webhooks_ready" in schema
    assert "FOR UPDATE SKIP LOCKED" in (
        Path(__file__).resolve().parents[1] / "servicios" / "tiendanube_app.py"
    ).read_text(encoding="utf-8")
