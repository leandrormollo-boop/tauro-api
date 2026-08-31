"""Borde de Shipping Carrier para TAURO Nacional en Tiendanube.

El callback traduce el contrato de Tiendanube al ``CarrierAdapter`` neutral de
TAURO. Nunca inventa tarifas: si no hay un adapter nacional registrado y
habilitado, falla cerrado. El costo del operador y la regla comercial tampoco
salen del backend; Tiendanube recibe únicamente el precio final al comprador.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable, Mapping

from core.database import get_conn
from servicios.carrier_adapter import (
    OperationState,
    Package,
    QuoteRequest,
    QuoteResult,
    adapter_for,
    registered_adapters,
    validate_quote_request,
    validate_quote_result,
)
from servicios.carrier_contract import Ambito, Capacidad


RATE_CODE = "tauro_nacional_domicilio"
RATE_NAME = "TAURO Nacional · Entrega a domicilio"
_TRUE = {"1", "true", "yes", "si", "sí", "on"}
_tabla_lista = False


class TiendanubeShippingError(RuntimeError):
    """Base segura para errores del callback."""


class ShippingAuthenticationError(TiendanubeShippingError):
    pass


class ShippingContractError(TiendanubeShippingError):
    pass


class ShippingUnavailableError(TiendanubeShippingError):
    pass


def _enabled(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in _TRUE


def _ensure_tabla() -> None:
    global _tabla_lista
    if _tabla_lista:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tiendanube_shipping_config (
                    store_id            TEXT PRIMARY KEY,
                    callback_token_hash TEXT NOT NULL,
                    carrier_id          TEXT NOT NULL,
                    carrier_option_id   TEXT NOT NULL,
                    activa              BOOLEAN NOT NULL DEFAULT TRUE,
                    creada_en           TIMESTAMPTZ NOT NULL DEFAULT now(),
                    actualizada_en      TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        conn.commit()
    _tabla_lista = True


def hash_callback_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def configuracion(store_id: str) -> dict | None:
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM tiendanube_shipping_config WHERE store_id = %s",
                (str(store_id),),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def _guardar_config(
    store_id: str,
    token: str,
    carrier_id: str,
    option_id: str,
) -> None:
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tiendanube_shipping_config
                    (store_id, callback_token_hash, carrier_id,
                     carrier_option_id, activa)
                VALUES (%s, %s, %s, %s, TRUE)
                ON CONFLICT (store_id) DO UPDATE
                    SET callback_token_hash = EXCLUDED.callback_token_hash,
                        carrier_id = EXCLUDED.carrier_id,
                        carrier_option_id = EXCLUDED.carrier_option_id,
                        activa = TRUE,
                        actualizada_en = now()
                """,
                (
                    str(store_id),
                    hash_callback_token(token),
                    str(carrier_id),
                    str(option_id),
                ),
            )
        conn.commit()


def desactivar(store_id: str) -> None:
    """Corta cotizaciones sin borrar evidencia de la instalación."""
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tiendanube_shipping_config
                   SET activa = FALSE, actualizada_en = now()
                 WHERE store_id = %s
                """,
                (str(store_id),),
            )
        conn.commit()


def registrar_shipping_carrier(store_id: str, access_token: str) -> dict:
    """Crea el carrier y su opción fija después del OAuth.

    Los dos flags explícitos impiden que unas credenciales de Tiendanube
    publiquen accidentalmente un medio sin tarifas nacionales contractuales.
    """
    if not _enabled("TIENDANUBE_SHIPPING_ENABLED"):
        return {"ready": False, "reason": "shipping_api_no_habilitada"}
    if not _enabled("TAURO_NACIONAL_RATES_READY"):
        return {"ready": False, "reason": "tarifas_nacionales_no_habilitadas"}

    actual = configuracion(store_id)
    if actual and actual.get("activa"):
        return {
            "ready": True,
            "carrier_id": str(actual["carrier_id"]),
            "option_id": str(actual["carrier_option_id"]),
            "existing": True,
        }

    from servicios.tiendanube_app import _api

    token_callback = secrets.token_urlsafe(32)
    base = (os.getenv("BASE_URL") or "https://taurosolutions.ar").rstrip("/")
    callback_url = (
        f"{base}/integraciones/tiendanube/shipping/rates/{token_callback}"
    )
    response = _api(
        str(store_id),
        access_token,
        "POST",
        "shipping_carriers",
        {
            "name": "TAURO Nacional",
            "callback_url": callback_url,
            "types": "ship",
        },
    )
    if response is None or response.status_code not in (200, 201):
        raise ShippingUnavailableError("Tiendanube no creó el Shipping Carrier.")
    try:
        carrier_id = str(response.json()["id"])
    except Exception as exc:
        raise ShippingUnavailableError("Tiendanube devolvió un carrier inválido.") from exc

    option_response = _api(
        str(store_id),
        access_token,
        "POST",
        f"shipping_carriers/{carrier_id}/options",
        {"code": RATE_CODE, "name": RATE_NAME, "allow_free_shipping": False},
    )
    if option_response is None or option_response.status_code not in (200, 201):
        # Rollback acotado: un carrier sin opciones no es utilizable y deja una
        # instalación engañosa en el panel del merchant.
        _api(
            str(store_id), access_token, "DELETE", f"shipping_carriers/{carrier_id}"
        )
        raise ShippingUnavailableError("Tiendanube no creó la opción de envío.")
    try:
        option_id = str(option_response.json()["id"])
    except Exception as exc:
        raise ShippingUnavailableError("Tiendanube devolvió una opción inválida.") from exc

    _guardar_config(store_id, token_callback, carrier_id, option_id)
    return {
        "ready": True,
        "carrier_id": carrier_id,
        "option_id": option_id,
        "existing": False,
    }


def _decimal(value, field: str, *, positive: bool = True) -> Decimal:
    if isinstance(value, bool):
        raise ShippingContractError(f"{field} inválido.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ShippingContractError(f"{field} inválido.") from None
    if not result.is_finite() or (positive and result <= 0):
        raise ShippingContractError(f"{field} inválido.")
    return result


def _address(raw: Mapping, field: str) -> dict:
    if not isinstance(raw, Mapping):
        raise ShippingContractError(f"{field} inválido.")
    country = str(raw.get("country") or "").strip().upper()
    postal_code = "".join(ch for ch in str(raw.get("postal_code") or "") if ch.isdigit())
    if country != "AR" or len(postal_code) != 4:
        raise ShippingContractError(
            "TAURO Nacional requiere origen y destino AR con código postal de 4 dígitos."
        )
    return {
        "pais": "AR",
        "country": "AR",
        "postal_code": postal_code,
        "cp": postal_code,
        "provincia": str(raw.get("province") or "").strip(),
        "localidad": str(raw.get("locality") or raw.get("city") or "").strip(),
        "direccion": str(raw.get("address") or "").strip(),
        "numero": str(raw.get("number") or "").strip(),
        "location_id": str(raw.get("location_id") or "").strip(),
    }


def _packages(items: Iterable[Mapping], *, paid_only: bool = False) -> tuple[Package, ...]:
    packages: list[Package] = []
    for raw in items:
        if not isinstance(raw, Mapping):
            raise ShippingContractError("El carrito contiene un ítem inválido.")
        if paid_only and bool(raw.get("free_shipping")):
            continue
        try:
            quantity = int(raw.get("quantity") or 0)
        except (TypeError, ValueError):
            raise ShippingContractError("La cantidad de un ítem es inválida.") from None
        dimensions = raw.get("dimensions")
        if not isinstance(dimensions, Mapping) or not dimensions:
            raise ShippingContractError("Faltan las dimensiones de un producto.")
        grams = _decimal(raw.get("grams"), "El peso de un producto")
        packages.append(
            Package(
                quantity=quantity,
                weight_kg=grams / Decimal("1000"),
                length_cm=_decimal(dimensions.get("depth"), "El largo de un producto"),
                width_cm=_decimal(dimensions.get("width"), "El ancho de un producto"),
                height_cm=_decimal(dimensions.get("height"), "El alto de un producto"),
            )
        )
    if not packages:
        raise ShippingContractError("El carrito no contiene productos cotizables.")
    return tuple(packages)


def _declared_value(payload: Mapping, items: Iterable[Mapping], *, paid_only=False) -> Decimal:
    total = Decimal("0")
    for item in items:
        if paid_only and bool(item.get("free_shipping")):
            continue
        price = _decimal(item.get("price"), "El precio de un producto", positive=False)
        try:
            quantity = int(item.get("quantity") or 0)
        except (TypeError, ValueError):
            raise ShippingContractError("La cantidad de un ítem es inválida.") from None
        total += price * quantity
    if not paid_only:
        raw_total = payload.get("total_price")
        if raw_total not in (None, ""):
            total = _decimal(raw_total, "El total del carrito")
    if total <= 0:
        raise ShippingContractError("El carrito necesita un valor declarado positivo.")
    return total


def _request_id(payload: Mapping, suffix: str = "full") -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"tn-{suffix}-{digest}"


def _quote_request(payload: Mapping, customer_id: str, *, paid_only=False) -> QuoteRequest:
    items = payload.get("items") or []
    if not isinstance(items, list) or not items:
        raise ShippingContractError("El carrito no contiene productos.")
    currency = str(payload.get("currency") or "").strip().upper()
    if currency != "ARS":
        raise ShippingContractError("TAURO Nacional cotiza únicamente en ARS.")
    return QuoteRequest(
        request_id=_request_id(payload, "paid" if paid_only else "full"),
        customer_id=str(customer_id),
        scope=Ambito.NACIONAL,
        origin=_address(payload.get("origin") or {}, "El origen"),
        destination=_address(payload.get("destination") or {}, "El destino"),
        packages=_packages(items, paid_only=paid_only),
        declared_value=_declared_value(payload, items, paid_only=paid_only),
        declared_currency="ARS",
        origin_mode="domicilio",
        destination_mode="domicilio",
        metadata={
            "source": "tiendanube",
            "store_id": str(payload.get("store_id") or ""),
            "cart_id": str(payload.get("cart_id") or ""),
        },
    )


def _valid_quotes(adapter, request: QuoteRequest) -> tuple[QuoteResult, ...]:
    validate_quote_request(request, adapter.carrier_id)
    results = adapter.quote(request)
    valid: list[QuoteResult] = []
    for result in results:
        validate_quote_result(result, adapter.carrier_id)
        if result.state == OperationState.COTIZADO:
            valid.append(result)
    return tuple(valid)


def _default_adapters() -> tuple:
    adapters = []
    for carrier_id in registered_adapters():
        if carrier_id not in {"oca", "andreani"}:
            continue
        try:
            adapters.append(adapter_for(carrier_id, Capacidad.COTIZAR))
        except (RuntimeError, ValueError):
            continue
    return tuple(adapters)


def cotizar_callback(
    payload: Mapping,
    callback_token: str,
    *,
    installation_loader: Callable[[str], dict | None] | None = None,
    config_loader: Callable[[str], dict | None] | None = None,
    adapters: Iterable | None = None,
) -> dict:
    """Cotiza el carrito con los adapters nacionales disponibles."""
    if not isinstance(payload, Mapping):
        raise ShippingContractError("El payload no es un objeto JSON.")
    store_id = str(payload.get("store_id") or "").strip()
    if not store_id.isdigit():
        raise ShippingContractError("La tienda no es válida.")

    cfg_loader = config_loader or configuracion
    cfg = cfg_loader(store_id)
    expected_hash = str((cfg or {}).get("callback_token_hash") or "")
    if not cfg or not cfg.get("activa") or not expected_hash or not hmac.compare_digest(
        expected_hash, hash_callback_token(callback_token)
    ):
        raise ShippingAuthenticationError("Callback no autorizado.")

    if installation_loader is None:
        from servicios.tiendanube_app import instalacion

        installation_loader = instalacion
    installation = installation_loader(store_id)
    if not installation or not installation.get("cliente_id"):
        raise ShippingUnavailableError("La tienda no está vinculada a TAURO.")
    if str(installation.get("estado") or "active").lower() not in {"active", "activa"}:
        raise ShippingUnavailableError("La integración está suspendida.")

    request = _quote_request(payload, str(installation["cliente_id"]))
    candidates: list[tuple[object, QuoteResult]] = []
    carrier_responded = False
    for adapter in tuple(adapters) if adapters is not None else _default_adapters():
        try:
            quotes = _valid_quotes(adapter, request)
            carrier_responded = True
            candidates.extend((adapter, quote) for quote in quotes)
        except RuntimeError:
            continue
        except ValueError as exc:
            raise ShippingContractError(str(exc)) from None
    if not candidates:
        if carrier_responded:
            raise ShippingContractError(
                "No hay cobertura o tarifa para este envío nacional."
            )
        raise ShippingUnavailableError("No hay tarifas nacionales disponibles.")

    adapter, selected = min(
        candidates,
        key=lambda item: item[1].customer_price
        if item[1].customer_price is not None
        else Decimal("Infinity"),
    )
    if selected.customer_price is None:
        raise ShippingUnavailableError("La tarifa no tiene precio final.")

    items = payload.get("items") or []
    has_free = any(bool(item.get("free_shipping")) for item in items)
    has_paid = any(not bool(item.get("free_shipping")) for item in items)
    buyer_price = selected.customer_price
    if has_free and has_paid:
        partial_request = _quote_request(
            payload, str(installation["cliente_id"]), paid_only=True
        )
        partial_quotes = _valid_quotes(adapter, partial_request)
        same_service = next(
            (q for q in partial_quotes if q.service_code == selected.service_code), None
        )
        if same_service is None or same_service.customer_price is None:
            raise ShippingUnavailableError(
                "No se pudo calcular el descuento de envío gratis del carrito mixto."
            )
        buyer_price = same_service.customer_price

    now = datetime.now(timezone.utc)
    days = max(int(selected.estimated_days or 1), 1)
    min_date = now + timedelta(days=days)
    max_date = now + timedelta(days=days + 2)
    reference = f"tauro:{selected.carrier_id}:{selected.quote_id}"
    return {
        "rates": [
            {
                "name": RATE_NAME,
                "code": RATE_CODE,
                "price": float(buyer_price.quantize(Decimal("0.01"))),
                "price_merchant": float(
                    selected.customer_price.quantize(Decimal("0.01"))
                ),
                "currency": "ARS",
                "type": "ship",
                "min_delivery_date": min_date.isoformat(timespec="seconds"),
                "max_delivery_date": max_date.isoformat(timespec="seconds"),
                "phone_required": True,
                "id_required": False,
                "accepts_cod": False,
                "reference": reference,
            }
        ]
    }
