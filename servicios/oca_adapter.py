"""Adapter OCA e-Pak nacional, apagado por defecto.

La primera capacidad operativa es la cotización mediante el método oficial
``Tarifar_Envio_Corporativo``. El registro es fail-closed: además del flag se
exigen cuenta, operativa, credenciales y evidencia explícita de UAT.
"""
from __future__ import annotations

import hashlib
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from typing import Callable, Mapping

import requests

from servicios.carrier_adapter import (
    OperationState,
    PickupResult,
    QuoteRequest,
    QuoteResult,
    ShipmentResult,
    TrackingResult,
    register_adapter,
    unregister_adapter,
    validate_quote_request,
    validate_quote_result,
)


QA_QUOTE_URL = (
    "https://integraciones.ocadev.com.ar/epak_tracking_test/"
    "Oep_TrackEPak.asmx/Tarifar_Envio_Corporativo"
)
PRODUCTION_QUOTE_URL = (
    "https://webservice.oca.com.ar/ePak_tracking/"
    "Oep_TrackEPak.asmx/Tarifar_Envio_Corporativo"
)
_TRUE = {"1", "true", "yes", "si", "sí", "on"}
_CUIT_RE = re.compile(r"^(\d{2})-(\d{8})-(\d)$")
_ACCOUNT_RE = re.compile(r"^\d{1,6}/\d{3}$")
_POSTAL_RE = re.compile(r"^\d{4}$")
_SAFE_XML_LIMIT = 1_000_000


class OCAError(RuntimeError):
    """Error seguro del borde OCA, sin payloads ni secretos."""


class OCAConfigurationError(OCAError):
    pass


class OCAUnavailableError(OCAError):
    pass


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


@dataclass(frozen=True)
class OCAConfig:
    enabled: bool
    uat_approved: bool
    production_approved: bool
    environment: str
    cuit: str = ""
    account: str = ""
    operation: int = 0
    username: str = ""
    password: str = ""
    origin_mode: str = ""
    destination_mode: str = ""
    connect_timeout_seconds: Decimal = Decimal("1")
    read_timeout_seconds: Decimal = Decimal("3")
    max_response_bytes: int = _SAFE_XML_LIMIT
    fallback_markup_pct: Decimal = Decimal("25")

    def __repr__(self) -> str:
        return (
            "OCAConfig("
            f"enabled={self.enabled!r}, uat_approved={self.uat_approved!r}, "
            f"production_approved={self.production_approved!r}, "
            f"environment={self.environment!r}, credentials=<redacted>)"
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
            operation=_integer_config(values.get("OCA_OPERATIVA") or "0", "OCA_OPERATIVA"),
            username=str(values.get("OCA_USUARIO") or "").strip(),
            password=str(values.get("OCA_PASSWORD") or "").strip(),
            origin_mode=str(values.get("OCA_ORIGIN_MODE") or "").strip().lower(),
            destination_mode=str(values.get("OCA_DESTINATION_MODE") or "").strip().lower(),
            connect_timeout_seconds=_decimal_config(
                values.get("OCA_CONNECT_TIMEOUT_SECONDS") or "1",
                "OCA_CONNECT_TIMEOUT_SECONDS",
            ),
            read_timeout_seconds=_decimal_config(
                values.get("OCA_READ_TIMEOUT_SECONDS") or "3",
                "OCA_READ_TIMEOUT_SECONDS",
            ),
            max_response_bytes=_integer_config(
                values.get("OCA_MAX_RESPONSE_BYTES") or str(_SAFE_XML_LIMIT),
                "OCA_MAX_RESPONSE_BYTES",
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
    def timeout(self) -> tuple[float, float]:
        return (
            float(self.connect_timeout_seconds),
            float(self.read_timeout_seconds),
        )

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
        if self.connect_timeout_seconds + self.read_timeout_seconds > Decimal("5"):
            errors.append("timeout_budget")
        if not 1_024 <= self.max_response_bytes <= _SAFE_XML_LIMIT:
            errors.append("response_limit")
        if not Decimal("0") <= self.fallback_markup_pct <= Decimal("300"):
            errors.append("fallback_markup")
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


def registration_status(env: Mapping[str, str] | None = None) -> dict:
    """Diagnóstico apto para preflight: sólo códigos, nunca valores."""
    values = os.environ if env is None else env
    try:
        config = OCAConfig.from_env(values)
    except OCAConfigurationError:
        return {
            "ready": False,
            "enabled": _flag(values.get("OCA_ADAPTER_ENABLED")),
            "uat_approved": _flag(values.get("OCA_UAT_APPROVED")),
            "configuration_valid": False,
            "environment_approved": (
                str(values.get("OCA_ENVIRONMENT") or "qa").strip().lower() == "qa"
            ),
        }
    errors = set(config.readiness_errors())
    return {
        "ready": not errors,
        "enabled": config.enabled,
        "uat_approved": config.uat_approved,
        "configuration_valid": not config.configuration_errors(),
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
    from servicios.pricing import get_pricing_config

    return get_pricing_config(
        customer_id,
        fallback_pct=fallback_pct,
        ambito="nacional",
    )


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


class OCAAdapter:
    carrier_id = "oca"

    def __init__(
        self,
        config: OCAConfig,
        *,
        session: object | None = None,
        pricing_loader: Callable[[str, float], Mapping[str, object]] | None = None,
    ) -> None:
        config.assert_ready()
        self._config = config
        self._session = session or requests.Session()
        self._pricing_loader = pricing_loader or _default_pricing_loader

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
            close = getattr(response, "close", None)
            if callable(close):
                close()
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
        self._unsupported()

    def get_label(self, external_id: str) -> ShipmentResult:
        self._unsupported()

    def create_pickup(
        self,
        external_id: str,
        pickup: Mapping[str, object],
        *,
        idempotency_key: str,
    ) -> PickupResult:
        self._unsupported()

    def cancel(self, operation_id: str, *, idempotency_key: str) -> OperationState:
        self._unsupported()

    def track(self, tracking: str) -> TrackingResult:
        self._unsupported()


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
