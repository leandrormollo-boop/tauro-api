"""Adapter OCA e-Pak nacional, apagado por defecto.

Implementa cotización, alta, etiqueta PDF, anulación y tracking sobre el
contrato oficial e-Pak. Cotizar y ejecutar tienen gates separados: disponer de
una tarifa nunca autoriza por sí solo a crear una orden de retiro/admisón.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from typing import Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

import requests

from servicios.carrier_adapter import (
    OperationState,
    Package,
    PickupResult,
    QuoteRequest,
    QuoteResult,
    ShipmentResult,
    TrackingEvent,
    TrackingResult,
    register_adapter,
    unregister_adapter,
    validate_quote_request,
    validate_quote_result,
)


QA_BASE_URL = (
    "https://integraciones.ocadev.com.ar/epak_tracking_test/Oep_TrackEPak.asmx"
)
PRODUCTION_BASE_URL = "https://webservice.oca.com.ar/ePak_tracking/Oep_TrackEPak.asmx"
QA_QUOTE_URL = f"{QA_BASE_URL}/Tarifar_Envio_Corporativo"
PRODUCTION_QUOTE_URL = f"{PRODUCTION_BASE_URL}/Tarifar_Envio_Corporativo"
QA_COST_CENTER_URL = f"{QA_BASE_URL}/GetCentroCostoPorOperativa"
PRODUCTION_COST_CENTER_URL = (
    "https://webservice.oca.com.ar/oep_tracking/Oep_Track.asmx/"
    "GetCentroCostoPorOperativa"
)
_CREATE_METHOD = "IngresoORMultiplesRetiros_v2"
_CANCEL_METHOD = "AnularOrdenGenerada"
_TRACK_METHOD = "Tracking_Pieza_ConIdEstado"
_LABEL_METHODS = {
    "a4": "GetPdfDeEtiquetasPorOrdenOrNumeroEnvio",
    "10x15": "GetPdfDeEtiquetasPorOrdenOrNumeroEnvioParaEtiquetadora",
}
_TRUE = {"1", "true", "yes", "si", "sí", "on"}
_CUIT_RE = re.compile(r"^(\d{2})-(\d{8})-(\d)$")
_ACCOUNT_RE = re.compile(r"^\d{6}/\d{3}$")
_POSTAL_RE = re.compile(r"^\d{4}$")
_ORDER_RE = re.compile(r"^\d{1,12}$")
_TRACKING_RE = re.compile(r"^\d{19}$")
_COST_CENTER_RE = re.compile(r"^\d{1,10}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SAFE_XML_LIMIT = 1_000_000
_MAX_LABEL_LIMIT = 10_000_000
_ARGENTINA_TZ = ZoneInfo("America/Argentina/Buenos_Aires")


class OCAError(RuntimeError):
    """Error seguro del borde OCA, sin payloads ni secretos."""


class OCAConfigurationError(OCAError):
    pass


class OCAUnavailableError(OCAError):
    pass


class OCAOutcomeUnknown(OCAError):
    """OCA pudo haber aplicado una escritura; nunca debe reintentarse a ciegas."""


class OCAUnsupportedOperation(OCAError):
    pass


def _flag(value: object) -> bool:
    return str(value or "").strip().lower() in _TRUE


def _decimal_config(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise OCAConfigurationError(f"{name} no tiene un valor válido.") from None
    if not result.is_finite():
        raise OCAConfigurationError(f"{name} no tiene un valor válido.")
    return result


def _integer_config(value: object, name: str) -> int:
    raw = str(value or "").strip()
    if not raw.isdigit():
        raise OCAConfigurationError(f"{name} no tiene un valor válido.")
    return int(raw)


def _valid_cuit(cuit: str) -> bool:
    match = _CUIT_RE.fullmatch(cuit)
    if not match:
        return False
    digits = [int(item) for item in "".join(match.groups())]
    weights = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
    verifier = 11 - (sum(a * b for a, b in zip(digits[:10], weights)) % 11)
    verifier = 0 if verifier == 11 else 9 if verifier == 10 else verifier
    return verifier == digits[-1]


def _valid_cost_center(value: str) -> bool:
    return (
        bool(_COST_CENTER_RE.fullmatch(value))
        and 1 <= int(value) <= 2_147_483_647
    )


@dataclass(frozen=True)
class OCAConfig:
    enabled: bool
    uat_approved: bool
    production_approved: bool
    environment: str
    cuit: str = ""
    account: str = ""
    operation: int = 0
    return_operation: int = 0
    username: str = ""
    password: str = ""
    origin_mode: str = ""
    destination_mode: str = ""
    fulfillment_enabled: bool = False
    fulfillment_uat_approved: bool = False
    confirm_withdrawal: bool = False
    cost_center: str = ""
    return_cost_center: str = ""
    time_slot: int = 1
    origin_branch_id: int = 0
    label_format: str = "a4"
    reverse_logistics: bool = False
    insured_operation: bool = False
    insured_operation_confirmed: bool = False
    return_insured_operation: bool = False
    return_insured_operation_confirmed: bool = False
    connect_timeout_seconds: Decimal = Decimal("0.5")
    read_timeout_seconds: Decimal = Decimal("1.3")
    max_response_bytes: int = _SAFE_XML_LIMIT
    max_label_bytes: int = 5_000_000
    fallback_markup_pct: Decimal = Decimal("25")

    def __repr__(self) -> str:
        return (
            "OCAConfig("
            f"enabled={self.enabled!r}, uat_approved={self.uat_approved!r}, "
            f"production_approved={self.production_approved!r}, "
            f"environment={self.environment!r}, "
            f"fulfillment_enabled={self.fulfillment_enabled!r}, "
            "credentials=<redacted>)"
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "OCAConfig":
        values = os.environ if env is None else env
        return cls(
            enabled=_flag(values.get("OCA_ADAPTER_ENABLED")),
            uat_approved=_flag(values.get("OCA_UAT_APPROVED")),
            production_approved=_flag(values.get("OCA_PRODUCTION_APPROVED")),
            environment=str(values.get("OCA_ENVIRONMENT") or "qa").strip().lower(),
            cuit=str(values.get("OCA_CUIT") or "").strip(),
            account=str(values.get("OCA_CUENTA") or "").strip(),
            operation=_integer_config(
                values.get("OCA_OPERATIVA") or "0", "OCA_OPERATIVA"
            ),
            return_operation=_integer_config(
                values.get("OCA_OPERATIVA_DEVOLUCION") or "0",
                "OCA_OPERATIVA_DEVOLUCION",
            ),
            username=str(values.get("OCA_USUARIO") or "").strip(),
            password=str(values.get("OCA_PASSWORD") or "").strip(),
            origin_mode=str(values.get("OCA_ORIGIN_MODE") or "").strip().lower(),
            destination_mode=str(
                values.get("OCA_DESTINATION_MODE") or ""
            ).strip().lower(),
            fulfillment_enabled=_flag(values.get("OCA_FULFILLMENT_ENABLED")),
            fulfillment_uat_approved=_flag(
                values.get("OCA_FULFILLMENT_UAT_APPROVED")
            ),
            confirm_withdrawal=_flag(values.get("OCA_CONFIRM_WITHDRAWAL")),
            cost_center=str(values.get("OCA_CENTRO_COSTO") or "").strip(),
            return_cost_center=str(
                values.get("OCA_CENTRO_COSTO_DEVOLUCION") or ""
            ).strip(),
            time_slot=_integer_config(
                values.get("OCA_FRANJA_HORARIA") or "1",
                "OCA_FRANJA_HORARIA",
            ),
            origin_branch_id=_integer_config(
                values.get("OCA_CENTRO_IMPOSICION_ORIGEN") or "0",
                "OCA_CENTRO_IMPOSICION_ORIGEN",
            ),
            label_format=str(values.get("OCA_LABEL_FORMAT") or "a4").strip().lower(),
            reverse_logistics=_flag(values.get("OCA_LOGISTICA_INVERSA")),
            insured_operation=_flag(values.get("OCA_OPERATIVA_ASEGURADA")),
            insured_operation_confirmed=_flag(
                values.get("OCA_OPERATIVA_SEGURO_CONFIRMADO")
            ),
            return_insured_operation=_flag(
                values.get("OCA_OPERATIVA_DEVOLUCION_ASEGURADA")
            ),
            return_insured_operation_confirmed=_flag(
                values.get("OCA_OPERATIVA_DEVOLUCION_SEGURO_CONFIRMADO")
            ),
            connect_timeout_seconds=_decimal_config(
                values.get("OCA_CONNECT_TIMEOUT_SECONDS") or "0.5",
                "OCA_CONNECT_TIMEOUT_SECONDS",
            ),
            read_timeout_seconds=_decimal_config(
                values.get("OCA_READ_TIMEOUT_SECONDS") or "1.3",
                "OCA_READ_TIMEOUT_SECONDS",
            ),
            max_response_bytes=_integer_config(
                values.get("OCA_MAX_RESPONSE_BYTES") or str(_SAFE_XML_LIMIT),
                "OCA_MAX_RESPONSE_BYTES",
            ),
            max_label_bytes=_integer_config(
                values.get("OCA_MAX_LABEL_BYTES") or "5000000",
                "OCA_MAX_LABEL_BYTES",
            ),
            fallback_markup_pct=_decimal_config(
                values.get("OCA_FALLBACK_MARKUP_PCT") or "25",
                "OCA_FALLBACK_MARKUP_PCT",
            ),
        )

    @property
    def quote_url(self) -> str:
        return PRODUCTION_QUOTE_URL if self.environment == "production" else QA_QUOTE_URL

    @property
    def base_url(self) -> str:
        return PRODUCTION_BASE_URL if self.environment == "production" else QA_BASE_URL

    def method_url(self, method: str) -> str:
        return f"{self.base_url}/{method}"

    @property
    def cost_center_url(self) -> str:
        return (
            PRODUCTION_COST_CENTER_URL
            if self.environment == "production"
            else QA_COST_CENTER_URL
        )

    @property
    def timeout(self) -> tuple[float, float]:
        return (
            float(self.connect_timeout_seconds),
            float(self.read_timeout_seconds),
        )

    @property
    def callback_timeout_budget_seconds(self) -> float:
        return float(self.connect_timeout_seconds + self.read_timeout_seconds)

    def configuration_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        if self.environment not in {"qa", "production"}:
            errors.append("environment")
        if not _valid_cuit(self.cuit):
            errors.append("cuit")
        if not _ACCOUNT_RE.fullmatch(self.account):
            errors.append("account")
        if not 1 <= self.operation <= 999_999:
            errors.append("operation")
        if self.return_operation and not 1 <= self.return_operation <= 999_999:
            errors.append("return_operation")
        if self.return_operation and self.return_operation == self.operation:
            errors.append("return_operation_not_separate")
        if not self.username or len(self.username) > 100:
            errors.append("username")
        if not self.password or len(self.password) > 200:
            errors.append("password")
        if self.origin_mode not in {"domicilio", "sucursal"}:
            errors.append("origin_mode")
        if self.destination_mode not in {"domicilio", "sucursal"}:
            errors.append("destination_mode")
        if not Decimal("0.1") <= self.connect_timeout_seconds <= Decimal("5"):
            errors.append("connect_timeout")
        if not Decimal("0.1") <= self.read_timeout_seconds <= Decimal("5"):
            errors.append("read_timeout")
        # Un carrito mixto requiere dos cotizaciones secuenciales. Reservamos
        # 0,4 s del SLA global de 4 s para parseo, DB y serialización.
        if self.connect_timeout_seconds + self.read_timeout_seconds > Decimal("1.8"):
            errors.append("timeout_budget")
        if not 1_024 <= self.max_response_bytes <= _SAFE_XML_LIMIT:
            errors.append("response_limit")
        if not Decimal("0") <= self.fallback_markup_pct <= Decimal("300"):
            errors.append("fallback_markup")
        for name, value in (
            ("insured_operation", self.insured_operation),
            ("insured_operation_confirmed", self.insured_operation_confirmed),
            ("return_insured_operation", self.return_insured_operation),
            (
                "return_insured_operation_confirmed",
                self.return_insured_operation_confirmed,
            ),
        ):
            if not isinstance(value, bool):
                errors.append(name)
        # El flag histórico era global a todas las etiquetas. Al configurar
        # ambas operativas sería inseguro porque no identifica a qué orden
        # corresponde cada etiqueta. El modo dual exige el prefijo explícito
        # ``return:`` que devuelve create_shipment para las devoluciones.
        if self.return_operation and self.reverse_logistics:
            errors.append("global_reverse_logistics_conflict")
        if self.return_insured_operation and not self.return_operation:
            errors.append("return_operation_required_for_insurance")
        return tuple(errors)

    def fulfillment_configuration_errors(self) -> tuple[str, ...]:
        errors = list(self.configuration_errors())
        if not _valid_cost_center(self.cost_center):
            errors.append("cost_center")
        if self.insured_operation_confirmed is not True:
            errors.append("insured_operation_not_confirmed")
        if self.return_cost_center and not _valid_cost_center(
            self.return_cost_center
        ):
            errors.append("return_cost_center")
        if self.return_cost_center and not self.return_operation:
            errors.append("return_operation_required")
        if self.time_slot not in {1, 2, 3}:
            errors.append("time_slot")
        if not 0 <= self.origin_branch_id <= 999:
            errors.append("origin_branch_id")
        if self.origin_mode == "sucursal" and self.origin_branch_id == 0:
            errors.append("origin_branch_required")
        if self.label_format not in _LABEL_METHODS:
            errors.append("label_format")
        if not 1_024 <= self.max_label_bytes <= _MAX_LABEL_LIMIT:
            errors.append("label_limit")
        return tuple(errors)

    def readiness_errors(self) -> tuple[str, ...]:
        errors = list(self.configuration_errors())
        if not self.enabled:
            errors.append("adapter_disabled")
        if not self.uat_approved:
            errors.append("uat_not_approved")
        if self.environment == "production" and not self.production_approved:
            errors.append("production_not_approved")
        return tuple(errors)

    def assert_ready(self) -> None:
        if self.readiness_errors():
            raise OCAConfigurationError(
                "El adapter OCA no tiene una configuración aprobada y completa."
            )

    def fulfillment_readiness_errors(self) -> tuple[str, ...]:
        errors = list(self.readiness_errors())
        errors.extend(
            item
            for item in self.fulfillment_configuration_errors()
            if item not in errors
        )
        if not self.fulfillment_enabled:
            errors.append("fulfillment_disabled")
        if not self.fulfillment_uat_approved:
            errors.append("fulfillment_uat_not_approved")
        if self.environment == "production" and not self.confirm_withdrawal:
            errors.append("production_confirmation_disabled")
        return tuple(errors)

    def assert_fulfillment_ready(self) -> None:
        if self.fulfillment_readiness_errors():
            raise OCAConfigurationError(
                "La ejecución OCA no tiene una configuración aprobada y completa."
            )


def registration_status(env: Mapping[str, str] | None = None) -> dict:
    """Diagnóstico apto para preflight: sólo códigos, nunca valores."""
    values = os.environ if env is None else env
    try:
        config = OCAConfig.from_env(values)
    except OCAConfigurationError:
        return {
            "ready": False,
            "fulfillment_ready": False,
            "enabled": _flag(values.get("OCA_ADAPTER_ENABLED")),
            "uat_approved": _flag(values.get("OCA_UAT_APPROVED")),
            "fulfillment_enabled": _flag(
                values.get("OCA_FULFILLMENT_ENABLED")
            ),
            "fulfillment_uat_approved": _flag(
                values.get("OCA_FULFILLMENT_UAT_APPROVED")
            ),
            "configuration_valid": False,
            "fulfillment_configuration_valid": False,
            "environment_approved": (
                str(values.get("OCA_ENVIRONMENT") or "qa").strip().lower() == "qa"
            ),
        }
    errors = set(config.readiness_errors())
    fulfillment_errors = set(config.fulfillment_readiness_errors())
    return {
        "ready": not errors,
        "fulfillment_ready": not fulfillment_errors,
        "enabled": config.enabled,
        "uat_approved": config.uat_approved,
        "fulfillment_enabled": config.fulfillment_enabled,
        "fulfillment_uat_approved": config.fulfillment_uat_approved,
        "configuration_valid": not config.configuration_errors(),
        "fulfillment_configuration_valid": not (
            config.fulfillment_configuration_errors()
        ),
        "environment_approved": config.environment == "qa" or (
            config.environment == "production" and config.production_approved
        ),
    }


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].strip().lower()


def _safe_xml(content: bytes, limit: int) -> ET.Element:
    if not content or len(content) > limit:
        raise OCAUnavailableError("OCA devolvió una respuesta inválida.")
    lowered = content.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise OCAUnavailableError("OCA devolvió una respuesta inválida.")
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, ValueError):
        raise OCAUnavailableError("OCA devolvió una respuesta inválida.") from None
    if _local_name(root.tag) == "html":
        raise OCAUnavailableError("OCA devolvió una respuesta inválida.")
    return root


def _bounded_response_content(response: object, limit: int) -> bytes:
    headers = getattr(response, "headers", {}) or {}
    length = str(headers.get("content-length") or "").strip()
    if length.isdigit() and int(length) > limit:
        raise OCAUnavailableError("OCA devolvió una respuesta inválida.")
    iterator = getattr(response, "iter_content", None)
    if not callable(iterator):
        content = bytes(getattr(response, "content", b""))
        if len(content) > limit:
            raise OCAUnavailableError("OCA devolvió una respuesta inválida.")
        return content
    chunks: list[bytes] = []
    total = 0
    for chunk in iterator(chunk_size=65_536):
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise OCAUnavailableError("OCA devolvió una respuesta inválida.")
        chunks.append(bytes(chunk))
    return b"".join(chunks)


def _quiet_close(resource: object) -> None:
    close = getattr(resource, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        # El cierre ocurre después de consumir la respuesta. No debe sustituir
        # el resultado contractual ni filtrar excepciones crudas del cliente.
        pass


def _post_bounded(
    session: object,
    url: str,
    data: Mapping[str, str],
    *,
    timeout: tuple[float, float],
    limit: int,
    write_operation: bool,
    unavailable_message: str,
) -> bytes:
    """POST acotado compartido por runtime y onboarding de solo lectura."""
    try:
        response = session.post(
            url,
            data=dict(data),
            timeout=timeout,
            headers={"Accept": "application/xml, text/xml"},
            stream=True,
            allow_redirects=False,
        )
    except (requests.Timeout, requests.ConnectionError, requests.RequestException):
        if write_operation:
            raise OCAOutcomeUnknown(
                "OCA pudo haber aplicado la operación; requiere conciliación manual."
            ) from None
        raise OCAUnavailableError(unavailable_message) from None
    try:
        if not 200 <= int(getattr(response, "status_code", 0)) < 300:
            if write_operation:
                raise OCAOutcomeUnknown(
                    "OCA pudo haber aplicado la operación; requiere conciliación manual."
                )
            raise OCAUnavailableError(unavailable_message)
        return _bounded_response_content(response, limit)
    except OCAUnavailableError:
        if write_operation:
            raise OCAOutcomeUnknown(
                "OCA pudo haber aplicado la operación; requiere conciliación manual."
            ) from None
        raise
    except requests.RequestException:
        if write_operation:
            raise OCAOutcomeUnknown(
                "OCA pudo haber aplicado la operación; requiere conciliación manual."
            ) from None
        raise OCAUnavailableError(unavailable_message) from None
    finally:
        _quiet_close(response)


def _all_text(root: ET.Element) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    for element in root.iter():
        text = (element.text or "").strip()
        if text:
            fields.setdefault(_local_name(element.tag), []).append(text)
    return fields


def _embedded_xml(root: ET.Element, limit: int) -> ET.Element | None:
    for text in (item.strip() for item in root.itertext()):
        if text.startswith("<") and text.endswith(">"):
            encoded = text.encode("utf-8")
            return _safe_xml(encoded, limit)
    return None


def _machine_decimal(raw: str) -> Decimal:
    value = raw.strip().replace("$", "").replace("ARS", "").replace("ars", "")
    value = re.sub(r"\s+", "", value)
    if not re.fullmatch(r"[+-]?[0-9][0-9.,]*", value):
        raise InvalidOperation
    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    elif "," in value:
        value = value.replace(",", ".")
    result = Decimal(value)
    if not result.is_finite():
        raise InvalidOperation
    return result


def _first_field(fields: Mapping[str, list[str]], names: tuple[str, ...]) -> str:
    for name in names:
        values = fields.get(name)
        if values:
            return values[0]
    return ""


def _delivery_days(raw: str) -> int | None:
    if not raw:
        return None
    numbers = [Decimal(value.replace(",", ".")) for value in re.findall(r"\d+(?:[.,]\d+)?", raw)]
    if not numbers:
        return None
    maximum = max(numbers)
    if re.search(r"\b(h|hs|hora|horas)\b", raw.lower()):
        maximum /= Decimal("24")
    days = int(maximum.to_integral_value(rounding=ROUND_CEILING))
    return days if 1 <= days <= 365 else None


def _parse_quote_response(
    content: bytes,
    limit: int,
) -> tuple[Decimal | None, int | None, str, str]:
    root = _safe_xml(content, limit)
    if any(_local_name(item.tag) == "fault" for item in root.iter()):
        raise OCAUnavailableError("OCA no pudo cotizar el envío.")
    fields = _all_text(root)
    cost_raw = _first_field(
        fields,
        ("total", "costototal", "precio", "tarifa", "importe"),
    )
    if not cost_raw:
        nested = _embedded_xml(root, limit)
        if nested is not None:
            if any(_local_name(item.tag) == "fault" for item in nested.iter()):
                raise OCAUnavailableError("OCA no pudo cotizar el envío.")
            fields = _all_text(nested)
            cost_raw = _first_field(
                fields,
                ("total", "costototal", "precio", "tarifa", "importe"),
            )
    if not cost_raw:
        return None, None, "", ""
    try:
        cost = _machine_decimal(cost_raw)
    except (InvalidOperation, ValueError):
        raise OCAUnavailableError("OCA devolvió una tarifa inválida.") from None
    if cost <= 0:
        return None, None, "", ""
    delivery = _first_field(
        fields,
        ("plazoentrega", "diasentrega", "tiempoentrega", "dias"),
    )
    service_code = _first_field(
        fields,
        ("idtiposervicio", "codigoservicio", "idservicio"),
    )
    service_name = _first_field(
        fields,
        ("tiposervicio", "servicio", "descripcionservicio"),
    )
    return cost, _delivery_days(delivery), service_code, service_name


def _postal_code(address: Mapping[str, object], label: str) -> str:
    raw = str(
        address.get("codigo_postal")
        or address.get("postal_code")
        or address.get("cp")
        or ""
    ).strip()
    if not _POSTAL_RE.fullmatch(raw):
        raise ValueError(f"{label} necesita un código postal argentino de 4 dígitos.")
    return raw


def _payload(request: QuoteRequest, config: OCAConfig) -> dict[str, str]:
    if request.declared_currency != "ARS":
        raise ValueError("OCA Nacional requiere valor declarado en ARS.")
    if request.declared_value > Decimal("2147483647"):
        raise ValueError("El valor declarado supera el máximo aceptado.")
    declared_value = request.declared_value.to_integral_value(rounding=ROUND_CEILING)
    if request.origin_mode != config.origin_mode or request.destination_mode != config.destination_mode:
        raise ValueError("La operativa OCA configurada no coincide con la modalidad solicitada.")
    total_weight = sum(
        package.weight_kg * package.quantity for package in request.packages
    )
    total_volume = sum(
        package.length_cm
        * package.width_cm
        * package.height_cm
        * package.quantity
        / Decimal("1000000")
        for package in request.packages
    )
    total_packages = sum(package.quantity for package in request.packages)
    return {
        "Cuit": config.cuit,
        "Operativa": str(config.operation),
        "PesoTotal": format(total_weight.normalize(), "f"),
        "VolumenTotal": format(total_volume.normalize(), "f"),
        "CodigoPostalOrigen": _postal_code(request.origin, "El origen"),
        "CodigoPostalDestino": _postal_code(request.destination, "El destino"),
        "CantidadPaquetes": str(total_packages),
        # El método oficial recibe INT. Se redondea siempre hacia arriba para
        # no subdeclarar mercadería cuando Tiendanube informa centavos.
        "ValorDeclarado": str(int(declared_value)),
    }


def _default_pricing_loader(customer_id: str, fallback_pct: float) -> dict:
    from servicios.pricing import get_pricing_nacional_estricto

    # Se conserva la firma del contrato del adapter, pero el fallback global
    # nunca es válido para cotizaciones nacionales publicadas en Tiendanube.
    _ = fallback_pct
    return get_pricing_nacional_estricto(customer_id)


def _safe_service_code(value: str, fallback: str) -> str:
    code = re.sub(r"[^A-Za-z0-9_.:-]", "", value.strip())[:80]
    return code or fallback


def _safe_service_name(value: str) -> str:
    name = re.sub(r"[\x00-\x1f\x7f<>]", "", value).strip()[:120]
    return name or "OCA Nacional"


def _customer_price(cost: Decimal, pricing: Mapping[str, object]) -> Decimal:
    mode = str(pricing.get("tipo") or "").strip().upper()
    try:
        value = Decimal(str(pricing.get("valor")))
    except (InvalidOperation, ValueError):
        raise OCAUnavailableError("La tarifa no tiene pricing comercial válido.") from None
    if not value.is_finite():
        raise OCAUnavailableError("La tarifa no tiene pricing comercial válido.")
    if mode == "FIJO_ARS" and value >= 0:
        result = cost + value
    elif mode == "MULTIPLICADOR" and value >= 1:
        result = cost * value
    elif mode == "PCT" and Decimal("0") <= value <= Decimal("300"):
        result = cost * (Decimal("1") + value / Decimal("100"))
    else:
        raise OCAUnavailableError("La tarifa no tiene pricing comercial válido.")
    result = result.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if result <= 0:
        raise OCAUnavailableError("La tarifa no tiene pricing comercial válido.")
    return result


def _clean_text(value: object, *, field: str, maximum: int, required: bool) -> str:
    result = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))
    result = re.sub(r"\s+", " ", result).strip()
    if required and not result:
        raise ValueError(f"{field} es obligatorio para emitir con OCA.")
    if len(result) > maximum:
        raise ValueError(f"{field} supera el máximo admitido por OCA.")
    try:
        result.encode("iso-8859-1")
    except UnicodeEncodeError:
        raise ValueError(f"{field} contiene caracteres no admitidos por OCA.") from None
    return result


def _mapped_text(
    source: Mapping[str, object],
    keys: Sequence[str],
    *,
    field: str,
    maximum: int,
    required: bool = False,
) -> str:
    value = next((source.get(key) for key in keys if source.get(key) is not None), "")
    return _clean_text(value, field=field, maximum=maximum, required=required)


def _positive_decimal(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field} no es válido.") from None
    if not result.is_finite() or result <= 0 or result > Decimal("9999999.99"):
        raise ValueError(f"{field} no es válido.")
    return result.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def _oca_decimal(value: Decimal) -> str:
    return format(value, "f").rstrip("0").rstrip(".") or "0"


def _shipment_date(value: object, today: date) -> str:
    if value in (None, ""):
        parsed = today
    elif isinstance(value, datetime):
        parsed = (
            value.astimezone(_ARGENTINA_TZ).date()
            if value.tzinfo is not None
            else value.date()
        )
    elif isinstance(value, date):
        parsed = value
    else:
        raw = str(value).strip()
        try:
            parsed = datetime.strptime(raw, "%Y%m%d" if len(raw) == 8 else "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("La fecha de admisión/retiro OCA no es válida.") from None
    if parsed < today or parsed > today + timedelta(days=30):
        raise ValueError("La fecha OCA debe estar entre hoy y los próximos 30 días.")
    return parsed.strftime("%Y%m%d")


def _street_number(source: Mapping[str, object], field: str) -> str:
    result = _mapped_text(
        source,
        ("nro", "numero", "number", "street_number"),
        field=field,
        maximum=5,
        required=True,
    )
    if not re.fullmatch(r"[A-Za-z0-9/-]{1,5}", result):
        raise ValueError(f"{field} no es válido para OCA.")
    return result


def _email(source: Mapping[str, object], field: str, *, required: bool) -> str:
    result = _mapped_text(
        source,
        ("email", "correo"),
        field=field,
        maximum=100,
        required=required,
    )
    if result and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", result):
        raise ValueError(f"{field} no es válido.")
    return result


def _recipient_names(source: Mapping[str, object]) -> tuple[str, str]:
    last_name = _mapped_text(
        source,
        ("apellido", "last_name"),
        field="El apellido del destinatario",
        maximum=30,
    )
    first_name = _mapped_text(
        source,
        ("nombre", "first_name"),
        field="El nombre del destinatario",
        maximum=30,
    )
    if last_name and first_name:
        return last_name, first_name
    full_name = _mapped_text(
        source,
        ("name", "nombre_completo", "full_name"),
        field="El nombre del destinatario",
        maximum=61,
        required=True,
    )
    parts = full_name.rsplit(" ", 1)
    if len(parts) == 1:
        return "-", _clean_text(parts[0], field="El nombre", maximum=30, required=True)
    return (
        _clean_text(parts[1], field="El apellido", maximum=30, required=True),
        _clean_text(parts[0], field="El nombre", maximum=30, required=True),
    )


def _package_values(
    raw_packages: object,
    *,
    insured: bool,
    declared_value: object,
) -> tuple[dict[str, str], ...]:
    if not isinstance(raw_packages, (list, tuple)) or not raw_packages:
        raise ValueError("El envío OCA necesita al menos un paquete.")
    expanded: list[dict[str, Decimal]] = []
    for raw in raw_packages:
        if isinstance(raw, Package):
            quantity = raw.quantity
            values = {
                "alto": raw.height_cm,
                "ancho": raw.width_cm,
                "largo": raw.length_cm,
                "peso": raw.weight_kg,
            }
        elif isinstance(raw, Mapping):
            try:
                quantity = int(raw.get("quantity") or raw.get("cantidad") or 1)
            except (TypeError, ValueError):
                raise ValueError("La cantidad de paquetes OCA no es válida.") from None
            values = {
                "alto": raw.get("height_cm") or raw.get("alto"),
                "ancho": raw.get("width_cm") or raw.get("ancho"),
                "largo": raw.get("length_cm") or raw.get("largo"),
                "peso": raw.get("weight_kg") or raw.get("peso"),
            }
        else:
            raise ValueError("El formato de paquete OCA no es válido.")
        if not 1 <= quantity <= 100 or len(expanded) + quantity > 100:
            raise ValueError("La cantidad de paquetes OCA no es válida.")
        normalized = {
            key: _positive_decimal(value, f"El {key} del paquete")
            for key, value in values.items()
        }
        expanded.extend(dict(normalized) for _ in range(quantity))

    amounts = [Decimal("0.00")] * len(expanded)
    if insured:
        total = _positive_decimal(declared_value, "El valor declarado")
        unit = (total / len(expanded)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        amounts = [unit] * len(expanded)
        amounts[-1] += total - sum(amounts)
        if amounts[-1] <= 0:
            raise ValueError("El valor declarado por paquete no es válido.")

    return tuple(
        {
            **{key: _oca_decimal(value) for key, value in package.items()},
            "valor": _oca_decimal(amounts[index]),
            "cant": "1",
        }
        for index, package in enumerate(expanded)
    )


def _remittance(idempotency_key: str) -> str:
    if not _IDEMPOTENCY_RE.fullmatch(str(idempotency_key or "")):
        raise ValueError("La clave idempotente OCA no es válida.")
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    return f"TAURO-{digest}"


def _shipment_operation(
    shipment: Mapping[str, object],
    config: OCAConfig,
) -> tuple[int, str, bool, bool]:
    """Elige la operativa sólo desde una marca explícita del expediente.

    La ausencia conserva compatibilidad con los envíos normales existentes.
    Una devolución nunca reutiliza silenciosamente operativa o centro de costo
    normales, aun cuando OCA hubiese asignado el mismo valor a ambas.
    """
    raw_kind = shipment.get("oca_operation_kind")
    kind = str(raw_kind or "normal").strip().lower()
    if kind == "normal":
        if config.insured_operation_confirmed is not True:
            raise OCAConfigurationError(
                "El alcance de Seguro OCA para la operativa normal no está confirmado."
            )
        return config.operation, config.cost_center, False, config.insured_operation
    if kind != "devolucion":
        raise ValueError(
            "oca_operation_kind debe ser 'normal' o 'devolucion'."
        )
    if not 1 <= config.return_operation <= 999_999:
        raise OCAConfigurationError(
            "La operativa OCA de devolución no está configurada."
        )
    if config.return_operation == config.operation:
        raise OCAConfigurationError(
            "La operativa OCA de devolución debe estar separada de la normal."
        )
    if not _valid_cost_center(config.return_cost_center):
        raise OCAConfigurationError(
            "El centro de costo OCA de devolución no está configurado."
        )
    if config.return_insured_operation_confirmed is not True:
        raise OCAConfigurationError(
            "El alcance de Seguro OCA para la operativa de devolución no está confirmado."
        )
    return (
        config.return_operation,
        config.return_cost_center,
        True,
        config.return_insured_operation,
    )


def _shipment_xml(
    shipment: Mapping[str, object],
    config: OCAConfig,
    *,
    idempotency_key: str,
    today: date,
) -> tuple[str, str, int, bool]:
    origin = shipment.get("origin")
    destination = shipment.get("destination")
    recipient = shipment.get("recipient")
    if not isinstance(origin, Mapping) or not isinstance(destination, Mapping):
        raise ValueError("El envío OCA necesita origen y destino completos.")
    recipient_data = dict(destination)
    if isinstance(recipient, Mapping):
        recipient_data.update(recipient)
    last_name, first_name = _recipient_names(recipient_data)
    operation, cost_center, is_return, insured = _shipment_operation(
        shipment, config
    )
    origin_is_withdrawal = config.origin_mode == "domicilio"
    destination_branch = 0
    if config.destination_mode == "sucursal":
        raw_branch = destination.get("branch_id") or destination.get("idci")
        try:
            destination_branch = int(str(raw_branch))
        except (TypeError, ValueError):
            raise ValueError("La sucursal OCA de destino es obligatoria.") from None
        if not 1 <= destination_branch <= 999:
            raise ValueError("La sucursal OCA de destino no es válida.")

    package_rows = _package_values(
        shipment.get("packages") or shipment.get("bultos"),
        insured=insured,
        declared_value=shipment.get("declared_value") or shipment.get("valor_declarado"),
    )
    reference = _remittance(idempotency_key)
    root = ET.Element("ROWS")
    ET.SubElement(
        root,
        "cabecera",
        {"ver": "2.0", "nrocuenta": config.account, "origen": "API"},
    )
    origins = ET.SubElement(root, "origenes")
    origin_element = ET.SubElement(
        origins,
        "origen",
        {
            "calle": _mapped_text(
                origin,
                ("calle", "street", "address"),
                field="La calle de origen",
                maximum=30,
                required=True,
            ),
            "nro": _street_number(origin, "El número de origen"),
            "piso": _mapped_text(origin, ("piso", "floor"), field="El piso de origen", maximum=2),
            "depto": _mapped_text(origin, ("depto", "apartment"), field="El departamento de origen", maximum=4),
            "cp": _postal_code(origin, "El origen"),
            "localidad": _mapped_text(
                origin,
                ("localidad", "city"),
                field="La localidad de origen",
                maximum=30,
                required=True,
            ),
            "provincia": _mapped_text(
                origin,
                ("provincia", "province", "state"),
                field="La provincia de origen",
                maximum=30,
                required=True,
            ),
            "contacto": _mapped_text(
                origin,
                ("contacto", "contact", "name"),
                field="El contacto de origen",
                maximum=30,
            ),
            "email": _email(origin, "El email de origen", required=origin_is_withdrawal),
            "solicitante": _mapped_text(
                origin,
                ("solicitante", "requester"),
                field="El solicitante",
                maximum=30,
            ),
            "observaciones": _mapped_text(
                origin,
                ("observaciones", "notes"),
                field="Las observaciones de origen",
                maximum=100,
            ) or ("Retiro TAURO" if origin_is_withdrawal else ""),
            "centrocosto": cost_center,
            "idfranjahoraria": str(config.time_slot),
            "idcentroimposicionorigen": str(config.origin_branch_id),
            "fecha": _shipment_date(
                shipment.get("pickup_date") or shipment.get("admission_date"),
                today,
            ),
        },
    )
    shipments = ET.SubElement(origin_element, "envios")
    shipment_element = ET.SubElement(
        shipments,
        "envio",
        {"idoperativa": str(operation), "nroremito": reference},
    )
    ET.SubElement(
        shipment_element,
        "destinatario",
        {
            "apellido": last_name,
            "nombre": first_name,
            "calle": _mapped_text(
                destination,
                ("calle", "street", "address"),
                field="La calle de destino",
                maximum=30,
                required=True,
            ),
            "nro": _street_number(destination, "El número de destino"),
            "piso": _mapped_text(destination, ("piso", "floor"), field="El piso de destino", maximum=6),
            "depto": _mapped_text(destination, ("depto", "apartment"), field="El departamento de destino", maximum=4),
            "localidad": _mapped_text(
                destination,
                ("localidad", "city"),
                field="La localidad de destino",
                maximum=30,
                required=True,
            ),
            "provincia": _mapped_text(
                destination,
                ("provincia", "province", "state"),
                field="La provincia de destino",
                maximum=30,
                required=True,
            ),
            "cp": _postal_code(destination, "El destino"),
            "telefono": _mapped_text(
                recipient_data,
                ("telefono", "phone"),
                field="El teléfono del destinatario",
                maximum=30,
            ),
            "email": _email(recipient_data, "El email del destinatario", required=False),
            "idci": str(destination_branch),
            "celular": _mapped_text(
                recipient_data,
                ("celular", "mobile"),
                field="El celular del destinatario",
                maximum=15,
            ),
            "observaciones": _mapped_text(
                destination,
                ("observaciones", "notes"),
                field="Las observaciones de destino",
                maximum=100,
            ),
        },
    )
    packages = ET.SubElement(shipment_element, "paquetes")
    for package in package_rows:
        ET.SubElement(packages, "paquete", package)
    serialized = ET.tostring(root, encoding="iso-8859-1", xml_declaration=True)
    return serialized.decode("iso-8859-1"), reference, operation, is_return


def _fields_for(element: ET.Element) -> dict[str, list[str]]:
    return _all_text(element)


def _parse_create_response(
    content: bytes,
    limit: int,
    reference: str,
    *,
    operation: int,
    confirmed: bool,
    is_return: bool = False,
) -> ShipmentResult:
    root = _safe_xml(content, limit)
    if any(_local_name(item.tag) == "fault" for item in root.iter()):
        raise OCAUnavailableError("OCA no pudo crear el envío.")

    # El endpoint acepta múltiples envíos. TAURO envía exactamente uno y no
    # puede asociar la primera fila de una respuesta ambigua: exigimos que la
    # única fila devuelta corresponda al remito determinístico y a la
    # operativa contractual solicitada.
    table_details = [
        item
        for item in root.iter()
        if _local_name(item.tag) == "table"
        and _first_field(
            _fields_for(item),
            ("ordenretiro", "idordenretiro", "numeroenvio", "nroenvio", "remito"),
        )
    ]
    details = table_details or [
        item
        for item in root.iter()
        if _local_name(item.tag) == "detalleingresos"
        and _first_field(
            _fields_for(item),
            ("ordenretiro", "idordenretiro", "numeroenvio", "nroenvio", "remito"),
        )
    ]
    all_fields = _all_text(root)
    rejected = _first_field(all_fields, ("cantidadrechazados",))
    accepted = _first_field(all_fields, ("cantidadingresados",))
    registered = _first_field(all_fields, ("cantidadregistros",))
    if not all(value.isdigit() for value in (registered, accepted, rejected)):
        raise OCAUnavailableError("OCA devolvió un resumen de alta incompleto.")
    counts = (int(registered), int(accepted), int(rejected))
    if counts not in {(1, 1, 0), (1, 0, 1)}:
        raise OCAUnavailableError("OCA devolvió un resumen de alta ambiguo.")
    if len(details) != 1:
        raise OCAUnavailableError("OCA devolvió una respuesta de alta ambigua.")

    detail_fields = _fields_for(details[0])
    order = _first_field(detail_fields, ("ordenretiro", "idordenretiro"))
    tracking = _first_field(detail_fields, ("numeroenvio", "nroenvio"))
    response_reference = _first_field(detail_fields, ("remito", "nroremito"))
    response_operation = _first_field(detail_fields, ("operativa", "idoperativa"))
    state = _first_field(detail_fields, ("estado", "resultado"))
    if response_reference != reference or response_operation != str(operation):
        raise OCAUnavailableError("OCA devolvió una respuesta de alta no correlacionada.")
    explicitly_rejected = bool(
        re.search(r"rechaz|error", state, flags=re.IGNORECASE)
    )
    if counts == (1, 0, 1):
        if not explicitly_rejected:
            raise OCAUnavailableError("OCA devolvió un rechazo de alta ambiguo.")
        return ShipmentResult(
            state=OperationState.ERROR_DEFINITIVO,
            carrier_id="oca",
            message_reference=reference,
            safe_message="OCA rechazó la creación del envío.",
        )
    if (
        explicitly_rejected
        or not _ORDER_RE.fullmatch(order)
        or not _TRACKING_RE.fullmatch(tracking)
    ):
        raise OCAUnavailableError("OCA devolvió un alta incompleta o ambigua.")
    return ShipmentResult(
        state=OperationState.EMITIDO if confirmed else OperationState.PENDIENTE,
        carrier_id="oca",
        external_id=f"{'return:' if is_return else ''}order:{order}",
        tracking=tracking,
        message_reference=reference,
    )


def _label_lookup(external_id: str) -> tuple[dict[str, str], bool]:
    raw = str(external_id or "").strip().lower()
    is_return = raw.startswith("return:")
    if is_return:
        raw = raw.removeprefix("return:")
    if raw.startswith("order:"):
        order = raw.removeprefix("order:")
        if _ORDER_RE.fullmatch(order):
            return {"idOrdenRetiro": order, "nroEnvio": ""}, is_return
    elif raw.startswith("tracking:"):
        tracking = raw.removeprefix("tracking:")
        if _TRACKING_RE.fullmatch(tracking):
            return {"idOrdenRetiro": "", "nroEnvio": tracking}, is_return
    elif _TRACKING_RE.fullmatch(raw):
        return {"idOrdenRetiro": "", "nroEnvio": raw}, is_return
    elif _ORDER_RE.fullmatch(raw):
        return {"idOrdenRetiro": raw, "nroEnvio": ""}, is_return
    raise ValueError("El identificador de etiqueta OCA no es válido.")


def _parse_label_response(content: bytes, *, wire_limit: int, pdf_limit: int) -> bytes:
    if content.startswith(b"%PDF-"):
        pdf = content
    else:
        root = _safe_xml(content, wire_limit)
        if any(_local_name(item.tag) == "fault" for item in root.iter()):
            raise OCAUnavailableError("OCA no pudo entregar la etiqueta.")
        pdf = b""
        candidates = sorted(
            (re.sub(r"\s+", "", text) for text in root.itertext()),
            key=len,
            reverse=True,
        )
        for candidate in candidates:
            if len(candidate) < 16:
                continue
            try:
                decoded = base64.b64decode(candidate, validate=True)
            except (binascii.Error, ValueError):
                continue
            if decoded.startswith(b"%PDF-"):
                pdf = decoded
                break
    if not pdf.startswith(b"%PDF-") or len(pdf) > pdf_limit:
        raise OCAUnavailableError("OCA devolvió una etiqueta inválida.")
    return pdf


def _parse_cancel_response(content: bytes, limit: int) -> OperationState:
    root = _safe_xml(content, limit)
    if any(_local_name(item.tag) == "fault" for item in root.iter()):
        raise OCAUnavailableError("OCA no pudo anular la orden.")
    fields = _all_text(root)
    code = _first_field(fields, ("idresult", "codigo", "codigooperacion"))
    if not code:
        code = next(
            (text.strip() for text in root.itertext() if re.fullmatch(r"\d{3}", text.strip())),
            "",
        )
    if not re.fullmatch(r"\d{3}", code):
        raise OCAUnavailableError("OCA devolvió una anulación ambigua.")
    if code == "100":
        return OperationState.CANCELADO
    if code in {"110", "120", "130"}:
        return OperationState.ERROR_DEFINITIVO
    raise OCAUnavailableError("OCA devolvió una anulación ambigua.")


def _parse_cost_centers(content: bytes, limit: int) -> tuple[str, ...]:
    root = _safe_xml(content, limit)
    if any(_local_name(item.tag) == "fault" for item in root.iter()):
        raise OCAUnavailableError("OCA no pudo consultar los centros de costo.")
    rows = [item for item in root.iter() if _local_name(item.tag) == "table"]
    if not rows:
        return ()
    centers: list[str] = []
    for row in rows:
        value = _first_field(
            _fields_for(row),
            ("nrocentrocosto", "centrocosto", "idcentrocosto"),
        )
        if not _valid_cost_center(value):
            raise OCAUnavailableError(
                "OCA devolvió centros de costo inválidos."
            )
        if value not in centers:
            centers.append(value)
    return tuple(centers)


def discover_oca_cost_centers(
    config: OCAConfig,
    *,
    operation: int | None = None,
    session: object | None = None,
) -> tuple[str, ...]:
    """Descubre centros durante onboarding sin habilitar el adapter.

    El método oficial sólo recibe CUIT y operativa. Exigir usuario, contraseña
    o un UAT ya aprobado crearía un ciclo imposible durante la configuración;
    por eso esta función valida exclusivamente el borde de solo lectura.
    """
    requested = config.operation if operation is None else operation
    if (
        isinstance(requested, bool)
        or not isinstance(requested, int)
        or not 1 <= requested <= 999_999
        or requested not in {config.operation, config.return_operation}
    ):
        raise OCAConfigurationError(
            "La operativa OCA a consultar no está configurada."
        )
    invalid = (
        config.environment not in {"qa", "production"}
        or not _valid_cuit(config.cuit)
        or not Decimal("0.1") <= config.connect_timeout_seconds <= Decimal("5")
        or not Decimal("0.1") <= config.read_timeout_seconds <= Decimal("5")
        or not 1_024 <= config.max_response_bytes <= _SAFE_XML_LIMIT
        or (config.environment == "production" and not config.production_approved)
    )
    if invalid:
        raise OCAConfigurationError(
            "La consulta OCA de centros de costo no tiene una configuración segura."
        )

    http = session or requests.Session()
    owns_session = session is None
    try:
        content = _post_bounded(
            http,
            config.cost_center_url,
            {"CUIT": config.cuit, "Operativa": str(requested)},
            timeout=config.timeout,
            limit=config.max_response_bytes,
            write_operation=False,
            unavailable_message=(
                "OCA no está disponible para consultar centros de costo."
            ),
        )
    finally:
        if owns_session:
            _quiet_close(http)
    return _parse_cost_centers(content, config.max_response_bytes)


def _argentina_today() -> date:
    return datetime.now(_ARGENTINA_TZ).date()


def _tracking_datetime(raw: str) -> str:
    value = raw.strip()
    if "T" in value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            parsed = (
                parsed.replace(tzinfo=_ARGENTINA_TZ)
                if parsed.tzinfo is None
                else parsed.astimezone(_ARGENTINA_TZ)
            )
            return parsed.isoformat()
        except ValueError:
            pass
    for pattern in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(value, pattern).replace(tzinfo=_ARGENTINA_TZ)
            return parsed.isoformat()
        except ValueError:
            continue
    return ""


def _safe_tracking_text(value: str, fallback: str = "") -> str:
    result = re.sub(r"[\x00-\x1f\x7f<>]", " ", value or "")
    result = re.sub(r"\s+", " ", result).strip()[:120]
    return result or fallback


def _parse_tracking_response(content: bytes, limit: int, tracking: str) -> TrackingResult:
    root = _safe_xml(content, limit)
    if any(_local_name(item.tag) == "fault" for item in root.iter()):
        raise OCAUnavailableError("OCA no pudo consultar el tracking.")
    rows = [item for item in root.iter() if _local_name(item.tag) == "table"]
    events: list[TrackingEvent] = []
    for row in rows:
        fields = _fields_for(row)
        label = _safe_tracking_text(
            _first_field(
                fields,
                ("desdcripcion_estado", "descripcion_estado", "estado", "descripcion"),
            )
        )
        if not label:
            continue
        reason = _safe_tracking_text(
            _first_field(fields, ("descripcion_motivo", "motivo"))
        )
        if reason and reason.lower() not in {"sin motivo", label.lower()}:
            label = f"{label} · {reason}"[:120]
        code = _safe_service_code(
            _first_field(fields, ("idestado", "codigoestado", "estadoid")),
            re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:80] or "evento",
        )
        events.append(
            TrackingEvent(
                code=code,
                label=label,
                occurred_at_iso=_tracking_datetime(
                    _first_field(fields, ("fecha", "fechaestado", "fechaevento"))
                ),
                location=_safe_tracking_text(
                    _first_field(fields, ("suc", "sucursal", "localidad"))
                ),
            )
        )
    if not events:
        return TrackingResult(
            # OCA responde un diffgram vacío tanto por propagación eventual
            # como por ausencia de eventos. Sin un código explícito de pieza
            # inexistente no hay evidencia para cortar el polling.
            state=OperationState.PENDIENTE,
            carrier_id="oca",
            tracking=tracking,
            safe_message="OCA todavía no devolvió eventos para este seguimiento.",
        )
    events.sort(key=lambda item: item.occurred_at_iso)
    return TrackingResult(
        state=OperationState.PENDIENTE,
        carrier_id="oca",
        tracking=tracking,
        current_status=events[-1].label,
        events=tuple(events),
    )


class OCAAdapter:
    carrier_id = "oca"

    def __init__(
        self,
        config: OCAConfig,
        *,
        session: object | None = None,
        pricing_loader: Callable[[str, float], Mapping[str, object]] | None = None,
        today_provider: Callable[[], date] | None = None,
    ) -> None:
        config.assert_ready()
        self._config = config
        self._session = session or requests.Session()
        self._pricing_loader = pricing_loader or _default_pricing_loader
        self._today_provider = today_provider or _argentina_today

    @property
    def callback_timeout_budget_seconds(self) -> float:
        """Presupuesto declarado al callback de Tiendanube."""
        return self._config.callback_timeout_budget_seconds

    def _post(
        self,
        method: str,
        data: Mapping[str, str],
        *,
        limit: int,
        write_operation: bool,
        unavailable_message: str,
    ) -> bytes:
        return self._post_url(
            self._config.method_url(method),
            data,
            limit=limit,
            write_operation=write_operation,
            unavailable_message=unavailable_message,
        )

    def _post_url(
        self,
        url: str,
        data: Mapping[str, str],
        *,
        limit: int,
        write_operation: bool,
        unavailable_message: str,
    ) -> bytes:
        return _post_bounded(
            self._session,
            url,
            data,
            timeout=self._config.timeout,
            limit=limit,
            write_operation=write_operation,
            unavailable_message=unavailable_message,
        )

    def quote(self, request: QuoteRequest) -> tuple[QuoteResult, ...]:
        validate_quote_request(request, self.carrier_id)
        payload = _payload(request, self._config)
        try:
            response = self._session.post(
                self._config.quote_url,
                data=payload,
                timeout=self._config.timeout,
                headers={"Accept": "application/xml, text/xml"},
                stream=True,
                allow_redirects=False,
            )
        except (requests.Timeout, requests.ConnectionError, requests.RequestException):
            raise OCAUnavailableError("OCA no está disponible para cotizar.") from None
        try:
            if not 200 <= int(getattr(response, "status_code", 0)) < 300:
                raise OCAUnavailableError("OCA no está disponible para cotizar.")
            content = _bounded_response_content(
                response,
                self._config.max_response_bytes,
            )
        except requests.RequestException:
            raise OCAUnavailableError("OCA no está disponible para cotizar.") from None
        finally:
            _quiet_close(response)
        cost, days, external_code, external_name = _parse_quote_response(
            content,
            self._config.max_response_bytes,
        )
        if cost is None:
            return (
                QuoteResult(
                    state=OperationState.SIN_TARIFA,
                    carrier_id=self.carrier_id,
                    safe_message="OCA no ofreció tarifa para esta ruta.",
                ),
            )
        try:
            pricing = self._pricing_loader(
                request.customer_id,
                float(self._config.fallback_markup_pct),
            )
        except OCAError:
            raise
        except Exception:
            raise OCAUnavailableError("No se pudo aplicar el pricing comercial.") from None
        customer_price = _customer_price(cost, pricing)
        digest = hashlib.sha256(
            f"{request.request_id}|{self._config.operation}|{cost}".encode("utf-8")
        ).hexdigest()[:24]
        service_code = _safe_service_code(
            external_code,
            f"oca-{self._config.operation}",
        )
        service_name = _safe_service_name(external_name)
        result = QuoteResult(
            state=OperationState.COTIZADO,
            carrier_id=self.carrier_id,
            quote_id=f"oca-{digest}",
            service_code=service_code,
            service_name=service_name,
            carrier_cost=cost.quantize(Decimal("0.01")),
            carrier_currency="ARS",
            customer_price=customer_price,
            currency="ARS",
            estimated_days=days,
            origin_mode=request.origin_mode,
            destination_mode=request.destination_mode,
        )
        validate_quote_result(result, self.carrier_id)
        return (result,)

    @staticmethod
    def _unsupported() -> None:
        raise OCAUnsupportedOperation(
            "La operación OCA todavía no está habilitada en este adapter."
        )

    def create_shipment(
        self,
        quote_id: str,
        shipment: Mapping[str, object],
        *,
        idempotency_key: str,
    ) -> ShipmentResult:
        self._config.assert_fulfillment_ready()
        if not _IDEMPOTENCY_RE.fullmatch(str(quote_id or "")):
            raise ValueError("El identificador de cotización OCA no es válido.")
        if not isinstance(shipment, Mapping):
            raise ValueError("El envío OCA no es válido.")
        xml_data, reference, operation, is_return = _shipment_xml(
            shipment,
            self._config,
            idempotency_key=idempotency_key,
            today=self._today_provider(),
        )
        return_quote = str(quote_id).startswith("oca-return-")
        if is_return != return_quote:
            raise OCAConfigurationError(
                "La cotización OCA no coincide con el tipo de operativa solicitado."
            )
        content = self._post(
            _CREATE_METHOD,
            {
                "usr": self._config.username,
                "psw": self._config.password,
                "xml_Datos": xml_data,
                "ConfirmarRetiro": (
                    "true" if self._config.confirm_withdrawal else "false"
                ),
                "ArchivoCliente": "",
                "ArchivoProceso": "",
                "compatMode": "",
            },
            limit=self._config.max_response_bytes,
            write_operation=True,
            unavailable_message="OCA no está disponible para crear el envío.",
        )
        try:
            return _parse_create_response(
                content,
                self._config.max_response_bytes,
                reference,
                operation=operation,
                confirmed=self._config.confirm_withdrawal,
                is_return=is_return,
            )
        except OCAUnavailableError:
            # La petición de escritura ya salió. Un 2xx truncado, inválido o
            # no correlacionado no demuestra que OCA haya rechazado el alta.
            raise OCAOutcomeUnknown(
                "OCA pudo haber aplicado la operación; requiere conciliación manual."
            ) from None

    def get_label(self, external_id: str) -> ShipmentResult:
        self._config.assert_fulfillment_ready()
        lookup, explicit_return = _label_lookup(external_id)
        if explicit_return and not self._config.return_operation:
            raise OCAConfigurationError(
                "La operativa OCA de devolución no está configurada."
            )
        if self._config.label_format == "10x15":
            request_lookup = {
                "ordenRetiro": lookup["idOrdenRetiro"],
                "numeroEnvio": lookup["nroEnvio"],
            }
        else:
            request_lookup = lookup
        wire_limit = min(
            _MAX_LABEL_LIMIT * 2,
            (self._config.max_label_bytes * 4 // 3) + 16_384,
        )
        content = self._post(
            _LABEL_METHODS[self._config.label_format],
            {
                **request_lookup,
                "logisticaInversa": (
                    "true"
                    if explicit_return or self._config.reverse_logistics
                    else "false"
                ),
            },
            limit=wire_limit,
            write_operation=False,
            unavailable_message="OCA no está disponible para obtener la etiqueta.",
        )
        pdf = _parse_label_response(
            content,
            wire_limit=wire_limit,
            pdf_limit=self._config.max_label_bytes,
        )
        return ShipmentResult(
            state=OperationState.ETIQUETA_LISTA,
            carrier_id=self.carrier_id,
            external_id=str(external_id),
            tracking=lookup["nroEnvio"],
            label_pdf=pdf,
        )

    def create_pickup(
        self,
        external_id: str,
        pickup: Mapping[str, object],
        *,
        idempotency_key: str,
    ) -> PickupResult:
        self._unsupported()

    def cancel(self, operation_id: str, *, idempotency_key: str) -> OperationState:
        self._config.assert_fulfillment_ready()
        _remittance(idempotency_key)
        lookup, explicit_return = _label_lookup(operation_id)
        if explicit_return and not self._config.return_operation:
            raise OCAConfigurationError(
                "La operativa OCA de devolución no está configurada."
            )
        order = lookup["idOrdenRetiro"]
        if not order:
            raise ValueError("OCA sólo permite anular por número de orden.")
        content = self._post(
            _CANCEL_METHOD,
            {
                "usr": self._config.username,
                "psw": self._config.password,
                "IdOrdenRetiro": order,
            },
            limit=self._config.max_response_bytes,
            write_operation=True,
            unavailable_message="OCA no está disponible para anular la orden.",
        )
        try:
            return _parse_cancel_response(content, self._config.max_response_bytes)
        except OCAUnavailableError:
            raise OCAOutcomeUnknown(
                "OCA pudo haber aplicado la operación; requiere conciliación manual."
            ) from None

    def discover_cost_centers(
        self,
        *,
        operation: int | None = None,
    ) -> tuple[str, ...]:
        """Consulta explícita de preflight; nunca se invoca desde callbacks.

        Se limita a las operativas declaradas en configuración para que una
        herramienta de onboarding no explore ni seleccione contratos ajenos.
        """
        return discover_oca_cost_centers(
            self._config,
            operation=operation,
            session=self._session,
        )

    def discover_unique_cost_center(self, *, operation: int | None = None) -> str:
        centers = self.discover_cost_centers(operation=operation)
        if len(centers) != 1:
            raise OCAConfigurationError(
                "OCA no devolvió un único centro de costo; requiere selección explícita."
            )
        return centers[0]

    def track(self, tracking: str) -> TrackingResult:
        self._config.assert_fulfillment_ready()
        normalized = str(tracking or "").strip()
        if not _TRACKING_RE.fullmatch(normalized):
            raise ValueError("El tracking OCA debe tener 19 dígitos.")
        content = self._post(
            _TRACK_METHOD,
            {"NumeroEnvio": normalized},
            limit=self._config.max_response_bytes,
            write_operation=False,
            unavailable_message="OCA no está disponible para consultar el tracking.",
        )
        return _parse_tracking_response(
            content,
            self._config.max_response_bytes,
            normalized,
        )


def register_oca_from_env(
    env: Mapping[str, str] | None = None,
    *,
    session: object | None = None,
    pricing_loader: Callable[[str, float], Mapping[str, object]] | None = None,
) -> bool:
    """Registra OCA únicamente cuando todos los gates están aprobados."""
    config = OCAConfig.from_env(env)
    if not config.enabled:
        unregister_adapter("oca")
        return False
    config.assert_ready()
    register_adapter(
        OCAAdapter(config, session=session, pricing_loader=pricing_loader)
    )
    return True
