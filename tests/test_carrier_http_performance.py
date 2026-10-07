from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest

from core import carrier_http
from core.dhl_client import DHLClient
from core.fedex_client import FedExClient


class Response:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data or {}

    def json(self):
        return self._data


def fedex(key="key", secret="secret", environment="sandbox"):
    client = FedExClient()
    client.api_key = key
    client.secret_key = secret
    client.account_number = "account"
    client.environment = environment
    client.base_url = (
        client.SANDBOX_URL if environment == "sandbox" else client.PROD_URL
    )
    return client


@pytest.fixture(autouse=True)
def clear_fedex_token_cache():
    FedExClient._clear_token_cache_for_tests()
    yield
    FedExClient._clear_token_cache_for_tests()


def test_oauth_se_reutiliza_entre_instancias_y_aisla_credenciales(monkeypatch):
    calls = []

    def request(_self, _method, url, **_kwargs):
        calls.append(url)
        return Response(data={"access_token": f"token-{len(calls)}", "expires_in": 3600})

    monkeypatch.setattr(FedExClient, "_request_with_retry", request)
    first = fedex()
    second = fedex()
    isolated = fedex(secret="other-secret")
    production = fedex(environment="production")

    assert first._get_token() == second._get_token() == "token-1"
    assert isolated._get_token() == "token-2"
    assert production._get_token() == "token-3"
    assert len(calls) == 3
    assert first._token_cache_key() != isolated._token_cache_key()
    assert first._token_cache_key() != production._token_cache_key()
    assert "secret" not in first._token_cache_key()


def test_oauth_vence_con_margen_de_60_segundos(monkeypatch):
    clock = [1000.0]
    issued = []

    monkeypatch.setattr("core.fedex_client.time.monotonic", lambda: clock[0])

    def request(_self, _method, _url, **_kwargs):
        token = f"token-{len(issued) + 1}"
        issued.append(token)
        return Response(data={"access_token": token, "expires_in": 120})

    monkeypatch.setattr(FedExClient, "_request_with_retry", request)
    client = fedex()
    assert client._get_token() == "token-1"
    clock[0] += 59
    assert client._get_token() == "token-1"
    clock[0] += 2
    assert client._get_token() == "token-2"


def test_oauth_concurrente_hace_una_sola_solicitud(monkeypatch):
    calls = 0
    calls_lock = threading.Lock()

    def request(_self, _method, _url, **_kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.02)
        return Response(data={"access_token": "shared", "expires_in": 3600})

    monkeypatch.setattr(FedExClient, "_request_with_retry", request)
    clients = [fedex() for _ in range(12)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        tokens = list(pool.map(lambda client: client._get_token(), clients))

    assert tokens == ["shared"] * 12
    assert calls == 1


def test_oauth_de_credenciales_distintas_no_comparte_lock_de_red(monkeypatch):
    slow_started = threading.Event()
    release_slow = threading.Event()

    def request(self, _method, _url, **_kwargs):
        if self.api_key == "slow-key":
            slow_started.set()
            release_slow.wait(timeout=2)
        return Response(data={
            "access_token": f"token-{self.api_key}", "expires_in": 3600,
        })

    monkeypatch.setattr(FedExClient, "_request_with_retry", request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(fedex(key="slow-key")._get_token)
        assert slow_started.wait(timeout=1)
        fast = pool.submit(fedex(key="fast-key")._get_token)
        assert fast.result(timeout=1) == "token-fast-key"
        assert not slow.done()
        release_slow.set()
        assert slow.result(timeout=1) == "token-slow-key"


def test_quote_no_espera_el_lock_mas_alla_de_su_presupuesto(monkeypatch):
    class BlockedLock:
        timeout = None

        def acquire(self, *, timeout=None):
            self.timeout = timeout
            return False

        def release(self):
            raise AssertionError("No se libera un lock que no fue adquirido")

    lock = BlockedLock()
    client = fedex()
    monkeypatch.setattr(client, "_token_lock_for_key", lambda _key: lock)
    request = mock.Mock()
    monkeypatch.setattr(client, "_request_with_retry", request)
    budget = carrier_http.QuoteBudget.start(total_seconds=7)

    with pytest.raises(carrier_http.QuoteDeadlineExceeded):
        client._get_token(quote_read=True, quote_budget=budget)

    assert 0 < lock.timeout <= 7
    request.assert_not_called()


@pytest.mark.parametrize("expires_in", [float("inf"), float("-inf"), float("nan")])
def test_oauth_descarta_expiraciones_no_finitas(monkeypatch, expires_in):
    clock = [1000.0]
    monkeypatch.setattr("core.fedex_client.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        FedExClient, "_request_with_retry",
        lambda _self, _method, _url, **_kwargs: Response(data={
            "access_token": "token", "expires_in": expires_in,
        }),
    )

    client = fedex()
    client._get_token()
    _, expires_at = FedExClient._token_cache[client._token_cache_key()]
    assert expires_at == 4600.0


def test_cache_oauth_tiene_limite_lru(monkeypatch):
    monkeypatch.setattr(
        FedExClient,
        "_request_with_retry",
        lambda _self, _method, _url, **_kwargs: Response(
            data={"access_token": "token", "expires_in": 3600}
        ),
    )
    for index in range(FedExClient._TOKEN_CACHE_MAX + 5):
        fedex(key=f"key-{index}")._get_token()
    assert len(FedExClient._token_cache) == FedExClient._TOKEN_CACHE_MAX


def test_rate_fedex_renueva_una_vez_ante_401(monkeypatch):
    auth_calls = 0
    rate_headers = []

    class Session:
        def request(self, _method, url, **kwargs):
            nonlocal auth_calls
            assert kwargs["timeout"] == carrier_http.QUOTE_TIMEOUT
            if url.endswith("/oauth/token"):
                auth_calls += 1
                return Response(data={
                    "access_token": f"token-{auth_calls}", "expires_in": 3600,
                })
            rate_headers.append(kwargs["headers"]["Authorization"])
            return Response(status=401 if len(rate_headers) == 1 else 200)

    session = Session()
    monkeypatch.setattr("core.fedex_client.quote_session", lambda: session)
    response = fedex()._request_with_retry(
        "POST", "https://apis-sandbox.fedex.com/rate/v1/rates/quotes",
        quote_read=True, max_retries=1, json={},
    )

    assert response.status_code == 200
    assert auth_calls == 2
    assert rate_headers == ["Bearer token-1", "Bearer token-2"]


def test_fedex_comparte_presupuesto_total_entre_oauth_401_y_rate(monkeypatch):
    clock = [0.0]
    timeouts = []
    auth_calls = 0
    rate_calls = 0

    monkeypatch.setattr(carrier_http, "_monotonic", lambda: clock[0])

    class Session:
        def request(self, _method, url, **kwargs):
            nonlocal auth_calls, rate_calls
            timeout = kwargs["timeout"]
            timeouts.append(timeout)
            # Simula hasta ocho segundos consumidos por request, siempre
            # respetando el límite connect+read que entrega el presupuesto.
            clock[0] += min(sum(timeout), 8.0)
            if url.endswith("/oauth/token"):
                auth_calls += 1
                return Response(data={
                    "access_token": f"token-{auth_calls}", "expires_in": 3600,
                })
            rate_calls += 1
            if rate_calls == 1:
                return Response(status=401)
            return Response(data={"output": {"rateReplyDetails": [{
                "serviceType": "INTERNATIONAL_PRIORITY",
                "ratedShipmentDetails": [{
                    "rateType": "ACCOUNT", "totalNetCharge": 100,
                    "currency": "USD",
                }],
            }]}})

    monkeypatch.setattr("core.fedex_client.quote_session", lambda: Session())
    result = fedex().get_rates(
        {"country": "AR", "city": "CABA", "postal_code": "1000"},
        {"country": "US", "city": "Miami", "postal_code": "33101"},
        {"peso_kg": 1, "largo": 10, "ancho": 10, "alto": 10},
    )

    assert result["encontrado"] is True
    assert (auth_calls, rate_calls) == (2, 2)
    assert clock[0] <= carrier_http.QUOTE_TOTAL_TIMEOUT
    assert sum(timeouts[-1]) <= 6.0


def test_presupuesto_corta_reintentos_y_backoff_al_llegar_a_30s(monkeypatch):
    clock = [0.0]
    calls = 0

    monkeypatch.setattr(carrier_http, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(carrier_http, "_sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    class Session:
        def request(self, _method, _url, **kwargs):
            nonlocal calls
            calls += 1
            clock[0] += sum(kwargs["timeout"])
            return Response(status=503)

    monkeypatch.setattr("core.fedex_client.quote_session", lambda: Session())
    with pytest.raises(carrier_http.QuoteDeadlineExceeded):
        fedex()._request_with_retry(
            "POST", "https://apis.fedex.com/oauth/token",
            auth_call=True, quote_read=True, max_retries=3,
        )

    assert calls == 2
    assert clock[0] <= carrier_http.QUOTE_TOTAL_TIMEOUT


def test_401_no_se_reintenta_en_operación_cobrable(monkeypatch):
    client = fedex()
    auth_calls = 0
    shipment_headers = []

    def responder(_method, url, **kwargs):
        nonlocal auth_calls
        if url.endswith("/oauth/token"):
            auth_calls += 1
            return Response(data={
                "access_token": f"token-{auth_calls}", "expires_in": 3600,
            })
        shipment_headers.append(kwargs["headers"]["Authorization"])
        return Response(status=401 if len(shipment_headers) == 1 else 200)

    request = mock.Mock(side_effect=responder)
    monkeypatch.setattr("core.fedex_client.requests.request", request)

    first = client._request_with_retry(
        "POST", "https://apis.fedex.com/ship/v1/shipments", max_retries=1,
    )
    assert first.status_code == 401
    assert shipment_headers == ["Bearer token-1"]

    second = client._request_with_retry(
        "POST", "https://apis.fedex.com/ship/v1/shipments", max_retries=1,
    )
    assert second.status_code == 200
    assert auth_calls == 2
    assert shipment_headers == ["Bearer token-1", "Bearer token-2"]


def test_401_no_invalida_token_que_otro_thread_ya_renovo(monkeypatch):
    client = fedex()
    key = client._token_cache_key()
    FedExClient._token_cache[key] = ("token-viejo", time.monotonic() + 3600)

    def responder(_method, _url, **_kwargs):
        with FedExClient._token_cache_lock:
            FedExClient._token_cache[key] = (
                "token-nuevo", time.monotonic() + 3600,
            )
        return Response(status=401)

    request = mock.Mock(side_effect=responder)
    monkeypatch.setattr("core.fedex_client.requests.request", request)

    response = client._request_with_retry(
        "POST", "https://apis.fedex.com/ship/v1/shipments", max_retries=1,
    )

    assert response.status_code == 401
    request.assert_called_once()
    assert client._get_token() == "token-nuevo"


def test_no_duerme_después_del_último_reintento(monkeypatch):
    monkeypatch.setattr(
        "core.fedex_client.requests.request",
        mock.Mock(return_value=Response(status=503)),
    )
    sleep = mock.Mock()
    monkeypatch.setattr("core.fedex_client.time.sleep", sleep)
    with pytest.raises(RuntimeError):
        fedex()._request_with_retry(
            "POST", "https://apis.fedex.com/oauth/token",
            auth_call=True, max_retries=1,
        )
    sleep.assert_not_called()


def test_session_de_tarifas_se_reutiliza_por_thread_y_configura_pool(monkeypatch):
    monkeypatch.setattr(carrier_http, "_thread_local", threading.local())
    same_thread = [carrier_http.quote_session(), carrier_http.quote_session()]
    with ThreadPoolExecutor(max_workers=1) as pool:
        other_thread = pool.submit(carrier_http.quote_session).result()

    assert same_thread[0] is same_thread[1]
    assert other_thread is not same_thread[0]
    adapter = same_thread[0].get_adapter("https://")
    assert adapter._pool_connections == carrier_http.QUOTE_POOL_CONNECTIONS
    assert adapter._pool_maxsize == carrier_http.QUOTE_POOL_MAXSIZE
    assert adapter.max_retries.total == 0


def test_dhl_rate_usa_timeout_separado_de_conexión_y_lectura(monkeypatch):
    session = mock.Mock()
    session.request.return_value = Response()
    monkeypatch.setattr("core.dhl_client.quote_session", lambda: session)

    DHLClient._rate_request("GET", "https://example.invalid/rates")

    session.request.assert_called_once_with(
        "GET", "https://example.invalid/rates", timeout=carrier_http.QUOTE_TIMEOUT,
    )


def test_dhl_recorta_los_tres_http_al_mismo_presupuesto(monkeypatch):
    clock = [0.0]
    seen = []
    monkeypatch.setattr(carrier_http, "_monotonic", lambda: clock[0])
    budget = carrier_http.QuoteBudget.start()

    class Session:
        def request(self, _method, _url, **kwargs):
            seen.append(kwargs["timeout"])
            clock[0] += 10.0
            return Response()

    monkeypatch.setattr("core.dhl_client.quote_session", lambda: Session())
    # Representa POST /rates, GET de feriado y segundo POST /rates.
    for _ in range(3):
        DHLClient._rate_request(
            "POST", "https://example.invalid/rates", quote_budget=budget,
        )

    assert seen[:2] == [carrier_http.QUOTE_TIMEOUT] * 2
    assert sum(seen[2]) == pytest.approx(10.0)
    assert clock[0] == carrier_http.QUOTE_TOTAL_TIMEOUT
