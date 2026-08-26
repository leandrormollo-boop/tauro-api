from decimal import Decimal

import pytest

from servicios.carrier_adapter import (
    OperationState,
    PickupResult,
    QuoteResult,
    ShipmentResult,
    TrackingResult,
    Package,
    QuoteRequest,
    adapter_for,
    public_quote,
    register_adapter,
    registered_adapters,
    unregister_adapter,
    validate_quote_request,
    validate_quote_result,
)
from servicios.carrier_contract import Ambito, Capacidad


class CompleteFakeAdapter:
    carrier_id = "oca"

    def quote(self, request):
        return (QuoteResult(
            state=OperationState.COTIZADO,
            carrier_id=self.carrier_id,
            quote_id="quote-1",
            service_code="DOM",
            service_name="Entrega a domicilio",
            carrier_cost=Decimal("10000.00"),
            carrier_currency="ARS",
            customer_price=Decimal("12345.67"),
        ),)

    def create_shipment(self, quote_id, shipment, *, idempotency_key):
        return ShipmentResult(OperationState.EMITIDO, self.carrier_id)

    def get_label(self, external_id):
        return ShipmentResult(OperationState.ETIQUETA_LISTA, self.carrier_id)

    def create_pickup(self, external_id, pickup, *, idempotency_key):
        return PickupResult(OperationState.RECOLECCION_AGENDADA, self.carrier_id)

    def cancel(self, operation_id, *, idempotency_key):
        return OperationState.CANCELADO

    def track(self, tracking):
        return TrackingResult(OperationState.COTIZADO, self.carrier_id, tracking)


def teardown_function():
    unregister_adapter("oca")


def test_declarar_carrier_no_lo_hace_ejecutable():
    with pytest.raises(RuntimeError, match="todavía no tiene un adapter"):
        adapter_for("oca", Capacidad.COTIZAR)


def test_adapter_completo_se_registra_y_resuelve_por_capacidad():
    adapter = CompleteFakeAdapter()
    register_adapter(adapter)
    assert registered_adapters() == ("oca",)
    assert adapter_for("oca", "emitir") is adapter


def test_no_registra_operador_desconocido():
    adapter = CompleteFakeAdapter()
    adapter.carrier_id = "inventado"
    with pytest.raises(ValueError, match="no declarado"):
        register_adapter(adapter)


def _request(scope=Ambito.NACIONAL, origin="AR", destination="AR"):
    return QuoteRequest(
        request_id="req-1",
        customer_id="MELCIOR",
        scope=scope,
        origin={"pais": origin},
        destination={"pais": destination},
        packages=(Package(1, Decimal("1.2"), Decimal("12"), Decimal("33"), Decimal("36")),),
        declared_value=Decimal("40000"),
        declared_currency="ARS",
    )


def test_valida_ambito_y_ruta_antes_del_adapter():
    validate_quote_request(_request(), "oca")
    with pytest.raises(ValueError, match="AR → AR"):
        validate_quote_request(_request(destination="US"), "oca")
    with pytest.raises(ValueError, match="no puede usar"):
        validate_quote_request(
            _request(scope=Ambito.INTERNACIONAL), "dhl"
        )


def test_resultado_cotizado_exige_costo_y_precio_decimal():
    result = CompleteFakeAdapter().quote(_request())[0]
    validate_quote_result(result, "oca")
    with pytest.raises(ValueError, match="costo del operador"):
        validate_quote_result(
            QuoteResult(
                state=OperationState.COTIZADO,
                carrier_id="oca",
                quote_id="q",
                service_code="DOM",
                service_name="Domicilio",
                carrier_cost=None,
                carrier_currency="ARS",
                customer_price=Decimal("12000"),
            ),
            "oca",
        )


def test_tarifa_publica_nunca_expone_costo_o_margen():
    result = CompleteFakeAdapter().quote(_request())[0]
    data = public_quote(result)
    assert data["precio_final"] == "12345.67"
    assert "carrier_cost" not in data
    assert "costo" not in data
    assert "margen" not in data
