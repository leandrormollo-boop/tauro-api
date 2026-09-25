"""Worker durable para generar etiquetas Tiendanube con OCA.

La emisión está separada en tres checkpoints explícitos::

    CREATE_SHIPMENT -> FETCH_LABEL -> PUBLISH

Una orden OCA confirmada se persiste antes de pedir el PDF. De ese modo, un
reinicio posterior nunca vuelve a ejecutar el alta. Un resultado incierto del
alta queda para revisión manual y no se reintenta automáticamente.

El módulo no habilita ningún flag de producción. ``labels_execution_ready`` y
los gates propios del adapter OCA siguen siendo obligatorios.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Callable, ContextManager, Iterator, Mapping, Protocol
from urllib.parse import quote

from core.database import get_conn
from servicios.carrier_adapter import (
    CarrierAdapter,
    OperationState,
    ShipmentResult,
    adapter_for,
)
from servicios.carrier_contract import Capacidad
from servicios.oca_adapter import (
    OCAConfigurationError,
    OCAOutcomeUnknown,
    OCAUnavailableError,
    OCAUnsupportedOperation,
)
from servicios.tiendanube_labels import (
    LabelsUnavailableError,
    _ensure_tables,
    labels_execution_ready,
)
from servicios.tiendanube_rate_quotes import (
    RateQuoteContractError,
    RateQuoteNotFoundError,
    RateQuoteSnapshotError,
    resolver_referencia,
)


MAX_ATTEMPTS = 8
CLAIM_STALE_MINUTES = 10
MAX_PDF_BYTES = 10_000_000
_REFERENCE_RE = re.compile(
    r"^tauro:([a-z0-9_-]{2,32}):(tnq_[0-9a-f]{64})$"
)
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_worker_tables_ready = False


class LabelStage(StrEnum):
    CREATE_SHIPMENT = "CREATE_SHIPMENT"
    FETCH_LABEL = "FETCH_LABEL"
    PUBLISH = "PUBLISH"
    DONE = "DONE"


class LabelExecutionError(RuntimeError):
    """Error seguro: su texto nunca incluye payload, direcciones o tokens."""

    code = "LABEL_EXECUTION_ERROR"


class LabelRetryableError(LabelExecutionError):
    """La etapa es idempotente y puede volver a intentarse con backoff."""

    code = "LABEL_RETRYABLE"


class LabelPermanentError(LabelExecutionError):
    """El contrato es inválido y un reintento no lo corregirá."""

    code = "LABEL_PERMANENT"


class LabelUncertainError(LabelExecutionError):
    """Una escritura externa pudo ocurrir; exige conciliación manual."""

    code = "LABEL_OUTCOME_UNKNOWN"


class LabelCheckpointError(LabelUncertainError):
    code = "LABEL_CREATE_CHECKPOINT_UNKNOWN"


@dataclass(frozen=True)
class LabelTask:
    id: int
    store_id: str
    label_id: str
    fulfillment_order_id: str
    payload: Mapping[str, Any]
    attempts: int
    claim_id: str
    carrier_id: str
    rate_quote_snapshot_id: str
    stage: LabelStage
    order_id: str = ""
    external_operation_id: str = ""
    tracking_number: str = ""
    document_key: str = ""
    document_url: str = ""
    document_size: int = 0

    @property
    def idempotency_key(self) -> str:
        raw = f"{self.store_id}|{self.label_id}|GENERATE".encode("utf-8")
        return "tn-label-" + hashlib.sha256(raw).hexdigest()[:48]


@dataclass(frozen=True)
class DurableDocument:
    key: str
    download_url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class WorkerResult:
    processed: int = 0
    completed: int = 0
    retried: int = 0
    failed: int = 0
    manual_review: int = 0
    stale_claims: int = 0
    ready: bool = False


class LabelWorkerRepository(Protocol):
    """Persistencia con compare-and-set por ``claim_id`` y etapa."""

    def recover_stale_claims(self) -> int: ...

    def claim_next(self) -> Mapping[str, Any] | None: ...

    def checkpoint_created(
        self, task: LabelTask, outcome: ShipmentResult
    ) -> LabelTask: ...

    def checkpoint_document(
        self, task: LabelTask, document: DurableDocument
    ) -> LabelTask: ...

    def complete(self, task: LabelTask) -> bool: ...

    def retry(self, task: LabelTask, code: str) -> bool: ...

    def fail(self, task: LabelTask, code: str) -> bool: ...

    def manual_review(self, task: LabelTask, code: str) -> bool: ...


class LabelDocumentStore(Protocol):
    """``persist`` debe ser idempotente para la clave estable de la tarea."""

    def persist(self, task: LabelTask, pdf: bytes) -> DurableDocument: ...


class LabelPublisher(Protocol):
    """Publica READY_TO_DOWNLOAD de forma idempotente."""

    def publish(self, task: LabelTask, document: DurableDocument) -> None: ...


SnapshotResolver = Callable[[LabelTask], Mapping[str, Any]]
AdapterLoader = Callable[[str], CarrierAdapter]
ExecutionGuard = Callable[[LabelTask], ContextManager[None]]


def _safe_code(error: BaseException) -> str:
    code = str(getattr(error, "code", "") or type(error).__name__).upper()
    code = re.sub(r"[^A-Z0-9_-]", "_", code)
    return (code or "LABEL_ERROR")[:80]


def _reference(payload: Mapping[str, Any]) -> tuple[str, str]:
    fulfillment = payload.get("fulfillment_order_info")
    shipping = fulfillment.get("shipping") if isinstance(fulfillment, Mapping) else None
    option = shipping.get("option") if isinstance(shipping, Mapping) else None
    raw = option.get("reference") if isinstance(option, Mapping) else None
    match = _REFERENCE_RE.fullmatch(str(raw or "").strip())
    if not match:
        raise LabelPermanentError("La etiqueta no contiene una tarifa TAURO válida.")
    return match.group(1), match.group(2)


def _task(row: Mapping[str, Any]) -> LabelTask:
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        raise LabelPermanentError("El snapshot de la etiqueta no es válido.")
    carrier_id, reference_snapshot = _reference(payload)
    snapshot_id = str(row.get("rate_quote_snapshot_id") or "").strip()
    if snapshot_id != reference_snapshot:
        raise LabelPermanentError("La etiqueta no coincide con la tarifa congelada.")
    try:
        stage = LabelStage(str(row.get("stage") or ""))
    except ValueError:
        raise LabelPermanentError("La etapa durable de la etiqueta no es válida.") from None
    task = LabelTask(
        id=int(row["id"]),
        store_id=str(row["store_id"]),
        label_id=str(row["label_id"]),
        fulfillment_order_id=str(row["fulfillment_order_id"]),
        payload=payload,
        attempts=int(row.get("intentos") or 0),
        claim_id=str(row["claim_id"]),
        carrier_id=carrier_id,
        rate_quote_snapshot_id=snapshot_id,
        stage=stage,
        order_id=str(row.get("order_id") or ""),
        external_operation_id=str(row.get("external_operation_id") or ""),
        tracking_number=str(row.get("tracking_number") or ""),
        document_key=str(row.get("document_key") or ""),
        document_url=str(row.get("document_url") or ""),
        document_size=int(row.get("document_size") or 0),
    )
    if stage in {LabelStage.FETCH_LABEL, LabelStage.PUBLISH, LabelStage.DONE}:
        if not task.external_operation_id:
            raise LabelPermanentError("Falta el identificador durable del operador.")
    if stage in {LabelStage.PUBLISH, LabelStage.DONE}:
        if not task.document_key or not task.document_url or task.document_size <= 0:
            raise LabelPermanentError("Falta el documento durable de la etiqueta.")
    return task


def _fallback_task(row: Mapping[str, Any]) -> LabelTask:
    try:
        stage = LabelStage(str(row.get("stage") or LabelStage.CREATE_SHIPMENT))
    except ValueError:
        stage = LabelStage.CREATE_SHIPMENT
    return LabelTask(
        id=int(row["id"]),
        store_id=str(row.get("store_id") or ""),
        label_id=str(row.get("label_id") or ""),
        fulfillment_order_id=str(row.get("fulfillment_order_id") or ""),
        payload={},
        attempts=int(row.get("intentos") or 0),
        claim_id=str(row.get("claim_id") or ""),
        carrier_id="",
        rate_quote_snapshot_id=str(row.get("rate_quote_snapshot_id") or ""),
        stage=stage,
        order_id=str(row.get("order_id") or ""),
    )


def _ensure_worker_tables() -> None:
    """Verifica el schema del worker; las migraciones ocurren antes del tráfico."""
    global _worker_tables_ready
    if _worker_tables_ready:
        return

    _ensure_tables()
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        to_regclass(
                            'tiendanube_label_execution'
                        ) IS NOT NULL
                        AND to_regclass(
                            'tiendanube_label_documents'
                        ) IS NOT NULL
                        AND EXISTS (
                            SELECT 1
                              FROM pg_attribute a
                             WHERE a.attrelid = to_regclass(
                                       'tiendanube_label_outbox'
                                   )
                               AND a.attname = 'claim_id'
                               AND NOT a.attisdropped
                        )
                        AND EXISTS (
                            SELECT 1
                              FROM pg_attribute a
                             WHERE a.attrelid = to_regclass(
                                       'tiendanube_label_outbox'
                                   )
                               AND a.attname = 'claimed_at'
                               AND NOT a.attisdropped
                        )
                        AS schema_ready
                    """
                )
                row = cur.fetchone()
            conn.commit()
    except Exception as exc:
        raise LabelsUnavailableError(
            "No se pudo verificar el esquema del worker de Labels."
        ) from exc

    ready = bool(
        row.get("schema_ready")
        if hasattr(row, "get")
        else row[0] if row else False
    )
    if not ready:
        raise LabelsUnavailableError(
            "El worker de Labels requiere aplicar sql/schema.sql "
            "antes de habilitar tráfico."
        )
    _worker_tables_ready = True


class PostgresLabelWorkerRepository:
    """Claims atómicos y checkpoints protegidos por etapa + ``claim_id``."""

    def recover_stale_claims(self) -> int:
        _ensure_worker_tables()
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE tiendanube_label_outbox o
                       SET estado = CASE
                               WHEN e.stage = 'CREATE_SHIPMENT'
                                   THEN 'VERIFICAR_MANUAL'
                               ELSE 'PENDIENTE'
                           END,
                           ultimo_error_codigo = CASE
                               WHEN e.stage = 'CREATE_SHIPMENT'
                                   THEN 'CREATE_CLAIM_EXPIRED'
                               ELSE 'SAFE_STAGE_CLAIM_EXPIRED'
                           END,
                           proximo_intento_en = now(),
                           claim_id = NULL,
                           claimed_at = NULL,
                           actualizada_en = now()
                      FROM tiendanube_label_execution e
                     WHERE e.outbox_id = o.id
                       AND o.operacion = 'GENERATE'
                       AND o.estado = 'PROCESANDO'
                       AND o.claimed_at < now() - (%s * INTERVAL '1 minute')
                    """,
                    (CLAIM_STALE_MINUTES,),
                )
                count = max(int(cur.rowcount or 0), 0)
                cur.execute(
                    """
                    UPDATE tiendanube_label_execution e
                       SET claim_id = NULL, claimed_at = NULL,
                           actualizada_en = now()
                      FROM tiendanube_label_outbox o
                     WHERE o.id = e.outbox_id
                       AND e.claim_id IS NOT NULL
                       AND o.claim_id IS NULL
                    """
                )
            conn.commit()
        return count

    def claim_next(self) -> Mapping[str, Any] | None:
        _ensure_worker_tables()
        claim_id = secrets.token_urlsafe(18)
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO tiendanube_label_execution
                        (outbox_id, store_id, label_id, stage)
                    SELECT o.id, o.store_id, o.label_id, 'CREATE_SHIPMENT'
                      FROM tiendanube_label_outbox o
                     WHERE o.operacion = 'GENERATE'
                       AND o.payload_complete = TRUE
                    ON CONFLICT (outbox_id) DO NOTHING
                    """
                )
                cur.execute(
                    """
                    WITH candidate AS (
                        SELECT o.id
                          FROM tiendanube_label_outbox o
                          JOIN tiendanube_label_execution e
                            ON e.outbox_id = o.id
                         WHERE o.operacion = 'GENERATE'
                           AND o.estado = 'PENDIENTE'
                           AND o.payload_complete = TRUE
                           AND o.intentos < %s
                           AND o.proximo_intento_en <= now()
                         ORDER BY o.creada_en, o.id
                         FOR UPDATE OF o SKIP LOCKED
                         LIMIT 1
                    ), claimed AS (
                        UPDATE tiendanube_label_outbox o
                           SET estado = 'PROCESANDO',
                               intentos = o.intentos + 1,
                               claim_id = %s,
                               claimed_at = now(),
                               actualizada_en = now()
                          FROM candidate c
                         WHERE o.id = c.id
                        RETURNING o.*
                    ), execution_claimed AS (
                        UPDATE tiendanube_label_execution e
                           SET claim_id = %s, claimed_at = now(),
                               actualizada_en = now()
                          FROM claimed c
                         WHERE e.outbox_id = c.id
                        RETURNING e.*
                    )
                    SELECT c.id, c.store_id, c.label_id, c.intentos,
                           c.claim_id, l.fulfillment_order_id,
                           l.rate_quote_snapshot_id, l.order_id,
                           l.generate_payload AS payload,
                           e.stage, e.external_operation_id,
                           e.tracking_number, e.document_key,
                           e.document_url, e.document_size
                      FROM claimed c
                      JOIN execution_claimed e ON e.outbox_id = c.id
                      JOIN tiendanube_labels l
                        ON l.store_id = c.store_id AND l.label_id = c.label_id
                    """,
                    (MAX_ATTEMPTS, claim_id, claim_id),
                )
                row = cur.fetchone()
            conn.commit()
        return dict(row) if row else None

    @staticmethod
    def _guard(task: LabelTask, expected_stage: LabelStage) -> tuple:
        return (task.id, task.claim_id, expected_stage.value)

    def checkpoint_created(
        self, task: LabelTask, outcome: ShipmentResult
    ) -> LabelTask:
        external_id = str(outcome.external_id or "").strip()
        tracking = str(outcome.tracking or "").strip()
        persisted_stage = LabelStage.FETCH_LABEL
        persisted_document_key = ""
        persisted_document_url = ""
        persisted_document_size = 0
        with get_conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE tiendanube_label_execution e
                           SET stage = 'FETCH_LABEL',
                               external_operation_id = %s,
                               tracking_number = %s,
                               actualizada_en = now()
                          FROM tiendanube_label_outbox o
                         WHERE e.outbox_id = o.id
                           AND e.outbox_id = %s
                           AND o.estado = 'PROCESANDO'
                           AND o.claim_id = %s
                           AND e.claim_id = %s
                           AND e.stage = %s
                        """,
                        (
                            external_id,
                            tracking or None,
                            task.id,
                            task.claim_id,
                            task.claim_id,
                            LabelStage.CREATE_SHIPMENT.value,
                        ),
                    )
                    advanced = cur.rowcount == 1
                    if not advanced:
                        # Replay posterior a un commit cuyo ACK local se
                        # perdió. Nunca retrocede la etapa ni repite el alta.
                        cur.execute(
                            """
                            SELECT stage, external_operation_id, tracking_number,
                                   document_key, document_url, document_size
                              FROM tiendanube_label_execution
                             WHERE outbox_id = %s
                            """,
                            (task.id,),
                        )
                        existing = cur.fetchone()
                        existing = dict(existing) if existing else {}
                        if (
                            str(existing.get("stage") or "")
                            not in {"FETCH_LABEL", "PUBLISH", "DONE"}
                            or str(existing.get("external_operation_id") or "")
                            != external_id
                            or str(existing.get("tracking_number") or "") != tracking
                        ):
                            raise LabelCheckpointError(
                                "No se pudo fijar el alta OCA de forma durable."
                            )
                        persisted_stage = LabelStage(str(existing["stage"]))
                        persisted_document_key = str(
                            existing.get("document_key") or ""
                        )
                        persisted_document_url = str(
                            existing.get("document_url") or ""
                        )
                        persisted_document_size = int(
                            existing.get("document_size") or 0
                        )
                    else:
                        cur.execute(
                            """
                            UPDATE tiendanube_labels
                               SET estado = 'EMITIDA_EN_COURIER',
                                   external_operation_id = %s,
                                   tracking_number = %s,
                                   actualizada_en = now()
                             WHERE store_id = %s AND label_id = %s
                            """,
                            (
                                external_id,
                                tracking or None,
                                task.store_id,
                                task.label_id,
                            ),
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return replace(
            task,
            stage=persisted_stage,
            external_operation_id=external_id,
            tracking_number=tracking,
            document_key=persisted_document_key,
            document_url=persisted_document_url,
            document_size=persisted_document_size,
        )

    def checkpoint_document(
        self, task: LabelTask, document: DurableDocument
    ) -> LabelTask:
        persisted_stage = LabelStage.PUBLISH
        with get_conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE tiendanube_label_execution e
                           SET stage = 'PUBLISH', document_key = %s,
                               document_url = %s, document_size = %s,
                               actualizada_en = now()
                          FROM tiendanube_label_outbox o
                         WHERE e.outbox_id = o.id
                           AND e.outbox_id = %s
                           AND o.estado = 'PROCESANDO'
                           AND o.claim_id = %s
                           AND e.claim_id = %s
                           AND e.stage = 'FETCH_LABEL'
                        """,
                        (
                            document.key,
                            document.download_url,
                            document.size,
                            task.id,
                            task.claim_id,
                            task.claim_id,
                        ),
                    )
                    advanced = cur.rowcount == 1
                    if not advanced:
                        cur.execute(
                            """
                            SELECT stage, document_key, document_url, document_size
                              FROM tiendanube_label_execution
                             WHERE outbox_id = %s
                            """,
                            (task.id,),
                        )
                        existing = cur.fetchone()
                        existing = dict(existing) if existing else {}
                        if (
                            str(existing.get("stage") or "")
                            not in {"PUBLISH", "DONE"}
                            or str(existing.get("document_key") or "") != document.key
                            or str(existing.get("document_url") or "")
                            != document.download_url
                            or int(existing.get("document_size") or 0) != document.size
                        ):
                            raise LabelRetryableError(
                                "No se pudo fijar el documento durable."
                            )
                        persisted_stage = LabelStage(str(existing["stage"]))
                    else:
                        cur.execute(
                            """
                            UPDATE tiendanube_labels
                               SET estado = 'ETIQUETA_PERSISTIDA',
                                   actualizada_en = now()
                             WHERE store_id = %s AND label_id = %s
                            """,
                            (task.store_id, task.label_id),
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return replace(
            task,
            stage=persisted_stage,
            document_key=document.key,
            document_url=document.download_url,
            document_size=document.size,
        )

    def complete(self, task: LabelTask) -> bool:
        with get_conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE tiendanube_label_execution e
                           SET stage = 'DONE', claim_id = NULL, claimed_at = NULL,
                               actualizada_en = now()
                          FROM tiendanube_label_outbox o
                         WHERE e.outbox_id = o.id
                           AND e.outbox_id = %s
                           AND o.estado = 'PROCESANDO'
                           AND o.claim_id = %s
                           AND e.claim_id = %s
                           AND e.stage = 'PUBLISH'
                        """,
                        (task.id, task.claim_id, task.claim_id),
                    )
                    if cur.rowcount != 1:
                        cur.execute(
                            """
                            SELECT e.stage, o.estado
                              FROM tiendanube_label_execution e
                              JOIN tiendanube_label_outbox o ON o.id = e.outbox_id
                             WHERE e.outbox_id = %s
                            """,
                            (task.id,),
                        )
                        existing = cur.fetchone()
                        existing = dict(existing) if existing else {}
                        conn.rollback()
                        return (
                            str(existing.get("stage") or "") == "DONE"
                            and str(existing.get("estado") or "") == "COMPLETADO"
                        )
                    cur.execute(
                        """
                        UPDATE tiendanube_label_outbox
                           SET estado = 'COMPLETADO', procesada_en = now(),
                               ultimo_error_codigo = NULL,
                               payload = '{}'::jsonb,
                               payload_complete = TRUE,
                               claim_id = NULL, claimed_at = NULL,
                               actualizada_en = now()
                         WHERE id = %s AND estado = 'PROCESANDO'
                           AND claim_id = %s
                        """,
                        (task.id, task.claim_id),
                    )
                    if cur.rowcount != 1:
                        raise LabelRetryableError(
                            "No se pudo completar el outbox de la etiqueta."
                        )
                    cur.execute(
                        """
                        UPDATE tiendanube_labels
                           SET estado = 'READY_TO_DOWNLOAD',
                               generate_payload = NULL,
                               generate_payload_complete = TRUE,
                               actualizada_en = now()
                         WHERE store_id = %s AND label_id = %s
                        """,
                        (task.store_id, task.label_id),
                    )
                    if cur.rowcount != 1:
                        raise LabelRetryableError(
                            "No se pudo cerrar la etiqueta publicada."
                        )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def _finish(
        self,
        task: LabelTask,
        *,
        state: str,
        code: str,
        retry: bool = False,
    ) -> bool:
        delay_seconds = min(3600, 30 * (2 ** max(task.attempts - 1, 0)))
        with get_conn() as conn:
            with conn.cursor() as cur:
                # La ejecucion principal ya libero su lock de sesion al llegar
                # a este cierre. Lo retomamos transaccionalmente para que una
                # cancelacion no pueda confirmar su tombstone entre el cambio
                # del outbox GENERATE y el estado canonico de la etiqueta.
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"tauro:tiendanube:{task.store_id}.tiendanube",),
                )
                cur.execute(
                    """
                    UPDATE tiendanube_label_outbox
                       SET estado = %s, ultimo_error_codigo = %s,
                           proximo_intento_en = CASE
                               WHEN %s THEN now() + (%s * INTERVAL '1 second')
                               ELSE proximo_intento_en
                           END,
                           claim_id = NULL, claimed_at = NULL,
                           actualizada_en = now()
                     WHERE id = %s AND estado = 'PROCESANDO' AND claim_id = %s
                    """,
                    (
                        state,
                        code[:80],
                        retry,
                        delay_seconds,
                        task.id,
                        task.claim_id,
                    ),
                )
                updated = cur.rowcount == 1
                if updated:
                    cur.execute(
                        """
                        UPDATE tiendanube_label_execution
                           SET claim_id = NULL, claimed_at = NULL,
                               actualizada_en = now()
                         WHERE outbox_id = %s AND claim_id = %s
                        """,
                        (task.id, task.claim_id),
                    )
                    cur.execute(
                        """
                        UPDATE tiendanube_labels label
                           SET estado = %s, actualizada_en = now()
                         WHERE label.store_id = %s AND label.label_id = %s
                           AND NOT EXISTS (
                               SELECT 1
                                 FROM tiendanube_label_outbox cancellation
                                WHERE cancellation.store_id = label.store_id
                                  AND cancellation.label_id = label.label_id
                                  AND cancellation.operacion = 'CANCEL'
                                  AND cancellation.estado IN (
                                      'CANCELACION_ENVIADA',
                                      'CANCELACION_CONFIRMADA',
                                      'CANCELACION_REVISION_MANUAL'
                                  )
                           )
                        """,
                        (state, task.store_id, task.label_id),
                    )
            conn.commit()
        return updated

    def retry(self, task: LabelTask, code: str) -> bool:
        if task.attempts >= MAX_ATTEMPTS:
            return self.fail(task, "MAX_ATTEMPTS")
        return self._finish(task, state="PENDIENTE", code=code, retry=True)

    def fail(self, task: LabelTask, code: str) -> bool:
        return self._finish(task, state="FALLIDO", code=code)

    def manual_review(self, task: LabelTask, code: str) -> bool:
        return self._finish(task, state="VERIFICAR_MANUAL", code=code)


def _download_secret() -> bytes:
    value = str(os.getenv("TIENDANUBE_LABEL_DOWNLOAD_SECRET") or "").strip()
    if len(value) < 32:
        raise LabelPermanentError("Falta configurar el secreto de documentos.")
    return value.encode("utf-8")


def _document_token(store_id: str, label_id: str, digest: str) -> str:
    message = f"{store_id}|{label_id}|{digest}".encode("utf-8")
    return hmac.new(_download_secret(), message, hashlib.sha256).hexdigest()


class PostgresLabelDocumentStore:
    """Conserva el PDF antes de exponerlo a Tiendanube."""

    def persist(self, task: LabelTask, pdf: bytes) -> DurableDocument:
        if not isinstance(pdf, bytes) or not pdf.startswith(b"%PDF-"):
            raise LabelPermanentError("OCA no devolvió un PDF de etiqueta válido.")
        if not 0 < len(pdf) <= MAX_PDF_BYTES:
            raise LabelPermanentError("La etiqueta PDF supera el límite permitido.")
        _ensure_worker_tables()
        digest = hashlib.sha256(pdf).hexdigest()
        key = f"tn-label/{task.store_id}/{task.label_id}/{digest}"
        token = _document_token(task.store_id, task.label_id, digest)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        base = str(os.getenv("BASE_URL") or "https://taurosolutions.ar").rstrip("/")
        if not base.startswith("https://"):
            raise LabelPermanentError("La URL pública de documentos debe usar HTTPS.")
        url = (
            f"{base}/integraciones/tiendanube/shipping/labels/documents/"
            f"{quote(task.store_id, safe='')}/{quote(task.label_id, safe='')}"
            f"?token={token}"
        )
        with get_conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO tiendanube_label_documents
                            (store_id, label_id, document_key, pdf_sha256,
                             pdf_size, pdf_content, token_hash)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (store_id, label_id) DO NOTHING
                        """,
                        (
                            task.store_id,
                            task.label_id,
                            key,
                            digest,
                            len(pdf),
                            pdf,
                            token_hash,
                        ),
                    )
                    cur.execute(
                        """
                        SELECT document_key, pdf_sha256, pdf_size, token_hash, activa
                          FROM tiendanube_label_documents
                         WHERE store_id = %s AND label_id = %s
                         FOR UPDATE
                        """,
                        (task.store_id, task.label_id),
                    )
                    row = cur.fetchone()
                conn.commit()
            except LabelExecutionError:
                conn.rollback()
                raise
            except Exception as exc:
                conn.rollback()
                raise LabelRetryableError(
                    "No se pudo persistir el PDF de la etiqueta."
                ) from exc
        row = dict(row) if row else {}
        if (
            str(row.get("document_key") or "") != key
            or str(row.get("pdf_sha256") or "").strip() != digest
            or int(row.get("pdf_size") or 0) != len(pdf)
            or str(row.get("token_hash") or "").strip() != token_hash
            or not row.get("activa")
        ):
            raise LabelUncertainError("El documento durable está en conflicto.")
        return DurableDocument(key=key, download_url=url, size=len(pdf), sha256=digest)


def load_label_document(store_id: str, label_id: str, token: str) -> bytes | None:
    """Valida el token en tiempo constante; punto de uso del futuro endpoint."""
    _ensure_worker_tables()
    received = hashlib.sha256(str(token or "").encode("ascii", "ignore")).hexdigest()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pdf_content, token_hash
                  FROM tiendanube_label_documents
                 WHERE store_id = %s AND label_id = %s AND activa = TRUE
                """,
                (str(store_id), str(label_id)),
            )
            row = cur.fetchone()
    if not row or not hmac.compare_digest(
        received, str(row.get("token_hash") or "").strip()
    ):
        return None
    return bytes(row["pdf_content"])


def revoke_label_document(store_id: str, label_id: str) -> bool:
    """Revoca la URL de forma idempotente y conserva la primera revocación."""
    _ensure_worker_tables()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tiendanube_label_documents
                   SET activa = FALSE,
                       revocada_en = COALESCE(revocada_en, now())
                 WHERE store_id = %s AND label_id = %s
                """,
                (str(store_id), str(label_id)),
            )
            exists = cur.rowcount == 1
        conn.commit()
    return exists


class TiendanubeLabelPublisher:
    """Publica y reconcilia ``READY_TO_DOWNLOAD`` contra el estado remoto.

    ``PUBLISH`` ya es la intención durable local. Si Tiendanube aceptó el
    PATCH y el proceso perdió la respuesta o cayó antes del checkpoint, un
    reintento puede recibir 4xx porque la etiqueta avanzó a READY_TO_USE o
    DOWNLOADED. En ese caso sólo se acepta éxito después de leer el FFO y
    verificar identidad, tracking y documento; nunca por el status aislado.
    """

    _PUBLISHED_STATUSES = {"READY_TO_DOWNLOAD", "READY_TO_USE", "DOWNLOADED"}

    @staticmethod
    def _response_json(response) -> Mapping[str, Any] | None:
        if response is None or getattr(response, "status_code", 0) != 200:
            return None
        try:
            payload = response.json()
        except Exception:
            return None
        return payload if isinstance(payload, Mapping) else None

    @classmethod
    def _remote_confirms(
        cls,
        task: LabelTask,
        document: DurableDocument,
        fulfillment: Mapping[str, Any],
    ) -> bool:
        if str(fulfillment.get("id") or "") != task.fulfillment_order_id:
            return False
        labels = fulfillment.get("labels")
        if not isinstance(labels, list):
            return False
        remote = next(
            (
                item
                for item in labels
                if isinstance(item, Mapping)
                and str(item.get("id") or "") == task.label_id
            ),
            None,
        )
        if remote is None or str(remote.get("status") or "") not in cls._PUBLISHED_STATUSES:
            return False
        if task.tracking_number:
            tracking = remote.get("tracking_info")
            if (
                not isinstance(tracking, Mapping)
                or str(tracking.get("code") or "") != task.tracking_number
            ):
                return False
        documents = remote.get("documents")
        if not isinstance(documents, list):
            return False
        expected_name = f"tauro-{task.label_id}.pdf"

        def matches_document(item: object) -> bool:
            if not isinstance(item, Mapping):
                return False
            try:
                size = int(item.get("size") or 0)
            except (TypeError, ValueError, OverflowError):
                return False
            return (
                str(item.get("file_name") or "") == expected_name
                and str(item.get("type") or "") == "LABEL"
                and str(item.get("format") or "") == "PDF"
                and size == document.size
            )

        return any(
            matches_document(item) for item in documents
        )

    @classmethod
    def _reconcile(
        cls,
        *,
        api,
        token: str,
        task: LabelTask,
        document: DurableDocument,
    ) -> tuple[bool, bool]:
        """Devuelve ``(confirmada, lectura_concluyente)``."""
        if not task.order_id:
            return False, False
        response = api(
            task.store_id,
            token,
            "GET",
            f"orders/{task.order_id}/fulfillment-orders/{task.fulfillment_order_id}",
        )
        payload = cls._response_json(response)
        if payload is not None:
            return cls._remote_confirms(task, document, payload), True
        status = int(getattr(response, "status_code", 0) or 0)
        # 401/403/404/422 son lecturas concluyentes del recurso/permiso. Los
        # timeouts, 429 y 5xx no autorizan a declarar conflicto.
        return False, status in {401, 403, 404, 422}

    def publish(self, task: LabelTask, document: DurableDocument) -> None:
        from servicios.tiendanube_app import _api, instalacion

        installation = instalacion(task.store_id) or {}
        token = str(installation.get("access_token") or "")
        if installation.get("estado") != "ACTIVA" or not token:
            raise LabelPermanentError("La instalación Tiendanube no está activa.")
        payload: dict[str, Any] = {
            "status": "READY_TO_DOWNLOAD",
            "documents": [
                {
                    "file_name": f"tauro-{task.label_id}.pdf",
                    "type": "LABEL",
                    "format": "PDF",
                    "download_url_from_app": document.download_url,
                    "size": document.size,
                }
            ],
        }
        if task.tracking_number:
            payload["tracking_info"] = {"code": task.tracking_number, "url": None}
        response = _api(
            task.store_id,
            token,
            "PATCH",
            "fulfillment-orders/"
            f"{task.fulfillment_order_id}/labels/{task.label_id}",
            payload,
        )
        if response is not None and response.status_code == 200:
            return

        confirmed, conclusive = self._reconcile(
            api=_api,
            token=token,
            task=task,
            document=document,
        )
        if confirmed:
            return
        status = int(getattr(response, "status_code", 0) or 0)
        if conclusive and 400 <= status < 500 and status != 429:
            raise LabelPermanentError("Tiendanube rechazó la etiqueta generada.")
        raise LabelRetryableError("Tiendanube no pudo confirmar la etiqueta.")


def _default_adapter_loader(carrier_id: str) -> CarrierAdapter:
    try:
        return adapter_for(carrier_id, Capacidad.EMITIR)
    except (RuntimeError, ValueError) as exc:
        raise LabelRetryableError("El adapter nacional no está disponible.") from exc


def _default_snapshot_resolver(task: LabelTask) -> Mapping[str, Any]:
    from servicios.tiendanube_app import instalacion

    installation = instalacion(task.store_id) or {}
    customer_id = str(installation.get("cliente_id") or "").strip()
    if installation.get("estado") != "ACTIVA" or not customer_id:
        raise RateQuoteNotFoundError("La tienda no está vinculada a un cliente.")
    fulfillment = task.payload.get("fulfillment_order_info")
    shipping = fulfillment.get("shipping") if isinstance(fulfillment, Mapping) else None
    option = shipping.get("option") if isinstance(shipping, Mapping) else None
    reference = option.get("reference") if isinstance(option, Mapping) else None
    snapshot = resolver_referencia(
        task.store_id,
        customer_id,
        str(reference or ""),
        expected_carrier_id=task.carrier_id,
    )
    if str(snapshot.get("snapshot_id") or "") != task.rate_quote_snapshot_id:
        raise RateQuoteContractError("La tarifa vinculada no coincide.")
    return snapshot


def _plain_address(raw: Mapping[str, Any], *, contact: str = "") -> dict[str, Any]:
    province = raw.get("province")
    country = raw.get("country")
    result = dict(raw)
    result.update(
        {
            "cp": raw.get("zipcode") or raw.get("postal_code") or raw.get("cp"),
            "calle": raw.get("street") or raw.get("address") or raw.get("calle"),
            "nro": raw.get("number") or raw.get("numero") or raw.get("nro"),
            "piso": raw.get("floor") or raw.get("piso"),
            "localidad": raw.get("city") or raw.get("locality") or raw.get("localidad"),
            "provincia": (
                province.get("name") or province.get("code")
                if isinstance(province, Mapping)
                else province or raw.get("provincia")
            ),
            "pais": (
                country.get("code") or country.get("name")
                if isinstance(country, Mapping)
                else country or raw.get("pais")
            ),
        }
    )
    if contact:
        result["contacto"] = contact
    return result


def _shipment_from_snapshot(
    task: LabelTask, snapshot: Mapping[str, Any]
) -> dict[str, Any]:
    """Combina geometría congelada con PII recibida en ``generate``.

    Nunca usa ``line_items`` como paquetes: Tiendanube no documenta las
    unidades de ``unit_dimension`` y un producto no equivale a un bulto.
    """
    fulfillment = task.payload.get("fulfillment_order_info")
    if not isinstance(fulfillment, Mapping):
        raise LabelPermanentError("Falta el Fulfillment Order de la etiqueta.")
    assigned = fulfillment.get("assigned_location")
    origin_raw = assigned.get("address") if isinstance(assigned, Mapping) else None
    destination_raw = fulfillment.get("destination")
    recipient_raw = fulfillment.get("recipient")
    if not isinstance(origin_raw, Mapping) or not isinstance(destination_raw, Mapping):
        raise LabelPermanentError("Faltan las direcciones del Fulfillment Order.")
    if not isinstance(recipient_raw, Mapping):
        raise LabelPermanentError("Falta el destinatario del Fulfillment Order.")
    packages = snapshot.get("packages")
    if not isinstance(packages, list) or not packages:
        raise LabelPermanentError("La tarifa congelada no contiene bultos.")
    carrier_quote_id = str(snapshot.get("carrier_quote_id") or "").strip()
    if not _SAFE_ID_RE.fullmatch(carrier_quote_id):
        raise LabelPermanentError("La tarifa no contiene el quote del operador.")

    expected_zip = str(
        (snapshot.get("destination_route") or {}).get("postal_code")
        if isinstance(snapshot.get("destination_route"), Mapping)
        else ""
    )
    destination_zip = "".join(
        char
        for char in str(
            destination_raw.get("zipcode")
            or destination_raw.get("postal_code")
            or destination_raw.get("cp")
            or ""
        )
        if char.isdigit()
    )
    if expected_zip != destination_zip:
        raise LabelPermanentError("El destino cambió desde la cotización aceptada.")

    origin = _plain_address(
        origin_raw,
        contact=str(assigned.get("name") or "") if isinstance(assigned, Mapping) else "",
    )
    destination = _plain_address(destination_raw)
    recipient = dict(recipient_raw)
    # El adapter OCA acepta el contacto en recipient, pero calle y número
    # siempre provienen de destination, nunca del snapshot de tarifa.
    return {
        "origin": origin,
        "destination": destination,
        "recipient": recipient,
        "packages": [dict(package) for package in packages],
        "declared_value": snapshot.get("declared_value"),
        "declared_currency": snapshot.get("declared_currency"),
        "service_code": snapshot.get("service_code"),
        "origin_mode": snapshot.get("origin_mode"),
        "destination_mode": snapshot.get("destination_mode"),
    }


def _validate_create_outcome(outcome: ShipmentResult, carrier_id: str) -> None:
    if not isinstance(outcome, ShipmentResult) or outcome.carrier_id != carrier_id:
        raise LabelUncertainError("El operador devolvió un alta inválida.")
    if outcome.state == OperationState.ERROR_DEFINITIVO:
        raise LabelPermanentError("El operador rechazó la creación del envío.")
    if outcome.state == OperationState.INCIERTO:
        raise LabelUncertainError("El resultado del alta es incierto.")
    if outcome.state not in {OperationState.EMITIDO, OperationState.PENDIENTE}:
        raise LabelUncertainError("El operador devolvió un estado de alta inesperado.")
    if not str(outcome.external_id or "").strip() or not str(outcome.tracking or "").strip():
        raise LabelUncertainError("El alta no devolvió identificadores completos.")


def _document_from_task(task: LabelTask) -> DurableDocument:
    return DurableDocument(
        key=task.document_key,
        download_url=task.document_url,
        size=task.document_size,
        sha256="",
    )


def _noop_execution_guard(_task: LabelTask) -> ContextManager[None]:
    return nullcontext()


@contextmanager
def _postgres_execution_guard(task: LabelTask) -> Iterator[None]:
    """Serializa ejecución y redacción con un lock de sesión por tienda.

    La validación se confirma antes del acceso al carrier. El lock permanece
    ligado a la conexión durante toda ``_execute_task``; por eso una redacción
    que llega después espera a que terminen también los checkpoints durables.
    """
    lock_key = f"tauro:tiendanube:{task.store_id}.tiendanube"
    acquired = False
    with get_conn() as conn:
        try:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
                        (lock_key,),
                    )
                    acquired = True
                    cur.execute(
                        """
                        SELECT m.order_id
                          FROM tiendanube_label_outbox o
                          JOIN tiendanube_labels l
                            ON l.store_id = o.store_id
                           AND l.label_id = o.label_id
                          JOIN tiendanube_fulfillment_order_orders m
                            ON m.store_id = l.store_id
                           AND m.fulfillment_order_id = l.fulfillment_order_id
                           AND m.order_id = l.order_id
                         WHERE o.id = %s
                           AND o.store_id = %s
                           AND o.label_id = %s
                           AND o.operacion = 'GENERATE'
                           AND o.estado = 'PROCESANDO'
                           AND o.claim_id = %s
                           AND o.payload_complete = TRUE
                           AND l.fulfillment_order_id = %s
                           AND l.generate_payload_complete = TRUE
                           AND NOT EXISTS (
                               SELECT 1
                                 FROM tiendanube_label_outbox cancellation
                                WHERE cancellation.store_id = o.store_id
                                  AND cancellation.label_id = o.label_id
                                  AND cancellation.operacion = 'CANCEL'
                                  AND cancellation.estado IN (
                                      'CANCELACION_ENVIADA',
                                      'CANCELACION_CONFIRMADA',
                                      'CANCELACION_REVISION_MANUAL'
                                  )
                           )
                           AND NOT EXISTS (
                               SELECT 1
                                 FROM tiendanube_pedidos_redactados r
                                WHERE r.dominio = %s
                                  AND r.pedido_externo_id = m.order_id
                           )
                         LIMIT 1
                        """,
                        (
                            task.id,
                            task.store_id,
                            task.label_id,
                            task.claim_id,
                            task.fulfillment_order_id,
                            f"{task.store_id}.tiendanube",
                        ),
                    )
                    valid = cur.fetchone()
                # Libera locks de filas/MVCC, pero el advisory lock de sesión
                # continúa activo hasta el ``pg_advisory_unlock`` del finally.
                conn.commit()
            except Exception as exc:
                conn.rollback()
                raise LabelRetryableError(
                    "No se pudo revalidar el estado de la etiqueta."
                ) from exc

            if not valid:
                raise LabelPermanentError(
                    "La etiqueta ya no está habilitada para ejecución."
                )
            yield
        finally:
            if acquired:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                        (lock_key,),
                    )
                # El unlock es de sesión (no transaccional), pero cerrar esta
                # transacción evita devolver al pool una conexión en estado idle.
                conn.commit()


def _execute_task(
    task: LabelTask,
    *,
    repository: LabelWorkerRepository,
    adapter_loader: AdapterLoader,
    snapshot_resolver: SnapshotResolver,
    document_store: LabelDocumentStore,
    publisher: LabelPublisher,
) -> LabelTask:
    current = task
    if current.stage == LabelStage.CREATE_SHIPMENT:
        try:
            snapshot = snapshot_resolver(current)
        except (RateQuoteContractError, RateQuoteNotFoundError) as exc:
            raise LabelPermanentError("La tarifa vinculada no es válida.") from exc
        except RateQuoteSnapshotError as exc:
            raise LabelRetryableError("No se pudo verificar la tarifa vinculada.") from exc
        shipment = _shipment_from_snapshot(current, snapshot)
        quote_id = str(snapshot.get("carrier_quote_id") or "").strip()
        adapter = adapter_loader(current.carrier_id)
        try:
            outcome = adapter.create_shipment(
                quote_id,
                shipment,
                idempotency_key=current.idempotency_key,
            )
        except OCAOutcomeUnknown as exc:
            raise LabelUncertainError("El alta OCA requiere conciliación.") from exc
        except OCAUnavailableError as exc:
            # Es una escritura. El adapter actual convierte los fallos
            # post-envío a OCAOutcomeUnknown; ante una versión anterior se
            # conserva igualmente el criterio más seguro.
            raise LabelUncertainError("El alta OCA no tiene resultado cierto.") from exc
        except (OCAConfigurationError, OCAUnsupportedOperation, ValueError) as exc:
            raise LabelPermanentError("OCA rechazó el contrato del envío.") from exc
        except LabelExecutionError:
            raise
        except Exception as exc:
            raise LabelUncertainError("El alta pudo haber llegado a OCA.") from exc
        _validate_create_outcome(outcome, current.carrier_id)
        try:
            current = repository.checkpoint_created(current, outcome)
        except LabelCheckpointError:
            raise
        except Exception as exc:
            raise LabelCheckpointError(
                "No se pudo confirmar el checkpoint posterior al alta."
            ) from exc

    if current.stage == LabelStage.FETCH_LABEL:
        adapter = adapter_loader(current.carrier_id)
        try:
            outcome = adapter.get_label(current.external_operation_id)
        except OCAUnavailableError as exc:
            raise LabelRetryableError("OCA no pudo entregar el PDF todavía.") from exc
        except (OCAConfigurationError, OCAUnsupportedOperation, ValueError) as exc:
            raise LabelPermanentError("No se puede obtener la etiqueta OCA.") from exc
        except OCAOutcomeUnknown as exc:
            raise LabelUncertainError("La lectura de etiqueta quedó incierta.") from exc
        except LabelExecutionError:
            raise
        except Exception as exc:
            raise LabelRetryableError("La lectura idempotente de etiqueta falló.") from exc
        if (
            not isinstance(outcome, ShipmentResult)
            or outcome.carrier_id != current.carrier_id
            or outcome.state != OperationState.ETIQUETA_LISTA
            or not isinstance(outcome.label_pdf, bytes)
        ):
            raise LabelRetryableError("OCA todavía no devolvió la etiqueta.")
        try:
            document = document_store.persist(current, outcome.label_pdf)
            current = repository.checkpoint_document(current, document)
        except LabelExecutionError:
            raise
        except Exception as exc:
            # GET + persist son idempotentes; el alta OCA ya está fijada.
            raise LabelRetryableError("No se pudo fijar el documento.") from exc

    if current.stage == LabelStage.PUBLISH:
        document = _document_from_task(current)
        try:
            publisher.publish(current, document)
            if not repository.complete(current):
                raise LabelRetryableError("No se pudo cerrar el checkpoint final.")
        except LabelPermanentError:
            raise
        except LabelRetryableError:
            raise
        except Exception as exc:
            # PATCH READY_TO_DOWNLOAD es idempotente. Repetir la misma
            # publicación no crea otra guía ni otro PDF.
            raise LabelRetryableError("No se pudo publicar la etiqueta.") from exc
        current = replace(current, stage=LabelStage.DONE)
    return current


def process_label_outbox(
    *,
    limit: int = 20,
    repository: LabelWorkerRepository | None = None,
    readiness: Callable[[], bool] = labels_execution_ready,
    adapter_loader: AdapterLoader = _default_adapter_loader,
    snapshot_resolver: SnapshotResolver = _default_snapshot_resolver,
    document_store: LabelDocumentStore | None = None,
    publisher: LabelPublisher | None = None,
    execution_guard: ExecutionGuard | None = None,
) -> WorkerResult:
    """Procesa un lote acotado sin registrar payloads ni mensajes externos."""
    if not readiness():
        return WorkerResult(ready=False)
    repository = repository or PostgresLabelWorkerRepository()
    document_store = document_store or PostgresLabelDocumentStore()
    publisher = publisher or TiendanubeLabelPublisher()
    if execution_guard is None:
        execution_guard = (
            _postgres_execution_guard
            if isinstance(repository, PostgresLabelWorkerRepository)
            else _noop_execution_guard
        )
    stale = repository.recover_stale_claims()
    counts = {
        "processed": 0,
        "completed": 0,
        "retried": 0,
        "failed": 0,
        "manual_review": 0,
    }
    for _ in range(max(0, min(int(limit), 100))):
        row = repository.claim_next()
        if not row:
            break
        counts["processed"] += 1
        task: LabelTask | None = None
        try:
            task = _task(row)
            with execution_guard(task):
                result = _execute_task(
                    task,
                    repository=repository,
                    adapter_loader=adapter_loader,
                    snapshot_resolver=snapshot_resolver,
                    document_store=document_store,
                    publisher=publisher,
                )
            if result.stage == LabelStage.DONE:
                counts["completed"] += 1
        except LabelRetryableError as exc:
            task = task or _fallback_task(row)
            if task.attempts >= MAX_ATTEMPTS:
                if repository.fail(task, "MAX_ATTEMPTS"):
                    counts["failed"] += 1
            elif repository.retry(task, _safe_code(exc)):
                counts["retried"] += 1
        except LabelPermanentError as exc:
            task = task or _fallback_task(row)
            if repository.fail(task, _safe_code(exc)):
                counts["failed"] += 1
        except (LabelUncertainError, OCAOutcomeUnknown) as exc:
            task = task or _fallback_task(row)
            if repository.manual_review(task, _safe_code(exc)):
                counts["manual_review"] += 1
        except Exception as exc:
            # Un error no clasificado puede esconder una escritura OCA. La
            # salida segura es conciliación, nunca reintento automático.
            task = task or _fallback_task(row)
            if repository.manual_review(task, _safe_code(exc)):
                counts["manual_review"] += 1
    return WorkerResult(**counts, stale_claims=stale, ready=True)
