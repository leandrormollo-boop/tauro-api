import asyncio
import inspect
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from servicios import tiendanube_labels
from servicios.tiendanube_labels import (
    LabelsAuthenticationError,
    LabelsBlockedError,
    LabelsConflictError,
    LabelsContractError,
    LabelsUnavailableError,
    PersistResult,
    recibir_cancel,
    recibir_generate,
)


TOKEN = "label-callback-super-secreto-123456"
SNAPSHOT_ID = "tnq_" + "a" * 64
REFERENCE = f"tauro:oca:{SNAPSHOT_ID}"


@pytest.fixture(autouse=True)
def _sin_postgres_para_lock_de_registro(monkeypatch):
    """Los tests de contrato del carrier no necesitan una DB real."""
    from servicios import tiendanube_shipping

    monkeypatch.setattr(
        tiendanube_shipping,
        "_shipping_registration_lock",
        lambda _store_id: nullcontext(),
    )


def _config(_token):
    return {"store_id": "123456", "activa": True}


def _generate_payload(*, address="Calle 1"):
    return [
        {
            "id": "label-1",
            "fulfillment_order_info": {
                "id": "ffo-1",
                "total_quantity": 1,
                "total_weight": "1.5",
                "total_price": {"value": "10000", "currency": "ARS"},
                "assigned_location": {
                    "location_id": "loc-1",
                    "name": "Depósito TAURO",
                    "address": {
                        "zipcode": "1425",
                        "street": "Origen 1",
                        "country": {"code": "AR", "name": "Argentina"},
                    },
                },
                "line_items": [
                    {
                        "quantity": 1,
                        "unit_dimension": {
                            "weight": "1.5",
                            "depth": "20",
                            "width": "15",
                            "height": "10",
                        },
                    }
                ],
                "recipient": {"name": "Comprador", "address": address},
                "shipping": {
                    "type": "ship",
                    "carrier": {"carrier_id": "77", "code": "api"},
                    "option": {
                        "name": "TAURO nacional",
                        "code": "tauro_nacional_domicilio",
                        "reference": REFERENCE,
                    },
                    "merchant_cost": {"value": "7000", "currency": "ARS"},
                    "consumer_cost": {"value": "7000", "currency": "ARS"},
                },
                "destination": {
                    "zipcode": "2000",
                    "street": address,
                    "country": {"code": "AR", "name": "Argentina"},
                },
            },
        }
    ]


def _ready_kwargs():
    return {
        "execution_ready": lambda: True,
        "installation_loader": lambda _store: {"cliente_id": "CLIENTE-1"},
        "order_context_loader": lambda _store, _ffo: {
            "order_id": "order-1",
            "redacted": False,
        },
        "quote_resolver": lambda store, customer, reference: {
            "snapshot_id": SNAPSHOT_ID,
            "store_id": store,
            "customer_id": customer,
            "carrier_id": "oca",
            "price_currency": "ARS",
            "tauro_price": "7000",
            "buyer_price": "7000",
            "expected_consumer_price": "7000",
            "declared_value": "10000",
            "declared_currency": "ARS",
            "origin_route": {
                "country": "AR",
                "postal_code": "1425",
                "location_id": "loc-1",
            },
            "destination_route": {"country": "AR", "postal_code": "2000"},
            "packages": [
                {
                    "quantity": 1,
                    "weight_kg": "1.5",
                    "length_cm": "20",
                    "width_cm": "15",
                    "height_cm": "10",
                }
            ],
            "pricing_mode": "pagado",
            "reference": reference,
        },
    }


def _cancel_payload():
    return {
        "labels": [
            {"fulfillment_order_id": "ffo-1", "label_id": "label-1"}
        ]
    }


class MemoryRepository:
    def __init__(self):
        self.operations = {}

    def persist(self, operations, *, state):
        created = 0
        replayed = 0
        for operation in operations:
            key = (operation.store_id, operation.label_id, operation.operation)
            existing = self.operations.get(key)
            if existing and existing.fingerprint != operation.fingerprint:
                raise LabelsConflictError("payload distinto")
            if existing:
                replayed += 1
            else:
                self.operations[key] = operation
                created += 1
        self.last_state = state
        return PersistResult(created=created, replayed=replayed, state=state)


def test_generate_persiste_antes_de_fallar_cerrado_y_reintento_es_idempotente():
    repository = MemoryRepository()

    with pytest.raises(LabelsBlockedError):
        recibir_generate(
            _generate_payload(),
            TOKEN,
            repository=repository,
            config_loader=_config,
        )

    assert repository.last_state == "BLOQUEADA_SIN_ADAPTER"
    assert len(repository.operations) == 1
    blocked = next(iter(repository.operations.values()))
    assert blocked.payload_complete is False
    assert blocked.payload == {
        "id": "label-1",
        "fulfillment_order_info": {"id": "ffo-1"},
    }
    assert "Comprador" not in json.dumps(blocked.payload)

    with pytest.raises(LabelsBlockedError):
        recibir_generate(
            _generate_payload(),
            TOKEN,
            repository=repository,
            config_loader=_config,
        )
    assert len(repository.operations) == 1


def test_generate_solo_acepta_despues_de_persistir_si_worker_fuera_habilitado():
    repository = MemoryRepository()

    result = recibir_generate(
        _generate_payload(),
        TOKEN,
        repository=repository,
        config_loader=_config,
        **_ready_kwargs(),
    )

    assert result == PersistResult(created=1, replayed=0, state="PENDIENTE")
    assert len(repository.operations) == 1
    operation = next(iter(repository.operations.values()))
    assert operation.rate_quote_snapshot_id == SNAPSHOT_ID
    assert operation.order_id == "order-1"


def test_generate_listo_rechaza_referencia_ausente_antes_de_persistir():
    repository = MemoryRepository()
    payload = _generate_payload()
    payload[0]["fulfillment_order_info"]["shipping"]["option"]["reference"] = None

    with pytest.raises(LabelsContractError, match="cotización aceptada"):
        recibir_generate(
            payload,
            TOKEN,
            repository=repository,
            config_loader=_config,
            **_ready_kwargs(),
        )

    assert repository.operations == {}


def test_generate_listo_rechaza_precio_o_destino_distinto_del_snapshot():
    repository = MemoryRepository()
    payload = _generate_payload()
    payload[0]["fulfillment_order_info"]["shipping"]["consumer_cost"]["value"] = "1"

    with pytest.raises(LabelsContractError, match="importes"):
        recibir_generate(
            payload,
            TOKEN,
            repository=repository,
            config_loader=_config,
            **_ready_kwargs(),
        )

    assert repository.operations == {}


def test_generate_no_guarda_pii_si_la_orden_fue_redactada():
    repository = MemoryRepository()
    kwargs = _ready_kwargs()
    kwargs["order_context_loader"] = lambda *_: {
        "order_id": "order-1",
        "redacted": True,
    }

    with pytest.raises(LabelsContractError, match="privacidad"):
        recibir_generate(
            _generate_payload(),
            TOKEN,
            repository=repository,
            config_loader=_config,
            **kwargs,
        )

    assert repository.operations == {}


def test_generate_no_guarda_pii_sin_vinculo_ffo_orden():
    repository = MemoryRepository()
    kwargs = _ready_kwargs()
    kwargs["order_context_loader"] = lambda *_: None

    with pytest.raises(LabelsUnavailableError, match="todavía no está vinculado"):
        recibir_generate(
            _generate_payload(),
            TOKEN,
            repository=repository,
            config_loader=_config,
            **kwargs,
        )

    assert repository.operations == {}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda fulfillment: fulfillment["assigned_location"]["address"].update(
                {"zipcode": "5000"}
            ),
            "origen",
        ),
        (
            lambda fulfillment: fulfillment.update({"total_weight": "2.5"}),
            "bultos",
        ),
        (
            lambda fulfillment: fulfillment["line_items"][0]["unit_dimension"].update(
                {"width": "99"}
            ),
            "peso o las medidas",
        ),
    ],
)
def test_generate_rechaza_geometria_u_origen_distinto(mutate, message):
    repository = MemoryRepository()
    payload = _generate_payload()
    mutate(payload[0]["fulfillment_order_info"])

    with pytest.raises(LabelsContractError, match=message):
        recibir_generate(
            payload,
            TOKEN,
            repository=repository,
            config_loader=_config,
            **_ready_kwargs(),
        )

    assert repository.operations == {}


def test_repositorio_serializa_privacidad_y_audita_snapshot_por_ffo():
    source = inspect.getsource(tiendanube_labels.PostgresLabelRepository.persist)
    normalized = " ".join(source.split())

    assert "pg_advisory_xact_lock" in normalized
    assert "tiendanube_pedidos_redactados" in normalized
    assert "tiendanube_rate_quote_claims" in normalized
    assert (
        "ON CONFLICT ( store_id, snapshot_id, fulfillment_order_id ) DO NOTHING"
        in normalized
    )
    assert (
        "AND snapshot_id = %s AND fulfillment_order_id = %s FOR UPDATE"
        in normalized
    )
    assert "La cotización ya fue usada por otra orden" not in source


def test_mismo_snapshot_respalda_varios_ffo_y_reintenta_por_label(monkeypatch):
    class FakeCursor:
        def __init__(self):
            self.claims = set()
            self.labels = {}
            self.outbox = {}
            self.result = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            sql = " ".join(query.split())
            params = params or ()
            self.result = None
            if "pg_advisory_xact_lock" in sql:
                return
            if "FROM tiendanube_fulfillment_order_orders m" in sql:
                self.result = {
                    "order_id": f"order-{params[2]}",
                    "redacted": False,
                }
                return
            if "INSERT INTO tiendanube_rate_quote_claims" in sql:
                self.claims.add(tuple(params))
                return
            if "SELECT 1 AS claim_exists" in sql:
                self.result = {"claim_exists": 1} if tuple(params) in self.claims else None
                return
            if "INSERT INTO tiendanube_labels" in sql:
                key = (params[0], params[1])
                if key not in self.labels:
                    self.labels[key] = {
                        "fulfillment_order_id": params[2],
                        "rate_quote_snapshot_id": params[3],
                        "order_id": params[4],
                        "generate_fingerprint": params[6],
                        "generate_payload_complete": params[7],
                    }
                    self.result = {"store_id": params[0]}
                return
            if "SELECT fulfillment_order_id, rate_quote_snapshot_id" in sql:
                self.result = self.labels.get(tuple(params))
                return
            if "INSERT INTO tiendanube_label_outbox" in sql:
                key = (params[0], params[1], params[2])
                if key not in self.outbox:
                    self.outbox[key] = {
                        "payload_fingerprint": params[4],
                        "payload_complete": params[5],
                    }
                    self.result = {"id": len(self.outbox)}
                return
            if "SELECT payload_fingerprint, payload_complete" in sql:
                self.result = self.outbox.get(tuple(params))
                return
            raise AssertionError(f"SQL inesperado: {sql}")

        def fetchone(self):
            return self.result

    class FakeConnection:
        def __init__(self):
            self.cursor_instance = FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return self.cursor_instance

        def commit(self):
            pass

        def rollback(self):
            pass

    connection = FakeConnection()
    monkeypatch.setattr(tiendanube_labels, "_ensure_tables", lambda: None)
    monkeypatch.setattr(tiendanube_labels, "get_conn", lambda: connection)
    operations = tuple(
        tiendanube_labels.LabelOperation(
            store_id="store-1",
            label_id=f"label-{ffo}",
            fulfillment_order_id=ffo,
            operation="GENERATE",
            payload={"fulfillment_order_info": {"id": ffo}},
            fingerprint=ffo.ljust(64, "0"),
            rate_quote_snapshot_id=SNAPSHOT_ID,
            order_id=f"order-{ffo}",
        )
        for ffo in ("ffo-1", "ffo-2")
    )
    repository = tiendanube_labels.PostgresLabelRepository()

    assert repository.persist(operations, state="PENDIENTE") == PersistResult(
        created=2,
        replayed=0,
        state="PENDIENTE",
    )
    assert connection.cursor_instance.claims == {
        ("store-1", SNAPSHOT_ID, "ffo-1"),
        ("store-1", SNAPSHOT_ID, "ffo-2"),
    }
    assert repository.persist(operations, state="PENDIENTE") == PersistResult(
        created=0,
        replayed=2,
        state="PENDIENTE",
    )


def test_claims_migran_pk_legacy_a_auditoria_por_ffo():
    runtime_source = inspect.getsource(tiendanube_labels._ensure_tables)
    schema_source = (
        Path(tiendanube_labels.__file__).parents[1] / "sql" / "schema.sql"
    ).read_text(encoding="utf-8")

    runtime_normalized = " ".join(runtime_source.split())
    assert "to_regclass" in runtime_normalized
    assert "schema_ready" in runtime_normalized
    assert "LOCK TABLE" not in runtime_normalized
    assert "CREATE TABLE" not in runtime_normalized
    assert "ALTER TABLE" not in runtime_normalized

    schema_normalized = " ".join(schema_source.split())
    assert "PRIMARY KEY" in schema_normalized
    assert "store_id, snapshot_id, fulfillment_order_id" in schema_normalized
    assert "ARRAY['store_id', 'snapshot_id']::name[]" in schema_normalized
    assert "'store_id', 'snapshot_id', 'fulfillment_order_id'" in schema_normalized
    assert "LOCK TABLE tiendanube_rate_quote_claims" in schema_normalized


def test_readiness_de_labels_exige_flags_secreto_y_oca(monkeypatch):
    from servicios import oca_adapter

    monkeypatch.setenv("TIENDANUBE_LABELS_WORKER_ENABLED", "true")
    monkeypatch.setenv("TIENDANUBE_SHIPPING_ENABLED", "true")
    monkeypatch.setenv("TAURO_NACIONAL_RATES_READY", "true")
    monkeypatch.setenv("TIENDANUBE_LABEL_DOWNLOAD_SECRET", "x" * 32)
    monkeypatch.setenv("OCA_ENVIRONMENT", "qa")
    monkeypatch.setattr(
        oca_adapter,
        "registration_status",
        lambda: {"fulfillment_ready": True},
    )

    assert tiendanube_labels.labels_execution_ready() is True
    monkeypatch.setenv("TIENDANUBE_LABEL_DOWNLOAD_SECRET", "corto")
    assert tiendanube_labels.labels_execution_ready() is False


def test_readiness_productiva_exige_homologacion_tiendanube(monkeypatch):
    from servicios import oca_adapter

    for name in (
        "TIENDANUBE_LABELS_WORKER_ENABLED",
        "TIENDANUBE_SHIPPING_ENABLED",
        "TAURO_NACIONAL_RATES_READY",
    ):
        monkeypatch.setenv(name, "true")
    monkeypatch.setenv("TIENDANUBE_LABEL_DOWNLOAD_SECRET", "x" * 32)
    monkeypatch.setenv("OCA_ENVIRONMENT", "production")
    monkeypatch.setenv("TIENDANUBE_HOMOLOGATION_APPROVED", "false")
    monkeypatch.setattr(
        oca_adapter,
        "registration_status",
        lambda: {"fulfillment_ready": True},
    )

    assert tiendanube_labels.labels_execution_ready() is False
    monkeypatch.setenv("TIENDANUBE_HOMOLOGATION_APPROVED", "true")
    assert tiendanube_labels.labels_execution_ready() is True


def test_mismo_store_y_label_con_payload_distinto_es_conflicto():
    repository = MemoryRepository()
    recibir_generate(
        _generate_payload(),
        TOKEN,
        repository=repository,
        config_loader=_config,
        **_ready_kwargs(),
    )

    with pytest.raises(LabelsConflictError):
        recibir_generate(
            _generate_payload(address="Otra calle"),
            TOKEN,
            repository=repository,
            config_loader=_config,
            **_ready_kwargs(),
        )


def test_cancel_no_toca_carrier_mientras_worker_esta_bloqueado():
    calls = []

    with pytest.raises(LabelsBlockedError):
        recibir_cancel(
            _cancel_payload(),
            TOKEN,
            config_loader=_config,
            cancel_service=lambda *_args, **_kwargs: calls.append(True),
        )

    assert calls == []


def test_cancel_listo_delega_lote_autenticado_al_servicio_confirmado():
    calls = []
    expected = SimpleNamespace(http_status=204, response_body=None)

    result = recibir_cancel(
        _cancel_payload(),
        TOKEN,
        config_loader=_config,
        execution_ready=lambda: True,
        cancel_service=lambda store, items: calls.append((store, items)) or expected,
    )

    assert result is expected
    assert calls == [(
        "123456",
        [{"label_id": "label-1", "fulfillment_order_id": "ffo-1"}],
    )]


def test_token_invalido_no_persiste():
    repository = MemoryRepository()

    with pytest.raises(LabelsAuthenticationError):
        recibir_generate(
            _generate_payload(),
            TOKEN,
            repository=repository,
            config_loader=lambda _token: None,
        )
    assert repository.operations == {}


def test_outbox_tiene_clave_idempotente_y_fk_de_redaccion():
    source = (
        Path(tiendanube_labels.__file__).parents[1] / "sql" / "schema.sql"
    ).read_text(encoding="utf-8")
    normalized = " ".join(source.split())

    assert "UNIQUE (store_id, label_id, operacion)" in normalized
    assert "PRIMARY KEY (store_id, label_id)" in normalized
    assert "ON DELETE CASCADE" in normalized


class _Request:
    def __init__(self, payload, *, content_length=None, raw_body=None):
        self._body = (
            bytes(raw_body)
            if raw_body is not None
            else json.dumps(payload).encode("utf-8")
        )
        self.headers = {
            "content-length": str(
                len(self._body) if content_length is None else content_length
            )
        }

    async def body(self):
        return self._body

    async def stream(self):
        yield self._body


def test_endpoint_generate_devuelve_202_solo_si_servicio_acepta(monkeypatch):
    from endpoints import tiendanube_shipping as endpoint

    called = []
    monkeypatch.setattr(
        endpoint,
        "recibir_generate",
        lambda payload, token: called.append((payload, token)),
    )

    response = asyncio.run(endpoint.generate_labels(TOKEN, _Request(_generate_payload())))

    assert response.status_code == 202
    assert called and called[0][1] == TOKEN


def test_endpoint_generate_bloqueado_devuelve_503(monkeypatch):
    from endpoints import tiendanube_shipping as endpoint

    monkeypatch.setattr(
        endpoint,
        "recibir_generate",
        lambda *_: (_ for _ in ()).throw(LabelsBlockedError("bloqueado")),
    )

    response = asyncio.run(endpoint.generate_labels(TOKEN, _Request(_generate_payload())))
    assert response.status_code == 503


def test_endpoint_rechaza_content_length_excesivo_con_413(monkeypatch):
    from endpoints import tiendanube_shipping as endpoint

    monkeypatch.setattr(
        endpoint,
        "recibir_generate",
        lambda *_: pytest.fail("No debe procesar un body anunciado como excesivo"),
    )

    response = asyncio.run(
        endpoint.generate_labels(
            TOKEN,
            _Request(
                _generate_payload(),
                content_length=endpoint._MAX_LABEL_CALLBACK_BYTES + 1,
            ),
        )
    )
    assert response.status_code == 413


def test_endpoint_corta_stream_excesivo_aunque_header_mienta(monkeypatch):
    from endpoints import tiendanube_shipping as endpoint

    monkeypatch.setattr(
        endpoint,
        "recibir_generate",
        lambda *_: pytest.fail("No debe procesar un body realmente excesivo"),
    )
    response = asyncio.run(
        endpoint.generate_labels(
            TOKEN,
            _Request(
                None,
                content_length=1,
                raw_body=b"x" * (endpoint._MAX_LABEL_CALLBACK_BYTES + 1),
            ),
        )
    )
    assert response.status_code == 413


def test_endpoint_cancel_solo_devuelve_204_con_confirmacion_total(monkeypatch):
    from endpoints import tiendanube_shipping as endpoint

    monkeypatch.setattr(
        endpoint,
        "recibir_cancel",
        lambda *_: SimpleNamespace(http_status=204, response_body=None),
    )

    response = asyncio.run(endpoint.cancel_labels(TOKEN, _Request(_cancel_payload())))
    assert response.status_code == 204


@pytest.mark.parametrize("status", [207, 409])
def test_endpoint_cancel_con_resultados_devuelve_contrato_tiendanube(
    monkeypatch, status
):
    from endpoints import tiendanube_shipping as endpoint

    body = {"labels": [{"label_id": "label-1", "status": "FAILED"}]}
    monkeypatch.setattr(
        endpoint,
        "recibir_cancel",
        lambda *_: SimpleNamespace(http_status=status, response_body=body),
    )

    response = asyncio.run(endpoint.cancel_labels(TOKEN, _Request(_cancel_payload())))
    assert response.status_code == status
    assert json.loads(response.body) == body


def test_endpoint_cancel_falla_cerrado_sin_resultado_interpretable(monkeypatch):
    from endpoints import tiendanube_shipping as endpoint

    monkeypatch.setattr(endpoint, "recibir_cancel", lambda *_: None)

    response = asyncio.run(endpoint.cancel_labels(TOKEN, _Request(_cancel_payload())))
    assert response.status_code == 503


def test_endpoint_cancel_corta_antes_del_sla_aunque_el_servicio_no_responda(monkeypatch):
    from endpoints import tiendanube_shipping as endpoint

    monkeypatch.setattr(endpoint, "_LABEL_CANCEL_ENDPOINT_TIMEOUT_SECONDS", 0.01)

    async def servicio_bloqueado(*_args, **_kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(endpoint.asyncio, "to_thread", servicio_bloqueado)
    response = asyncio.run(endpoint.cancel_labels(TOKEN, _Request(_cancel_payload())))

    assert response.status_code == 503
    assert json.loads(response.body) == {"error": "operacion_no_disponible"}


class _Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = {} if payload is None else payload

    def json(self):
        return self._payload


def test_lock_registro_shipping_usa_misma_sesion_y_libera_en_finally(monkeypatch):
    from servicios import tiendanube_shipping

    ejecutadas = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            ejecutadas.append((" ".join(str(sql).split()), params))

    class Connection:
        def __enter__(self):
            ejecutadas.append(("CONNECTION_ENTER", None))
            return self

        def __exit__(self, *_args):
            ejecutadas.append(("CONNECTION_EXIT", None))
            return False

        def cursor(self):
            return Cursor()

    conexiones = []

    def connection_factory():
        connection = Connection()
        conexiones.append(connection)
        return connection

    monkeypatch.setattr(tiendanube_shipping, "get_conn", connection_factory)

    with pytest.raises(RuntimeError, match="fallo remoto"):
        with tiendanube_shipping._postgres_shipping_registration_lock("123"):
            ejecutadas.append(("BODY", None))
            raise RuntimeError("fallo remoto")

    assert len(conexiones) == 1
    sql = [statement for statement, _params in ejecutadas]
    assert "pg_advisory_lock(hashtextextended(%s, 0))" in sql[1]
    assert sql[2] == "BODY"
    assert "pg_advisory_unlock(hashtextextended(%s, 0))" in sql[3]
    assert ejecutadas[1][1] == ejecutadas[3][1] == (
        "tauro:tiendanube:shipping-carrier:123",
    )
    assert sql[-1] == "CONNECTION_EXIT"


def test_registro_shipping_envuelve_toda_la_operacion_con_lock(monkeypatch):
    from contextlib import contextmanager
    from servicios import tiendanube_shipping

    orden = []

    @contextmanager
    def fake_lock(store_id):
        orden.append(("lock", store_id))
        try:
            yield
        finally:
            orden.append(("unlock", store_id))

    def fake_registration(store_id, access_token):
        orden.append(("body", store_id, access_token))
        return {"ready": True}

    monkeypatch.setattr(tiendanube_shipping, "_shipping_registration_lock", fake_lock)
    monkeypatch.setattr(
        tiendanube_shipping,
        "_registrar_shipping_carrier_locked",
        fake_registration,
    )

    assert tiendanube_shipping.registrar_shipping_carrier("123", "access") == {
        "ready": True,
    }
    assert orden == [
        ("lock", "123"),
        ("body", "123", "access"),
        ("unlock", "123"),
    ]


def test_registro_nuevo_incluye_callback_labels_con_secreto_distinto(monkeypatch):
    from servicios import tiendanube_app, tiendanube_labels, tiendanube_shipping

    monkeypatch.setenv("TIENDANUBE_SHIPPING_ENABLED", "true")
    monkeypatch.setenv("TAURO_NACIONAL_RATES_READY", "true")
    monkeypatch.setenv("BASE_URL", "https://api.tauro.test")
    monkeypatch.setattr(tiendanube_labels, "labels_execution_ready", lambda: True)
    monkeypatch.setattr(tiendanube_app, "label_api_habilitada", lambda _store: True)
    monkeypatch.setattr(tiendanube_shipping, "configuracion", lambda _store: None)
    tokens = iter(("rate-token", "labels-token"))
    monkeypatch.setattr(
        tiendanube_shipping.secrets, "token_urlsafe", lambda _size: next(tokens)
    )
    calls = []

    def fake_api(store_id, access_token, method, path, payload=None):
        calls.append((store_id, method, path, payload))
        if method == "GET" and path == "shipping_carriers":
            return _Response(200, [])
        if method == "POST" and path == "shipping_carriers":
            return _Response(201, {"id": 77})
        return _Response(201, {"id": 88})

    monkeypatch.setattr(tiendanube_app, "_api", fake_api)
    saved = []
    monkeypatch.setattr(
        tiendanube_shipping,
        "_guardar_config",
        lambda *args, **kwargs: saved.append((args, kwargs)),
    )

    result = tiendanube_shipping.registrar_shipping_carrier("123", "access")

    carrier_payload = next(
        call[3]
        for call in calls
        if call[1:3] == ("POST", "shipping_carriers")
    )
    assert carrier_payload["callback_url"].endswith("/rates/rate-token")
    assert carrier_payload["callback_labels_url"].endswith(
        "/labels/labels-token"
    )
    assert saved[0][1]["label_token"] == "labels-token"
    assert result["labels_callback_registered"] is True


def test_registro_existente_agrega_labels_sin_rotar_callback_rates(monkeypatch):
    from servicios import tiendanube_app, tiendanube_labels, tiendanube_shipping

    monkeypatch.setenv("TIENDANUBE_SHIPPING_ENABLED", "true")
    monkeypatch.setenv("TAURO_NACIONAL_RATES_READY", "true")
    monkeypatch.setenv("BASE_URL", "https://api.tauro.test")
    monkeypatch.setattr(tiendanube_labels, "labels_execution_ready", lambda: True)
    monkeypatch.setattr(tiendanube_app, "label_api_habilitada", lambda _store: True)
    rate_token = "rate-token-existente-12345678901234567890"
    monkeypatch.setattr(
        tiendanube_shipping,
        "configuracion",
        lambda _store: {
            "store_id": "123",
            "activa": True,
            "callback_token_hash": tiendanube_shipping.hash_callback_token(
                rate_token
            ),
            "label_callback_token_hash": None,
            "carrier_id": "77",
            "carrier_option_id": "88",
        },
    )
    monkeypatch.setattr(
        tiendanube_shipping.secrets,
        "token_urlsafe",
        lambda _size: "labels-token",
    )
    calls = []
    def fake_api(*args):
        calls.append(args)
        if args[2] == "GET" and args[3].endswith("/options"):
            return _Response(200, [{
                "id": 88,
                "code": "tauro_nacional_domicilio",
                "active": True,
            }])
        if args[2] == "GET":
            return _Response(200, {
                "id": 77,
                "active": True,
                "callback_url": (
                    "https://api.tauro.test/integraciones/tiendanube/"
                    f"shipping/rates/{rate_token}"
                ),
            })
        return _Response(200)

    monkeypatch.setattr(tiendanube_app, "_api", fake_api)
    saved = []
    monkeypatch.setattr(
        tiendanube_shipping,
        "_guardar_label_callback_token",
        lambda store, token: saved.append((store, token)),
    )
    monkeypatch.setattr(tiendanube_shipping, "reactivar", lambda _store: None)

    result = tiendanube_shipping.registrar_shipping_carrier("123", "access")

    update = next(call for call in calls if call[2] == "PUT")
    assert update[3] == "shipping_carriers/77"
    assert set(update[4]) == {"callback_labels_url"}
    assert "/labels/labels-token" in update[4]["callback_labels_url"]
    assert saved == [("123", "labels-token")]
    assert result["existing"] is True


def test_registro_omite_labels_mientras_worker_esta_bloqueado(monkeypatch):
    from servicios import tiendanube_app, tiendanube_labels, tiendanube_shipping

    monkeypatch.setenv("TIENDANUBE_SHIPPING_ENABLED", "true")
    monkeypatch.setenv("TAURO_NACIONAL_RATES_READY", "true")
    monkeypatch.setenv("BASE_URL", "https://api.tauro.test")
    monkeypatch.setattr(tiendanube_labels, "labels_execution_ready", lambda: False)
    monkeypatch.setattr(tiendanube_shipping, "configuracion", lambda _store: None)
    monkeypatch.setattr(
        tiendanube_shipping.secrets,
        "token_urlsafe",
        lambda _size: "rate-token",
    )
    calls = []

    def fake_api(store_id, access_token, method, path, payload=None):
        calls.append((store_id, method, path, payload))
        if method == "GET":
            return _Response(200, [])
        if path == "shipping_carriers":
            return _Response(201, {"id": 77})
        return _Response(201, {"id": 88})

    monkeypatch.setattr(tiendanube_app, "_api", fake_api)
    saved = []
    monkeypatch.setattr(
        tiendanube_shipping,
        "_guardar_config",
        lambda *args, **kwargs: saved.append((args, kwargs)),
    )

    result = tiendanube_shipping.registrar_shipping_carrier("123", "access")

    carrier_payload = next(
        call[3]
        for call in calls
        if call[1:3] == ("POST", "shipping_carriers")
    )
    assert "callback_labels_url" not in carrier_payload
    assert saved[0][1]["label_token"] is None
    assert result["labels_callback_registered"] is False


def test_registro_existente_elimina_callback_labels_si_worker_no_esta_listo(
    monkeypatch,
):
    from servicios import tiendanube_app, tiendanube_labels, tiendanube_shipping

    monkeypatch.setenv("TIENDANUBE_SHIPPING_ENABLED", "true")
    monkeypatch.setenv("TAURO_NACIONAL_RATES_READY", "true")
    monkeypatch.setenv("BASE_URL", "https://api.tauro.test")
    monkeypatch.setattr(tiendanube_labels, "labels_execution_ready", lambda: False)
    rate_token = "rate-token-existente-12345678901234567890"
    monkeypatch.setattr(
        tiendanube_shipping,
        "configuracion",
        lambda _store: {
            "store_id": "123",
            "activa": True,
            "callback_token_hash": tiendanube_shipping.hash_callback_token(
                rate_token
            ),
            "label_callback_token_hash": "hash-labels",
            "carrier_id": "77",
            "carrier_option_id": "88",
        },
    )
    calls = []

    def fake_api(*args):
        calls.append(args)
        if args[2] == "GET" and args[3].endswith("/options"):
            return _Response(200, [{
                "id": 88,
                "code": "tauro_nacional_domicilio",
                "active": True,
            }])
        if args[2] == "GET":
            return _Response(
                200,
                {
                    "id": 77,
                    "active": True,
                    "callback_url": (
                        "https://api.tauro.test/integraciones/tiendanube/"
                        f"shipping/rates/{rate_token}"
                    ),
                    "callback_labels_url": (
                        "https://api.tauro.test/integraciones/tiendanube/"
                        "shipping/labels/labels-token-remoto"
                    ),
                },
            )
        return _Response(200)

    monkeypatch.setattr(tiendanube_app, "_api", fake_api)
    monkeypatch.setattr(tiendanube_shipping, "reactivar", lambda _store: None)
    cleaned = []
    monkeypatch.setattr(
        tiendanube_shipping,
        "_limpiar_label_callback_token",
        lambda store: cleaned.append(store),
    )

    result = tiendanube_shipping.registrar_shipping_carrier("123", "access")

    update = next(call for call in calls if call[2] == "PUT")
    assert update[4] == {"callback_labels_url": None}
    assert cleaned == ["123"]
    assert result["labels_callback_registered"] is False


def test_reinstalacion_reactiva_carrier_inactivo_sin_crear_otro(monkeypatch):
    from servicios import tiendanube_app, tiendanube_labels, tiendanube_shipping

    monkeypatch.setenv("TIENDANUBE_SHIPPING_ENABLED", "true")
    monkeypatch.setenv("TAURO_NACIONAL_RATES_READY", "true")
    monkeypatch.setenv("BASE_URL", "https://api.tauro.test")
    monkeypatch.setattr(tiendanube_labels, "labels_execution_ready", lambda: False)
    rate_token = "rate-token-existente-12345678901234567890"
    monkeypatch.setattr(
        tiendanube_shipping,
        "configuracion",
        lambda _store: {
            "activa": False,
            "callback_token_hash": tiendanube_shipping.hash_callback_token(
                rate_token
            ),
            "carrier_id": "77",
            "carrier_option_id": "88",
            "label_callback_token_hash": None,
        },
    )
    calls = []

    def fake_api(*args):
        calls.append(args)
        if args[2] == "GET" and args[3].endswith("/options"):
            return _Response(200, [{
                "id": 88,
                "code": "tauro_nacional_domicilio",
                "active": True,
            }])
        if args[2] == "GET":
            return _Response(200, {
                "id": 77,
                "active": False,
                "callback_url": (
                    "https://api.tauro.test/integraciones/tiendanube/"
                    f"shipping/rates/{rate_token}"
                ),
            })
        return _Response(200)

    monkeypatch.setattr(tiendanube_app, "_api", fake_api)
    activated = []
    monkeypatch.setattr(
        tiendanube_shipping, "reactivar", lambda store: activated.append(store)
    )

    result = tiendanube_shipping.registrar_shipping_carrier("123", "access")

    assert not [call for call in calls if call[2] == "POST"]
    update = next(call for call in calls if call[2] == "PUT")
    assert update[4] == {"active": True}
    assert activated == ["123"]
    assert result["existing"] is True


def test_reconcilia_carrier_remoto_tras_fallo_db_sin_duplicar(monkeypatch):
    from servicios import tiendanube_app, tiendanube_labels, tiendanube_shipping

    monkeypatch.setenv("TIENDANUBE_SHIPPING_ENABLED", "true")
    monkeypatch.setenv("TAURO_NACIONAL_RATES_READY", "true")
    monkeypatch.setenv("BASE_URL", "https://api.tauro.test")
    monkeypatch.setattr(tiendanube_labels, "labels_execution_ready", lambda: False)
    monkeypatch.setattr(tiendanube_shipping, "configuracion", lambda _store: None)
    rate_token = "rate-token-remoto-12345678901234567890"
    calls = []

    def fake_api(*args):
        calls.append(args)
        method, path = args[2], args[3]
        if method == "GET" and path == "shipping_carriers":
            return _Response(200, [{
                "id": 77,
                "name": "TAURO Solutions Ar",
                "active": True,
                "callback_url": (
                    "https://api.tauro.test/integraciones/tiendanube/"
                    f"shipping/rates/{rate_token}"
                ),
            }])
        if method == "GET" and path.endswith("/options"):
            return _Response(200, [{
                "id": 88,
                "code": "tauro_nacional_domicilio",
            }])
        if method == "GET":
            return _Response(200, {"id": 77, "active": True})
        return _Response(200)

    monkeypatch.setattr(tiendanube_app, "_api", fake_api)
    saved = []
    monkeypatch.setattr(
        tiendanube_shipping,
        "_guardar_config",
        lambda *args, **kwargs: saved.append((args, kwargs)),
    )
    monkeypatch.setattr(tiendanube_shipping, "reactivar", lambda _store: None)

    result = tiendanube_shipping.registrar_shipping_carrier("123", "access")

    assert not [call for call in calls if call[2] == "POST"]
    assert saved[0][0][1] == rate_token
    assert saved[0][0][2:] == ("77", "88")
    assert result["existing"] is True


def test_config_local_repara_callback_y_opcion_remota_sin_duplicar_carrier(
    monkeypatch,
):
    from servicios import tiendanube_app, tiendanube_labels, tiendanube_shipping

    monkeypatch.setenv("TIENDANUBE_SHIPPING_ENABLED", "true")
    monkeypatch.setenv("TAURO_NACIONAL_RATES_READY", "true")
    monkeypatch.setenv("BASE_URL", "https://api.tauro.test")
    monkeypatch.setattr(tiendanube_labels, "labels_execution_ready", lambda: False)
    monkeypatch.setattr(
        tiendanube_shipping,
        "configuracion",
        lambda _store: {
            "activa": True,
            "callback_token_hash": "hash-local-desactualizado",
            "label_callback_token_hash": None,
            "carrier_id": "77",
            "carrier_option_id": "88",
        },
    )
    monkeypatch.setattr(
        tiendanube_shipping.secrets,
        "token_urlsafe",
        lambda _size: "rate-token-nuevo-12345678901234567890",
    )
    calls = []

    def fake_api(*args):
        calls.append(args)
        method, path = args[2], args[3]
        if method == "GET" and path.endswith("/options"):
            return _Response(200, [])
        if method == "GET":
            return _Response(200, {
                "id": 77,
                "active": True,
                "callback_url": (
                    "https://otra.example/integraciones/tiendanube/"
                    "shipping/rates/token-ajeno"
                ),
            })
        if method == "POST" and path.endswith("/options"):
            return _Response(201, {
                "id": 99,
                "code": "tauro_nacional_domicilio",
                "active": True,
            })
        return _Response(200)

    monkeypatch.setattr(tiendanube_app, "_api", fake_api)
    monkeypatch.setattr(tiendanube_shipping, "reactivar", lambda _store: None)
    saved = []
    monkeypatch.setattr(
        tiendanube_shipping,
        "_guardar_config",
        lambda *args, **kwargs: saved.append((args, kwargs)),
    )

    result = tiendanube_shipping.registrar_shipping_carrier("123", "access")

    assert not [
        call
        for call in calls
        if call[2] == "POST" and call[3] == "shipping_carriers"
    ]
    assert any(
        call[2] == "PUT"
        and call[3] == "shipping_carriers/77"
        and "callback_url" in call[4]
        for call in calls
    )
    assert saved[0][0][1] == "rate-token-nuevo-12345678901234567890"
    assert saved[0][0][3] == "99"
    assert result["option_id"] == "99"
