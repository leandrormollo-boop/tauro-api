"""Interfaz única que deberán implementar las APIs logísticas de TAURO.

No reemplaza todavía los clientes DHL/FedEx existentes. Define el borde nuevo
para que Portal, Web y Admin no dependan del payload particular de un courier.
El registro arranca vacío: declarar un operador en ``carrier_contract`` nunca
lo vuelve ejecutable por sí solo.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping, Protocol, Tuple, runtime_checkable

from servicios.carrier_contract import Ambito, Capacidad, carrier_spec
from servicios.paises import normalizar_iso2


class OperationState(StrEnum):
    COTIZADO = "cotizado"
    EMITIDO = "emitido"
    ETIQUETA_LISTA = "etiqueta_lista"
    RECOLECCION_AGENDADA = "recoleccion_agendada"
    CANCELADO = "cancelado"
    PENDIENTE = "pendiente"
    SIN_TARIFA = "sin_tarifa"
    INCIERTO = "incierto"
    ERROR_DEFINITIVO = "error_definitivo"


@dataclass(frozen=True)
class Package:
    quantity: int
    weight_kg: Decimal
    length_cm: Decimal
    width_cm: Decimal
    height_cm: Decimal


@dataclass(frozen=True)
class QuoteRequest:
    request_id: str
    customer_id: str
    scope: Ambito
    origin: Mapping[str, Any]
    destination: Mapping[str, Any]
    packages: Tuple[Package, ...]
    declared_value: Decimal
    declared_currency: str
    origin_mode: str = "domicilio"
    destination_mode: str = "domicilio"
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class QuoteResult:
    state: OperationState
    carrier_id: str
    quote_id: str = ""
    service_code: str = ""
    service_name: str = ""
    carrier_cost: Decimal | None = None
    carrier_currency: str = ""
    customer_price: Decimal | None = None
    currency: str = "ARS"
    estimated_days: int | None = None
    origin_mode: str = ""
    destination_mode: str = ""
    expires_at_iso: str = ""
    safe_message: str = ""


@dataclass(frozen=True)
class ShipmentResult:
    state: OperationState
    carrier_id: str
    external_id: str = ""
    tracking: str = ""
    label_pdf: bytes | None = None
    message_reference: str = ""
    safe_message: str = ""


@dataclass(frozen=True)
class PickupResult:
    state: OperationState
    carrier_id: str
    confirmation_code: str = ""
    message_reference: str = ""
    safe_message: str = ""


@dataclass(frozen=True)
class TrackingEvent:
    code: str
    label: str
    occurred_at_iso: str
    location: str = ""


@dataclass(frozen=True)
class TrackingResult:
    state: OperationState
    carrier_id: str
    tracking: str
    current_status: str = ""
    events: Tuple[TrackingEvent, ...] = ()
    safe_message: str = ""


@runtime_checkable
class CarrierAdapter(Protocol):
    """Contrato de ejecución; cada método debe ser idempotente/fail-closed."""

    carrier_id: str

    def quote(self, request: QuoteRequest) -> Tuple[QuoteResult, ...]: ...

    def create_shipment(
        self,
        quote_id: str,
        shipment: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> ShipmentResult: ...

    def get_label(self, external_id: str) -> ShipmentResult: ...

    def create_pickup(
        self,
        external_id: str,
        pickup: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> PickupResult: ...

    def cancel(
        self,
        operation_id: str,
        *,
        idempotency_key: str,
    ) -> OperationState: ...

    def track(self, tracking: str) -> TrackingResult: ...


_ADAPTERS: dict[str, CarrierAdapter] = {}


def register_adapter(adapter: CarrierAdapter) -> None:
    """Registra sólo adapters completos de operadores declarados."""
    carrier_id = (getattr(adapter, "carrier_id", "") or "").strip().lower()
    if not carrier_spec(carrier_id):
        raise ValueError("El adapter pertenece a un operador no declarado.")
    if not isinstance(adapter, CarrierAdapter):
        raise TypeError("El adapter no implementa el contrato completo de TAURO.")
    _ADAPTERS[carrier_id] = adapter


def unregister_adapter(carrier_id: str) -> None:
    """Helper de tests/startup; no modifica permisos ni datos de clientes."""
    _ADAPTERS.pop((carrier_id or "").strip().lower(), None)


def adapter_for(carrier_id: str, capacidad: Capacidad | str) -> CarrierAdapter:
    """Obtiene un adapter o falla antes de tocar red, crédito o cuenta."""
    spec = carrier_spec(carrier_id)
    if not spec:
        raise ValueError("Operador no reconocido.")
    try:
        capacidad = Capacidad(str(capacidad).lower())
    except ValueError:
        raise ValueError("Capacidad logística no reconocida.") from None
    if capacidad not in spec.capacidades:
        raise ValueError(f"{spec.nombre} no admite esa operación.")
    adapter = _ADAPTERS.get(spec.id)
    if not adapter:
        raise RuntimeError(
            f"{spec.nombre} todavía no tiene un adapter operativo registrado."
        )
    return adapter


def registered_adapters() -> Tuple[str, ...]:
    return tuple(sorted(_ADAPTERS))


def _country_of(data: Mapping[str, Any]) -> str:
    return normalizar_iso2(
        data.get("pais")
        or data.get("country")
        or data.get("country_code")
        or ""
    )


def validate_quote_request(request: QuoteRequest, carrier_id: str) -> None:
    """Valida el contrato común antes de entregar datos a un adapter."""
    spec = carrier_spec(carrier_id)
    if not spec:
        raise ValueError("Operador no reconocido.")
    if request.scope not in spec.ambitos:
        raise ValueError(f"{spec.nombre} no está declarado para ese ámbito.")
    if not request.request_id.strip() or not request.customer_id.strip():
        raise ValueError("La cotización necesita referencia y cliente.")

    origin_country = _country_of(request.origin)
    destination_country = _country_of(request.destination)
    if not origin_country or not destination_country:
        raise ValueError("Origen y destino necesitan países válidos.")
    if request.scope == Ambito.NACIONAL:
        if origin_country != "AR" or destination_country != "AR":
            raise ValueError("El circuito nacional requiere una ruta AR → AR.")
    elif origin_country == "AR" and destination_country == "AR":
        raise ValueError("Una ruta AR → AR no puede usar el circuito internacional.")

    if request.origin_mode not in {"domicilio", "sucursal"}:
        raise ValueError("La modalidad de origen no es válida.")
    if request.destination_mode not in {"domicilio", "sucursal"}:
        raise ValueError("La modalidad de destino no es válida.")
    if not request.packages or len(request.packages) > 20:
        raise ValueError("La cotización debe contener entre 1 y 20 tipos de bulto.")

    total_packages = 0
    for package in request.packages:
        if (
            isinstance(package.quantity, bool)
            or not isinstance(package.quantity, int)
            or package.quantity <= 0
        ):
            raise ValueError("La cantidad de bultos debe ser un entero positivo.")
        total_packages += package.quantity
        if total_packages > 100:
            raise ValueError("La cotización supera 100 bultos.")
        values = (
            package.weight_kg,
            package.length_cm,
            package.width_cm,
            package.height_cm,
        )
        if any(not isinstance(value, Decimal) or not value.is_finite() or value <= 0
               for value in values):
            raise ValueError("Peso y medidas deben ser decimales positivos.")

    if (
        not isinstance(request.declared_value, Decimal)
        or not request.declared_value.is_finite()
        or request.declared_value <= 0
    ):
        raise ValueError("El valor declarado debe ser un decimal positivo.")
    declared_currency = request.declared_currency.strip()
    if (
        len(declared_currency) != 3
        or not declared_currency.isalpha()
        or declared_currency != declared_currency.upper()
    ):
        raise ValueError("La moneda declarada debe usar un código ISO de 3 letras.")


def validate_quote_result(result: QuoteResult, expected_carrier_id: str) -> None:
    """Bloquea tarifas incompletas o no finitas antes de pricing/UI."""
    expected = (expected_carrier_id or "").strip().lower()
    if result.carrier_id != expected:
        raise ValueError("La respuesta pertenece a otro operador.")
    if result.state != OperationState.COTIZADO:
        return
    if not result.quote_id or not result.service_code or not result.service_name:
        raise ValueError("La tarifa no tiene identificador o servicio.")
    for value, label in (
        (result.carrier_cost, "costo del operador"),
        (result.customer_price, "precio del cliente"),
    ):
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise ValueError(f"La tarifa no tiene {label} válido.")
    carrier_currency = (result.carrier_currency or "").strip()
    if (
        len(carrier_currency) != 3
        or not carrier_currency.isalpha()
        or carrier_currency != carrier_currency.upper()
    ):
        raise ValueError("La moneda del costo del operador no es válida.")
    customer_currency = (result.currency or "").strip()
    if (
        len(customer_currency) != 3
        or not customer_currency.isalpha()
        or customer_currency != customer_currency.upper()
    ):
        raise ValueError("La moneda del precio final no es válida.")


def public_quote(result: QuoteResult) -> dict:
    """Serializa una tarifa sin filtrar costo, margen ni regla comercial."""
    validate_quote_result(result, result.carrier_id)
    return {
        "estado": result.state.value,
        "carrier_id": result.carrier_id,
        "quote_id": result.quote_id,
        "servicio_codigo": result.service_code,
        "servicio_nombre": result.service_name,
        "precio_final": (
            str(result.customer_price) if result.customer_price is not None else None
        ),
        "moneda": result.currency,
        "dias_estimados": result.estimated_days,
        "modalidad_origen": result.origin_mode,
        "modalidad_destino": result.destination_mode,
        "valida_hasta": result.expires_at_iso,
        "mensaje": result.safe_message,
    }
