from __future__ import annotations

import re
from contextlib import contextmanager

import pytest

from servicios.carrier_adapter import OperationState
from servicios.oca_adapter import OCAConfigurationError, OCAOutcomeUnknown
from servicios.tiendanube_label_cancel import (
    CancelClaimState,
    CancelItemState,
    CancelLabelRequest,
    CancelTarget,
    LabelCancelContractError,
    PostgresLabelCancelRepository,
    cancel_labels,
    cancellation_idempotency_key,
)


STORE_ID = "123456"
LABEL_ID = "label-1"
FULFILLMENT_ID = "ffo-1"
EXTERNAL_ID = "order:123"


def _request(label_id=LABEL_ID, fulfillment_id=FULFILLMENT_ID):
    return CancelLabelRequest(label_id, fulfillment_id)


def _target(label_id=LABEL_ID, fulfillment_id=FULFILLMENT_ID, **changes):
    values = {
        "store_id": STORE_ID,
        "label_id": label_id,
        "fulfillment_order_id": fulfillment_id,
        "external_operation_id": EXTERNAL_ID,
        "carrier_id": "oca",
    }
    values.update(changes)
    return CancelTarget(**values)


class MemoryRepository:
    def __init__(self, targets=None):
        targets = [_target()] if targets is None else targets
        self.targets = {(target.store_id, target.label_id): target for target in targets}
        self.states = {}
        self.events = []
        self.confirm_error = None

    def lookup(self, store_id, label_id):
        self.events.append(("lookup", store_id, label_id))
        return self.targets.get((store_id, label_id))

    def claim(self, target, request):
        self.events.append(("claim", target.label_id, request.fulfillment_order_id))
        state = self.states.get(target.label_id)
        if state == "confirmed":
            return CancelClaimState.ALREADY_APPROVED
        if state in {"sent", "manual"}:
            return CancelClaimState.MANUAL_REVIEW
        if state == "rejected":
            return CancelClaimState.ALREADY_REJECTED
        if state == "conflict":
            return CancelClaimState.CONFLICT
        self.states[target.label_id] = "sent"
        return CancelClaimState.CLAIMED

    def cancel_before_emission(self, target, request):
        self.events.append(("cancel_before_emission", target.label_id))
        state = self.states.get(target.label_id)
        if state == "confirmed":
            return CancelClaimState.ALREADY_APPROVED
        if state == "manual":
            return CancelClaimState.MANUAL_REVIEW
        if state in {"rejected", "conflict"}:
            return (
                CancelClaimState.ALREADY_REJECTED
                if state == "rejected"
                else CancelClaimState.CONFLICT
            )
        self.states[target.label_id] = "confirmed"
        return CancelClaimState.ALREADY_APPROVED

    def mark_confirmed(self, target):
        self.events.append(("confirmed", target.label_id))
        if self.confirm_error:
            raise self.confirm_error
        assert self.states[target.label_id] == "sent"
        self.states[target.label_id] = "confirmed"

    def mark_rejected(self, target, error_code):
        self.events.append(("rejected", target.label_id, error_code))
        assert self.states[target.label_id] == "sent"
        self.states[target.label_id] = "rejected"

    def mark_manual_review(self, target, error_code):
        self.events.append(("manual", target.label_id, error_code))
        assert self.states[target.label_id] == "sent"
        self.states[target.label_id] = "manual"


class FakeAdapter:
    carrier_id = "oca"
    callback_timeout_budget_seconds = 0.25

    def __init__(self, outcome=OperationState.CANCELADO, error=None, on_call=None):
        self.outcome = outcome
        self.error = error
        self.on_call = on_call
        self.calls = []

    def cancel(self, operation_id, *, idempotency_key):
        self.calls.append((operation_id, idempotency_key))
        if self.on_call:
            self.on_call()
        if self.error:
            raise self.error
        return self.outcome


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_solo_cancela_la_operacion_resuelta_y_confirmada_por_el_carrier():
    repository = MemoryRepository()
    adapter = FakeAdapter()
    loaded = []

    result = cancel_labels(
        STORE_ID,
        [_request()],
        repository=repository,
        adapter_loader=lambda carrier_id: loaded.append(carrier_id) or adapter,
    )

    assert loaded == ["oca"]
    assert len(adapter.calls) == 1
    assert adapter.calls[0][0] == EXTERNAL_ID
    assert re.fullmatch(r"tn-cancel-[0-9a-f]{48}", adapter.calls[0][1])
    assert repository.states[LABEL_ID] == "confirmed"
    assert result.http_status == 204
    assert result.response_body is None
    assert result.approved_count == 1


def test_clave_idempotente_es_determinista_y_cambia_con_la_identidad():
    target = _target()

    assert cancellation_idempotency_key(target) == cancellation_idempotency_key(target)
    assert cancellation_idempotency_key(target) != cancellation_idempotency_key(
        _target(label_id="label-2")
    )


@pytest.mark.parametrize(
    "carrier_state",
    [
        OperationState.PENDIENTE,
        OperationState.ERROR_DEFINITIVO,
        "cancelando",
        "cancelado",
    ],
)
def test_nunca_aprueba_un_estado_distinto_de_cancelado(carrier_state):
    repository = MemoryRepository()
    adapter = FakeAdapter(outcome=carrier_state)

    result = cancel_labels(
        STORE_ID,
        [_request()],
        repository=repository,
        adapter_loader=lambda _carrier: adapter,
    )

    assert repository.states[LABEL_ID] == "rejected"
    assert result.http_status == 409
    assert result.items[0].state == CancelItemState.REJECTED
    assert result.response_body["labels"][0]["status"] == "FAILED"
    assert (
        result.response_body["labels"][0]["reason"]["code"]
        == "CARRIER_CANCELLATION_REJECTED"
    )


def test_outcome_unknown_queda_manual_y_un_replay_no_repite_la_escritura():
    repository = MemoryRepository()
    adapter = FakeAdapter(error=OCAOutcomeUnknown("secreto que no debe salir"))

    first = cancel_labels(
        STORE_ID,
        [_request()],
        repository=repository,
        adapter_loader=lambda _carrier: adapter,
    )
    second = cancel_labels(
        STORE_ID,
        [_request()],
        repository=repository,
        adapter_loader=lambda _carrier: adapter,
    )

    assert len(adapter.calls) == 1
    assert repository.states[LABEL_ID] == "manual"
    assert first.manual_review_count == second.manual_review_count == 1
    assert first.http_status == second.http_status == 409
    assert "secreto" not in str(first.response_body)


def test_error_no_clasificado_del_adapter_tambien_es_incierto_y_sin_retry():
    repository = MemoryRepository()
    adapter = FakeAdapter(error=RuntimeError("respuesta truncada sensible"))

    result = cancel_labels(
        STORE_ID,
        [_request()],
        repository=repository,
        adapter_loader=lambda _carrier: adapter,
    )

    assert len(adapter.calls) == 1
    assert repository.states[LABEL_ID] == "manual"
    assert result.items[0].requires_manual_review
    assert "sensible" not in str(result.response_body)


def test_error_previo_a_red_se_rechaza_sin_mandarlo_a_revision_manual():
    repository = MemoryRepository()
    adapter = FakeAdapter(error=OCAConfigurationError("configuración incompleta"))

    result = cancel_labels(
        STORE_ID,
        [_request()],
        repository=repository,
        adapter_loader=lambda _carrier: adapter,
    )

    assert len(adapter.calls) == 1
    assert repository.states[LABEL_ID] == "rejected"
    assert result.manual_review_count == 0
    assert result.items[0].reason_code == "CARRIER_POLICY_VIOLATION"


def test_valida_fulfillment_antes_de_cargar_adapter_o_tocar_red():
    repository = MemoryRepository()
    adapter = FakeAdapter()
    loader_calls = []

    result = cancel_labels(
        STORE_ID,
        [_request(fulfillment_id="ffo-ajena")],
        repository=repository,
        adapter_loader=lambda carrier: loader_calls.append(carrier) or adapter,
    )

    assert loader_calls == []
    assert adapter.calls == []
    assert not any(event[0] == "claim" for event in repository.events)
    assert result.http_status == 409
    assert result.response_body["labels"][0]["reason"]["code"] == "INSUFFICIENT_PERMISSIONS"


def test_no_confia_en_un_target_que_no_coincide_con_la_clave_consultada():
    repository = MemoryRepository()
    repository.targets[(STORE_ID, LABEL_ID)] = _target(label_id="label-ajena")
    adapter = FakeAdapter()

    result = cancel_labels(
        STORE_ID,
        [_request()],
        repository=repository,
        adapter_loader=lambda _carrier: adapter,
    )

    assert adapter.calls == []
    assert result.http_status == 409
    assert result.items[0].reason_code == "INSUFFICIENT_PERMISSIONS"


@pytest.mark.parametrize(
    "target",
    [
        None,
        _target(external_operation_id=""),
        _target(carrier_id=""),
    ],
)
def test_etiqueta_inexistente_o_sin_emision_no_llama_al_carrier(target):
    repository = MemoryRepository([])
    if target:
        repository.targets[(STORE_ID, LABEL_ID)] = target
    adapter = FakeAdapter()

    result = cancel_labels(
        STORE_ID,
        [_request()],
        repository=repository,
        adapter_loader=lambda _carrier: adapter,
    )

    assert adapter.calls == []
    assert result.http_status == (204 if target and not target.external_operation_id else 409)


def test_cancelacion_antes_de_emitir_cierra_local_y_replay_no_toca_carrier():
    repository = MemoryRepository([_target(external_operation_id="")])
    adapter = FakeAdapter()

    first = cancel_labels(
        STORE_ID,
        [_request()],
        repository=repository,
        adapter_loader=lambda _carrier: adapter,
    )
    second = cancel_labels(
        STORE_ID,
        [_request()],
        repository=repository,
        adapter_loader=lambda _carrier: adapter,
    )

    assert first.http_status == second.http_status == 204
    assert repository.states[LABEL_ID] == "confirmed"
    assert adapter.calls == []
    assert [event[0] for event in repository.events].count(
        "cancel_before_emission"
    ) == 2


def test_cancelacion_temprana_ambigua_queda_manual_y_no_toca_carrier():
    repository = MemoryRepository([_target(external_operation_id="")])
    repository.states[LABEL_ID] = "manual"
    adapter = FakeAdapter()

    result = cancel_labels(
        STORE_ID,
        [_request()],
        repository=repository,
        adapter_loader=lambda _carrier: adapter,
    )

    assert result.http_status == 409
    assert result.manual_review_count == 1
    assert adapter.calls == []


def test_lote_parcial_devuelve_207_con_formato_tiendanube():
    repository = MemoryRepository(
        [_target(), _target(label_id="label-2", fulfillment_id="ffo-2")]
    )
    adapters = {
        LABEL_ID: OperationState.CANCELADO,
        "label-2": OperationState.PENDIENTE,
    }

    class PerLabelAdapter(FakeAdapter):
        def cancel(self, operation_id, *, idempotency_key):
            label = LABEL_ID if operation_id == EXTERNAL_ID else "label-2"
            self.calls.append((operation_id, idempotency_key))
            return adapters[label]

    # El segundo target usa un external distinto para elegir su resultado.
    repository.targets[(STORE_ID, "label-2")] = _target(
        label_id="label-2",
        fulfillment_id="ffo-2",
        external_operation_id="order:456",
    )
    adapter = PerLabelAdapter()

    result = cancel_labels(
        STORE_ID,
        [_request(), _request("label-2", "ffo-2")],
        repository=repository,
        adapter_loader=lambda _carrier: adapter,
    )

    assert result.http_status == 207
    assert result.approved_count == 1
    assert result.rejected_count == 1
    assert result.response_body == {
        "labels": [
            {
                "fulfillment_order_id": FULFILLMENT_ID,
                "label_id": LABEL_ID,
                "status": "OK",
            },
            {
                "fulfillment_order_id": "ffo-2",
                "label_id": "label-2",
                "status": "FAILED",
                "reason": {
                    "code": "CARRIER_CANCELLATION_REJECTED",
                    "message": "El operador no aprobo la cancelacion.",
                },
            },
        ]
    }


def test_deadline_global_no_inicia_otra_escritura_si_no_hay_presupuesto():
    clock = FakeClock()
    repository = MemoryRepository(
        [_target(), _target(label_id="label-2", fulfillment_id="ffo-2")]
    )
    repository.targets[(STORE_ID, "label-2")] = _target(
        label_id="label-2",
        fulfillment_id="ffo-2",
        external_operation_id="order:456",
    )
    adapter = FakeAdapter(on_call=lambda: clock.advance(3.8))

    result = cancel_labels(
        STORE_ID,
        [_request(), _request("label-2", "ffo-2")],
        repository=repository,
        adapter_loader=lambda _carrier: adapter,
        clock=clock,
    )

    assert len(adapter.calls) == 1
    assert result.http_status == 207
    assert result.items[0].approved
    assert result.items[1].reason_code == "CARRIER_SYSTEM_ERROR"


def test_si_falla_checkpoint_despues_del_cancel_queda_revision_manual():
    repository = MemoryRepository()
    repository.confirm_error = RuntimeError("db down")
    adapter = FakeAdapter()

    result = cancel_labels(
        STORE_ID,
        [_request()],
        repository=repository,
        adapter_loader=lambda _carrier: adapter,
    )

    assert len(adapter.calls) == 1
    assert repository.states[LABEL_ID] == "manual"
    assert result.http_status == 409
    assert result.items[0].requires_manual_review


def test_replay_ya_confirmado_no_vuelve_a_llamar_al_carrier():
    repository = MemoryRepository()
    repository.states[LABEL_ID] = "confirmed"
    adapter = FakeAdapter()

    result = cancel_labels(
        STORE_ID,
        [_request()],
        repository=repository,
        adapter_loader=lambda _carrier: adapter,
    )

    assert adapter.calls == []
    assert result.http_status == 204


@pytest.mark.parametrize(
    "items",
    [
        [],
        [{"label_id": "", "fulfillment_order_id": FULFILLMENT_ID}],
        [
            {"label_id": LABEL_ID, "fulfillment_order_id": FULFILLMENT_ID},
            {"label_id": LABEL_ID, "fulfillment_order_id": FULFILLMENT_ID},
        ],
    ],
)
def test_rechaza_lotes_invalidos_antes_de_resolver(items):
    repository = MemoryRepository()

    with pytest.raises(LabelCancelContractError):
        cancel_labels(STORE_ID, items, repository=repository)

    assert repository.events == []


def test_repositorio_postgres_resuelve_carrier_desde_snapshot(monkeypatch):
    executed = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params):
            executed.append((" ".join(sql.split()), params))

        def fetchone(self):
            return {
                "store_id": STORE_ID,
                "label_id": LABEL_ID,
                "fulfillment_order_id": FULFILLMENT_ID,
                "external_operation_id": EXTERNAL_ID,
                "carrier_id": "OCA",
            }

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor()

    monkeypatch.setattr(
        "servicios.tiendanube_label_cancel.get_conn",
        lambda: Conn(),
    )

    target = PostgresLabelCancelRepository().lookup(STORE_ID, LABEL_ID)

    assert target == _target()
    assert "LEFT JOIN tiendanube_rate_quote_snapshots" in executed[0][0]
    assert executed[0][1] == (STORE_ID, LABEL_ID)


def test_confirmacion_durable_revoca_token_y_documento_en_misma_transaccion(
    monkeypatch,
):
    executed = []

    class Cursor:
        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params):
            executed.append((" ".join(sql.split()), params))

    class Conn:
        commits = 0
        rollbacks = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor()

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

    conn = Conn()
    monkeypatch.setattr(
        "servicios.tiendanube_label_cancel.get_conn",
        lambda: conn,
    )

    PostgresLabelCancelRepository().mark_confirmed(_target())

    sql = "\n".join(query for query, _params in executed)
    assert "UPDATE tiendanube_label_outbox" in sql
    assert "download_token_hash = NULL" in sql
    assert "UPDATE tiendanube_label_documents" in sql
    assert "COALESCE(revocada_en, now())" in sql
    assert conn.commits == 1
    assert conn.rollbacks == 0


class _MemoryPostgresRepository(MemoryRepository, PostgresLabelCancelRepository):
    """Activa el guard automatico sin usar PostgreSQL para los checkpoints."""


class _GuardCursor:
    def __init__(self, events, acquired):
        self.events = events
        self.acquired = acquired
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
        else:
            self.events.append(("guard", "try_lock"))

    def fetchone(self):
        return {"acquired": self.acquired}


class _GuardConnection:
    def __init__(self, cursor):
        self.cursor_instance = cursor
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1


def test_guard_postgres_ocupado_rechaza_sin_lookup_claim_ni_carrier(monkeypatch):
    from servicios import tiendanube_label_cancel as cancel_service

    repository = _MemoryPostgresRepository()
    cursor = _GuardCursor(repository.events, acquired=False)
    conn = _GuardConnection(cursor)
    adapter = FakeAdapter()
    monkeypatch.setattr(cancel_service, "get_conn", lambda: conn)

    result = cancel_labels(
        STORE_ID,
        [_request()],
        repository=repository,
        adapter_loader=lambda _carrier: adapter,
    )

    assert result.http_status == 409
    assert result.items[0].reason_code == "CARRIER_SYSTEM_ERROR"
    assert repository.events == [("guard", "try_lock")]
    assert repository.states == {}
    assert adapter.calls == []
    lock_sql, lock_params = cursor.commands[0]
    assert "pg_try_advisory_lock" in lock_sql
    assert "hashtextextended(%s, 0)" in lock_sql
    assert lock_params == ("tauro:tiendanube:123456.tiendanube",)
    assert not any("pg_advisory_unlock" in sql for sql, _ in cursor.commands)


def test_guard_postgres_cubre_lookup_carrier_y_checkpoint_y_libera(monkeypatch):
    from servicios import tiendanube_label_cancel as cancel_service

    repository = _MemoryPostgresRepository()
    cursor = _GuardCursor(repository.events, acquired=True)
    conn = _GuardConnection(cursor)
    adapter = FakeAdapter(on_call=lambda: repository.events.append(("carrier", "cancel")))
    monkeypatch.setattr(cancel_service, "get_conn", lambda: conn)

    result = cancel_labels(
        STORE_ID,
        [_request()],
        repository=repository,
        adapter_loader=lambda _carrier: adapter,
    )

    assert result.http_status == 204
    ordered = [
        ("guard", "try_lock"),
        ("lookup", STORE_ID, LABEL_ID),
        ("claim", LABEL_ID, FULFILLMENT_ID),
        ("carrier", "cancel"),
        ("confirmed", LABEL_ID),
        ("guard", "unlock"),
    ]
    assert [repository.events.index(event) for event in ordered] == sorted(
        repository.events.index(event) for event in ordered
    )
    unlock_sql, unlock_params = cursor.commands[-1]
    assert "pg_advisory_unlock" in unlock_sql
    assert "hashtextextended(%s, 0)" in unlock_sql
    assert unlock_params == ("tauro:tiendanube:123456.tiendanube",)
    assert conn.commits == 2


def test_repositorio_memory_admite_guard_inyectado():
    repository = MemoryRepository()
    adapter = FakeAdapter()
    guard_events = []

    @contextmanager
    def injected_guard(store_id):
        guard_events.append(("enter", store_id))
        try:
            yield True
        finally:
            guard_events.append(("exit", store_id))

    result = cancel_labels(
        STORE_ID,
        [_request()],
        repository=repository,
        adapter_loader=lambda _carrier: adapter,
        execution_guard=injected_guard,
    )

    assert result.http_status == 204
    assert guard_events == [("enter", STORE_ID), ("exit", STORE_ID)]
