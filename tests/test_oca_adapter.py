from decimal import Decimal

import pytest
import requests

from servicios.carrier_adapter import (
    OperationState,
    Package,
    QuoteRequest,
    adapter_for,
    unregister_adapter,
)
from servicios.carrier_contract import Ambito, Capacidad
from servicios.oca_adapter import (
    OCAAdapter,
    OCAConfig,
    OCAConfigurationError,
    OCAUnavailableError,
    OCAUnsupportedOperation,
    PRODUCTION_QUOTE_URL,
    QA_QUOTE_URL,
    register_oca_from_env,
    registration_status,
)


class FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code
        self.headers = {}
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start:start + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


def _config(**changes):
    data = {
        "enabled": True,
        "uat_approved": True,
        "production_approved": False,
        "environment": "qa",
        "cuit": "20-12345678-6",
        "account": "123456/001",
        "operation": 123456,
        "username": "usuario-contractual",
        "password": "password-contractual",
        "origin_mode": "domicilio",
        "destination_mode": "domicilio",
    }
    data.update(changes)
    return OCAConfig(**data)


def _request(**changes):
    data = {
        "request_id": "req-oca-1",
        "customer_id": "CLIENTE-1",
        "scope": Ambito.NACIONAL,
        "origin": {"pais": "AR", "codigo_postal": "1425"},
        "destination": {"pais": "AR", "postal_code": "2000"},
        "packages": (
            Package(2, Decimal("1.25"), Decimal("10"), Decimal("20"), Decimal("30")),
            Package(1, Decimal("0.5"), Decimal("5"), Decimal("10"), Decimal("20")),
        ),
        "declared_value": Decimal("40000"),
        "declared_currency": "ARS",
        "origin_mode": "domicilio",
        "destination_mode": "domicilio",
    }
    data.update(changes)
    return QuoteRequest(**data)


def _adapter(response, *, config=None, pricing=None):
    session = FakeSession(response=response)
    adapter = OCAAdapter(
        config or _config(),
        session=session,
        pricing_loader=lambda _customer, _fallback: pricing
        or {"tipo": "PCT", "valor": 25},
    )
    return adapter, session


def _env(**changes):
    values = {
        "OCA_ADAPTER_ENABLED": "true",
        "OCA_UAT_APPROVED": "true",
        "OCA_ENVIRONMENT": "qa",
        "OCA_PRODUCTION_APPROVED": "false",
        "OCA_CUIT": "20-12345678-6",
        "OCA_CUENTA": "123456/001",
        "OCA_OPERATIVA": "123456",
        "OCA_USUARIO": "usuario",
        "OCA_PASSWORD": "password",
        "OCA_ORIGIN_MODE": "domicilio",
        "OCA_DESTINATION_MODE": "domicilio",
        "OCA_CONNECT_TIMEOUT_SECONDS": "1",
        "OCA_READ_TIMEOUT_SECONDS": "3",
    }
    values.update(changes)
    return values


def teardown_function():
    unregister_adapter("oca")


def test_disabled_by_default_and_never_registers():
    status = registration_status({})
    assert status["ready"] is False
    assert status["enabled"] is False
    assert register_oca_from_env({}) is False
    with pytest.raises(RuntimeError, match="todavía no tiene"):
        adapter_for("oca", Capacidad.COTIZAR)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OCA_CUIT", "20-12345678-0"),
        ("OCA_CUENTA", "123456001"),
        ("OCA_OPERATIVA", "0"),
        ("OCA_USUARIO", ""),
        ("OCA_PASSWORD", ""),
        ("OCA_ORIGIN_MODE", "inventado"),
        ("OCA_CONNECT_TIMEOUT_SECONDS", "4"),
    ],
)
def test_rejects_incomplete_or_unsafe_configuration(name, value):
    values = _env(**{name: value})
    with pytest.raises(OCAConfigurationError, match="configuración aprobada"):
        register_oca_from_env(values)
    assert registration_status(values)["ready"] is False


def test_configuration_repr_and_errors_never_expose_credentials():
    config = _config(username="very-secret-user", password="very-secret-password")
    visible = repr(config)
    assert "very-secret-user" not in visible
    assert "very-secret-password" not in visible
    assert "redacted" in visible


def test_production_requires_separate_explicit_approval():
    blocked = _env(OCA_ENVIRONMENT="production")
    assert registration_status(blocked)["environment_approved"] is False
    with pytest.raises(OCAConfigurationError):
        register_oca_from_env(blocked)

    approved = _env(
        OCA_ENVIRONMENT="production",
        OCA_PRODUCTION_APPROVED="true",
    )
    session = FakeSession(FakeResponse(b"<root><Total>100</Total></root>"))
    assert register_oca_from_env(
        approved,
        session=session,
        pricing_loader=lambda _customer, _fallback: {"tipo": "PCT", "valor": 25},
    ) is True
    assert adapter_for("oca", "cotizar")._config.quote_url == PRODUCTION_QUOTE_URL


def test_quote_posts_exact_official_parameters_to_qa_with_bounded_timeout():
    xml = b"""
        <NewDataSet><Table>
          <Precio>10000.50</Precio><Adicional>100</Adicional><Total>10100.50</Total>
          <idTiposervicio>1</idTiposervicio><TipoServicio>Puerta a Puerta</TipoServicio>
          <PlazoEntrega>2 a 3 dias</PlazoEntrega>
        </Table></NewDataSet>
    """
    adapter, session = _adapter(FakeResponse(xml))

    result = adapter.quote(_request())[0]

    assert session.calls == [
        (
            QA_QUOTE_URL,
            {
                "data": {
                    "Cuit": "20-12345678-6",
                    "Operativa": "123456",
                    "PesoTotal": "3",
                    "VolumenTotal": "0.013",
                    "CodigoPostalOrigen": "1425",
                    "CodigoPostalDestino": "2000",
                    "CantidadPaquetes": "3",
                    "ValorDeclarado": "40000",
                },
                "timeout": (1.0, 3.0),
                "headers": {"Accept": "application/xml, text/xml"},
                "stream": True,
                "allow_redirects": False,
            },
        )
    ]
    assert result.state == OperationState.COTIZADO
    assert result.carrier_cost == Decimal("10100.50")
    assert result.customer_price == Decimal("12626")
    assert result.estimated_days == 3
    assert result.service_code == "1"
    assert result.service_name == "Puerta a Puerta"
    assert result.quote_id.startswith("oca-")


def test_parses_namespaced_soap_with_embedded_xml_comma_decimal_and_hours():
    xml = b"""
      <soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
        <soap:Body><TarifarResponse><TarifarResult>
          &lt;Data&gt;&lt;Table&gt;&lt;Total&gt;1.234,50&lt;/Total&gt;
          &lt;PlazoEntrega&gt;24 a 48 hs&lt;/PlazoEntrega&gt;&lt;/Table&gt;&lt;/Data&gt;
        </TarifarResult></TarifarResponse></soap:Body>
      </soap:Envelope>
    """
    adapter, _ = _adapter(FakeResponse(xml), pricing={"tipo": "FIJO_ARS", "valor": 500})

    result = adapter.quote(_request())[0]

    assert result.carrier_cost == Decimal("1234.50")
    assert result.customer_price == Decimal("1735")
    assert result.estimated_days == 2


def test_well_formed_response_without_rate_is_business_no_rate():
    adapter, _ = _adapter(
        FakeResponse(b"<NewDataSet><Mensaje>Sin cobertura</Mensaje></NewDataSet>")
    )

    result = adapter.quote(_request())[0]

    assert result.state == OperationState.SIN_TARIFA
    assert result.carrier_cost is None
    assert "cobertura" not in result.safe_message.lower()


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(b"<broken>"),
        FakeResponse(b"<!DOCTYPE foo [<!ENTITY x 'boom'>]><foo>&x;</foo>"),
        FakeResponse(b"<html><body>gateway</body></html>"),
        FakeResponse(b"<root><Total>importe-invalido</Total></root>"),
        FakeResponse(b"<root><Total>1</Total></root>", status_code=503),
    ],
)
def test_malformed_or_http_error_is_sanitized_unavailability(response):
    adapter, _ = _adapter(response)
    with pytest.raises(OCAUnavailableError) as error:
        adapter.quote(_request())
    visible = str(error.value)
    assert "gateway" not in visible
    assert "importe-invalido" not in visible
    assert "password-contractual" not in visible


def test_rejects_oversized_xml_and_network_timeout_without_retrying():
    config = _config(max_response_bytes=1024)
    adapter, session = _adapter(FakeResponse(b"<x>" + b"a" * 2000 + b"</x>"), config=config)
    with pytest.raises(OCAUnavailableError):
        adapter.quote(_request())
    assert len(session.calls) == 1

    timeout_session = FakeSession(error=requests.Timeout("internal endpoint details"))
    adapter = OCAAdapter(
        _config(),
        session=timeout_session,
        pricing_loader=lambda *_: {"tipo": "PCT", "valor": 25},
    )
    with pytest.raises(OCAUnavailableError, match="no está disponible") as error:
        adapter.quote(_request())
    assert "internal endpoint" not in str(error.value)
    assert len(timeout_session.calls) == 1


@pytest.mark.parametrize(
    "quote_request",
    [
        _request(declared_currency="USD"),
        _request(origin={"pais": "AR", "codigo_postal": "C1425ABC"}),
        _request(origin_mode="sucursal"),
    ],
)
def test_business_contract_is_rejected_before_network(quote_request):
    adapter, session = _adapter(FakeResponse(b"<root><Total>1</Total></root>"))
    with pytest.raises(ValueError):
        adapter.quote(quote_request)
    assert session.calls == []


def test_declared_value_with_cents_is_rounded_up_without_underdeclaring():
    adapter, session = _adapter(FakeResponse(b"<root><Total>100</Total></root>"))

    adapter.quote(_request(declared_value=Decimal("40000.01")))

    assert session.calls[0][1]["data"]["ValorDeclarado"] == "40001"


def test_only_quote_is_exposed_and_other_protocol_methods_fail_closed():
    adapter, _ = _adapter(FakeResponse(b"<root><Total>1</Total></root>"))
    calls = (
        lambda: adapter.create_shipment("q", {}, idempotency_key="i"),
        lambda: adapter.get_label("external"),
        lambda: adapter.create_pickup("external", {}, idempotency_key="i"),
        lambda: adapter.cancel("external", idempotency_key="i"),
        lambda: adapter.track("tracking"),
    )
    for call in calls:
        with pytest.raises(OCAUnsupportedOperation, match="no está habilitada"):
            call()


def test_registration_requires_all_gates_and_registers_complete_protocol():
    session = FakeSession(FakeResponse(b"<root><Total>100</Total></root>"))
    assert register_oca_from_env(
        _env(),
        session=session,
        pricing_loader=lambda _customer, _fallback: {"tipo": "PCT", "valor": 25},
    ) is True
    assert adapter_for("oca", Capacidad.COTIZAR).carrier_id == "oca"
    with pytest.raises(ValueError, match="no admite"):
        adapter_for("oca", Capacidad.EMITIR)
