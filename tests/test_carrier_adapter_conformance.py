"""Harness común: cada adapter futuro debe pasar este recorrido completo."""
import ast
from decimal import Decimal
from pathlib import Path

import pytest

from servicios.carrier_adapter import (
    OperationState,
    Package,
    PickupResult,
    QuoteRequest,
    QuoteResult,
    ShipmentResult,
    TrackingResult,
    public_quote,
    register_adapter,
    unregister_adapter,
    validate_quote_request,
    validate_quote_result,
)
from servicios.carrier_contract import Ambito, carrier_spec


class DeterministicContractAdapter:
    """Fake sin red ni secretos; modela la respuesta que deberá normalizar cada API."""

    def __init__(self, carrier_id: str):
        self.carrier_id = carrier_id

    def quote(self, request):
        return (QuoteResult(
            state=OperationState.COTIZADO,
            carrier_id=self.carrier_id,
            quote_id=f"{self.carrier_id}-quote-1",
            service_code="STANDARD",
            service_name="Servicio estándar",
            carrier_cost=Decimal("100.00"),
            carrier_currency="USD" if request.scope == Ambito.INTERNACIONAL else "ARS",
            customer_price=Decimal("150000.00"),
            currency="ARS",
            estimated_days=3,
            origin_mode=request.origin_mode,
            destination_mode=request.destination_mode,
        ),)

    def create_shipment(self, quote_id, shipment, *, idempotency_key):
        return ShipmentResult(
            OperationState.EMITIDO,
            self.carrier_id,
            external_id=f"{self.carrier_id}-shipment-1",
            tracking="TRACK-1",
            message_reference=idempotency_key,
        )

    def get_label(self, external_id):
        return ShipmentResult(
            OperationState.ETIQUETA_LISTA,
            self.carrier_id,
            external_id=external_id,
            tracking="TRACK-1",
            label_pdf=b"%PDF-1.4 test",
        )

    def create_pickup(self, external_id, pickup, *, idempotency_key):
        return PickupResult(
            OperationState.RECOLECCION_AGENDADA,
            self.carrier_id,
            confirmation_code="PICKUP-1",
            message_reference=idempotency_key,
        )

    def cancel(self, operation_id, *, idempotency_key):
        return OperationState.CANCELADO

    def track(self, tracking):
        return TrackingResult(
            OperationState.PENDIENTE,
            self.carrier_id,
            tracking,
            current_status="en_transito",
        )


def request_for(carrier_id: str) -> QuoteRequest:
    spec = carrier_spec(carrier_id)
    scope = next(iter(spec.ambitos))
    national = scope == Ambito.NACIONAL
    return QuoteRequest(
        request_id=f"req-{carrier_id}",
        customer_id="CLIENTE_TEST",
        scope=scope,
        origin={"pais": "AR"},
        destination={"pais": "AR" if national else "US"},
        packages=(Package(
            quantity=1,
            weight_kg=Decimal("5.5"),
            length_cm=Decimal("30"),
            width_cm=Decimal("20"),
            height_cm=Decimal("10"),
        ),),
        declared_value=Decimal("100000" if national else "100"),
        declared_currency="ARS" if national else "USD",
    )


@pytest.mark.parametrize("carrier_id", ["dhl", "fedex", "ups", "andreani", "oca"])
def test_todos_los_operadores_comparten_contrato_sin_exponer_costo(carrier_id):
    adapter = DeterministicContractAdapter(carrier_id)
    request = request_for(carrier_id)
    validate_quote_request(request, carrier_id)
    register_adapter(adapter)
    try:
        quote = adapter.quote(request)[0]
        validate_quote_result(quote, carrier_id)
        public = public_quote(quote)
        assert public["carrier_id"] == carrier_id
        assert public["precio_final"] == "150000.00"
        assert "carrier_cost" not in public
        assert "costo" not in public
    finally:
        unregister_adapter(carrier_id)


def test_fake_no_hace_red_ni_lee_variables_de_entorno():
    tree = ast.parse(Path(__file__).read_text())
    imported = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imported.isdisjoint({"requests", "httpx", "os"})
