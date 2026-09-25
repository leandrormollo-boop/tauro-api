import importlib.util
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
import pytest
import httpx, asyncio
from servicios.oca_portal_qa import QAPortal, QAPortalError
from servicios.oca_adapter import OCAConfig
from servicios.carrier_adapter import (
    QuoteResult,
    ShipmentResult,
    OperationState,
    validate_quote_request,
)


class Adapter:
    def __init__(self):
        self.emissions = 0
        self.cancellations = 0
        self.fail = False
        self.pdf_fail = False

    def quote(self, r):
        validate_quote_request(r, "oca")
        return (
            QuoteResult(
                state=OperationState.COTIZADO,
                carrier_id="oca",
                quote_id="oca-test",
                customer_price=Decimal("476"),
            ),
        )

    def create_shipment(self, *a, **kw):
        self.emissions += 1
        if self.fail:
            raise TimeoutError()
        return ShipmentResult(
            state=OperationState.EMITIDO,
            carrier_id="oca",
            external_id="order:123",
            tracking="1234567890123456789",
        )

    def get_label(self, *a):
        if self.pdf_fail:
            raise TimeoutError()
        return ShipmentResult(
            state=OperationState.ETIQUETA_LISTA,
            carrier_id="oca",
            label_pdf=b"%PDF-test",
        )

    def cancel(self, *a, **kw):
        self.cancellations += 1
        return OperationState.CANCELADO


@pytest.fixture
def service(tmp_path):
    c = OCAConfig.from_env(
        {
            "OCA_ENVIRONMENT": "qa",
            "OCA_CUIT": "30-53625919-4",
            "OCA_CUENTA": "111757/001",
            "OCA_OPERATIVA": "64665",
            "OCA_CENTRO_COSTO": "2",
            "OCA_USUARIO": "test@oca.com.ar",
            "OCA_PASSWORD": "fixture",
            "OCA_ORIGIN_MODE": "domicilio",
            "OCA_DESTINATION_MODE": "domicilio",
            "OCA_OPERATIVA_SEGURO_CONFIRMADO": "true",
            "OCA_CONFIRM_WITHDRAWAL": "true",
            "OCA_ADAPTER_ENABLED": "true",
            "OCA_UAT_APPROVED": "true",
            "OCA_FULFILLMENT_ENABLED": "true",
            "OCA_FULFILLMENT_UAT_APPROVED": "true",
        }
    )
    return QAPortal(tmp_path / "qa.db", c, Adapter())


def quote(s):
    return s.quote("QA", dict(weight="1", length="10", width="10", height="10"))


def test_only_test_account(service, tmp_path):
    for values in [
        dict(environment="production"),
        dict(operation=472095),
        dict(cuit="20-12345678-6"),
        dict(confirm_withdrawal=False),
    ]:
        with pytest.raises(QAPortalError):
            QAPortal(tmp_path / "bad.db", replace(service.config, **values), Adapter())


def test_duplicate_click_and_cancel(service):
    ident = quote(service)
    with ThreadPoolExecutor(2) as p:
        list(p.map(lambda _: service.emit(ident, "QA"), range(2)))
    assert service.adapter.emissions == 1
    assert service.get(ident, "QA")["state"] == "ETIQUETA_LISTA"
    service.cancel(ident, "QA")
    service.cancel(ident, "QA")
    assert service.adapter.cancellations == 1
    assert service.get(ident, "QA")["state"] == "CANCELADO"


def test_timeout_is_not_retried(service):
    ident = quote(service)
    service.adapter.fail = True
    service.emit(ident, "QA")
    service.emit(ident, "QA")
    assert service.get(ident, "QA")["state"] == "INCIERTO"
    assert service.adapter.emissions == 1


def test_label_recovery_does_not_reemit(service):
    ident = quote(service)
    service.adapter.pdf_fail = True
    service.emit(ident, "QA")
    assert service.get(ident, "QA")["state"] == "EMITIDO"
    service.adapter.pdf_fail = False
    service.label(ident, "QA")
    assert service.get(ident, "QA")["pdf"].startswith(b"%PDF-")
    assert service.adapter.emissions == 1


def test_owner_and_expiration(service):
    ident = quote(service)
    with pytest.raises(QAPortalError):
        service.emit(ident, "OTHER")
    with service.db() as c:
        c.execute("UPDATE operaciones SET created=0 WHERE id=?", (ident,))
    with pytest.raises(QAPortalError):
        service.emit(ident, "QA")
    assert service.adapter.emissions == 0


def test_nonfinite_weight(service):
    with pytest.raises(QAPortalError):
        service.quote("QA", dict(weight="NaN", length="10", width="10", height="10"))


def test_http_csrf_and_local_only(service):
    spec = importlib.util.spec_from_file_location(
        "qa_launcher", Path(__file__).resolve().parents[1] / "scripts/oca_portal_qa.py"
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    app = m.create_app(service)

    class Client:
        def request(self, method, url, **kwargs):
            async def run():
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                    follow_redirects=True,
                ) as client:
                    return await client.request(method, url, **kwargs)

            return asyncio.run(run())

        def get(self, url, **kw):
            return self.request("GET", url, **kw)

        def post(self, url, **kw):
            return self.request("POST", url, **kw)

    client = Client()
    assert client.get("/portal/nacional/oca").status_code == 200
    assert client.post("/portal/nacional/oca/cotizar", data={}).status_code == 403
    assert (
        client.get("/portal/nacional/oca", headers={"host": "evil.example"}).status_code
        == 403
    )
    data = {
        "weight": "1",
        "length": "10",
        "width": "10",
        "height": "10",
        "csrf": app.state.oca_csrf,
    }
    assert (
        client.post(
            "/portal/nacional/oca/cotizar",
            data=data,
            headers={"origin": "https://evil.example"},
        ).status_code
        == 403
    )
    r = client.post("/portal/nacional/oca/cotizar", data=data)
    assert r.status_code == 200
    assert "COTIZADO" in r.text
