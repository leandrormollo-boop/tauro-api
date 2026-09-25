"""Seguridad de la superficie Shopify embebida.

El HTML inicial no contiene datos privados. App Bridge obtiene un ID token
(antes llamado session token) y cada lectura del backend se autentica con ese
JWT de un minuto. No se usa una cookie de sesión dentro del iframe.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import secrets
import time
from urllib.parse import quote, urlsplit


APP_BRIDGE_CDN = "https://cdn.shopify.com/shopifycloud/app-bridge.js"
POLARIS_CDN = "https://cdn.shopify.com/shopifycloud/polaris-1.js"
_OAUTH_STATE_TTL_SECONDS = 600
_MAX_JWT_BYTES = 8192


class ShopifyEmbeddedAuthError(ValueError):
    def __init__(self, code: str, message: str = "Sesión Shopify inválida."):
        super().__init__(message)
        self.code = code


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    if not value or len(value) > _MAX_JWT_BYTES:
        raise ShopifyEmbeddedAuthError("TOKEN_MALFORMED")
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError) as exc:
        raise ShopifyEmbeddedAuthError("TOKEN_MALFORMED") from exc


def _json_object(segment: str) -> dict:
    try:
        value = json.loads(_b64url_decode(segment).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShopifyEmbeddedAuthError("TOKEN_MALFORMED") from exc
    if not isinstance(value, dict):
        raise ShopifyEmbeddedAuthError("TOKEN_MALFORMED")
    return value


def _credenciales_publicas() -> tuple[str, str]:
    from servicios.shopify_app import _credenciales_publicas as credenciales

    return credenciales()


def _timestamp_claim(payload: dict, name: str) -> float:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ShopifyEmbeddedAuthError(f"TOKEN_{name.upper()}_INVALID")
    value = float(value)
    if not math.isfinite(value):
        raise ShopifyEmbeddedAuthError(f"TOKEN_{name.upper()}_INVALID")
    return value


def validar_session_token(token: str, *, ahora: float | None = None) -> dict:
    """Valida completamente un ID/session token emitido para esta app.

    Shopify firma estos JWT con HS256 y el client secret. Además de la firma,
    el vínculo de ``iss``/``dest`` y el dominio myshopify evitan que un token
    válido para otra tienda pueda seleccionar datos locales.
    """
    token = str(token or "").strip()
    if len(token.encode("utf-8")) > _MAX_JWT_BYTES:
        raise ShopifyEmbeddedAuthError("TOKEN_MALFORMED")
    partes = token.split(".")
    if len(partes) != 3:
        raise ShopifyEmbeddedAuthError("TOKEN_MALFORMED")
    header = _json_object(partes[0])
    payload = _json_object(partes[1])
    if header.get("alg") != "HS256":
        raise ShopifyEmbeddedAuthError("TOKEN_ALGORITHM_INVALID")

    client_id, client_secret = _credenciales_publicas()
    if not client_id or not client_secret:
        raise ShopifyEmbeddedAuthError("APP_NOT_CONFIGURED")
    firma = _b64url_decode(partes[2])
    esperada = hmac.new(
        client_secret.encode("utf-8"),
        f"{partes[0]}.{partes[1]}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(firma, esperada):
        raise ShopifyEmbeddedAuthError("TOKEN_SIGNATURE_INVALID")

    if payload.get("aud") != client_id:
        raise ShopifyEmbeddedAuthError("TOKEN_AUDIENCE_INVALID")
    ahora = float(time.time() if ahora is None else ahora)
    if _timestamp_claim(payload, "exp") <= ahora:
        raise ShopifyEmbeddedAuthError("TOKEN_EXPIRED")
    if _timestamp_claim(payload, "nbf") > ahora:
        raise ShopifyEmbeddedAuthError("TOKEN_NOT_YET_VALID")

    try:
        issuer = urlsplit(str(payload.get("iss") or ""))
        destino = urlsplit(str(payload.get("dest") or ""))
        issuer_port = issuer.port
        destino_port = destino.port
    except ValueError as exc:
        raise ShopifyEmbeddedAuthError("TOKEN_DESTINATION_INVALID") from exc
    issuer_host = str(issuer.hostname or "").lower()
    destino_host = str(destino.hostname or "").lower()
    from servicios.shopify_app import dominio_valido

    if (
        issuer.scheme != "https"
        or destino.scheme != "https"
        or issuer_port is not None
        or destino_port is not None
        or issuer.username is not None
        or destino.username is not None
        or issuer.password is not None
        or destino.password is not None
        or issuer_host != destino_host
        or not dominio_valido(destino_host)
        or issuer.path.rstrip("/") != "/admin"
        or destino.path not in ("", "/")
        or issuer.query
        or destino.query
        or issuer.fragment
        or destino.fragment
    ):
        raise ShopifyEmbeddedAuthError("TOKEN_DESTINATION_INVALID")

    validado = dict(payload)
    validado["shop"] = destino_host
    return validado


def bearer_token(authorization: str) -> str:
    partes = str(authorization or "").strip().split()
    if len(partes) != 2 or partes[0].lower() != "bearer" or not partes[1]:
        raise ShopifyEmbeddedAuthError("TOKEN_MISSING")
    return partes[1]


def instalacion_para_session(payload: dict) -> dict:
    """Exige que el JWT corresponda a la generación pública operativa."""
    from servicios.shopify_app import api_key_publica, instalacion

    shop = str(payload.get("shop") or "").lower()
    fila = instalacion(shop)
    if not fila:
        raise ShopifyEmbeddedAuthError("REAUTHORIZE")
    if str(fila.get("app_client_id") or "") != api_key_publica():
        raise ShopifyEmbeddedAuthError("REAUTHORIZE")
    if (
        not fila.get("webhooks_ready")
        or not fila.get("token_rotativo")
        or not fila.get("access_token")
    ):
        raise ShopifyEmbeddedAuthError("REAUTHORIZE")
    return fila


def host_embebido_para_shop(shop: str, host: str = "") -> str:
    """Normaliza ``host`` o lo deriva del dominio validado, sin confiar en input."""
    from servicios.shopify_app import dominio_valido

    shop = str(shop or "").strip().lower()
    if not dominio_valido(shop):
        raise ValueError("Dominio Shopify inválido.")
    handle = shop.removesuffix(".myshopify.com")
    canonico = f"admin.shopify.com/store/{handle}"
    permitidos = {canonico, f"{shop}/admin"}
    host = str(host or "").strip()
    if host:
        try:
            decodificado = _b64url_decode(host).decode("utf-8")
        except (ShopifyEmbeddedAuthError, UnicodeDecodeError) as exc:
            raise ValueError("Host Shopify inválido.") from exc
        if decodificado not in permitidos:
            raise ValueError("El host no corresponde a la tienda.")
        return _b64url_encode(decodificado.encode("utf-8"))
    return _b64url_encode(canonico.encode("utf-8"))


def shop_desde_host_embebido(host: str) -> str:
    try:
        decodificado = _b64url_decode(str(host or "").strip()).decode("utf-8")
    except (ShopifyEmbeddedAuthError, UnicodeDecodeError):
        return ""
    from servicios.shopify_app import dominio_valido

    prefijo = "admin.shopify.com/store/"
    if decodificado.startswith(prefijo):
        handle = decodificado[len(prefijo):]
        if not handle or "/" in handle:
            return ""
        shop = f"{handle}.myshopify.com"
        return shop if dominio_valido(shop) else ""
    if decodificado.endswith("/admin"):
        shop = decodificado.removesuffix("/admin")
        return shop if dominio_valido(shop) else ""
    return ""


def crear_estado_oauth(shop: str, host: str = "", *, ahora: int | None = None) -> str:
    client_id, client_secret = _credenciales_publicas()
    if not client_id or not client_secret:
        raise ValueError("Shopify no está configurado.")
    ahora = int(time.time() if ahora is None else ahora)
    payload = {
        "v": 1,
        "shop": shop,
        "host": host_embebido_para_shop(shop, host),
        "exp": ahora + _OAUTH_STATE_TTL_SECONDS,
        "nonce": secrets.token_urlsafe(18),
    }
    cuerpo = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    firma = _b64url_encode(
        hmac.new(client_secret.encode("utf-8"), cuerpo.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{cuerpo}.{firma}"


def verificar_estado_oauth(
    state: str,
    shop: str,
    *,
    ahora: int | None = None,
) -> dict:
    partes = str(state or "").split(".")
    if len(partes) != 2:
        raise ValueError("State OAuth inválido.")
    _client_id, client_secret = _credenciales_publicas()
    if not client_secret:
        raise ValueError("Shopify no está configurado.")
    firma = _b64url_decode(partes[1])
    esperada = hmac.new(
        client_secret.encode("utf-8"), partes[0].encode("ascii"), hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(firma, esperada):
        raise ValueError("State OAuth inválido.")
    payload = _json_object(partes[0])
    ahora = int(time.time() if ahora is None else ahora)
    if payload.get("v") != 1 or payload.get("shop") != shop:
        raise ValueError("State OAuth inválido.")
    exp = payload.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, int) or exp < ahora:
        raise ValueError("State OAuth expirado.")
    host_embebido_para_shop(shop, str(payload.get("host") or ""))
    return payload


def url_admin_app(shop: str) -> str:
    """Destino embebido derivado sólo del shop verificado por OAuth/JWT."""
    host_embebido_para_shop(shop)
    handle = shop.removesuffix(".myshopify.com")
    client_id, _secret = _credenciales_publicas()
    if not client_id:
        raise ValueError("Shopify no está configurado.")
    return (
        "https://admin.shopify.com/store/"
        f"{quote(handle, safe='')}/apps/{quote(client_id, safe='')}"
    )
