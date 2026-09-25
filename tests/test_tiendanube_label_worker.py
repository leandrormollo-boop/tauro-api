import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI

from servicios.carrier_adapter import OperationState, ShipmentResult
from servicios.oca_adapter import OCAOutcomeUnknown, OCAUnavailableError
from servicios.tiendanube_label_worker import (
    DurableDocument,
    LabelPermanentError,
    LabelRetryableError,
    LabelStage,
    LabelTask,
    PostgresLabelWorkerRepository,
    TiendanubeLabelPublisher,
    WorkerResult,
    _shipment_from_snapshot,
    process_label_outbox,
    revoke_label_document,
)


SNAPSHOT_ID = "tnq_" + "a" * 64
TRACKING = "1234567890123456789"


def _payload():
    return {
        "id": "label-1",
        "fulfillment_order_info": {
            "id": "ffo-1",
            "assigned_location": {
                "name": "Depósito TAURO",
                "address": {
                    "zipcode": "1425",
                    "street": "Av. Origen",
                    "number": "100",
                    "city": "CABA",
                    "province": {"name": "Buenos Aires", "code": "B"},
                    "country": {"code": "AR"},
                    "email": "operaciones@taurosolutions.ar",
                },
            },
            "recipient": {
                "name": "Ada Cliente",
                "phone": "3415550101",
                "email": "ada@example.com",
            },
            "destination": {
                "zipcode": "2000",
                "street": "Calle Payload",
                "number": "550",
                "floor": "2",
                "city": "Rosario",
                "province": {"name": "Santa Fe", "code": "S"},
                "country": {"code": "AR"},
            },
            "shipping": {
                "option": {
                    "code": "tauro_nacional_domicilio",
                    "reference": f"tauro:oca:{SNAPSHOT_ID}",
                }
            },
            # No es geometría contractual de bultos y debe ignorarse.
            "line_items": [
                {
                    "quantity": 99,
                    "unit_dimension": {
                        "weight": 999,
                        "height": 999,
                        "width": 999,
                        "depth": 999,
                    },
                }
            ],
        },
    }


def _snapshot(**changes):
    result = {
        "snapshot_id": SNAPSHOT_ID,
        "store_id": "123456",
        "customer_id": "CLIENTE-1",
        "carrier_id": "oca",
        "carrier_quote_id": "oca-quote-real-123",
        "service_code": "oca-home",
        "packages": [
            {
                "quantity": 2,
                "weight_kg": "1.25",
                "length_cm": "20",
                "width_cm": "10",
                "height_cm": "5",
            }
        ],
        "declared_value": "50000",
        "declared_currency": "ARS",
        "origin_mode": "domicilio",
        "destination_mode": "domicilio",
        "origin_route": {"country": "AR", "postal_code": "1425"},
        "destination_route": {"country": "AR", "postal_code": "2000"},
    }
    result.update(changes)
    return result


class MemoryRepository:
    def __init__(self, *, stage=LabelStage.CREATE_SHIPMENT, attempts=0):
        self.stage = stage
        self.attempts = attempts
        self.state = "PENDIENTE"
        self.external_id = "order:123" if stage != LabelStage.CREATE_SHIPMENT else ""
        self.tracking = TRACKING if stage != LabelStage.CREATE_SHIPMENT else ""
        self.document = (
            DurableDocument("doc-1", "https://example.test/doc", 9, "d" * 64)
            if stage in {LabelStage.PUBLISH, LabelStage.DONE}
            else None
        )
        self.claim = ""
        self.events = []
        self.last_code = ""
        self.crash_after_created = False
        self.crash_after_document = False
        self.stale = False

    def recover_stale_claims(self):
        if self.state != "PROCESANDO" or not self.stale:
            return 0
        self.events.append(("recover", self.stage.value))
        self.state = (
            "VERIFICAR_MANUAL"
            if self.stage == LabelStage.CREATE_SHIPMENT
            else "PENDIENTE"
        )
        self.claim = ""
        self.stale = False
        return 1

    def claim_next(self):
        if self.state != "PENDIENTE":
            return None
        self.state = "PROCESANDO"
        self.attempts += 1
        self.claim = f"claim-{self.attempts}"
        self.stale = True
        self.events.append(("claim", self.stage.value))
        return {
            "id": 1,
            "store_id": "123456",
            "label_id": "label-1",
            "fulfillment_order_id": "ffo-1",
            "payload": _payload(),
            "intentos": self.attempts,
            "claim_id": self.claim,
            "rate_quote_snapshot_id": SNAPSHOT_ID,
            "stage": self.stage.value,
            "external_operation_id": self.external_id,
            "tracking_number": self.tracking,
            "document_key": self.document.key if self.document else "",
            "document_url": self.document.download_url if self.document else "",
            "document_size": self.document.size if self.document else 0,
        }

    def checkpoint_created(self, task, outcome):
        assert task.stage == LabelStage.CREATE_SHIPMENT
        self.external_id = outcome.external_id
        self.tracking = outcome.tracking
        self.stage = LabelStage.FETCH_LABEL
        self.events.append(("checkpoint", "created"))
        if self.crash_after_created:
            self.crash_after_created = False
            raise SystemExit("simulated crash after CREATE checkpoint")
        return task.__class__(
            **{
                **task.__dict__,
                "stage": LabelStage.FETCH_LABEL,
                "external_operation_id": self.external_id,
                "tracking_number": self.tracking,
            }
        )

    def checkpoint_document(self, task, document):
        assert task.stage == LabelStage.FETCH_LABEL
        self.document = document
        self.stage = LabelStage.PUBLISH
        self.events.append(("checkpoint", "document"))
        if self.crash_after_document:
            self.crash_after_document = False
            raise SystemExit("simulated crash after document checkpoint")
        return task.__class__(
            **{
                **task.__dict__,
                "stage": LabelStage.PUBLISH,
                "document_key": document.key,
                "document_url": document.download_url,
                "document_size": document.size,
            }
        )

    def complete(self, task):
        if self.state != "PROCESANDO" or task.claim_id != self.claim:
            return False
        self.stage = LabelStage.DONE
        self.state = "COMPLETADO"
        self.stale = False
        self.events.append(("complete", "done"))
        return True

    def retry(self, task, code):
        if self.state != "PROCESANDO" or task.claim_id != self.claim:
            return False
        self.state = "PENDIENTE"
        self.stale = False
        self.last_code = code
        self.events.append(("retry", self.stage.value))
        return True

    def fail(self, task, code):
        if self.state != "PROCESANDO" or task.claim_id != self.claim:
            return False
        self.state = "FALLIDO"
        self.stale = False
        self.last_code = code
        self.events.append(("fail", self.stage.value))
        return True

    def manual_review(self, task, code):
        if self.state != "PROCESANDO" or task.claim_id != self.claim:
            return False
        self.state = "VERIFICAR_MANUAL"
        self.stale = False
        self.last_code = code
        self.events.append(("manual", self.stage.value))
        return True


class FakeAdapter:
    carrier_id = "oca"

    def __init__(self):
        self.create_calls = []
        self.label_calls = []
        self.create_error = None
        self.create_outcome = None
        self.label_errors = []

    def create_shipment(self, quote_id, shipment, *, idempotency_key):
        self.create_calls.append((quote_id, shipment, idempotency_key))
        if self.create_error:
            raise self.create_error
        return self.create_outcome or ShipmentResult(
            state=OperationState.EMITIDO,
            carrier_id="oca",
            external_id="order:123",
            tracking=TRACKING,
        )

    def get_label(self, external_id):
        self.label_calls.append(external_id)
        if self.label_errors:
            raise self.label_errors.pop(0)
        return ShipmentResult(
            state=OperationState.ETIQUETA_LISTA,
            carrier_id="oca",
            external_id=external_id,
            label_pdf=b"%PDF-1.4\nlabel",
        )


class MemoryDocumentStore:
    def __init__(self, events):
        self.events = events
        self.calls = []

    def persist(self, task, pdf):
        self.calls.append((task.idempotency_key, pdf))
        self.events.append(("document_store", "persisted"))
        return DurableDocument(
            key="doc-1",
            download_url="https://example.test/doc?token=secret",
            size=len(pdf),
            sha256="d" * 64,
        )


class MemoryPublisher:
    def __init__(self, events):
        self.events = events
        self.calls = []
        self.errors = []

    def publish(self, task, document):
        self.calls.append((task, document))
        self.events.append(("publisher", "published"))
        if self.errors:
            raise self.errors.pop(0)


def _run(repository, adapter, *, store=None, publisher=None, limit=1):
    store = store or MemoryDocumentStore(repository.events)
    publisher = publisher or MemoryPublisher(repository.events)
    result = process_label_outbox(
        limit=limit,
        repository=repository,
        readiness=lambda: True,
        adapter_loader=lambda carrier: adapter,
        snapshot_resolver=lambda task: _snapshot(),
        document_store=store,
        publisher=publisher,
    )
    return result, store, publisher


def test_flujo_completo_checkpoint_por_etapa_y_usa_quote_real_y_bultos_congelados():
    repository = MemoryRepository()
    adapter = FakeAdapter()

    result, store, publisher = _run(repository, adapter)

    assert result == WorkerResult(processed=1, completed=1, ready=True)
    assert repository.state == "COMPLETADO"
    assert repository.stage == LabelStage.DONE
    assert len(adapter.create_calls) == 1
    quote_id, shipment, idempotency_key = adapter.create_calls[0]
    assert quote_id == "oca-quote-real-123"
    assert quote_id != SNAPSHOT_ID
    assert shipment["packages"] == _snapshot()["packages"]
    assert shipment["destination"]["calle"] == "Calle Payload"
    assert shipment["origin"]["calle"] == "Av. Origen"
    assert shipment["recipient"]["name"] == "Ada Cliente"
    assert idempotency_key.startswith("tn-label-")
    assert len(store.calls) == 1
    assert len(publisher.calls) == 1
    assert repository.events.index(("checkpoint", "created")) < repository.events.index(
        ("document_store", "persisted")
    )
    assert repository.events.index(("checkpoint", "document")) < repository.events.index(
        ("publisher", "published")
    )


def test_outcome_unknown_de_oca_va_a_revision_manual_y_no_se_reintenta():
    repository = MemoryRepository()
    adapter = FakeAdapter()
    adapter.create_error = OCAOutcomeUnknown("timeout después del POST")

    result, _, _ = _run(repository, adapter)

    assert result.manual_review == 1
    assert result.retried == 0
    assert repository.state == "VERIFICAR_MANUAL"
    assert len(adapter.create_calls) == 1
    result2, _, _ = _run(repository, adapter)
    assert result2.processed == 0
    assert len(adapter.create_calls) == 1


def test_unavailable_durante_escritura_create_tambien_es_manual_no_retry():
    repository = MemoryRepository()
    adapter = FakeAdapter()
    adapter.create_error = OCAUnavailableError("respuesta ambigua")

    result, _, _ = _run(repository, adapter)

    assert result.manual_review == 1
    assert result.retried == 0
    assert repository.state == "VERIFICAR_MANUAL"
    assert len(adapter.create_calls) == 1


def test_rechazo_definitivo_de_oca_termina_failed_sin_reintento():
    repository = MemoryRepository()
    adapter = FakeAdapter()
    adapter.create_outcome = ShipmentResult(
        state=OperationState.ERROR_DEFINITIVO,
        carrier_id="oca",
        safe_message="rechazado",
    )

    result, _, _ = _run(repository, adapter)

    assert result.failed == 1
    assert result.retried == 0
    assert result.manual_review == 0
    assert repository.state == "FALLIDO"
    assert repository.stage == LabelStage.CREATE_SHIPMENT
    assert len(adapter.create_calls) == 1


def test_error_transitorio_al_buscar_pdf_reintenta_sin_recrear_envio():
    repository = MemoryRepository()
    adapter = FakeAdapter()
    adapter.label_errors = [OCAUnavailableError("temporal")]

    first, _, _ = _run(repository, adapter)

    assert first.retried == 1
    assert repository.stage == LabelStage.FETCH_LABEL
    assert len(adapter.create_calls) == 1
    assert len(adapter.label_calls) == 1

    second, _, publisher = _run(repository, adapter)

    assert second.completed == 1
    assert len(adapter.create_calls) == 1
    assert len(adapter.label_calls) == 2
    assert len(publisher.calls) == 1


def test_crash_despues_de_checkpoint_create_reanuda_en_fetch_sin_doble_alta():
    repository = MemoryRepository()
    repository.crash_after_created = True
    adapter = FakeAdapter()

    with pytest.raises(SystemExit, match="CREATE checkpoint"):
        _run(repository, adapter)

    assert repository.stage == LabelStage.FETCH_LABEL
    assert len(adapter.create_calls) == 1
    result, _, _ = _run(repository, adapter)

    assert result.stale_claims == 1
    assert result.completed == 1
    assert len(adapter.create_calls) == 1
    assert len(adapter.label_calls) == 1


def test_crash_despues_de_checkpoint_document_publica_sin_refetch_ni_reemision():
    repository = MemoryRepository()
    repository.crash_after_document = True
    adapter = FakeAdapter()
    store = MemoryDocumentStore(repository.events)
    publisher = MemoryPublisher(repository.events)

    with pytest.raises(SystemExit, match="document checkpoint"):
        _run(repository, adapter, store=store, publisher=publisher)

    assert repository.stage == LabelStage.PUBLISH
    assert len(adapter.create_calls) == 1
    assert len(adapter.label_calls) == 1
    assert publisher.calls == []

    result, _, _ = _run(repository, adapter, store=store, publisher=publisher)

    assert result.completed == 1
    assert len(adapter.create_calls) == 1
    assert len(adapter.label_calls) == 1
    assert len(store.calls) == 1
    assert len(publisher.calls) == 1


def test_claim_expirado_en_create_va_a_manual_y_no_se_reclama():
    repository = MemoryRepository()
    repository.state = "PROCESANDO"
    repository.stale = True
    adapter = FakeAdapter()

    result, _, _ = _run(repository, adapter)

    assert result.stale_claims == 1
    assert result.processed == 0
    assert repository.state == "VERIFICAR_MANUAL"
    assert adapter.create_calls == []


def test_snapshot_o_payload_invalido_falla_antes_de_tocar_oca():
    repository = MemoryRepository()
    adapter = FakeAdapter()

    result = process_label_outbox(
        limit=1,
        repository=repository,
        readiness=lambda: True,
        adapter_loader=lambda _carrier: adapter,
        snapshot_resolver=lambda _task: _snapshot(
            destination_route={"country": "AR", "postal_code": "5000"}
        ),
        document_store=MemoryDocumentStore(repository.events),
        publisher=MemoryPublisher(repository.events),
    )

    assert result.failed == 1
    assert repository.state == "FALLIDO"
    assert adapter.create_calls == []


def test_publicacion_transitoria_reintenta_solo_publish():
    repository = MemoryRepository(stage=LabelStage.PUBLISH)
    adapter = FakeAdapter()
    publisher = MemoryPublisher(repository.events)
    publisher.errors = [LabelRetryableError("429")]

    first, _, _ = _run(repository, adapter, publisher=publisher)
    assert first.retried == 1
    assert adapter.create_calls == []
    assert adapter.label_calls == []

    second, _, _ = _run(repository, adapter, publisher=publisher)
    assert second.completed == 1
    assert len(publisher.calls) == 2
    assert adapter.create_calls == []


def test_readiness_apagado_no_reclama_ni_toca_dependencias():
    repository = MemoryRepository()
    adapter = FakeAdapter()

    result = process_label_outbox(
        repository=repository,
        readiness=lambda: False,
        adapter_loader=lambda _carrier: adapter,
    )

    assert result == WorkerResult(ready=False)
    assert repository.events == []
    assert adapter.create_calls == []


class _MemoryPostgresRepository(MemoryRepository, PostgresLabelWorkerRepository):
    """Fuerza la selección automática del guard sin abrir DB en el repositorio."""


class _GuardCursor:
    def __init__(self, events, validation_row):
        self.events = events
        self.validation_row = validation_row
        self.commands = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.commands.append((normalized, params))
        if "pg_advisory_unlock" in normalized:
            self.events.append(("guard", "unlock"))
        elif "pg_advisory_lock" in normalized:
            self.events.append(("guard", "lock"))
        else:
            self.events.append(("guard", "revalidate"))

    def fetchone(self):
        return self.validation_row


class _GuardConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_guard_postgres_revalida_despues_del_claim_y_frena_si_redaccion_gano(
    monkeypatch,
):
    from servicios import tiendanube_label_worker as worker

    repository = _MemoryPostgresRepository()
    cursor = _GuardCursor(repository.events, validation_row=None)
    conn = _GuardConnection(cursor)
    adapter = FakeAdapter()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)

    result, _, _ = _run(repository, adapter)

    assert result.failed == 1
    assert result.manual_review == 0
    assert adapter.create_calls == []
    assert repository.events[:4] == [
        ("claim", "CREATE_SHIPMENT"),
        ("guard", "lock"),
        ("guard", "revalidate"),
        ("guard", "unlock"),
    ]
    validation_sql, validation_params = cursor.commands[1]
    assert "JOIN tiendanube_labels" in validation_sql
    assert "JOIN tiendanube_fulfillment_order_orders" in validation_sql
    assert "o.claim_id = %s" in validation_sql
    assert "tiendanube_pedidos_redactados" in validation_sql
    assert "cancellation.operacion = 'CANCEL'" in validation_sql
    assert "CANCELACION_CONFIRMADA" in validation_sql
    assert "CANCELACION_REVISION_MANUAL" in validation_sql
    assert "NOT EXISTS" in validation_sql
    assert validation_params[3] == "claim-1"
    assert cursor.commands[0][1] == (
        "tauro:tiendanube:123456.tiendanube",
    )


def test_guard_postgres_libera_lock_en_finally_si_oca_falla(monkeypatch):
    from servicios import tiendanube_label_worker as worker

    repository = _MemoryPostgresRepository()
    cursor = _GuardCursor(repository.events, validation_row={"order_id": "order-1"})
    conn = _GuardConnection(cursor)

    class AdapterConEvento(FakeAdapter):
        def create_shipment(self, *args, **kwargs):
            repository.events.append(("carrier", "create"))
            raise OCAOutcomeUnknown("resultado incierto")

    adapter = AdapterConEvento()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)

    result, _, _ = _run(repository, adapter)

    assert result.manual_review == 1
    assert repository.events.index(("guard", "lock")) < repository.events.index(
        ("carrier", "create")
    )
    assert repository.events.index(("carrier", "create")) < repository.events.index(
        ("guard", "unlock")
    )
    assert conn.commits == 2
    assert cursor.commands[-1][0].startswith("SELECT pg_advisory_unlock")


def test_repositorio_memory_usa_guard_noop_sin_base_real(monkeypatch):
    from servicios import tiendanube_label_worker as worker

    repository = MemoryRepository()
    adapter = FakeAdapter()
    monkeypatch.setattr(
        worker,
        "get_conn",
        lambda: pytest.fail("MemoryRepository no debe abrir PostgreSQL"),
    )

    result, _, _ = _run(repository, adapter)

    assert result.completed == 1
    assert len(adapter.create_calls) == 1


def test_builder_toma_direcciones_del_generate_y_paquetes_solo_del_snapshot():
    task = LabelTask(
        id=1,
        store_id="123456",
        label_id="label-1",
        fulfillment_order_id="ffo-1",
        payload=_payload(),
        attempts=1,
        claim_id="claim",
        carrier_id="oca",
        rate_quote_snapshot_id=SNAPSHOT_ID,
        stage=LabelStage.CREATE_SHIPMENT,
    )
    snapshot = _snapshot()

    shipment = _shipment_from_snapshot(task, snapshot)

    assert shipment["origin"]["nro"] == "100"
    assert shipment["destination"]["nro"] == "550"
    assert shipment["packages"] == snapshot["packages"]
    assert shipment["packages"][0]["quantity"] == 2
    assert shipment["packages"][0]["weight_kg"] == "1.25"


def test_publisher_envia_contrato_ready_to_download_despues_del_documento(monkeypatch):
    from servicios import tiendanube_app

    calls = []

    class Response:
        status_code = 200

    monkeypatch.setattr(
        tiendanube_app,
        "instalacion",
        lambda _store: {"estado": "ACTIVA", "access_token": "secret-token"},
    )

    def api(store, token, method, path, payload=None):
        calls.append((store, token, method, path, payload))
        return Response()

    monkeypatch.setattr(tiendanube_app, "_api", api)
    task = LabelTask(
        id=1,
        store_id="123456",
        label_id="label-1",
        fulfillment_order_id="ffo-1",
        payload=_payload(),
        attempts=1,
        claim_id="claim",
        carrier_id="oca",
        rate_quote_snapshot_id=SNAPSHOT_ID,
        stage=LabelStage.PUBLISH,
        external_operation_id="order:123",
        tracking_number=TRACKING,
        document_key="doc-1",
        document_url="https://example.test/doc?token=abc",
        document_size=321,
    )
    document = DurableDocument(
        key="doc-1",
        download_url=task.document_url,
        size=321,
        sha256="d" * 64,
    )

    TiendanubeLabelPublisher().publish(task, document)

    assert calls == [
        (
            "123456",
            "secret-token",
            "PATCH",
            "fulfillment-orders/ffo-1/labels/label-1",
            {
                "status": "READY_TO_DOWNLOAD",
                "documents": [
                    {
                        "file_name": "tauro-label-1.pdf",
                        "type": "LABEL",
                        "format": "PDF",
                        "download_url_from_app": task.document_url,
                        "size": 321,
                    }
                ],
                "tracking_info": {"code": TRACKING, "url": None},
            },
        )
    ]


def _publisher_reconcile_task(**changes):
    values = {
        "id": 1,
        "store_id": "123456",
        "label_id": "label-1",
        "fulfillment_order_id": "ffo-1",
        "payload": _payload(),
        "attempts": 1,
        "claim_id": "claim",
        "carrier_id": "oca",
        "rate_quote_snapshot_id": SNAPSHOT_ID,
        "stage": LabelStage.PUBLISH,
        "order_id": "order-1",
        "external_operation_id": "order:123",
        "tracking_number": TRACKING,
        "document_key": "doc-1",
        "document_url": "https://example.test/doc?token=abc",
        "document_size": 321,
    }
    values.update(changes)
    return LabelTask(**values)


def _remote_ffo(*, tracking=TRACKING, size=321, status="READY_TO_USE"):
    return {
        "id": "ffo-1",
        "labels": [
            {
                "id": "label-1",
                "status": status,
                "tracking_info": {"code": tracking},
                "documents": [
                    {
                        "file_name": "tauro-label-1.pdf",
                        "type": "LABEL",
                        "format": "PDF",
                        "size": size,
                    }
                ],
            }
        ],
    }


class _PublisherResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("sin json")
        return self._payload


def _install_publisher_api(monkeypatch, responses):
    from servicios import tiendanube_app

    calls = []
    queue = list(responses)
    monkeypatch.setattr(
        tiendanube_app,
        "instalacion",
        lambda _store: {"estado": "ACTIVA", "access_token": "secret-token"},
    )

    def api(store, token, method, path, payload=None):
        calls.append((store, token, method, path, payload))
        return queue.pop(0)

    monkeypatch.setattr(tiendanube_app, "_api", api)
    return calls


def test_publisher_reconcilia_patch_ya_aceptado_y_estado_avanzado(monkeypatch):
    calls = _install_publisher_api(
        monkeypatch,
        [
            _PublisherResponse(400),
            _PublisherResponse(200, _remote_ffo(status="DOWNLOADED")),
        ],
    )
    task = _publisher_reconcile_task()
    document = DurableDocument("doc-1", task.document_url, 321, "d" * 64)

    TiendanubeLabelPublisher().publish(task, document)

    assert [call[2] for call in calls] == ["PATCH", "GET"]
    assert calls[1][3] == "orders/order-1/fulfillment-orders/ffo-1"


def test_publisher_no_acepta_estado_remoto_con_documento_ajeno(monkeypatch):
    _install_publisher_api(
        monkeypatch,
        [
            _PublisherResponse(400),
            _PublisherResponse(200, _remote_ffo(size=999)),
        ],
    )
    task = _publisher_reconcile_task()
    document = DurableDocument("doc-1", task.document_url, 321, "d" * 64)

    with pytest.raises(LabelPermanentError):
        TiendanubeLabelPublisher().publish(task, document)


def test_publisher_mantiene_retry_si_patch_y_lectura_son_transitorios(monkeypatch):
    _install_publisher_api(
        monkeypatch,
        [_PublisherResponse(500), _PublisherResponse(503)],
    )
    task = _publisher_reconcile_task()
    document = DurableDocument("doc-1", task.document_url, 321, "d" * 64)

    with pytest.raises(LabelRetryableError):
        TiendanubeLabelPublisher().publish(task, document)


def test_repository_claim_usa_skip_locked_y_recovery_distingue_create():
    claim_source = inspect.getsource(PostgresLabelWorkerRepository.claim_next)
    recovery_source = inspect.getsource(PostgresLabelWorkerRepository.recover_stale_claims)

    assert "FOR UPDATE OF o SKIP LOCKED" in claim_source
    assert "CREATE_SHIPMENT" in recovery_source
    assert "VERIFICAR_MANUAL" in recovery_source
    assert "PENDIENTE" in recovery_source


def test_worker_no_migra_schema_en_hot_path():
    from servicios import tiendanube_label_worker as worker

    source = inspect.getsource(worker._ensure_worker_tables)
    normalized = " ".join(source.split())

    assert "to_regclass" in normalized
    assert "schema_ready" in normalized
    assert "CREATE TABLE" not in normalized
    assert "ALTER TABLE" not in normalized
    assert "LOCK TABLE" not in normalized


def test_main_programa_worker_de_labels_cada_cinco_segundos():
    source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")

    assert "process_label_outbox as procesar_labels_tiendanube" in source
    assert "procesar_labels_tiendanube," in source
    assert 'id="tiendanube_label_outbox"' in source
    assert "seconds=5" in source
    assert "max_instances=1" in source
    assert "coalesce=True" in source


class _SqlCursor:
    def __init__(self, rowcounts, rows=()):
        self._rowcounts = iter(rowcounts)
        self._rows = iter(rows)
        self.rowcount = 0
        self.commands = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.commands.append((" ".join(sql.split()), params))
        self.rowcount = next(self._rowcounts)

    def fetchone(self):
        return next(self._rows, None)


class _SqlConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _publish_task():
    return LabelTask(
        id=1,
        store_id="123456",
        label_id="label-1",
        fulfillment_order_id="ffo-1",
        payload=_payload(),
        attempts=1,
        claim_id="claim-1",
        carrier_id="oca",
        rate_quote_snapshot_id=SNAPSHOT_ID,
        stage=LabelStage.PUBLISH,
        external_operation_id="order:123",
        tracking_number=TRACKING,
        document_key="doc-1",
        document_url="https://example.test/doc?token=secret",
        document_size=321,
    )


def test_complete_borra_pii_y_conserva_flags_de_completitud_en_una_transaccion(
    monkeypatch,
):
    from servicios import tiendanube_label_worker as worker

    cursor = _SqlCursor([1, 1, 1])
    conn = _SqlConnection(cursor)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)

    assert PostgresLabelWorkerRepository().complete(_publish_task()) is True

    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert len(cursor.commands) == 3
    outbox_sql = cursor.commands[1][0]
    label_sql = cursor.commands[2][0]
    assert "payload = '{}'::jsonb" in outbox_sql
    assert "payload_complete = TRUE" in outbox_sql
    assert "generate_payload = NULL" in label_sql
    assert "generate_payload_complete = TRUE" in label_sql


def test_complete_hace_rollback_si_no_puede_limpiar_el_payload_de_label(monkeypatch):
    from servicios import tiendanube_label_worker as worker

    cursor = _SqlCursor([1, 1, 0])
    conn = _SqlConnection(cursor)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)

    with pytest.raises(LabelRetryableError, match="cerrar la etiqueta"):
        PostgresLabelWorkerRepository().complete(_publish_task())

    assert conn.commits == 0
    assert conn.rollbacks == 1


class _FinishWithCancellationCursor:
    def __init__(self, cancellation_state):
        self.cancellation_state = cancellation_state
        self.label_state = cancellation_state or "ETIQUETA_PERSISTIDA"
        self.outbox_state = "PROCESANDO"
        self.rowcount = 0
        self.commands = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.commands.append((normalized, params))
        if "pg_advisory_xact_lock" in normalized:
            self.rowcount = 1
            return
        if "UPDATE tiendanube_label_outbox SET estado" in normalized:
            self.outbox_state = params[0]
            self.rowcount = 1
            return
        if "UPDATE tiendanube_label_execution" in normalized:
            self.rowcount = 1
            return
        if "UPDATE tiendanube_labels label" in normalized:
            active = self.cancellation_state in {
                "CANCELACION_ENVIADA",
                "CANCELACION_CONFIRMADA",
                "CANCELACION_REVISION_MANUAL",
            }
            if active and "NOT EXISTS" in normalized:
                self.rowcount = 0
                return
            self.label_state = params[0]
            self.rowcount = 1
            return
        raise AssertionError(f"SQL inesperado: {normalized}")


@pytest.mark.parametrize(
    "cancellation_state",
    [
        "CANCELACION_ENVIADA",
        "CANCELACION_CONFIRMADA",
        "CANCELACION_REVISION_MANUAL",
    ],
)
def test_finish_falla_generate_sin_pisar_cancelacion_activa(
    monkeypatch, cancellation_state
):
    from servicios import tiendanube_label_worker as worker

    cursor = _FinishWithCancellationCursor(cancellation_state)
    conn = _SqlConnection(cursor)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)

    assert PostgresLabelWorkerRepository().fail(_publish_task(), "TOMBSTONE") is True

    assert cursor.outbox_state == "FALLIDO"
    assert cursor.label_state == cancellation_state
    assert conn.commits == 1
    assert cursor.commands[0] == (
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        ("tauro:tiendanube:123456.tiendanube",),
    )
    label_sql = cursor.commands[-1][0]
    assert "cancellation.operacion = 'CANCEL'" in label_sql
    assert "CANCELACION_ENVIADA" in label_sql
    assert "CANCELACION_CONFIRMADA" in label_sql
    assert "CANCELACION_REVISION_MANUAL" in label_sql


def test_finish_actualiza_label_si_cancelacion_fue_rechazada(monkeypatch):
    from servicios import tiendanube_label_worker as worker

    cursor = _FinishWithCancellationCursor("CANCELACION_RECHAZADA")
    conn = _SqlConnection(cursor)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)

    assert PostgresLabelWorkerRepository().fail(_publish_task(), "PERMANENT") is True

    assert cursor.outbox_state == "FALLIDO"
    assert cursor.label_state == "FALLIDO"


class _RevokeCursor:
    def __init__(self):
        self.rowcount = 0
        self.commands = []
        self.documents = {
            ("123456", "label-1"): {
                "activa": True,
                "revocada_en": None,
            }
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.commands.append((normalized, params))
        document = self.documents.get(tuple(params))
        self.rowcount = int(document is not None)
        if document is not None:
            document["activa"] = False
            if document["revocada_en"] is None:
                document["revocada_en"] = "primera-revocacion"


def test_revoke_documento_es_idempotente_y_no_mueve_la_fecha(monkeypatch):
    from servicios import tiendanube_label_worker as worker

    cursor = _RevokeCursor()
    conn = _SqlConnection(cursor)
    monkeypatch.setattr(worker, "_ensure_worker_tables", lambda: None)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)

    assert revoke_label_document("123456", "label-1") is True
    first_revocation = cursor.documents[("123456", "label-1")]["revocada_en"]
    assert revoke_label_document("123456", "label-1") is True

    assert cursor.documents[("123456", "label-1")] == {
        "activa": False,
        "revocada_en": first_revocation,
    }
    assert conn.commits == 2
    assert all("COALESCE(revocada_en, now())" in sql for sql, _ in cursor.commands)
    assert all("AND activa = TRUE" not in sql for sql, _ in cursor.commands)
    assert revoke_label_document("123456", "missing") is False


def test_idempotency_key_es_estable_y_acotada():
    task = LabelTask(
        id=1,
        store_id="123456",
        label_id="label-1",
        fulfillment_order_id="ffo-1",
        payload=_payload(),
        attempts=1,
        claim_id="claim",
        carrier_id="oca",
        rate_quote_snapshot_id=SNAPSHOT_ID,
        stage=LabelStage.CREATE_SHIPMENT,
    )
    replay = LabelTask(**{**task.__dict__, "attempts": 7, "claim_id": "otro"})

    assert task.idempotency_key == replay.idempotency_key
    assert len(task.idempotency_key) <= 128


def _document_app(monkeypatch, loader):
    from endpoints import tiendanube_shipping as endpoint

    monkeypatch.setattr(endpoint, "load_label_document", loader)
    app = FastAPI()
    app.include_router(endpoint.router)
    return app


def _asgi_get(app, path, params=None):
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    query = urlencode(params or {}).encode("ascii")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query,
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 443),
        "state": {},
    }
    asyncio.run(app(scope, receive, send))
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in start["headers"]
    }
    return SimpleNamespace(status_code=start["status"], headers=headers, content=body)


def test_endpoint_documento_entrega_pdf_con_headers_seguros(monkeypatch):
    calls = []

    def loader(store_id, label_id, token):
        calls.append((store_id, label_id, token))
        return b"%PDF-1.4\netiqueta"

    token = "a" * 64
    response = _asgi_get(
        _document_app(monkeypatch, loader),
        "/integraciones/tiendanube/shipping/labels/documents/123456/label-1",
        {"token": token},
    )

    assert response.status_code == 200
    assert response.content == b"%PDF-1.4\netiqueta"
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["content-disposition"] == (
        'attachment; filename="tauro-label.pdf"'
    )
    assert calls == [("123456", "label-1", token)]


def test_endpoint_documento_uniforma_token_ausente_invalido_y_revocado(monkeypatch):
    calls = []

    def loader(store_id, label_id, token):
        calls.append((store_id, label_id, token))
        return None

    path = "/integraciones/tiendanube/shipping/labels/documents/123456/label-1"
    app = _document_app(monkeypatch, loader)
    responses = [
        _asgi_get(app, path),
        _asgi_get(app, path, {"token": "no-es-un-token"}),
        _asgi_get(app, path, {"token": "b" * 64}),
        _asgi_get(app, path, {"token": "c" * 64}),
    ]

    signatures = {
        (
            response.status_code,
            response.content,
            response.headers["cache-control"],
            response.headers["x-content-type-options"],
        )
        for response in responses
    }
    assert signatures == {
        (
            404,
            b'{"error":"documento_no_encontrado"}',
            "private, no-store",
            "nosniff",
        )
    }
    assert calls == [
        ("123456", "label-1", "b" * 64),
        ("123456", "label-1", "c" * 64),
    ]


def test_endpoint_documento_rechaza_longitudes_antes_de_consultar_storage(monkeypatch):
    calls = []

    def loader(*args):
        calls.append(args)
        return b"%PDF-1.4\nno-debe-leerse"

    base = "/integraciones/tiendanube/shipping/labels/documents"
    token = "a" * 64
    app = _document_app(monkeypatch, loader)
    responses = [
        _asgi_get(app, f"{base}/{'s' * 129}/label-1", {"token": token}),
        _asgi_get(app, f"{base}/123456/{'l' * 129}", {"token": token}),
        _asgi_get(app, f"{base}/123456/label-1", {"token": "a" * 65}),
    ]

    assert [response.status_code for response in responses] == [404, 404, 404]
    assert calls == []


def test_schema_incluye_persistencia_durable_del_worker():
    schema = (
        Path(__file__).resolve().parents[1] / "sql" / "schema.sql"
    ).read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS claim_id TEXT" in schema
    assert "ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ" in schema
    assert "CREATE TABLE IF NOT EXISTS tiendanube_label_execution" in schema
    assert "CREATE TABLE IF NOT EXISTS tiendanube_label_documents" in schema
    assert "ck_tn_label_execution_stage" in schema
    assert "ck_tn_label_document_size" in schema
    assert "idx_tiendanube_label_outbox_claim_vencido" in schema
