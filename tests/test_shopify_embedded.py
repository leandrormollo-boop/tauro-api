from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

import pytest


CLIENT_ID = "client-publico"
CLIENT_SECRET = "secret-publico"
SHOP = "tauro-qa.myshopify.com"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _token(payload: dict, *, secret: str = CLIENT_SECRET, alg: str = "HS256") -> str:
    header = _b64(json.dumps({"alg": alg, "typ": "JWT"}).encode())
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    signature = _b64(
        hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    )
    return f"{header}.{body}.{signature}"


def _claims(**changes) -> dict:
    claims = {
        "iss": f"https://{SHOP}/admin",
        "dest": f"https://{SHOP}",
        "aud": CLIENT_ID,
        "sub": "42",
        "exp": 1060,
        "nbf": 990,
        "iat": 990,
    }
    claims.update(changes)
    return claims


@pytest.fixture(autouse=True)
def _credenciales(monkeypatch):
    from servicios import shopify_embedded

    monkeypatch.setattr(
        shopify_embedded,
        "_credenciales_publicas",
        lambda: (CLIENT_ID, CLIENT_SECRET),
    )


def test_session_token_valido_selecciona_shop_desde_dest(monkeypatch):
    from servicios.shopify_embedded import validar_session_token

    payload = validar_session_token(_token(_claims()), ahora=1000)

    assert payload["shop"] == SHOP
    assert payload["aud"] == CLIENT_ID


@pytest.mark.parametrize(
    ("token", "code"),
    [
        (_token(_claims(), secret="otro"), "TOKEN_SIGNATURE_INVALID"),
        (_token(_claims(aud="otro-client")), "TOKEN_AUDIENCE_INVALID"),
        (_token(_claims(exp=1000)), "TOKEN_EXPIRED"),
        (_token(_claims(nbf=1001)), "TOKEN_NOT_YET_VALID"),
        (
            _token(_claims(iss="https://otra.myshopify.com/admin")),
            "TOKEN_DESTINATION_INVALID",
        ),
        (
            _token(_claims(dest="https://tauro-qa.myshopify.com.evil.example")),
            "TOKEN_DESTINATION_INVALID",
        ),
        (_token(_claims(), alg="none"), "TOKEN_ALGORITHM_INVALID"),
    ],
)
def test_session_token_falla_cerrado(token, code):
    from servicios.shopify_embedded import (
        ShopifyEmbeddedAuthError,
        validar_session_token,
    )

    with pytest.raises(ShopifyEmbeddedAuthError) as error:
        validar_session_token(token, ahora=1000)
    assert error.value.code == code


def test_session_token_exige_instalacion_publica_exacta(monkeypatch):
    from servicios import shopify_app, shopify_embedded

    monkeypatch.setattr(shopify_app, "api_key_publica", lambda: CLIENT_ID)
    monkeypatch.setattr(shopify_app, "instalacion", lambda _shop: {
        "app_client_id": "app-ajena",
        "webhooks_ready": True,
        "token_rotativo": True,
        "access_token": "token",
    })

    with pytest.raises(shopify_embedded.ShopifyEmbeddedAuthError) as error:
        shopify_embedded.instalacion_para_session({"shop": SHOP})
    assert error.value.code == "REAUTHORIZE"


class _State:
    csp_nonce = "nonce-prueba"


class _Request:
    def __init__(self, *, authorization: str = "", query_params=None):
        self.headers = {"authorization": authorization} if authorization else {}
        self.query_params = query_params or {}
        self.cookies = {}
        self.state = _State()


def test_app_home_carga_app_bridge_primero_y_csp_es_por_tienda(monkeypatch):
    from endpoints import shopify

    host = base64.urlsafe_b64encode(b"admin.shopify.com/store/tauro-qa").decode().rstrip("=")
    monkeypatch.setattr(shopify, "app_configurada", lambda: True)
    monkeypatch.setattr(shopify, "api_key_publica", lambda: CLIENT_ID)

    response = shopify.app_home(_Request(), shop=SHOP, host=host)
    body = response.body.decode()
    scripts = [line.strip() for line in body.splitlines() if "<script" in line]

    assert response.status_code == 200
    assert "cdn.shopify.com/shopifycloud/app-bridge.js" in scripts[0]
    assert "polaris-1.js" in scripts[1]
    assert body.index("app-bridge.js") < body.index("polaris-1.js")
    assert f"frame-ancestors https://{SHOP} https://admin.shopify.com;" in response.headers[
        "content-security-policy"
    ]
    assert "x-frame-options" not in response.headers
    assert "destinatario" not in body


def test_app_data_usa_jwt_y_no_expone_pii(monkeypatch):
    from endpoints import shopify
    from servicios import integraciones_tienda

    ahora = int(time.time())
    token = _token(_claims(exp=ahora + 60, nbf=ahora - 5, iat=ahora - 5))
    monkeypatch.setattr(shopify, "instalacion_para_session", lambda payload: {
        "cliente_id": "MELCIOR",
        "app_client_id": CLIENT_ID,
        "webhooks_ready": True,
        "token_rotativo": True,
        "access_token": "token",
    })
    consultas = []
    monkeypatch.setattr(
        integraciones_tienda,
        "listar_resumen_pedidos_shopify_embebido",
        lambda shop, cliente: consultas.append((shop, cliente)) or [{
            "numero": "#1001",
            "pedido_externo_id": "gid://shopify/Order/1",
            "estado": "PENDIENTE",
            "valor_total": 25,
            "moneda": "USD",
            "created_at": datetime(2026, 9, 25, tzinfo=timezone.utc),
            "destinatario": {"email": "no-debe-salir@example.com"},
        }],
    )

    response = shopify.app_home_data(_Request(authorization=f"Bearer {token}"))
    encoded = json.dumps(response)

    assert response["shop"] == SHOP
    assert response["linked"] is True
    assert consultas == [(SHOP, "MELCIOR")]
    assert response["orders"][0]["number"] == "#1001"
    assert "destinatario" not in encoded
    assert "no-debe-salir" not in encoded


def test_app_data_rechaza_falta_de_bearer_con_retry_header():
    from endpoints import shopify

    response = shopify.app_home_data(_Request())

    assert response.status_code == 401
    assert response.headers["x-shopify-retry-invalid-session-request"] == "1"


def test_app_data_exige_reautorizacion_para_instalacion_no_operativa(monkeypatch):
    from endpoints import shopify

    ahora = int(time.time())
    token = _token(_claims(exp=ahora + 60, nbf=ahora - 5, iat=ahora - 5))
    monkeypatch.setattr(
        shopify,
        "instalacion_para_session",
        lambda _payload: (_ for _ in ()).throw(
            shopify.ShopifyEmbeddedAuthError("REAUTHORIZE")
        ),
    )

    response = shopify.app_home_data(_Request(authorization=f"Bearer {token}"))

    assert response.status_code == 409
    assert json.loads(response.body)["code"] == "REAUTHORIZE"


def test_oauth_state_firma_shop_host_y_expiracion(monkeypatch):
    from servicios import shopify_embedded

    state = shopify_embedded.crear_estado_oauth(SHOP, ahora=1000)
    payload = shopify_embedded.verificar_estado_oauth(state, SHOP, ahora=1001)

    assert payload["shop"] == SHOP
    assert shopify_embedded.shop_desde_host_embebido(payload["host"]) == SHOP
    with pytest.raises(ValueError):
        shopify_embedded.verificar_estado_oauth(state, SHOP, ahora=1601)
    with pytest.raises(ValueError):
        shopify_embedded.verificar_estado_oauth(state, "otra.myshopify.com", ahora=1001)


def test_host_legacy_oficial_se_preserva_solo_si_corresponde_al_shop():
    from servicios.shopify_embedded import (
        host_embebido_para_shop,
        shop_desde_host_embebido,
    )

    legacy = _b64(f"{SHOP}/admin".encode())

    assert host_embebido_para_shop(SHOP, legacy) == legacy
    assert shop_desde_host_embebido(legacy) == SHOP
    with pytest.raises(ValueError):
        host_embebido_para_shop("otra.myshopify.com", legacy)


def test_consulta_de_pedidos_ata_tienda_y_owner_sin_columnas_pii(monkeypatch):
    from servicios import integraciones_tienda

    class Cursor:
        def __init__(self):
            self.sql = ""
            self.params = ()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params):
            self.sql = " ".join(sql.split())
            self.params = params

        def fetchall(self):
            return []

    class Conn:
        def __init__(self, cursor):
            self.cursor_value = cursor

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return self.cursor_value

    cursor = Cursor()
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "get_conn", lambda: Conn(cursor))

    resultado = integraciones_tienda.listar_resumen_pedidos_shopify_embebido(
        SHOP, "melcior", limite=999,
    )

    assert resultado == []
    assert "LOWER(t.dominio) = %s" in cursor.sql
    assert "UPPER(t.cliente_id) = %s" in cursor.sql
    assert "UPPER(p.cliente_id) = %s" in cursor.sql
    assert cursor.params == (SHOP, "MELCIOR", "MELCIOR", 50)
    for pii in ("destinatario", "direccion", "email", "telefono", "items"):
        assert pii not in cursor.sql.lower()
