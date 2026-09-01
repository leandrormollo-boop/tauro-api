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
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable, Mapping
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

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
_ARGENTINA_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
_CALLBACK_DEADLINE_SECONDS = 4.0
_LIMIT_ENV = {
    "package_weight_kg": "TAURO_NACIONAL_MAX_PACKAGE_WEIGHT_KG",
    "length_cm": "TAURO_NACIONAL_MAX_LENGTH_CM",
    "width_cm": "TAURO_NACIONAL_MAX_WIDTH_CM",
    "height_cm": "TAURO_NACIONAL_MAX_HEIGHT_CM",
    "total_weight_kg": "TAURO_NACIONAL_MAX_TOTAL_WEIGHT_KG",
    "total_volume_m3": "TAURO_NACIONAL_MAX_TOTAL_VOLUME_M3",
}
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
                    label_callback_token_hash TEXT,
                    carrier_id          TEXT NOT NULL,
                    carrier_option_id   TEXT NOT NULL,
                    activa              BOOLEAN NOT NULL DEFAULT TRUE,
                    creada_en           TIMESTAMPTZ NOT NULL DEFAULT now(),
                    actualizada_en      TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                """
                ALTER TABLE tiendanube_shipping_config
                    ADD COLUMN IF NOT EXISTS label_callback_token_hash TEXT
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


def configuracion_por_label_token(token: str) -> dict | None:
    """Resuelve el store por un secreto exclusivo del callback de labels."""
    token = str(token or "")
    if len(token) < 24 or len(token) > 200:
        return None
    calculated = hash_callback_token(token)
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                  FROM tiendanube_shipping_config
                 WHERE label_callback_token_hash = %s
                   AND activa = TRUE
                 LIMIT 2
                """,
                (calculated,),
            )
            rows = cur.fetchall()
    if len(rows) != 1:
        return None
    config = dict(rows[0])
    expected = str(config.get("label_callback_token_hash") or "")
    if not hmac.compare_digest(expected, calculated):
        return None
    return config


def _guardar_config(
    store_id: str,
    token: str,
    carrier_id: str,
    option_id: str,
    *,
    label_token: str | None = None,
) -> None:
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tiendanube_shipping_config
                    (store_id, callback_token_hash, carrier_id,
                     carrier_option_id, label_callback_token_hash, activa)
                VALUES (%s, %s, %s, %s, %s, TRUE)
                ON CONFLICT (store_id) DO UPDATE
                    SET callback_token_hash = EXCLUDED.callback_token_hash,
                        carrier_id = EXCLUDED.carrier_id,
                        carrier_option_id = EXCLUDED.carrier_option_id,
                        label_callback_token_hash =
                            COALESCE(EXCLUDED.label_callback_token_hash,
                                     tiendanube_shipping_config.label_callback_token_hash),
                        activa = TRUE,
                        actualizada_en = now()
                """,
                (
                    str(store_id),
                    hash_callback_token(token),
                    str(carrier_id),
                    str(option_id),
                    hash_callback_token(label_token) if label_token else None,
                ),
            )
        conn.commit()


def _guardar_label_callback_token(store_id: str, token: str) -> None:
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tiendanube_shipping_config
                   SET label_callback_token_hash = %s,
                       actualizada_en = now()
                 WHERE store_id = %s
                """,
                (hash_callback_token(token), str(store_id)),
            )
            if cur.rowcount != 1:
                raise ShippingUnavailableError(
                    "No se pudo vincular el callback de etiquetas."
                )
        conn.commit()


def _limpiar_label_callback_token(store_id: str) -> None:
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tiendanube_shipping_config
                   SET label_callback_token_hash = NULL,
                       actualizada_en = now()
                 WHERE store_id = %s
                """,
                (str(store_id),),
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


def reactivar(store_id: str) -> None:
    """Vuelve a habilitar el mismo carrier después de ``app/resumed``."""
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tiendanube_shipping_config
                   SET activa = TRUE, actualizada_en = now()
                 WHERE store_id = %s
                """,
                (str(store_id),),
            )
        conn.commit()


def _callback_token_from_url(value: object, base: str, kind: str) -> str:
    """Recupera un secreto remoto sólo desde una URL exacta de este backend."""
    candidate = urlparse(str(value or ""))
    expected = urlparse(base)
    prefix = f"{expected.path.rstrip('/')}/integraciones/tiendanube/shipping/{kind}/"
    if (
        candidate.scheme != "https"
        or candidate.scheme != expected.scheme
        or candidate.netloc != expected.netloc
        or candidate.params
        or candidate.query
        or candidate.fragment
        or not candidate.path.startswith(prefix)
    ):
        return ""
    token = candidate.path[len(prefix):]
    if "/" in token or len(token) < 24 or len(token) > 200:
        return ""
    return token


def _reconciliar_shipping_remoto(
    store_id: str,
    access_token: str,
    base: str,
    api: Callable,
) -> dict | None:
    """Adopta un carrier creado antes de un fallo local, sin duplicarlo."""
    response = api(str(store_id), access_token, "GET", "shipping_carriers")
    if response is None or response.status_code != 200:
        raise ShippingUnavailableError(
            "No se pudo reconciliar el Shipping Carrier existente."
        )
    try:
        carriers = response.json()
    except Exception as exc:
        raise ShippingUnavailableError(
            "Tiendanube devolvió carriers inválidos."
        ) from exc
    if not isinstance(carriers, list):
        raise ShippingUnavailableError("Tiendanube devolvió carriers inválidos.")

    matches: list[dict] = []
    for carrier in carriers:
        if not isinstance(carrier, Mapping) or carrier.get("name") != "TAURO Nacional":
            continue
        rate_token = _callback_token_from_url(
            carrier.get("callback_url"), base, "rates"
        )
        if not rate_token or not carrier.get("id"):
            continue
        options_response = api(
            str(store_id),
            access_token,
            "GET",
            f"shipping_carriers/{carrier['id']}/options",
        )
        if options_response is None or options_response.status_code != 200:
            raise ShippingUnavailableError(
                "No se pudieron reconciliar las opciones de envío."
            )
        try:
            options = options_response.json()
        except Exception as exc:
            raise ShippingUnavailableError(
                "Tiendanube devolvió opciones inválidas."
            ) from exc
        own_options = [
            option
            for option in options
            if isinstance(option, Mapping)
            and str(option.get("code") or "") == RATE_CODE
            and option.get("id")
        ] if isinstance(options, list) else []
        if not own_options:
            create_option = api(
                str(store_id),
                access_token,
                "POST",
                f"shipping_carriers/{carrier['id']}/options",
                {
                    "code": RATE_CODE,
                    "name": RATE_NAME,
                    "allow_free_shipping": False,
                },
            )
            if create_option is None or create_option.status_code not in (200, 201):
                raise ShippingUnavailableError(
                    "No se pudo reparar la opción TAURO remota."
                )
            try:
                created_option = create_option.json()
                if not isinstance(created_option, Mapping) or not created_option.get("id"):
                    raise ValueError("option id")
            except Exception as exc:
                raise ShippingUnavailableError(
                    "Tiendanube devolvió una opción inválida."
                ) from exc
            own_options = [created_option]
        if len(own_options) != 1:
            raise ShippingUnavailableError(
                "El carrier remoto no tiene una opción TAURO inequívoca."
            )
        label_token = _callback_token_from_url(
            carrier.get("callback_labels_url"), base, "labels"
        )
        matches.append({
            "store_id": str(store_id),
            "activa": bool(carrier.get("active", True)),
            "carrier_id": str(carrier["id"]),
            "carrier_option_id": str(own_options[0]["id"]),
            "rate_token": rate_token,
            "label_token": label_token,
            "callback_token_hash": hash_callback_token(rate_token),
            "label_callback_token_hash": (
                hash_callback_token(label_token) if label_token else None
            ),
        })

    if len(matches) > 1:
        raise ShippingUnavailableError(
            "Hay más de un Shipping Carrier TAURO para reconciliar."
        )
    if not matches:
        return None
    match = matches[0]
    _guardar_config(
        store_id,
        match["rate_token"],
        match["carrier_id"],
        match["carrier_option_id"],
        label_token=match["label_token"] or None,
    )
    return match


def registrar_shipping_carrier(store_id: str, access_token: str) -> dict:
    """Crea el carrier y su opción fija después del OAuth.

    Los dos flags explícitos impiden que unas credenciales de Tiendanube
    publiquen accidentalmente un medio sin tarifas nacionales contractuales.
    """
    if not _enabled("TIENDANUBE_SHIPPING_ENABLED"):
        return {"ready": False, "reason": "shipping_api_no_habilitada"}
    if not _enabled("TAURO_NACIONAL_RATES_READY"):
        return {"ready": False, "reason": "tarifas_nacionales_no_habilitadas"}

    from servicios.tiendanube_app import _api
    from servicios.tiendanube_labels import labels_execution_ready

    base = (os.getenv("BASE_URL") or "https://taurosolutions.ar").rstrip("/")
    parsed_base = urlparse(base)
    if parsed_base.scheme != "https" or not parsed_base.netloc:
        raise ShippingUnavailableError(
            "Los callbacks de Shipping requieren una BASE_URL HTTPS."
        )
    labels_ready = bool(labels_execution_ready())

    actual = configuracion(store_id)
    if not actual:
        actual = _reconciliar_shipping_remoto(
            str(store_id), access_token, base, _api
        )
    if actual:
        remote_response = _api(
            str(store_id),
            access_token,
            "GET",
            f"shipping_carriers/{actual['carrier_id']}",
        )
        if remote_response is not None and remote_response.status_code == 404:
            actual = None
        elif remote_response is None or remote_response.status_code != 200:
            raise ShippingUnavailableError(
                "No se pudo verificar el Shipping Carrier existente."
            )
        else:
            try:
                remote = remote_response.json()
            except Exception as exc:
                raise ShippingUnavailableError(
                    "Tiendanube devolvió un carrier inválido."
                ) from exc
            remote = remote if isinstance(remote, Mapping) else {}
            changes: dict[str, object] = {}
            config_needs_save = False
            if not actual.get("activa") or remote.get("active") is False:
                changes["active"] = True

            remote_rate_token = _callback_token_from_url(
                remote.get("callback_url"), base, "rates"
            )
            expected_rate_hash = str(actual.get("callback_token_hash") or "")
            if (
                not remote_rate_token
                or not expected_rate_hash
                or not hmac.compare_digest(
                    expected_rate_hash, hash_callback_token(remote_rate_token)
                )
            ):
                remote_rate_token = secrets.token_urlsafe(32)
                changes["callback_url"] = (
                    f"{base}/integraciones/tiendanube/shipping/rates/"
                    f"{remote_rate_token}"
                )
                config_needs_save = True

            options_response = _api(
                str(store_id),
                access_token,
                "GET",
                f"shipping_carriers/{actual['carrier_id']}/options",
            )
            if options_response is None or options_response.status_code != 200:
                raise ShippingUnavailableError(
                    "No se pudo verificar la opción TAURO existente."
                )
            try:
                remote_options = options_response.json()
            except Exception as exc:
                raise ShippingUnavailableError(
                    "Tiendanube devolvió opciones inválidas."
                ) from exc
            own_options = [
                option
                for option in remote_options
                if isinstance(option, Mapping)
                and str(option.get("code") or "") == RATE_CODE
                and option.get("id")
            ] if isinstance(remote_options, list) else []
            if len(own_options) > 1:
                raise ShippingUnavailableError(
                    "El carrier remoto tiene opciones TAURO duplicadas."
                )
            if not own_options:
                repaired = _api(
                    str(store_id),
                    access_token,
                    "POST",
                    f"shipping_carriers/{actual['carrier_id']}/options",
                    {
                        "code": RATE_CODE,
                        "name": RATE_NAME,
                        "allow_free_shipping": False,
                    },
                )
                if repaired is None or repaired.status_code not in (200, 201):
                    raise ShippingUnavailableError(
                        "No se pudo recrear la opción TAURO."
                    )
                try:
                    repaired_option = repaired.json()
                    if not isinstance(repaired_option, Mapping) or not repaired_option.get("id"):
                        raise ValueError("option id")
                except Exception as exc:
                    raise ShippingUnavailableError(
                        "Tiendanube devolvió una opción inválida."
                    ) from exc
                own_options = [repaired_option]
            option = own_options[0]
            option_id = str(option["id"])
            if option.get("active") is False:
                activate_option = _api(
                    str(store_id),
                    access_token,
                    "PUT",
                    f"shipping_carriers/{actual['carrier_id']}/options/{option_id}",
                    {"active": True},
                )
                if (
                    activate_option is None
                    or activate_option.status_code not in (200, 201, 204)
                ):
                    raise ShippingUnavailableError(
                        "No se pudo reactivar la opción TAURO."
                    )
            if option_id != str(actual.get("carrier_option_id") or ""):
                config_needs_save = True

            new_label_token = ""
            remote_labels = str(remote.get("callback_labels_url") or "")
            remote_label_token = _callback_token_from_url(
                remote_labels, base, "labels"
            )
            expected_label_hash = str(
                actual.get("label_callback_token_hash") or ""
            )
            label_callback_valid = bool(
                remote_label_token
                and expected_label_hash
                and hmac.compare_digest(
                    expected_label_hash,
                    hash_callback_token(remote_label_token),
                )
            )
            if labels_ready and not label_callback_valid:
                new_label_token = secrets.token_urlsafe(32)
                changes["callback_labels_url"] = (
                    f"{base}/integraciones/tiendanube/shipping/labels/"
                    f"{new_label_token}"
                )
            elif not labels_ready and remote_labels:
                changes["callback_labels_url"] = None

            if changes:
                response = _api(
                    str(store_id),
                    access_token,
                    "PUT",
                    f"shipping_carriers/{actual['carrier_id']}",
                    changes,
                )
                if response is None or response.status_code not in (200, 201, 204):
                    raise ShippingUnavailableError(
                        "Tiendanube no pudo reparar el Shipping Carrier."
                    )

            reactivar(store_id)
            if config_needs_save:
                _guardar_config(
                    store_id,
                    remote_rate_token,
                    str(actual["carrier_id"]),
                    option_id,
                    label_token=new_label_token or None,
                )
            if new_label_token:
                if not config_needs_save:
                    _guardar_label_callback_token(store_id, new_label_token)
            elif not labels_ready and actual.get("label_callback_token_hash"):
                _limpiar_label_callback_token(store_id)
            return {
                "ready": True,
                "carrier_id": str(actual["carrier_id"]),
                "option_id": option_id,
                "existing": True,
                "labels_callback_registered": labels_ready,
            }

    token_callback = secrets.token_urlsafe(32)
    label_token = secrets.token_urlsafe(32) if labels_ready else ""
    callback_url = (
        f"{base}/integraciones/tiendanube/shipping/rates/{token_callback}"
    )
    carrier_payload: dict[str, object] = {
        "name": "TAURO Nacional",
        "callback_url": callback_url,
        "types": "ship",
    }
    if labels_ready:
        carrier_payload["callback_labels_url"] = (
            f"{base}/integraciones/tiendanube/shipping/labels/{label_token}"
        )
    response = _api(
        str(store_id),
        access_token,
        "POST",
        "shipping_carriers",
        carrier_payload,
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
        _api(
            str(store_id), access_token, "DELETE",
            f"shipping_carriers/{carrier_id}",
        )
        raise ShippingUnavailableError("Tiendanube devolvió una opción inválida.") from exc

    _guardar_config(
        store_id,
        token_callback,
        carrier_id,
        option_id,
        label_token=label_token or None,
    )
    return {
        "ready": True,
        "carrier_id": carrier_id,
        "option_id": option_id,
        "existing": False,
        "labels_callback_registered": labels_ready,
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


def _configured_limits() -> dict[str, Decimal]:
    """Lee topes contractuales sin asignar valores comerciales por defecto."""
    limits: dict[str, Decimal] = {}
    missing: list[str] = []
    for key, env_name in _LIMIT_ENV.items():
        raw = (os.getenv(env_name) or "").strip()
        if not raw:
            missing.append(env_name)
            continue
        try:
            limits[key] = _decimal(raw, env_name)
        except ShippingContractError as exc:
            raise ShippingUnavailableError(
                "Los límites contractuales nacionales no son válidos."
            ) from exc
    if missing and _enabled("TIENDANUBE_SHIPPING_ENABLED"):
        raise ShippingUnavailableError(
            "Faltan límites contractuales nacionales para habilitar tarifas."
        )
    return limits


def _holiday_calendar(raw: str | None = None) -> frozenset[date]:
    """Convierte el calendario aprobado; nunca obtiene feriados por inferencia."""
    value = os.getenv("TAURO_NACIONAL_HOLIDAYS") if raw is None else raw
    holidays: set[date] = set()
    for item in str(value or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            holidays.add(date.fromisoformat(item))
        except ValueError as exc:
            raise ShippingUnavailableError(
                "El calendario de feriados nacionales no es válido."
            ) from exc
    current_year = datetime.now(_ARGENTINA_TZ).year
    if _enabled("TIENDANUBE_SHIPPING_ENABLED") and not any(
        holiday.year == current_year for holiday in holidays
    ):
        raise ShippingUnavailableError(
            "Falta el calendario de feriados nacionales del año en curso."
        )
    return frozenset(holidays)


def _add_business_days(
    start: datetime,
    days: int,
    holidays: frozenset[date],
) -> datetime:
    result = start
    remaining = max(int(days), 0)
    while remaining:
        result += timedelta(days=1)
        if result.weekday() < 5 and result.date() not in holidays:
            remaining -= 1
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
    limits = _configured_limits()
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
        if quantity <= 0:
            raise ShippingContractError("La cantidad de un ítem es inválida.")
        dimensions = raw.get("dimensions")
        if not isinstance(dimensions, Mapping) or not dimensions:
            raise ShippingContractError("Faltan las dimensiones de un producto.")
        grams = _decimal(raw.get("grams"), "El peso de un producto")
        package = Package(
            quantity=quantity,
            weight_kg=grams / Decimal("1000"),
            length_cm=_decimal(dimensions.get("depth"), "El largo de un producto"),
            width_cm=_decimal(dimensions.get("width"), "El ancho de un producto"),
            height_cm=_decimal(dimensions.get("height"), "El alto de un producto"),
        )
        for field in ("package_weight_kg", "length_cm", "width_cm", "height_cm"):
            maximum = limits.get(field)
            package_field = "weight_kg" if field == "package_weight_kg" else field
            if maximum is not None and getattr(package, package_field) > maximum:
                raise ShippingContractError(
                    "El paquete supera los límites contractuales del servicio."
                )
        packages.append(package)
    if not packages:
        raise ShippingContractError("El carrito no contiene productos cotizables.")
    total_weight = sum(
        package.weight_kg * package.quantity for package in packages
    )
    total_volume = sum(
        package.length_cm
        * package.width_cm
        * package.height_cm
        * package.quantity
        / Decimal("1000000")
        for package in packages
    )
    if (
        limits.get("total_weight_kg") is not None
        and total_weight > limits["total_weight_kg"]
    ) or (
        limits.get("total_volume_m3") is not None
        and total_volume > limits["total_volume_m3"]
    ):
        raise ShippingContractError(
            "El envío supera los límites contractuales del servicio."
        )
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


def _valid_quotes(
    adapter,
    request: QuoteRequest,
    *,
    deadline: float | None = None,
) -> tuple[QuoteResult, ...]:
    if deadline is not None and time.monotonic() >= deadline:
        raise ShippingUnavailableError("Se agotó el tiempo de cotización.")
    validate_quote_request(request, adapter.carrier_id)
    results = adapter.quote(request)
    if deadline is not None and time.monotonic() >= deadline:
        raise ShippingUnavailableError("Se agotó el tiempo de cotización.")
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

    deadline = time.monotonic() + _CALLBACK_DEADLINE_SECONDS
    request = _quote_request(payload, str(installation["cliente_id"]))
    candidates: list[tuple[object, QuoteResult]] = []
    carrier_responded = False
    production_adapters = adapters is None
    available_adapters = (
        tuple(adapters) if adapters is not None else _default_adapters()
    )
    if production_adapters:
        if len(available_adapters) != 1:
            raise ShippingUnavailableError(
                "Debe existir un único adapter nacional homologado."
            )
        declared_budget = getattr(
            available_adapters[0], "callback_timeout_budget_seconds", None
        )
        if declared_budget is None or float(declared_budget) > 1.8:
            raise ShippingUnavailableError(
                "El adapter nacional excede el presupuesto del checkout."
            )
    for adapter in available_adapters:
        try:
            quotes = _valid_quotes(adapter, request, deadline=deadline)
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
        partial_quotes = _valid_quotes(
            adapter, partial_request, deadline=deadline
        )
        same_service = next(
            (q for q in partial_quotes if q.service_code == selected.service_code), None
        )
        if same_service is None or same_service.customer_price is None:
            raise ShippingUnavailableError(
                "No se pudo calcular el descuento de envío gratis del carrito mixto."
            )
        buyer_price = same_service.customer_price

    now = datetime.now(_ARGENTINA_TZ)
    days = max(int(selected.estimated_days or 1), 1)
    holidays = _holiday_calendar()
    min_date = _add_business_days(now, days, holidays)
    max_date = _add_business_days(min_date, 2, holidays)
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
