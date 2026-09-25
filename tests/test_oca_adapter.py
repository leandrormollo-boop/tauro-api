import base64
import hashlib
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
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
    OCAOutcomeUnknown,
    OCAUnavailableError,
    OCAUnsupportedOperation,
    PRODUCTION_QUOTE_URL,
    QA_BASE_URL,
    QA_COST_CENTER_URL,
    QA_QUOTE_URL,
    discover_oca_cost_centers,
    register_oca_from_env,
    registration_status,
)


# Reloj fijo para que las fechas de retiro no dependan del día de ejecución.
TEST_TODAY = date(2026, 9, 18)


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


class ExplodingCloseResponse(FakeResponse):
    def close(self):
        raise requests.ConnectionError("detalle privado de cierre")


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


def _fulfillment_config(**changes):
    data = {
        "fulfillment_enabled": True,
        "fulfillment_uat_approved": True,
        "cost_center": "1",
        "insured_operation_confirmed": True,
        "time_slot": 1,
        "origin_branch_id": 0,
        "label_format": "10x15",
    }
    data.update(changes)
    return _config(**data)


def _shipment(**changes):
    data = {
        "origin": {
            "calle": "Av. Corrientes & Uruguay",
            "nro": "1234",
            "cp": "1043",
            "localidad": "CABA",
            "provincia": "Buenos Aires",
            "contacto": "TAURO",
            "email": "operaciones@taurosolutions.ar",
        },
        "destination": {
            "calle": "San Martín",
            "nro": "550",
            "cp": "2000",
            "localidad": "Rosario",
            "provincia": "Santa Fe",
            "telefono": "3415550101",
            "email": "cliente@example.com",
        },
        "recipient": {"first_name": "Ana María", "last_name": "Pérez"},
        "packages": [
            {
                "quantity": 2,
                "weight_kg": "1.25",
                "length_cm": "20",
                "width_cm": "10",
                "height_cm": "5",
            }
        ],
        "pickup_date": TEST_TODAY.isoformat(),
        "declared_value": "50000",
    }
    data.update(changes)
    return data


def _adapter(response, *, config=None, pricing=None):
    session = FakeSession(response=response)
    adapter = OCAAdapter(
        config or _config(),
        session=session,
        pricing_loader=lambda _customer, _fallback: pricing
        or {"tipo": "PCT", "valor": 25},
        today_provider=lambda: TEST_TODAY,
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
        "OCA_CONNECT_TIMEOUT_SECONDS": "0.5",
        "OCA_READ_TIMEOUT_SECONDS": "1.3",
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
        ("OCA_CUENTA", "12345/001"),
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


def test_rejects_timeout_budget_without_margin_for_tiendanube():
    values = _env(
        OCA_CONNECT_TIMEOUT_SECONDS="0.8",
        OCA_READ_TIMEOUT_SECONDS="1.1",
    )

    with pytest.raises(OCAConfigurationError):
        register_oca_from_env(values)
    assert "timeout_budget" in OCAConfig.from_env(values).configuration_errors()


def test_return_operation_is_separate_and_fail_closed():
    same_operation = _config(return_operation=123456)
    assert "return_operation_not_separate" in same_operation.configuration_errors()
    with pytest.raises(OCAConfigurationError):
        OCAAdapter(same_operation)

    adapter, session = _adapter(
        FakeResponse(b"<root/>"),
        config=_fulfillment_config(return_operation=472096),
    )
    with pytest.raises(OCAConfigurationError, match="centro de costo OCA de devolución"):
        adapter.create_shipment(
            "oca-return-quote",
            _shipment(oca_operation_kind="devolucion"),
            idempotency_key="return-without-center",
        )
    assert session.calls == []


def test_fulfillment_requires_explicit_normal_insurance_confirmation():
    config = _fulfillment_config(insured_operation_confirmed=False)
    assert (
        "insured_operation_not_confirmed"
        in config.fulfillment_configuration_errors()
    )
    adapter, session = _adapter(FakeResponse(b"<root/>"), config=config)

    with pytest.raises(OCAConfigurationError, match="configuración aprobada"):
        adapter.create_shipment(
            "oca-quote",
            _shipment(),
            idempotency_key="normal-insurance-unknown",
        )
    assert session.calls == []


def test_return_requires_its_own_explicit_insurance_confirmation():
    adapter, session = _adapter(
        FakeResponse(b"<root/>"),
        config=_fulfillment_config(
            return_operation=472096,
            return_cost_center="2",
            return_insured_operation=False,
            return_insured_operation_confirmed=False,
        ),
    )

    with pytest.raises(OCAConfigurationError, match="devolución no está confirmado"):
        adapter.create_shipment(
            "oca-return-quote",
            _shipment(oca_operation_kind="devolucion"),
            idempotency_key="return-insurance-unknown",
        )
    assert session.calls == []


def test_unknown_operation_kind_is_rejected_before_network():
    adapter, session = _adapter(
        FakeResponse(b"<root/>"),
        config=_fulfillment_config(
            return_operation=472096,
            return_cost_center="2",
            return_insured_operation_confirmed=True,
        ),
    )

    with pytest.raises(ValueError, match="oca_operation_kind"):
        adapter.create_shipment(
            "oca-quote",
            _shipment(oca_operation_kind="automatica"),
            idempotency_key="unknown-kind",
        )
    assert session.calls == []


@pytest.mark.parametrize(
    ("quote_id", "operation_kind"),
    [
        ("oca-normal-quote", "devolucion"),
        ("oca-return-authorized", "normal"),
    ],
)
def test_quote_marker_cannot_be_mixed_between_normal_and_return(
    quote_id, operation_kind
):
    adapter, session = _adapter(
        FakeResponse(b"<root/>"),
        config=_fulfillment_config(
            return_operation=472096,
            return_cost_center="2",
            return_insured_operation_confirmed=True,
        ),
    )

    with pytest.raises(OCAConfigurationError, match="no coincide"):
        adapter.create_shipment(
            quote_id,
            _shipment(oca_operation_kind=operation_kind),
            idempotency_key=f"mismatch-{operation_kind}",
        )
    assert session.calls == []


def test_configuration_repr_and_errors_never_expose_credentials():
    config = _config(username="very-secret-user", password="very-secret-password")
    visible = repr(config)
    assert "very-secret-user" not in visible
    assert "very-secret-password" not in visible
    assert "redacted" in visible


def test_return_operation_and_cost_center_load_from_separate_env_keys():
    config = OCAConfig.from_env(
        _env(
            OCA_OPERATIVA="472095",
            OCA_OPERATIVA_DEVOLUCION="472096",
            OCA_CENTRO_COSTO_DEVOLUCION="2",
            OCA_OPERATIVA_ASEGURADA="true",
            OCA_OPERATIVA_SEGURO_CONFIRMADO="true",
            OCA_OPERATIVA_DEVOLUCION_ASEGURADA="false",
            OCA_OPERATIVA_DEVOLUCION_SEGURO_CONFIRMADO="true",
        )
    )

    assert config.operation == 472095
    assert config.return_operation == 472096
    assert config.return_cost_center == "2"
    assert config.insured_operation is True
    assert config.insured_operation_confirmed is True
    assert config.return_insured_operation is False
    assert config.return_insured_operation_confirmed is True


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


def test_fulfillment_has_independent_uat_and_production_confirmation_gates():
    quote_ready = _config()
    assert "fulfillment_disabled" in quote_ready.fulfillment_readiness_errors()
    assert "fulfillment_uat_not_approved" in quote_ready.fulfillment_readiness_errors()

    production = _fulfillment_config(
        environment="production",
        production_approved=True,
        confirm_withdrawal=False,
    )
    assert "production_confirmation_disabled" in (
        production.fulfillment_readiness_errors()
    )
    with pytest.raises(OCAConfigurationError, match="ejecución OCA"):
        production.assert_fulfillment_ready()

    approved = _fulfillment_config(
        environment="production",
        production_approved=True,
        confirm_withdrawal=True,
    )
    assert approved.fulfillment_readiness_errors() == ()


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
                "timeout": (0.5, 1.3),
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


def test_fulfillment_stays_blocked_until_its_separate_gates_are_approved():
    adapter, _ = _adapter(FakeResponse(b"<root><Total>1</Total></root>"))
    calls = (
        lambda: adapter.create_shipment("q", _shipment(), idempotency_key="i"),
        lambda: adapter.get_label("external"),
        lambda: adapter.cancel("external", idempotency_key="i"),
        lambda: adapter.track("tracking"),
    )
    for call in calls:
        with pytest.raises(OCAConfigurationError, match="ejecución OCA"):
            call()
    with pytest.raises(OCAUnsupportedOperation, match="no está habilitada"):
        adapter.create_pickup("external", {}, idempotency_key="i")


def test_create_builds_official_xml_and_keeps_qa_order_unconfirmed():
    idempotency_key = "tn:store-1:label-1"
    reference = "TAURO-" + hashlib.sha256(
        idempotency_key.encode("utf-8")
    ).hexdigest()[:24]
    response = f"""
      <Resultado>
        <DetalleIngresos>
          <Operativa>123456</Operativa><OrdenRetiro>7654321</OrdenRetiro>
          <NumeroEnvio>1234567890123456789</NumeroEnvio>
          <Remito>{reference}</Remito><Estado>Ingresado</Estado>
        </DetalleIngresos>
        <Resumen><CodigoOperacion>1</CodigoOperacion>
          <CantidadRegistros>1</CantidadRegistros>
          <CantidadIngresados>1</CantidadIngresados><CantidadRechazados>0</CantidadRechazados>
        </Resumen>
      </Resultado>
    """.encode()
    adapter, session = _adapter(
        FakeResponse(response),
        config=_fulfillment_config(),
    )
    adapter._today_provider = lambda: date(2026, 9, 18)

    result = adapter.create_shipment(
        "oca-quote-1",
        _shipment(),
        idempotency_key=idempotency_key,
    )

    assert result.state == OperationState.PENDIENTE
    assert result.external_id == "order:7654321"
    assert result.tracking == "1234567890123456789"
    assert result.message_reference.startswith("TAURO-")
    assert len(result.message_reference) == 30
    url, kwargs = session.calls[0]
    assert url == f"{QA_BASE_URL}/IngresoORMultiplesRetiros_v2"
    assert kwargs["data"]["ConfirmarRetiro"] == "false"
    assert kwargs["data"]["compatMode"] == ""
    xml_root = ET.fromstring(kwargs["data"]["xml_Datos"].encode("iso-8859-1"))
    assert xml_root.find("cabecera").attrib == {
        "ver": "2.0",
        "nrocuenta": "123456/001",
        "origen": "API",
    }
    origin = xml_root.find("./origenes/origen")
    assert origin is not None
    assert origin.attrib["calle"] == "Av. Corrientes & Uruguay"
    assert origin.attrib["centrocosto"] == "1"
    shipment = xml_root.find("./origenes/origen/envios/envio")
    assert shipment is not None
    assert shipment.attrib["nroremito"] == result.message_reference
    assert len(xml_root.findall("./origenes/origen/envios/envio/paquetes/paquete")) == 2


def test_return_create_uses_only_return_operation_and_cost_center():
    idempotency_key = "tn:store-1:return-1"
    reference = "TAURO-" + hashlib.sha256(
        idempotency_key.encode("utf-8")
    ).hexdigest()[:24]
    response = f"""
      <Resultado><DetalleIngresos><Operativa>472096</Operativa>
        <OrdenRetiro>7654321</OrdenRetiro>
        <NumeroEnvio>1234567890123456789</NumeroEnvio>
        <Remito>{reference}</Remito><Estado>Ingresado</Estado>
      </DetalleIngresos><Resumen><CantidadRegistros>1</CantidadRegistros>
        <CantidadIngresados>1</CantidadIngresados>
        <CantidadRechazados>0</CantidadRechazados></Resumen></Resultado>
    """.encode()
    adapter, session = _adapter(
        FakeResponse(response),
        config=_fulfillment_config(
            operation=472095,
            cost_center="1",
            return_operation=472096,
            return_cost_center="2",
            insured_operation=True,
            return_insured_operation=False,
            return_insured_operation_confirmed=True,
        ),
    )

    result = adapter.create_shipment(
        "oca-return-quote",
        _shipment(oca_operation_kind="devolucion"),
        idempotency_key=idempotency_key,
    )

    xml_root = ET.fromstring(
        session.calls[0][1]["data"]["xml_Datos"].encode("iso-8859-1")
    )
    assert xml_root.find("./origenes/origen").attrib["centrocosto"] == "2"
    assert (
        xml_root.find("./origenes/origen/envios/envio").attrib["idoperativa"]
        == "472096"
    )
    assert {
        item.attrib["valor"]
        for item in xml_root.findall(
            "./origenes/origen/envios/envio/paquetes/paquete"
        )
    } == {"0"}
    assert result.external_id == "return:order:7654321"


def test_create_rejects_invalid_input_before_network_and_timeout_is_uncertain():
    adapter, session = _adapter(
        FakeResponse(b"<root/>"),
        config=_fulfillment_config(),
    )
    adapter._today_provider = lambda: date(2026, 9, 18)
    bad = _shipment(destination={"cp": "2000"})
    with pytest.raises(ValueError):
        adapter.create_shipment("oca-q", bad, idempotency_key="label-1")
    assert session.calls == []

    timeout = FakeSession(error=requests.Timeout("private host"))
    adapter = OCAAdapter(
        _fulfillment_config(),
        session=timeout,
        pricing_loader=lambda *_: {"tipo": "PCT", "valor": 25},
        today_provider=lambda: date(2026, 9, 18),
    )
    with pytest.raises(OCAOutcomeUnknown, match="conciliación manual") as error:
        adapter.create_shipment(
            "oca-q",
            _shipment(),
            idempotency_key="label-1",
        )
    assert "private host" not in str(error.value)
    assert len(timeout.calls) == 1


def test_get_label_decodes_bounded_pdf_from_official_base64_response():
    pdf = b"%PDF-1.4\nfixture\n%%EOF"
    encoded = base64.b64encode(pdf).decode("ascii")
    adapter, session = _adapter(
        FakeResponse(f"<string>{encoded}</string>".encode()),
        config=_fulfillment_config(label_format="a4"),
    )

    result = adapter.get_label("tracking:1234567890123456789")

    assert result.state == OperationState.ETIQUETA_LISTA
    assert result.label_pdf == pdf
    assert result.tracking == "1234567890123456789"
    assert session.calls[0][0].endswith("/GetPdfDeEtiquetasPorOrdenOrNumeroEnvio")
    assert session.calls[0][1]["data"] == {
        "idOrdenRetiro": "",
        "nroEnvio": "1234567890123456789",
        "logisticaInversa": "false",
    }


def test_get_label_10x15_uses_the_official_parameter_names():
    pdf = b"%PDF-1.4\nfixture\n%%EOF"
    encoded = base64.b64encode(pdf).decode("ascii")
    adapter, session = _adapter(
        FakeResponse(f"<string>{encoded}</string>".encode()),
        config=_fulfillment_config(label_format="10x15"),
    )

    result = adapter.get_label("order:7654321")

    assert result.label_pdf == pdf
    assert session.calls[0][0].endswith(
        "/GetPdfDeEtiquetasPorOrdenOrNumeroEnvioParaEtiquetadora"
    )
    assert session.calls[0][1]["data"] == {
        "ordenRetiro": "7654321",
        "numeroEnvio": "",
        "logisticaInversa": "false",
    }


def test_return_external_id_requests_inverse_label_explicitly():
    pdf = b"%PDF-1.4\nfixture\n%%EOF"
    encoded = base64.b64encode(pdf).decode("ascii")
    adapter, session = _adapter(
        FakeResponse(f"<string>{encoded}</string>".encode()),
        config=_fulfillment_config(
            return_operation=472096,
            return_cost_center="2",
            label_format="a4",
        ),
    )

    result = adapter.get_label("return:order:7654321")

    assert result.label_pdf == pdf
    assert session.calls[0][1]["data"] == {
        "idOrdenRetiro": "7654321",
        "nroEnvio": "",
        "logisticaInversa": "true",
    }


def test_cost_center_discovery_is_explicit_and_never_selects_ambiguity():
    adapter, session = _adapter(
        FakeResponse(
            b"<NewDataSet><Table><NroCentroCosto>7</NroCentroCosto>"
            b"</Table></NewDataSet>"
        ),
        config=_fulfillment_config(
            operation=472095,
            return_operation=472096,
        ),
    )

    assert adapter.discover_unique_cost_center(operation=472096) == "7"
    assert session.calls[0][0] == QA_COST_CENTER_URL
    assert session.calls[0][1]["data"] == {
        "CUIT": "20-12345678-6",
        "Operativa": "472096",
    }

    ambiguous, _ = _adapter(
        FakeResponse(
            b"<NewDataSet><Table><NroCentroCosto>7</NroCentroCosto></Table>"
            b"<Table><NroCentroCosto>8</NroCentroCosto></Table></NewDataSet>"
        ),
        config=_fulfillment_config(
            operation=472095,
            return_operation=472096,
        ),
    )
    assert ambiguous.discover_cost_centers() == ("7", "8")
    with pytest.raises(OCAConfigurationError, match="selección explícita"):
        ambiguous.discover_unique_cost_center()


def test_cost_center_onboarding_does_not_require_runtime_gates_or_credentials():
    config = OCAConfig(
        enabled=False,
        uat_approved=False,
        production_approved=False,
        environment="qa",
        cuit="20-12345678-6",
        operation=472095,
        return_operation=472096,
    )
    session = FakeSession(
        FakeResponse(
            b"<NewDataSet><Table><NroCentroCosto>7</NroCentroCosto>"
            b"</Table></NewDataSet>"
        )
    )

    assert discover_oca_cost_centers(
        config,
        operation=472096,
        session=session,
    ) == ("7",)
    assert session.calls[0][0] == QA_COST_CENTER_URL


@pytest.mark.parametrize("invalid_operation", [-1, 1_000_000, True, "472095"])
def test_cost_center_onboarding_rejects_invalid_operation_types_and_ranges(
    invalid_operation,
):
    config = OCAConfig(
        enabled=False,
        uat_approved=False,
        production_approved=False,
        environment="qa",
        cuit="20-12345678-6",
        operation=invalid_operation,
    )
    session = FakeSession(FakeResponse(b"<NewDataSet/>"))

    with pytest.raises(OCAConfigurationError, match="no está configurada"):
        discover_oca_cost_centers(config, session=session)
    assert session.calls == []


def test_cost_center_onboarding_ignores_close_failures(monkeypatch):
    config = OCAConfig(
        enabled=False,
        uat_approved=False,
        production_approved=False,
        environment="qa",
        cuit="20-12345678-6",
        operation=472095,
    )

    class ExplodingCloseSession(FakeSession):
        def close(self):
            raise requests.ConnectionError("detalle privado de sesión")

    session = ExplodingCloseSession(
        ExplodingCloseResponse(
            b"<NewDataSet><Table><NroCentroCosto>7</NroCentroCosto>"
            b"</Table></NewDataSet>"
        )
    )
    monkeypatch.setattr("servicios.oca_adapter.requests.Session", lambda: session)

    assert discover_oca_cost_centers(config) == ("7",)


def test_cost_center_discovery_fails_closed_on_zero_or_unconfigured_operation():
    adapter, session = _adapter(
        FakeResponse(b"<NewDataSet/>"),
        config=_fulfillment_config(operation=472095),
    )

    with pytest.raises(OCAConfigurationError, match="selección explícita"):
        adapter.discover_unique_cost_center()
    with pytest.raises(OCAConfigurationError, match="no está configurada"):
        adapter.discover_cost_centers(operation=472096)
    assert len(session.calls) == 1


def test_cost_center_discovery_rejects_non_numeric_wsdl_value():
    adapter, _ = _adapter(
        FakeResponse(
            b"<NewDataSet><Table><NroCentroCosto>centro-a</NroCentroCosto>"
            b"</Table></NewDataSet>"
        ),
        config=_fulfillment_config(operation=472095),
    )

    with pytest.raises(OCAUnavailableError, match="centros de costo inválidos"):
        adapter.discover_cost_centers()


def test_real_adapter_exposes_its_checkout_timeout_budget():
    adapter, _ = _adapter(FakeResponse(b"<root><Total>1</Total></root>"))

    assert adapter.callback_timeout_budget_seconds == pytest.approx(1.8)


@pytest.mark.parametrize(
    "response",
    [
        b"<html>truncated",
        b"<Resultado><DetalleIngresos><Operativa>123456</Operativa>"
        b"<OrdenRetiro>7654321</OrdenRetiro><Remito>OTRO</Remito>"
        b"</DetalleIngresos></Resultado>",
        b"<Resultado>"
        b"<Table><Operativa>123456</Operativa><OrdenRetiro>1</OrdenRetiro>"
        b"<Remito>TAURO-a</Remito></Table>"
        b"<Table><Operativa>123456</Operativa><OrdenRetiro>2</OrdenRetiro>"
        b"<Remito>TAURO-b</Remito></Table>"
        b"</Resultado>",
    ],
)
def test_post_send_create_response_ambiguity_is_never_retryable(response):
    adapter, session = _adapter(
        FakeResponse(response),
        config=_fulfillment_config(),
    )

    with pytest.raises(OCAOutcomeUnknown, match="conciliación manual"):
        adapter.create_shipment(
            "oca-quote-1",
            _shipment(),
            idempotency_key="tn:store-1:label-ambiguous",
        )

    assert len(session.calls) == 1


def test_post_send_oversized_create_response_is_outcome_unknown():
    adapter, session = _adapter(
        FakeResponse(b"<x>" + b"a" * 2_000 + b"</x>"),
        config=_fulfillment_config(max_response_bytes=1_024),
    )

    with pytest.raises(OCAOutcomeUnknown):
        adapter.create_shipment(
            "oca-quote-1",
            _shipment(),
            idempotency_key="tn:store-1:label-oversized",
        )

    assert len(session.calls) == 1


def test_correlated_explicit_create_rejection_is_definitive():
    idempotency_key = "tn:store-1:label-rejected"
    reference = "TAURO-" + hashlib.sha256(
        idempotency_key.encode("utf-8")
    ).hexdigest()[:24]
    response = f"""
      <Resultado>
        <DetalleIngresos>
          <Operativa>123456</Operativa><OrdenRetiro></OrdenRetiro>
          <NumeroEnvio></NumeroEnvio><Remito>{reference}</Remito>
          <Estado>Rechazado por datos</Estado>
        </DetalleIngresos>
        <Resumen><CantidadRegistros>1</CantidadRegistros>
          <CantidadIngresados>0</CantidadIngresados>
          <CantidadRechazados>1</CantidadRechazados></Resumen>
      </Resultado>
    """.encode()
    adapter, _ = _adapter(
        FakeResponse(response),
        config=_fulfillment_config(),
    )

    result = adapter.create_shipment(
        "oca-quote-1",
        _shipment(),
        idempotency_key=idempotency_key,
    )

    assert result.state == OperationState.ERROR_DEFINITIVO


def test_aware_pickup_datetime_is_converted_to_argentina_before_serializing():
    idempotency_key = "tn:store-1:label-timezone"
    reference = "TAURO-" + hashlib.sha256(
        idempotency_key.encode("utf-8")
    ).hexdigest()[:24]
    response = f"""
      <Resultado><DetalleIngresos><Operativa>123456</Operativa>
        <OrdenRetiro>7654321</OrdenRetiro>
        <NumeroEnvio>1234567890123456789</NumeroEnvio>
        <Remito>{reference}</Remito><Estado>Ingresado</Estado>
      </DetalleIngresos><Resumen><CantidadRegistros>1</CantidadRegistros>
        <CantidadIngresados>1</CantidadIngresados>
        <CantidadRechazados>0</CantidadRechazados></Resumen></Resultado>
    """.encode()
    adapter, session = _adapter(
        FakeResponse(response),
        config=_fulfillment_config(),
    )
    adapter._today_provider = lambda: date(2026, 9, 18)

    adapter.create_shipment(
        "oca-quote-1",
        _shipment(
            pickup_date=datetime(2026, 9, 19, 1, 30, tzinfo=timezone.utc)
        ),
        idempotency_key=idempotency_key,
    )

    root = ET.fromstring(
        session.calls[0][1]["data"]["xml_Datos"].encode("iso-8859-1")
    )
    assert root.find("./origenes/origen").attrib["fecha"] == "20260918"


def test_cancel_maps_official_code_and_never_retries_unknown_outcome():
    adapter, session = _adapter(
        FakeResponse(b"<NewDataSet><Table><Codigo>100</Codigo></Table></NewDataSet>"),
        config=_fulfillment_config(),
    )
    assert adapter.cancel("order:7654321", idempotency_key="cancel-1") == (
        OperationState.CANCELADO
    )
    assert session.calls[0][1]["data"]["IdOrdenRetiro"] == "7654321"

    timeout = FakeSession(error=requests.Timeout("private host"))
    adapter = OCAAdapter(
        _fulfillment_config(),
        session=timeout,
        pricing_loader=lambda *_: {"tipo": "PCT", "valor": 25},
    )
    with pytest.raises(OCAOutcomeUnknown):
        adapter.cancel("order:7654321", idempotency_key="cancel-1")
    assert len(timeout.calls) == 1

    malformed, _ = _adapter(
        FakeResponse(
            "<NewDataSet><Table><Mensaje>sin código</Mensaje></Table></NewDataSet>".encode()
        ),
        config=_fulfillment_config(),
    )
    with pytest.raises(OCAOutcomeUnknown):
        malformed.cancel("order:7654321", idempotency_key="cancel-2")

    undocumented, _ = _adapter(
        FakeResponse(b"<NewDataSet><Table><Codigo>140</Codigo></Table></NewDataSet>"),
        config=_fulfillment_config(),
    )
    with pytest.raises(OCAOutcomeUnknown):
        undocumented.cancel("order:7654321", idempotency_key="cancel-3")


@pytest.mark.parametrize("code", ["110", "120", "130"])
def test_cancel_documented_rejections_are_definitive(code):
    adapter, _ = _adapter(
        FakeResponse(
            f"<NewDataSet><Table><Codigo>{code}</Codigo></Table></NewDataSet>".encode()
        ),
        config=_fulfillment_config(),
    )

    assert adapter.cancel("order:7654321", idempotency_key=f"cancel-{code}") == (
        OperationState.ERROR_DEFINITIVO
    )


def test_cancel_accepts_official_idresult_field():
    adapter, _ = _adapter(
        FakeResponse(
            b"<NewDataSet><Table><IdResult>100</IdResult>"
            b"<Mensaje>Anulacion exitosa</Mensaje></Table></NewDataSet>"
        ),
        config=_fulfillment_config(),
    )

    assert adapter.cancel("order:7654321", idempotency_key="cancel-idresult") == (
        OperationState.CANCELADO
    )


def test_tracking_normalizes_events_and_uses_latest_status():
    response = b"""
      <NewDataSet>
        <Table><IdEstado>20</IdEstado><Desdcripcion_Estado>Entregado</Desdcripcion_Estado>
          <Descripcion_Motivo>Sin Motivo</Descripcion_Motivo><SUC>ROS</SUC>
          <fecha>18/09/2026 14:30:00</fecha></Table>
        <Table><IdEstado>10</IdEstado><Desdcripcion_Estado>En viaje</Desdcripcion_Estado>
          <SUC>CBA</SUC><fecha>17/09/2026 09:00:00</fecha></Table>
      </NewDataSet>
    """
    adapter, session = _adapter(
        FakeResponse(response),
        config=_fulfillment_config(),
    )

    result = adapter.track("1234567890123456789")

    assert result.current_status == "Entregado"
    assert [event.label for event in result.events] == ["En viaje", "Entregado"]
    assert result.events[-1].occurred_at_iso == "2026-09-18T14:30:00-03:00"
    assert session.calls[0][0].endswith("/Tracking_Pieza_ConIdEstado")
    assert session.calls[0][1]["data"] == {"NumeroEnvio": "1234567890123456789"}


def test_tracking_accepts_wsdl_datetime_with_fraction_and_offset():
    response = b"""
      <NewDataSet><Table><IdEstado>20</IdEstado>
        <Desdcripcion_Estado>Entregado</Desdcripcion_Estado>
        <fecha>2026-09-18T14:30:00.000-03:00</fecha>
      </Table></NewDataSet>
    """
    adapter, _ = _adapter(
        FakeResponse(response),
        config=_fulfillment_config(),
    )

    result = adapter.track("1234567890123456789")

    assert result.events[0].occurred_at_iso == "2026-09-18T14:30:00-03:00"


def test_empty_tracking_diffgram_keeps_polling_pending():
    adapter, _ = _adapter(
        FakeResponse(
            b"<NewDataSet xmlns:diffgr='urn:schemas-microsoft-com:xml-diffgram-v1'>"
            b"<diffgr:diffgram /></NewDataSet>"
        ),
        config=_fulfillment_config(),
    )

    result = adapter.track("1234567890123456789")

    assert result.state == OperationState.PENDIENTE
    assert result.events == ()
    assert "todavía" in result.safe_message


def test_registration_requires_all_gates_and_registers_complete_protocol():
    session = FakeSession(FakeResponse(b"<root><Total>100</Total></root>"))
    assert register_oca_from_env(
        _env(),
        session=session,
        pricing_loader=lambda _customer, _fallback: {"tipo": "PCT", "valor": 25},
    ) is True
    assert adapter_for("oca", Capacidad.COTIZAR).carrier_id == "oca"
    assert adapter_for("oca", Capacidad.EMITIR).carrier_id == "oca"
    with pytest.raises(OCAConfigurationError, match="ejecución OCA"):
        adapter_for("oca", Capacidad.EMITIR).create_shipment(
            "oca-q",
            _shipment(),
            idempotency_key="label-1",
        )
