"""Cancelacion sincrona y fail-closed de etiquetas Tiendanube.

El callback de Tiendanube solo puede aprobar una cancelacion cuando el
operador logistico devuelve explicitamente ``OperationState.CANCELADO``. La
intencion queda marcada antes de tocar al carrier: si el proceso pierde el
resultado de la escritura externa, un replay queda en revision manual y nunca
repite la anulacion a ciegas.

Este modulo recibe una tienda ya autenticada. La autenticacion HTTP y el
parseo de la respuesta FastAPI pertenecen al endpoint que lo integre.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, ContextManager, Iterator, Mapping, Protocol, Sequence

from core.database import get_conn
from servicios.carrier_adapter import CarrierAdapter, OperationState, adapter_for
from servicios.carrier_contract import Capacidad
from servicios.oca_adapter import (
    OCAConfigurationError,
    OCAOutcomeUnknown,
    OCAUnsupportedOperation,
)


MAX_CANCEL_LABELS = 1_000
DEFAULT_DEADLINE_SECONDS = 4.0
_RESPONSE_RESERVE_SECONDS = 0.10
_DEFAULT_CARRIER_BUDGET_SECONDS = 1.80
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

_CONFIRMED = "CANCELACION_CONFIRMADA"
_SENT = "CANCELACION_ENVIADA"
_MANUAL = "CANCELACION_REVISION_MANUAL"
_REJECTED = "CANCELACION_RECHAZADA"


class LabelCancelError(RuntimeError):
    """Error seguro del servicio de cancelacion."""


class LabelCancelContractError(LabelCancelError):
    """El lote autenticado no cumple el contrato minimo."""


class LabelCancelRepositoryError(LabelCancelError):
    """No se pudo fijar o leer el estado durable de la cancelacion."""


class CancelItemState(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class CancelClaimState(StrEnum):
    CLAIMED = "CLAIMED"
    ALREADY_APPROVED = "ALREADY_APPROVED"
    ALREADY_REJECTED = "ALREADY_REJECTED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class CancelLabelRequest:
    label_id: str
    fulfillment_order_id: str


@dataclass(frozen=True)
class CancelTarget:
    store_id: str
    label_id: str
    fulfillment_order_id: str
    external_operation_id: str
    carrier_id: str


@dataclass(frozen=True)
class CancelItemResult:
    label_id: str
    fulfillment_order_id: str
    state: CancelItemState
    reason_code: str = ""
    reason_message: str = ""

    @property
    def approved(self) -> bool:
        return self.state == CancelItemState.APPROVED

    @property
    def requires_manual_review(self) -> bool:
        return self.state == CancelItemState.MANUAL_REVIEW

    def as_tiendanube_item(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "fulfillment_order_id": self.fulfillment_order_id,
            "label_id": self.label_id,
            "status": "OK" if self.approved else "FAILED",
        }
        if not self.approved:
            item["reason"] = {
                "code": self.reason_code or "CARRIER_CANCELLATION_REJECTED",
                "message": self.reason_message
                or "El operador no aprobo la cancelacion.",
            }
        return item


@dataclass(frozen=True)
class CancelBatchResult:
    items: tuple[CancelItemResult, ...]

    @property
    def approved_count(self) -> int:
        return sum(item.approved for item in self.items)

    @property
    def rejected_count(self) -> int:
        return len(self.items) - self.approved_count

    @property
    def manual_review_count(self) -> int:
        return sum(item.requires_manual_review for item in self.items)

    @property
    def http_status(self) -> int:
        """Status recomendado por el contrato del callback Labels API."""
        if self.approved_count == len(self.items):
            return 204
        if self.approved_count:
            return 207
        return 409

    @property
    def response_body(self) -> dict[str, Any] | None:
        if self.http_status == 204:
            return None
        return {"labels": [item.as_tiendanube_item() for item in self.items]}


class LabelCancelRepository(Protocol):
    def lookup(self, store_id: str, label_id: str) -> CancelTarget | None: ...

    def claim(
        self,
        target: CancelTarget,
        request: CancelLabelRequest,
    ) -> CancelClaimState: ...

    def cancel_before_emission(
        self,
        target: CancelTarget,
        request: CancelLabelRequest,
    ) -> CancelClaimState: ...

    def mark_confirmed(self, target: CancelTarget) -> None: ...

    def mark_rejected(self, target: CancelTarget, error_code: str) -> None: ...

    def mark_manual_review(self, target: CancelTarget, error_code: str) -> None: ...


AdapterLoader = Callable[[str], CarrierAdapter]
Clock = Callable[[], float]
ExecutionGuard = Callable[[str], ContextManager[bool]]


def _default_adapter_loader(carrier_id: str) -> CarrierAdapter:
    return adapter_for(carrier_id, Capacidad.CANCELAR)


def _identifier(value: object, field: str) -> str:
    if value is None or isinstance(value, bool):
        raise LabelCancelContractError(f"{field} es obligatorio.")
    identifier = str(value).strip()
    if not _ID_RE.fullmatch(identifier):
        raise LabelCancelContractError(f"{field} no es valido.")
    return identifier


def _normalize_requests(
    items: Sequence[CancelLabelRequest | Mapping[str, Any]],
) -> tuple[CancelLabelRequest, ...]:
    if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
        raise LabelCancelContractError("La cancelacion requiere una lista.")
    if not items:
        raise LabelCancelContractError("La cancelacion no contiene etiquetas.")
    if len(items) > MAX_CANCEL_LABELS:
        raise LabelCancelContractError("La cancelacion excede el maximo permitido.")

    normalized: list[CancelLabelRequest] = []
    seen: set[str] = set()
    for raw in items:
        if isinstance(raw, CancelLabelRequest):
            label_id = _identifier(raw.label_id, "label_id")
            fulfillment_id = _identifier(
                raw.fulfillment_order_id,
                "fulfillment_order_id",
            )
        elif isinstance(raw, Mapping):
            label_id = _identifier(raw.get("label_id"), "label_id")
            fulfillment_id = _identifier(
                raw.get("fulfillment_order_id"),
                "fulfillment_order_id",
            )
        else:
            raise LabelCancelContractError(
                "Cada cancelacion debe identificar etiqueta y fulfillment order."
            )
        if label_id in seen:
            raise LabelCancelContractError("El lote repite un label_id.")
        seen.add(label_id)
        normalized.append(CancelLabelRequest(label_id, fulfillment_id))
    return tuple(normalized)


def _cancel_payload(request: CancelLabelRequest) -> dict[str, str]:
    return {
        "label_id": request.label_id,
        "fulfillment_order_id": request.fulfillment_order_id,
    }


def _cancel_fingerprint(request: CancelLabelRequest) -> str:
    canonical = json.dumps(
        {"operation": "CANCEL", "payload": _cancel_payload(request)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def cancellation_idempotency_key(target: CancelTarget) -> str:
    """Clave estable; no incluye datos personales ni credenciales."""
    raw = (
        f"{target.store_id}|{target.label_id}|"
        f"{target.fulfillment_order_id}|CANCEL"
    ).encode("utf-8")
    return "tn-cancel-" + hashlib.sha256(raw).hexdigest()[:48]


class PostgresLabelCancelRepository:
    """Claim irreversible antes de la escritura externa.

    ``CANCELACION_ENVIADA`` nunca vuelve a ser reclamable. Si el proceso cae
    entre el request y su respuesta, el siguiente callback exige conciliacion
    manual en lugar de repetir la anulacion OCA.
    """

    def __init__(
        self,
        *,
        expected_generation: str = "",
        expected_customer_id: str = "",
    ) -> None:
        self.expected_generation = str(expected_generation or "").strip()
        self.expected_customer_id = str(expected_customer_id or "").strip().upper()

    def lookup(self, store_id: str, label_id: str) -> CancelTarget | None:
        with get_conn() as conn:
            with conn.cursor() as cur:
                if self.expected_generation and self.expected_customer_id:
                    cur.execute(
                        """
                        SELECT l.store_id, l.label_id,
                               l.fulfillment_order_id,
                               l.external_operation_id, s.carrier_id
                          FROM tiendanube_labels l
                          JOIN tiendanube_rate_quote_snapshots s
                            ON s.store_id = l.store_id
                           AND s.snapshot_id = l.rate_quote_snapshot_id
                           AND UPPER(s.customer_id) = UPPER(l.customer_id)
                          JOIN tiendanube_instalaciones i
                            ON i.store_id = l.store_id
                           AND i.install_generation = l.install_generation
                           AND UPPER(i.cliente_id) = UPPER(l.customer_id)
                          JOIN tiendanube_shipping_config c
                            ON c.store_id = i.store_id
                           AND c.install_generation = i.install_generation
                          JOIN tiendas_conectadas t
                            ON t.dominio = l.store_id || '.tiendanube'
                           AND t.plataforma = 'tiendanube'
                           AND UPPER(t.cliente_id) = UPPER(l.customer_id)
                         WHERE l.store_id = %s AND l.label_id = %s
                           AND l.install_generation = %s
                           AND UPPER(l.customer_id) = %s
                           AND i.estado = 'ACTIVA'
                           AND i.webhooks_ready = TRUE
                           AND c.activa = TRUE
                           AND t.activa = TRUE
                         FOR SHARE OF l, s, i, c, t
                        """,
                        (
                            store_id,
                            label_id,
                            self.expected_generation,
                            self.expected_customer_id,
                        ),
                    )
                else:
                    cur.execute(
                        """
                        SELECT l.store_id, l.label_id, l.fulfillment_order_id,
                               l.external_operation_id, s.carrier_id
                          FROM tiendanube_labels l
                          LEFT JOIN tiendanube_rate_quote_snapshots s
                            ON s.store_id = l.store_id
                           AND s.snapshot_id = l.rate_quote_snapshot_id
                         WHERE l.store_id = %s AND l.label_id = %s
                        """,
                        (store_id, label_id),
                    )
                row = cur.fetchone()
        if not row:
            return None
        data = dict(row)
        return CancelTarget(
            store_id=str(data.get("store_id") or ""),
            label_id=str(data.get("label_id") or ""),
            fulfillment_order_id=str(data.get("fulfillment_order_id") or ""),
            external_operation_id=str(data.get("external_operation_id") or "").strip(),
            carrier_id=str(data.get("carrier_id") or "").strip().lower(),
        )

    def claim(
        self,
        target: CancelTarget,
        request: CancelLabelRequest,
    ) -> CancelClaimState:
        payload = _cancel_payload(request)
        fingerprint = _cancel_fingerprint(request)
        with get_conn() as conn:
            try:
                with conn.cursor() as cur:
                    # Revalida la identidad bajo lock; ningun dato resuelto
                    # fuera de la transaccion autoriza por si solo una baja.
                    cur.execute(
                        """
                        SELECT l.fulfillment_order_id, l.external_operation_id,
                               l.install_generation, l.customer_id,
                               s.carrier_id
                          FROM tiendanube_labels l
                          LEFT JOIN tiendanube_rate_quote_snapshots s
                            ON s.store_id = l.store_id
                           AND s.snapshot_id = l.rate_quote_snapshot_id
                         WHERE l.store_id = %s AND l.label_id = %s
                         FOR UPDATE OF l
                        """,
                        (target.store_id, target.label_id),
                    )
                    current = cur.fetchone()
                    data = dict(current) if current else {}
                    if (
                        str(data.get("fulfillment_order_id") or "")
                        != target.fulfillment_order_id
                        or str(data.get("external_operation_id") or "").strip()
                        != target.external_operation_id
                        or str(data.get("carrier_id") or "").strip().lower()
                        != target.carrier_id
                        or (
                            self.expected_generation
                            and str(data.get("install_generation") or "")
                            != self.expected_generation
                        )
                        or (
                            self.expected_customer_id
                            and str(data.get("customer_id") or "").strip().upper()
                            != self.expected_customer_id
                        )
                    ):
                        conn.rollback()
                        return CancelClaimState.CONFLICT

                    cur.execute(
                        """
                        INSERT INTO tiendanube_label_outbox
                            (store_id, label_id, install_generation,
                             customer_id, operacion, payload,
                             payload_fingerprint, payload_complete, estado)
                        VALUES (%s, %s, %s, %s, 'CANCEL', %s::jsonb,
                                %s, TRUE, %s)
                        ON CONFLICT (store_id, label_id, operacion) DO NOTHING
                        RETURNING id
                        """,
                        (
                            target.store_id,
                            target.label_id,
                            self.expected_generation or None,
                            self.expected_customer_id or None,
                            json.dumps(payload, ensure_ascii=False),
                            fingerprint,
                            _SENT,
                        ),
                    )
                    inserted = cur.fetchone()
                    cur.execute(
                        """
                        SELECT payload_fingerprint, estado,
                               install_generation, customer_id
                          FROM tiendanube_label_outbox
                         WHERE store_id = %s AND label_id = %s
                           AND operacion = 'CANCEL'
                         FOR UPDATE
                        """,
                        (target.store_id, target.label_id),
                    )
                    outbox = cur.fetchone()
                    state = dict(outbox) if outbox else {}
                    if str(state.get("payload_fingerprint") or "").strip() != fingerprint:
                        conn.rollback()
                        return CancelClaimState.CONFLICT
                    if (
                        self.expected_generation
                        and str(state.get("install_generation") or "")
                        != self.expected_generation
                    ) or (
                        self.expected_customer_id
                        and str(state.get("customer_id") or "").strip().upper()
                        != self.expected_customer_id
                    ):
                        conn.rollback()
                        return CancelClaimState.CONFLICT
                    if inserted:
                        conn.commit()
                        return CancelClaimState.CLAIMED

                    current_state = str(state.get("estado") or "")
                    if current_state == _CONFIRMED:
                        conn.commit()
                        return CancelClaimState.ALREADY_APPROVED
                    if current_state in {_SENT, _MANUAL}:
                        conn.commit()
                        return CancelClaimState.MANUAL_REVIEW
                    if current_state == _REJECTED:
                        conn.commit()
                        return CancelClaimState.ALREADY_REJECTED

                    cur.execute(
                        """
                        UPDATE tiendanube_label_outbox
                           SET estado = %s, ultimo_error_codigo = NULL,
                               actualizada_en = now(), procesada_en = NULL
                         WHERE store_id = %s AND label_id = %s
                           AND operacion = 'CANCEL'
                        """,
                        (_SENT, target.store_id, target.label_id),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return CancelClaimState.CLAIMED

    def cancel_before_emission(
        self,
        target: CancelTarget,
        request: CancelLabelRequest,
    ) -> CancelClaimState:
        """Cierra localmente un GENERATE que demostradamente nunca se ejecuto.

        El advisory lock de ``cancel_labels`` excluye al worker durante toda
        esta transaccion. Aun asi, ``external_operation_id`` vacio no alcanza:
        un proceso pudo caer despues del POST al carrier y antes del checkpoint.
        Solo ``PENDIENTE`` con cero intentos y sin claim prueba ausencia de red;
        cualquier otra combinacion queda bloqueada para revision manual.
        """
        payload = _cancel_payload(request)
        fingerprint = _cancel_fingerprint(request)
        with get_conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT fulfillment_order_id, external_operation_id
                          FROM tiendanube_labels
                         WHERE store_id = %s AND label_id = %s
                         FOR UPDATE
                        """,
                        (target.store_id, target.label_id),
                    )
                    label = cur.fetchone()
                    label = dict(label) if label else {}
                    if (
                        str(label.get("fulfillment_order_id") or "")
                        != target.fulfillment_order_id
                        or str(label.get("external_operation_id") or "").strip()
                    ):
                        conn.rollback()
                        return CancelClaimState.CONFLICT

                    cur.execute(
                        """
                        SELECT id, estado, intentos, claim_id, claimed_at
                          FROM tiendanube_label_outbox
                         WHERE store_id = %s AND label_id = %s
                           AND operacion = 'GENERATE'
                         FOR UPDATE
                        """,
                        (target.store_id, target.label_id),
                    )
                    generate = cur.fetchone()
                    generate = dict(generate) if generate else {}
                    execution = {}
                    if generate.get("id") is not None:
                        cur.execute(
                            """
                            SELECT stage, external_operation_id, claim_id,
                                   claimed_at
                              FROM tiendanube_label_execution
                             WHERE outbox_id = %s
                             FOR UPDATE
                            """,
                            (generate["id"],),
                        )
                        row = cur.fetchone()
                        execution = dict(row) if row else {}

                    never_attempted = bool(
                        generate
                        and str(generate.get("estado") or "") == "PENDIENTE"
                        and int(generate.get("intentos") or 0) == 0
                        and not generate.get("claim_id")
                        and not generate.get("claimed_at")
                        and not str(
                            execution.get("external_operation_id") or ""
                        ).strip()
                        and str(
                            execution.get("stage") or "CREATE_SHIPMENT"
                        ) == "CREATE_SHIPMENT"
                        and not execution.get("claim_id")
                        and not execution.get("claimed_at")
                    )
                    cancel_state = _CONFIRMED if never_attempted else _MANUAL
                    error_code = (
                        None
                        if never_attempted
                        else "CANCEL_BEFORE_EMISSION_UNCERTAIN"
                    )
                    cur.execute(
                        """
                        INSERT INTO tiendanube_label_outbox
                            (store_id, label_id, operacion, payload,
                             payload_fingerprint, payload_complete, estado,
                             ultimo_error_codigo, procesada_en)
                        VALUES (
                            %s, %s, 'CANCEL', %s::jsonb, %s, TRUE, %s, %s,
                            now()
                        )
                        ON CONFLICT (store_id, label_id, operacion) DO NOTHING
                        RETURNING id
                        """,
                        (
                            target.store_id,
                            target.label_id,
                            json.dumps(payload, ensure_ascii=False),
                            fingerprint,
                            cancel_state,
                            error_code,
                        ),
                    )
                    inserted = cur.fetchone()
                    cur.execute(
                        """
                        SELECT payload_fingerprint, estado
                          FROM tiendanube_label_outbox
                         WHERE store_id = %s AND label_id = %s
                           AND operacion = 'CANCEL'
                         FOR UPDATE
                        """,
                        (target.store_id, target.label_id),
                    )
                    existing = cur.fetchone()
                    existing = dict(existing) if existing else {}
                    if str(existing.get("payload_fingerprint") or "") != fingerprint:
                        conn.rollback()
                        return CancelClaimState.CONFLICT

                    persisted = str(existing.get("estado") or "")
                    if not inserted:
                        if persisted == _CONFIRMED:
                            conn.commit()
                            return CancelClaimState.ALREADY_APPROVED
                        if persisted in {_SENT, _MANUAL}:
                            conn.commit()
                            return CancelClaimState.MANUAL_REVIEW
                        if persisted == _REJECTED:
                            conn.commit()
                            return CancelClaimState.ALREADY_REJECTED
                        conn.rollback()
                        return CancelClaimState.CONFLICT

                    generate_state = (
                        "CANCELADO_LOCAL" if never_attempted else "VERIFICAR_MANUAL"
                    )
                    if generate.get("id") is not None:
                        cur.execute(
                            """
                            UPDATE tiendanube_label_outbox
                               SET estado = %s, claim_id = NULL,
                                   claimed_at = NULL,
                                   ultimo_error_codigo = %s,
                                   procesada_en = now(), actualizada_en = now()
                             WHERE id = %s AND operacion = 'GENERATE'
                            """,
                            (
                                generate_state,
                                "CANCELLED_BEFORE_EMISSION"
                                if never_attempted else error_code,
                                generate["id"],
                            ),
                        )
                        cur.execute(
                            """
                            UPDATE tiendanube_label_execution
                               SET claim_id = NULL, claimed_at = NULL,
                                   actualizada_en = now()
                             WHERE outbox_id = %s
                            """,
                            (generate["id"],),
                        )
                    cur.execute(
                        """
                        UPDATE tiendanube_labels
                           SET estado = %s, download_token_hash = NULL,
                               download_token_revoked_at = COALESCE(
                                   download_token_revoked_at, now()
                               ),
                               actualizada_en = now()
                         WHERE store_id = %s AND label_id = %s
                        """,
                        (cancel_state, target.store_id, target.label_id),
                    )
                    cur.execute(
                        """
                        UPDATE tiendanube_label_documents
                           SET activa = FALSE,
                               revocada_en = COALESCE(revocada_en, now())
                         WHERE store_id = %s AND label_id = %s
                        """,
                        (target.store_id, target.label_id),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return (
            CancelClaimState.ALREADY_APPROVED
            if never_attempted
            else CancelClaimState.MANUAL_REVIEW
        )

    def mark_confirmed(self, target: CancelTarget) -> None:
        with get_conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE tiendanube_label_outbox
                           SET estado = %s, ultimo_error_codigo = NULL,
                               procesada_en = now(), actualizada_en = now()
                         WHERE store_id = %s AND label_id = %s
                           AND operacion = 'CANCEL' AND estado = %s
                        """,
                        (_CONFIRMED, target.store_id, target.label_id, _SENT),
                    )
                    if cur.rowcount != 1:
                        cur.execute(
                            """
                            SELECT estado FROM tiendanube_label_outbox
                             WHERE store_id = %s AND label_id = %s
                               AND operacion = 'CANCEL'
                            """,
                            (target.store_id, target.label_id),
                        )
                        current = cur.fetchone()
                        persisted = str(
                            (dict(current) if current else {}).get("estado") or ""
                        )
                        if persisted != _CONFIRMED:
                            raise LabelCancelRepositoryError(
                                "No se pudo confirmar la cancelacion durable."
                            )
                    cur.execute(
                        """
                        UPDATE tiendanube_labels
                           SET estado = %s,
                               download_token_hash = NULL,
                               download_token_revoked_at = COALESCE(
                                   download_token_revoked_at, now()
                               ),
                               actualizada_en = now()
                         WHERE store_id = %s AND label_id = %s
                           AND fulfillment_order_id = %s
                           AND external_operation_id = %s
                        """,
                        (
                            _CONFIRMED,
                            target.store_id,
                            target.label_id,
                            target.fulfillment_order_id,
                            target.external_operation_id,
                        ),
                    )
                    if cur.rowcount != 1:
                        raise LabelCancelRepositoryError(
                            "No se pudo actualizar la etiqueta cancelada."
                        )
                    cur.execute(
                        """
                        UPDATE tiendanube_label_documents
                           SET activa = FALSE,
                               revocada_en = COALESCE(revocada_en, now())
                         WHERE store_id = %s AND label_id = %s
                        """,
                        (target.store_id, target.label_id),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def mark_rejected(self, target: CancelTarget, error_code: str) -> None:
        self._mark_failure(target, _REJECTED, error_code)

    def mark_manual_review(self, target: CancelTarget, error_code: str) -> None:
        self._mark_failure(target, _MANUAL, error_code)

    @staticmethod
    def _mark_failure(
        target: CancelTarget,
        state: str,
        error_code: str,
    ) -> None:
        safe_code = re.sub(r"[^A-Z0-9_-]", "_", str(error_code).upper())[:80]
        with get_conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE tiendanube_label_outbox
                           SET estado = %s, ultimo_error_codigo = %s,
                               procesada_en = now(), actualizada_en = now()
                         WHERE store_id = %s AND label_id = %s
                           AND operacion = 'CANCEL' AND estado = %s
                        """,
                        (
                            state,
                            safe_code or "CANCEL_ERROR",
                            target.store_id,
                            target.label_id,
                            _SENT,
                        ),
                    )
                    if cur.rowcount != 1:
                        raise LabelCancelRepositoryError(
                            "No se pudo fijar el resultado de la cancelacion."
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise


def _approved(request: CancelLabelRequest) -> CancelItemResult:
    return CancelItemResult(
        request.label_id,
        request.fulfillment_order_id,
        CancelItemState.APPROVED,
    )


def _rejected(
    request: CancelLabelRequest,
    *,
    code: str = "CARRIER_CANCELLATION_REJECTED",
    message: str = "El operador no aprobo la cancelacion.",
) -> CancelItemResult:
    return CancelItemResult(
        request.label_id,
        request.fulfillment_order_id,
        CancelItemState.REJECTED,
        code,
        message,
    )


def _manual(request: CancelLabelRequest) -> CancelItemResult:
    return CancelItemResult(
        request.label_id,
        request.fulfillment_order_id,
        CancelItemState.MANUAL_REVIEW,
        "CARRIER_SYSTEM_ERROR",
        "El operador no confirmo el resultado; requiere revision manual.",
    )


def _carrier_budget(adapter: CarrierAdapter) -> float:
    try:
        budget = float(getattr(adapter, "callback_timeout_budget_seconds"))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return _DEFAULT_CARRIER_BUDGET_SECONDS
    if not math.isfinite(budget) or budget <= 0:
        return _DEFAULT_CARRIER_BUDGET_SECONDS
    return budget


def _try_mark(
    callback: Callable[[CancelTarget, str], None],
    target: CancelTarget,
    code: str,
) -> bool:
    try:
        callback(target, code)
    except Exception:
        return False
    return True


def _noop_execution_guard(_store_id: str) -> ContextManager[bool]:
    return nullcontext(True)


@contextmanager
def _postgres_execution_guard(store_id: str) -> Iterator[bool]:
    """Reserva una conexion y serializa el lote con el worker de la tienda."""
    lock_key = f"tauro:tiendanube:{store_id}.tiendanube"
    acquired = False
    with get_conn() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT pg_try_advisory_lock(
                        hashtextextended(%s, 0)
                    ) AS acquired
                    """,
                    (lock_key,),
                )
                row = cur.fetchone()
            if isinstance(row, Mapping):
                acquired = bool(row.get("acquired"))
            elif row:
                acquired = bool(row[0])
            # El advisory lock es de sesion y sobrevive al commit. Cerramos la
            # transaccion antes de tocar red sin devolver la conexion al pool.
            conn.commit()
            yield acquired
        finally:
            if acquired:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT pg_advisory_unlock(
                            hashtextextended(%s, 0)
                        )
                        """,
                        (lock_key,),
                    )
                conn.commit()


def _guard_rejected(requests: Sequence[CancelLabelRequest]) -> CancelBatchResult:
    return CancelBatchResult(
        tuple(
            _rejected(
                request,
                code="CARRIER_SYSTEM_ERROR",
                message="Otra operacion de la tienda esta en curso.",
            )
            for request in requests
        )
    )


def _cancel_labels_locked(
    normalized_store: str,
    requests: Sequence[CancelLabelRequest],
    *,
    repository: LabelCancelRepository,
    adapter_loader: AdapterLoader,
    clock: Clock,
    deadline: float,
) -> CancelBatchResult:
    """Ejecuta lookup, claim, carrier y checkpoints bajo el guard del lote."""

    results: list[CancelItemResult] = []
    for request in requests:
        if clock() >= deadline:
            results.append(
                _rejected(
                    request,
                    code="CARRIER_SYSTEM_ERROR",
                    message="Se agoto el tiempo del callback antes de contactar al operador.",
                )
            )
            continue

        try:
            target = repository.lookup(normalized_store, request.label_id)
        except Exception:
            results.append(
                _rejected(
                    request,
                    code="CARRIER_SYSTEM_ERROR",
                    message="No se pudo verificar la etiqueta.",
                )
            )
            continue
        if target is None:
            results.append(
                _rejected(request, message="La etiqueta no pertenece a TAURO.")
            )
            continue
        if (
            target.store_id != normalized_store
            or target.label_id != request.label_id
        ):
            results.append(
                _rejected(
                    request,
                    code="INSUFFICIENT_PERMISSIONS",
                    message="La identidad durable de la etiqueta no coincide.",
                )
            )
            continue
        if target.fulfillment_order_id != request.fulfillment_order_id:
            results.append(
                _rejected(
                    request,
                    code="INSUFFICIENT_PERMISSIONS",
                    message="La etiqueta no pertenece a esa fulfillment order.",
                )
            )
            continue
        if not target.external_operation_id:
            try:
                early = repository.cancel_before_emission(target, request)
            except Exception:
                results.append(
                    _rejected(
                        request,
                        code="CARRIER_SYSTEM_ERROR",
                        message="No se pudo fijar la cancelacion previa a la emision.",
                    )
                )
                continue
            if early == CancelClaimState.ALREADY_APPROVED:
                results.append(_approved(request))
            elif early == CancelClaimState.MANUAL_REVIEW:
                results.append(_manual(request))
            else:
                results.append(_rejected(request))
            continue
        if not target.carrier_id:
            results.append(
                _rejected(
                    request,
                    message="La etiqueta no tiene una operacion emitida cancelable.",
                )
            )
            continue

        try:
            claim = repository.claim(target, request)
        except Exception:
            results.append(
                _rejected(
                    request,
                    code="CARRIER_SYSTEM_ERROR",
                    message="No se pudo fijar la solicitud de cancelacion.",
                )
            )
            continue

        if claim == CancelClaimState.ALREADY_APPROVED:
            results.append(_approved(request))
            continue
        if claim == CancelClaimState.MANUAL_REVIEW:
            results.append(_manual(request))
            continue
        if claim in {CancelClaimState.ALREADY_REJECTED, CancelClaimState.CONFLICT}:
            results.append(_rejected(request))
            continue
        if claim != CancelClaimState.CLAIMED:
            results.append(_rejected(request))
            continue

        try:
            adapter = adapter_loader(target.carrier_id)
        except Exception:
            _try_mark(repository.mark_rejected, target, "CARRIER_UNAVAILABLE")
            results.append(
                _rejected(
                    request,
                    code="CARRIER_SYSTEM_ERROR",
                    message="El operador no esta disponible para cancelar.",
                )
            )
            continue

        required_budget = _carrier_budget(adapter) + _RESPONSE_RESERVE_SECONDS

        # El claim ya es durable. Si el presupuesto desaparecio antes del
        # request, cerramos como rechazo confirmado sin tocar al carrier.
        if clock() + required_budget > deadline:
            _try_mark(repository.mark_rejected, target, "CALLBACK_DEADLINE")
            results.append(
                _rejected(
                    request,
                    code="CARRIER_SYSTEM_ERROR",
                    message="No queda tiempo seguro para consultar al operador.",
                )
            )
            continue

        try:
            state = adapter.cancel(
                target.external_operation_id,
                idempotency_key=cancellation_idempotency_key(target),
            )
        except (OCAConfigurationError, OCAUnsupportedOperation, ValueError):
            _try_mark(repository.mark_rejected, target, "CARRIER_POLICY_VIOLATION")
            results.append(
                _rejected(
                    request,
                    code="CARRIER_POLICY_VIOLATION",
                    message="La operación no admite cancelación con la configuración actual.",
                )
            )
            continue
        except OCAOutcomeUnknown:
            _try_mark(repository.mark_manual_review, target, "OCA_OUTCOME_UNKNOWN")
            results.append(_manual(request))
            continue
        except Exception:
            # Una excepcion levantada por el borde del carrier puede ocurrir
            # despues de enviar la escritura. No se infiere rechazo ni retry.
            _try_mark(repository.mark_manual_review, target, "CARRIER_OUTCOME_UNKNOWN")
            results.append(_manual(request))
            continue

        if (
            not isinstance(state, OperationState)
            or state is not OperationState.CANCELADO
        ):
            _try_mark(repository.mark_rejected, target, "CARRIER_NOT_CANCELLED")
            results.append(_rejected(request))
            continue

        try:
            repository.mark_confirmed(target)
        except Exception:
            # El carrier confirmo, pero el checkpoint local no. Se evita un
            # 2xx no auditable y el claim impide repetir la escritura.
            _try_mark(repository.mark_manual_review, target, "CONFIRM_CHECKPOINT_FAILED")
            results.append(_manual(request))
            continue
        results.append(_approved(request))

    return CancelBatchResult(tuple(results))


def cancel_labels(
    store_id: str,
    items: Sequence[CancelLabelRequest | Mapping[str, Any]],
    *,
    repository: LabelCancelRepository | None = None,
    adapter_loader: AdapterLoader = _default_adapter_loader,
    clock: Clock = time.monotonic,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    execution_guard: ExecutionGuard | None = None,
    expected_generation: str = "",
    expected_customer_id: str = "",
) -> CancelBatchResult:
    """Cancela un lote autenticado con un unico intento por etiqueta.

    El guard abarca desde antes del primer lookup hasta el ultimo checkpoint.
    Para PostgreSQL comparte la clave de sesion del worker; repositorios de
    memoria usan un no-op y los tests pueden inyectar un guard explicito.
    """
    normalized_store = _identifier(store_id, "store_id")
    requests = _normalize_requests(items)
    try:
        requested_deadline = float(deadline_seconds)
    except (TypeError, ValueError, OverflowError):
        raise LabelCancelContractError("El deadline no es valido.") from None
    if not math.isfinite(requested_deadline) or requested_deadline <= 0:
        raise LabelCancelContractError("El deadline no es valido.")
    total_budget = min(requested_deadline, DEFAULT_DEADLINE_SECONDS)
    deadline = clock() + total_budget
    if repository is None:
        if not str(expected_generation or "").strip() or not str(
            expected_customer_id or ""
        ).strip():
            raise LabelCancelContractError(
                "La cancelacion no identifica la instalacion vigente."
            )
        repository = PostgresLabelCancelRepository(
            expected_generation=expected_generation,
            expected_customer_id=expected_customer_id,
        )
    if execution_guard is None:
        execution_guard = (
            _postgres_execution_guard
            if isinstance(repository, PostgresLabelCancelRepository)
            else _noop_execution_guard
        )

    try:
        with execution_guard(normalized_store) as acquired:
            if not acquired:
                return _guard_rejected(requests)
            return _cancel_labels_locked(
                normalized_store,
                requests,
                repository=repository,
                adapter_loader=adapter_loader,
                clock=clock,
                deadline=deadline,
            )
    except Exception:
        # Un fallo al adquirir o liberar el guard nunca autoriza otra escritura
        # externa ni expone detalles de PostgreSQL al callback.
        return _guard_rejected(requests)
