"""Recepción durable de callbacks Labels API de Tiendanube.

Este módulo sólo implementa el borde y el outbox. No traduce el payload a
OCA ni afirma que OCA pueda emitir o cancelar: mientras no exista un worker
homologado, los callbacks se persisten y fallan cerrado para que Tiendanube no
interprete una aceptación técnica como una operación logística realizada.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol, Sequence

from core.database import get_conn
from servicios.tiendanube_shipping import configuracion_por_label_token


MAX_LABELS_PER_CALLBACK = 1_000
MAX_LABEL_PAYLOAD_BYTES = 256 * 1024
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_tabla_lista = False


class TiendanubeLabelsError(RuntimeError):
    """Base segura para errores del callback de etiquetas."""


class LabelsAuthenticationError(TiendanubeLabelsError):
    pass


class LabelsContractError(TiendanubeLabelsError):
    pass


class LabelsConflictError(TiendanubeLabelsError):
    pass


class LabelsUnavailableError(TiendanubeLabelsError):
    pass


class LabelsBlockedError(TiendanubeLabelsError):
    """La solicitud quedó durable, pero no puede aceptarse para ejecución."""


@dataclass(frozen=True)
class LabelOperation:
    store_id: str
    label_id: str
    fulfillment_order_id: str
    operation: str
    payload: Mapping
    fingerprint: str
    payload_complete: bool = True


@dataclass(frozen=True)
class PersistResult:
    created: int
    replayed: int
    state: str


class LabelRepository(Protocol):
    def persist(
        self,
        operations: Sequence[LabelOperation],
        *,
        state: str,
    ) -> PersistResult: ...


def _ensure_tables() -> None:
    """Crea la evidencia y el outbox sin depender de un deploy de migración."""
    global _tabla_lista
    if _tabla_lista:
        return

    # La FK hace que el flujo de redacción ya existente, que elimina la
    # configuración del store, también elimine estos payloads con PII.
    from servicios.tiendanube_shipping import _ensure_tabla as ensure_shipping

    ensure_shipping()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tiendanube_labels (
                    store_id                TEXT NOT NULL,
                    label_id                TEXT NOT NULL,
                    fulfillment_order_id    TEXT NOT NULL,
                    generate_payload        JSONB,
                    generate_fingerprint    CHAR(64),
                    generate_payload_complete BOOLEAN NOT NULL DEFAULT FALSE,
                    estado                  TEXT NOT NULL,
                    external_operation_id   TEXT,
                    tracking_number         TEXT,
                    creada_en               TIMESTAMPTZ NOT NULL DEFAULT now(),
                    actualizada_en          TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (store_id, label_id),
                    FOREIGN KEY (store_id)
                        REFERENCES tiendanube_shipping_config(store_id)
                        ON DELETE CASCADE
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tiendanube_label_outbox (
                    id                      BIGSERIAL PRIMARY KEY,
                    store_id                TEXT NOT NULL,
                    label_id                TEXT NOT NULL,
                    operacion               TEXT NOT NULL
                        CHECK (operacion IN ('GENERATE', 'CANCEL')),
                    payload                 JSONB NOT NULL,
                    payload_fingerprint     CHAR(64) NOT NULL,
                    payload_complete        BOOLEAN NOT NULL DEFAULT FALSE,
                    estado                  TEXT NOT NULL,
                    intentos                INTEGER NOT NULL DEFAULT 0,
                    proximo_intento_en      TIMESTAMPTZ NOT NULL DEFAULT now(),
                    ultimo_error_codigo     TEXT,
                    creada_en               TIMESTAMPTZ NOT NULL DEFAULT now(),
                    actualizada_en          TIMESTAMPTZ NOT NULL DEFAULT now(),
                    procesada_en            TIMESTAMPTZ,
                    UNIQUE (store_id, label_id, operacion),
                    FOREIGN KEY (store_id, label_id)
                        REFERENCES tiendanube_labels(store_id, label_id)
                        ON DELETE CASCADE
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tiendanube_label_outbox_pendiente
                    ON tiendanube_label_outbox
                        (estado, proximo_intento_en, id)
                """
            )
            cur.execute(
                """
                ALTER TABLE tiendanube_labels
                    ADD COLUMN IF NOT EXISTS generate_payload_complete
                        BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                ALTER TABLE tiendanube_label_outbox
                    ADD COLUMN IF NOT EXISTS payload_complete
                        BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
        conn.commit()
    _tabla_lista = True


def _canonical_json(value) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise LabelsContractError("El payload de etiquetas no es JSON válido.") from None


def _fingerprint(operation: str, payload: Mapping) -> str:
    canonical = _canonical_json({"operation": operation, "payload": payload})
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _identifier(value, field: str) -> str:
    if isinstance(value, bool) or value is None:
        raise LabelsContractError(f"{field} es obligatorio.")
    result = str(value).strip()
    if not _ID_RE.fullmatch(result):
        raise LabelsContractError(f"{field} no es válido.")
    return result


def _json_copy(value):
    """Normaliza a tipos JSON y evita conservar objetos mutables del caller."""
    return json.loads(_canonical_json(value))


def _validate_batch_size(items: Sequence) -> None:
    if not items:
        raise LabelsContractError("El callback no contiene etiquetas.")
    if len(items) > MAX_LABELS_PER_CALLBACK:
        raise LabelsContractError("El callback excede el máximo de etiquetas.")


def _generate_operations(store_id: str, payload) -> tuple[LabelOperation, ...]:
    if not isinstance(payload, list):
        raise LabelsContractError("generate requiere una lista de etiquetas.")
    _validate_batch_size(payload)

    operations: list[LabelOperation] = []
    seen: set[str] = set()
    for raw in payload:
        if not isinstance(raw, Mapping):
            raise LabelsContractError("Cada etiqueta debe ser un objeto.")
        label_id = _identifier(raw.get("id"), "label_id")
        if label_id in seen:
            raise LabelsContractError("El callback repite un label_id.")
        seen.add(label_id)

        fulfillment = raw.get("fulfillment_order_info")
        if not isinstance(fulfillment, Mapping):
            raise LabelsContractError("Falta fulfillment_order_info.")
        fulfillment_id = _identifier(
            fulfillment.get("id"), "fulfillment_order_id"
        )
        snapshot = _json_copy(raw)
        if len(_canonical_json(snapshot).encode("utf-8")) > MAX_LABEL_PAYLOAD_BYTES:
            raise LabelsContractError("Una etiqueta excede el tamaño permitido.")
        operations.append(
            LabelOperation(
                store_id=store_id,
                label_id=label_id,
                fulfillment_order_id=fulfillment_id,
                operation="GENERATE",
                payload=snapshot,
                fingerprint=_fingerprint("GENERATE", snapshot),
            )
        )
    return tuple(operations)


def _cancel_operations(store_id: str, payload) -> tuple[LabelOperation, ...]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("labels"), list):
        raise LabelsContractError("cancel requiere el campo labels.")
    labels = payload["labels"]
    _validate_batch_size(labels)

    operations: list[LabelOperation] = []
    seen: set[str] = set()
    for raw in labels:
        if not isinstance(raw, Mapping):
            raise LabelsContractError("Cada cancelación debe ser un objeto.")
        label_id = _identifier(raw.get("label_id"), "label_id")
        if label_id in seen:
            raise LabelsContractError("El callback repite un label_id.")
        seen.add(label_id)
        fulfillment_id = _identifier(
            raw.get("fulfillment_order_id"), "fulfillment_order_id"
        )
        snapshot = {
            "label_id": label_id,
            "fulfillment_order_id": fulfillment_id,
        }
        operations.append(
            LabelOperation(
                store_id=store_id,
                label_id=label_id,
                fulfillment_order_id=fulfillment_id,
                operation="CANCEL",
                payload=snapshot,
                fingerprint=_fingerprint("CANCEL", snapshot),
            )
        )
    return tuple(operations)


def _minimize_blocked_generate(
    operations: Sequence[LabelOperation],
) -> tuple[LabelOperation, ...]:
    """Retiene evidencia idempotente sin conservar datos del destinatario.

    La huella sigue correspondiendo al payload original. Si un worker futuro
    queda homologado, un reintento idéntico puede completar el snapshot antes
    de poner la operación en cola.
    """
    return tuple(
        LabelOperation(
            store_id=operation.store_id,
            label_id=operation.label_id,
            fulfillment_order_id=operation.fulfillment_order_id,
            operation=operation.operation,
            payload={
                "id": operation.label_id,
                "fulfillment_order_info": {
                    "id": operation.fulfillment_order_id,
                },
            },
            fingerprint=operation.fingerprint,
            payload_complete=False,
        )
        for operation in operations
    )


class PostgresLabelRepository:
    """Persiste etiqueta y operación en la misma transacción."""

    def persist(
        self,
        operations: Sequence[LabelOperation],
        *,
        state: str,
    ) -> PersistResult:
        _ensure_tables()
        created = 0
        replayed = 0
        with get_conn() as conn:
            try:
                with conn.cursor() as cur:
                    for operation in operations:
                        generate_payload = (
                            json.dumps(operation.payload, ensure_ascii=False)
                            if operation.operation == "GENERATE"
                            else None
                        )
                        generate_fingerprint = (
                            operation.fingerprint
                            if operation.operation == "GENERATE"
                            else None
                        )
                        cur.execute(
                            """
                            INSERT INTO tiendanube_labels
                                (store_id, label_id, fulfillment_order_id,
                                 generate_payload, generate_fingerprint,
                                 generate_payload_complete, estado)
                            VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)
                            ON CONFLICT (store_id, label_id) DO NOTHING
                            RETURNING store_id
                            """,
                            (
                                operation.store_id,
                                operation.label_id,
                                operation.fulfillment_order_id,
                                generate_payload,
                                generate_fingerprint,
                                operation.payload_complete,
                                state,
                            ),
                        )
                        inserted_label = cur.fetchone()
                        cur.execute(
                            """
                            SELECT fulfillment_order_id, generate_fingerprint,
                                   generate_payload_complete
                              FROM tiendanube_labels
                             WHERE store_id = %s AND label_id = %s
                             FOR UPDATE
                            """,
                            (operation.store_id, operation.label_id),
                        )
                        current = cur.fetchone()
                        if not current:
                            raise LabelsUnavailableError(
                                "No se pudo fijar la etiqueta recibida."
                            )
                        current = dict(current)
                        if str(current["fulfillment_order_id"]) != operation.fulfillment_order_id:
                            raise LabelsConflictError(
                                "El label_id ya pertenece a otra fulfillment order."
                            )

                        current_generate = current.get("generate_fingerprint")
                        if operation.operation == "GENERATE":
                            if current_generate and str(current_generate).strip() != operation.fingerprint:
                                raise LabelsConflictError(
                                    "El label_id fue reutilizado con otro payload."
                                )
                            if not current_generate or (
                                operation.payload_complete
                                and not current.get("generate_payload_complete")
                            ):
                                cur.execute(
                                    """
                                    UPDATE tiendanube_labels
                                       SET generate_payload = %s::jsonb,
                                           generate_fingerprint = %s,
                                           generate_payload_complete = %s,
                                           estado = %s,
                                           actualizada_en = now()
                                     WHERE store_id = %s AND label_id = %s
                                    """,
                                    (
                                        generate_payload,
                                        operation.fingerprint,
                                        operation.payload_complete,
                                        state,
                                        operation.store_id,
                                        operation.label_id,
                                    ),
                                )
                        elif inserted_label:
                            # Una cancelación puede adelantarse a generate. Se
                            # conserva un placeholder que luego generate completa.
                            cur.execute(
                                """
                                UPDATE tiendanube_labels
                                   SET estado = %s, actualizada_en = now()
                                 WHERE store_id = %s AND label_id = %s
                                """,
                                (state, operation.store_id, operation.label_id),
                            )

                        cur.execute(
                            """
                            INSERT INTO tiendanube_label_outbox
                                (store_id, label_id, operacion, payload,
                                 payload_fingerprint, payload_complete, estado)
                            VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)
                            ON CONFLICT (store_id, label_id, operacion) DO NOTHING
                            RETURNING id
                            """,
                            (
                                operation.store_id,
                                operation.label_id,
                                operation.operation,
                                json.dumps(operation.payload, ensure_ascii=False),
                                operation.fingerprint,
                                operation.payload_complete,
                                state,
                            ),
                        )
                        inserted = cur.fetchone()
                        if inserted:
                            created += 1
                            continue

                        cur.execute(
                            """
                            SELECT payload_fingerprint, payload_complete
                              FROM tiendanube_label_outbox
                             WHERE store_id = %s AND label_id = %s
                               AND operacion = %s
                             FOR UPDATE
                            """,
                            (
                                operation.store_id,
                                operation.label_id,
                                operation.operation,
                            ),
                        )
                        existing = cur.fetchone()
                        existing = dict(existing) if existing else None
                        if not existing or (
                            str(existing["payload_fingerprint"]).strip()
                            != operation.fingerprint
                        ):
                            raise LabelsConflictError(
                                "La operación ya existe con otro payload."
                            )
                        if operation.payload_complete and not existing.get(
                            "payload_complete"
                        ):
                            cur.execute(
                                """
                                UPDATE tiendanube_label_outbox
                                   SET payload = %s::jsonb,
                                       payload_complete = TRUE,
                                       estado = %s,
                                       actualizada_en = now()
                                 WHERE store_id = %s AND label_id = %s
                                   AND operacion = %s
                                """,
                                (
                                    json.dumps(
                                        operation.payload, ensure_ascii=False
                                    ),
                                    state,
                                    operation.store_id,
                                    operation.label_id,
                                    operation.operation,
                                ),
                            )
                        replayed += 1
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return PersistResult(created=created, replayed=replayed, state=state)


def labels_execution_ready() -> bool:
    """Fail-closed hasta que exista worker OCA homologado con fixtures UAT."""
    return False


def _authenticated_store(
    callback_token: str,
    *,
    config_loader: Callable[[str], dict | None],
) -> str:
    try:
        config = config_loader(callback_token)
    except Exception as exc:
        raise LabelsUnavailableError("No se pudo validar el callback.") from exc
    if not config or not config.get("activa") or not config.get("store_id"):
        raise LabelsAuthenticationError("Callback no autorizado.")
    return str(config["store_id"])


def recibir_generate(
    payload,
    callback_token: str,
    *,
    repository: LabelRepository | None = None,
    config_loader: Callable[[str], dict | None] = configuracion_por_label_token,
    execution_ready: Callable[[], bool] = labels_execution_ready,
) -> PersistResult:
    store_id = _authenticated_store(callback_token, config_loader=config_loader)
    operations = _generate_operations(store_id, payload)
    ready = bool(execution_ready())
    persisted_operations = operations if ready else _minimize_blocked_generate(
        operations
    )
    result = (repository or PostgresLabelRepository()).persist(
        persisted_operations,
        state="PENDIENTE" if ready else "BLOQUEADA_SIN_ADAPTER",
    )
    if not ready:
        raise LabelsBlockedError("La emisión nacional todavía no está homologada.")
    return result


def recibir_cancel(
    payload,
    callback_token: str,
    *,
    repository: LabelRepository | None = None,
    config_loader: Callable[[str], dict | None] = configuracion_por_label_token,
) -> PersistResult:
    store_id = _authenticated_store(callback_token, config_loader=config_loader)
    operations = _cancel_operations(store_id, payload)
    (repository or PostgresLabelRepository()).persist(
        operations,
        state="BLOQUEADA_SIN_CANCELACION",
    )
    # Un 2xx cancelaría la etiqueta en Tiendanube. Sólo podrá devolverse cuando
    # el worker confirme una cancelación real e idempotente en el carrier.
    raise LabelsBlockedError("La cancelación nacional todavía no está homologada.")
