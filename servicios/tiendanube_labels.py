"""Recepción durable de callbacks Labels API de Tiendanube.

El módulo autentica el borde, valida el Fulfillment Order contra la tarifa
inmutable y lo vincula con su pedido antes de guardar PII. La generación se
delega al worker OCA por checkpoints y la cancelación exige confirmación del
carrier. Mientras los gates de UAT estén apagados, los callbacks fallan
cerrado y conservan sólo identificadores y huellas mínimas.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Callable, Mapping, Protocol, Sequence

from core.database import get_conn
from servicios.tiendanube_rate_quotes import (
    RateQuoteContractError,
    RateQuoteNotFoundError,
    RateQuoteSnapshotError,
    resolver_referencia,
    resolver_referencias,
)
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
    rate_quote_snapshot_id: str | None = None
    order_id: str | None = None


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
    from servicios.tiendanube_rate_quotes import ensure_rate_quote_storage

    ensure_shipping()
    ensure_rate_quote_storage()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tiendanube_labels (
                    store_id                TEXT NOT NULL,
                    label_id                TEXT NOT NULL,
                    fulfillment_order_id    TEXT NOT NULL,
                    rate_quote_snapshot_id  TEXT,
                    order_id                TEXT,
                    tiendanube_status       TEXT,
                    download_token_hash     TEXT,
                    download_token_revoked_at TIMESTAMPTZ,
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
                        ON DELETE CASCADE,
                    CONSTRAINT fk_tn_label_rate_quote_snapshot
                        FOREIGN KEY (store_id, rate_quote_snapshot_id)
                        REFERENCES tiendanube_rate_quote_snapshots(store_id, snapshot_id)
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
                CREATE TABLE IF NOT EXISTS tiendanube_fulfillment_order_orders (
                    store_id             TEXT NOT NULL,
                    fulfillment_order_id TEXT NOT NULL,
                    order_id             TEXT NOT NULL,
                    creada_en            TIMESTAMPTZ NOT NULL DEFAULT now(),
                    actualizada_en       TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (store_id, fulfillment_order_id),
                    FOREIGN KEY (store_id)
                        REFERENCES tiendanube_shipping_config(store_id)
                        ON DELETE CASCADE
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tiendanube_rate_quote_claims (
                    store_id             TEXT NOT NULL,
                    snapshot_id          TEXT NOT NULL,
                    fulfillment_order_id TEXT NOT NULL,
                    creada_en            TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (
                        store_id, snapshot_id, fulfillment_order_id
                    ),
                    FOREIGN KEY (store_id, snapshot_id)
                        REFERENCES tiendanube_rate_quote_snapshots(store_id, snapshot_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (store_id, fulfillment_order_id)
                        REFERENCES tiendanube_fulfillment_order_orders(
                            store_id, fulfillment_order_id
                        )
                        ON DELETE CASCADE
                )
                """
            )
            cur.execute(
                """
                DO $$
                DECLARE
                    restriccion RECORD;
                BEGIN
                    LOCK TABLE tiendanube_rate_quote_claims
                        IN ACCESS EXCLUSIVE MODE;

                    -- Migra solamente la PK legacy exacta. La búsqueda por
                    -- columnas mantiene el cambio seguro aunque la constraint
                    -- tenga un nombre no estándar.
                    FOR restriccion IN
                        SELECT c.conname
                          FROM pg_constraint c
                         WHERE c.conrelid =
                                   'tiendanube_rate_quote_claims'::regclass
                           AND c.contype = 'p'
                           AND (
                               SELECT ARRAY_AGG(a.attname ORDER BY u.ord)
                                 FROM UNNEST(c.conkey) WITH ORDINALITY
                                      u(attnum, ord)
                                 JOIN pg_attribute a
                                   ON a.attrelid = c.conrelid
                                  AND a.attnum = u.attnum
                           ) = ARRAY['store_id', 'snapshot_id']::name[]
                    LOOP
                        EXECUTE FORMAT(
                            'ALTER TABLE tiendanube_rate_quote_claims '
                            'DROP CONSTRAINT %I',
                            restriccion.conname
                        );
                    END LOOP;

                    IF NOT EXISTS (
                        SELECT 1
                          FROM pg_constraint c
                         WHERE c.conrelid =
                                   'tiendanube_rate_quote_claims'::regclass
                           AND c.contype = 'p'
                           AND (
                               SELECT ARRAY_AGG(a.attname ORDER BY u.ord)
                                 FROM UNNEST(c.conkey) WITH ORDINALITY
                                      u(attnum, ord)
                                 JOIN pg_attribute a
                                   ON a.attrelid = c.conrelid
                                  AND a.attnum = u.attnum
                           ) = ARRAY[
                               'store_id',
                               'snapshot_id',
                               'fulfillment_order_id'
                           ]::name[]
                    ) THEN
                        IF EXISTS (
                            SELECT 1
                              FROM pg_constraint c
                             WHERE c.conrelid =
                                       'tiendanube_rate_quote_claims'::regclass
                               AND c.contype = 'p'
                        ) THEN
                            RAISE EXCEPTION
                                'PK inesperada en tiendanube_rate_quote_claims';
                        END IF;
                        ALTER TABLE tiendanube_rate_quote_claims
                            ADD PRIMARY KEY (
                                store_id,
                                snapshot_id,
                                fulfillment_order_id
                            );
                    END IF;
                END $$
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
                ALTER TABLE tiendanube_labels
                    ADD COLUMN IF NOT EXISTS rate_quote_snapshot_id TEXT
                """
            )
            cur.execute(
                """
                ALTER TABLE tiendanube_labels
                    ADD COLUMN IF NOT EXISTS order_id TEXT;
                ALTER TABLE tiendanube_labels
                    ADD COLUMN IF NOT EXISTS tiendanube_status TEXT;
                ALTER TABLE tiendanube_labels
                    ADD COLUMN IF NOT EXISTS download_token_hash TEXT;
                ALTER TABLE tiendanube_labels
                    ADD COLUMN IF NOT EXISTS download_token_revoked_at TIMESTAMPTZ
                """
            )
            cur.execute(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                         WHERE conrelid = 'tiendanube_labels'::regclass
                           AND conname = 'fk_tn_label_rate_quote_snapshot'
                    ) THEN
                        ALTER TABLE tiendanube_labels
                            ADD CONSTRAINT fk_tn_label_rate_quote_snapshot
                            FOREIGN KEY (store_id, rate_quote_snapshot_id)
                            REFERENCES tiendanube_rate_quote_snapshots(store_id, snapshot_id)
                            ON DELETE CASCADE;
                    END IF;
                END $$
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


def _money(raw, field: str) -> tuple[Decimal, str]:
    if not isinstance(raw, Mapping):
        raise LabelsContractError(f"{field} no es válido.")
    try:
        value = Decimal(str(raw.get("value")))
    except (InvalidOperation, TypeError, ValueError):
        raise LabelsContractError(f"{field} no es válido.") from None
    currency = str(raw.get("currency") or "").strip().upper()
    if not value.is_finite() or value < 0 or len(currency) != 3:
        raise LabelsContractError(f"{field} no es válido.")
    return value, currency


def _decimal_equal(left, right) -> bool:
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return False


def _country_code(raw) -> str:
    if isinstance(raw, Mapping):
        raw = raw.get("code") or raw.get("name")
    value = str(raw or "").strip().upper()
    return "AR" if value in {"AR", "ARG", "ARGENTINA"} else value


def _positive_decimal(raw, field: str, *, allow_zero: bool = False) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        raise LabelsContractError(f"{field} no es válido.") from None
    if not value.is_finite() or value < 0 or (not allow_zero and value == 0):
        raise LabelsContractError(f"{field} no es válido.")
    return value


def _packages_totals(snapshot: Mapping) -> tuple[int, Decimal]:
    packages = snapshot.get("packages")
    if not isinstance(packages, list) or not packages:
        raise LabelsContractError("La tarifa aceptada no contiene bultos válidos.")
    quantity = 0
    weight = Decimal("0")
    for package in packages:
        if not isinstance(package, Mapping):
            raise LabelsContractError("La tarifa aceptada no contiene bultos válidos.")
        try:
            package_quantity = int(package.get("quantity"))
        except (TypeError, ValueError):
            raise LabelsContractError(
                "La tarifa aceptada no contiene bultos válidos."
            ) from None
        if isinstance(package.get("quantity"), bool) or package_quantity <= 0:
            raise LabelsContractError("La tarifa aceptada no contiene bultos válidos.")
        package_weight = _positive_decimal(
            package.get("weight_kg"), "El peso cotizado"
        )
        quantity += package_quantity
        weight += package_weight * package_quantity
    return quantity, weight


def _line_item_packages(fulfillment: Mapping) -> list[tuple]:
    line_items = fulfillment.get("line_items")
    if not isinstance(line_items, list) or not line_items:
        raise LabelsContractError("El Fulfillment Order no contiene sus ítems.")
    result: list[tuple] = []
    for item in line_items:
        dimension = item.get("unit_dimension") if isinstance(item, Mapping) else None
        if not isinstance(dimension, Mapping):
            raise LabelsContractError(
                "Un ítem no contiene peso y medidas verificables."
            )
        try:
            quantity = int(item.get("quantity"))
        except (TypeError, ValueError):
            raise LabelsContractError("La cantidad de un ítem no es válida.") from None
        if isinstance(item.get("quantity"), bool) or quantity <= 0:
            raise LabelsContractError("La cantidad de un ítem no es válida.")
        result.append(
            (
                quantity,
                _positive_decimal(dimension.get("weight"), "El peso de un ítem"),
                _positive_decimal(dimension.get("depth"), "El largo de un ítem"),
                _positive_decimal(dimension.get("width"), "El ancho de un ítem"),
                _positive_decimal(dimension.get("height"), "El alto de un ítem"),
            )
        )
    return sorted(result)


def _snapshot_packages(snapshot: Mapping) -> list[tuple]:
    result: list[tuple] = []
    for package in snapshot.get("packages") or []:
        if not isinstance(package, Mapping):
            raise LabelsContractError("La tarifa aceptada no contiene bultos válidos.")
        try:
            quantity = int(package.get("quantity"))
        except (TypeError, ValueError):
            raise LabelsContractError(
                "La tarifa aceptada no contiene bultos válidos."
            ) from None
        result.append(
            (
                quantity,
                _positive_decimal(package.get("weight_kg"), "El peso cotizado"),
                _positive_decimal(package.get("length_cm"), "El largo cotizado"),
                _positive_decimal(package.get("width_cm"), "El ancho cotizado"),
                _positive_decimal(package.get("height_cm"), "El alto cotizado"),
            )
        )
    return sorted(result)


def _reference_from_generate(operation: LabelOperation) -> tuple[str, Mapping]:
    fulfillment = operation.payload.get("fulfillment_order_info")
    if not isinstance(fulfillment, Mapping):
        raise LabelsContractError("Falta fulfillment_order_info.")
    shipping = fulfillment.get("shipping")
    option = shipping.get("option") if isinstance(shipping, Mapping) else None
    if not isinstance(option, Mapping):
        raise LabelsContractError("La etiqueta no identifica la opción de envío.")
    if str(option.get("code") or "").strip() != "tauro_nacional_domicilio":
        raise LabelsContractError("La etiqueta no pertenece al servicio nacional TAURO.")
    reference = str(option.get("reference") or "").strip()
    if not reference:
        raise LabelsContractError("La etiqueta no contiene la cotización aceptada.")
    return reference, fulfillment


def _validate_fulfillment_snapshot(fulfillment: Mapping, snapshot: Mapping) -> None:
    shipping = fulfillment.get("shipping")
    if not isinstance(shipping, Mapping) or str(shipping.get("type") or "") != "ship":
        raise LabelsContractError("La etiqueta no corresponde a un envío a domicilio.")
    carrier = shipping.get("carrier")
    if not isinstance(carrier, Mapping) or str(carrier.get("code") or "") != "api":
        raise LabelsContractError("La etiqueta no pertenece a un carrier API.")
    merchant_value, merchant_currency = _money(
        shipping.get("merchant_cost"), "El costo del comercio"
    )
    consumer_value, consumer_currency = _money(
        shipping.get("consumer_cost"), "El costo del comprador"
    )
    expected_currency = str(snapshot.get("price_currency") or "").strip().upper()
    if (
        merchant_currency != expected_currency
        or consumer_currency != expected_currency
        or not _decimal_equal(merchant_value, snapshot.get("tauro_price"))
        or not _decimal_equal(
            consumer_value,
            snapshot.get("expected_consumer_price", snapshot.get("buyer_price")),
        )
    ):
        raise LabelsContractError("Los importes de la orden no coinciden con la tarifa.")

    total_value, total_currency = _money(
        fulfillment.get("total_price"), "El valor declarado de la orden"
    )
    if (
        total_currency != str(snapshot.get("declared_currency") or "").strip().upper()
        or not _decimal_equal(total_value, snapshot.get("declared_value"))
    ):
        raise LabelsContractError("El valor declarado cambió desde la cotización.")

    destination = fulfillment.get("destination")
    destination_route = snapshot.get("destination_route")
    destination_zip = "".join(
        character
        for character in str(
            destination.get("zipcode") if isinstance(destination, Mapping) else ""
        )
        if character.isdigit()
    )
    expected_zip = str(
        destination_route.get("postal_code")
        if isinstance(destination_route, Mapping)
        else ""
    )
    destination_country = _country_code(
        destination.get("country") if isinstance(destination, Mapping) else None
    )
    expected_destination_country = str(
        destination_route.get("country")
        if isinstance(destination_route, Mapping)
        else ""
    ).upper()
    if (
        destination_zip != expected_zip
        or destination_country != expected_destination_country
    ):
        raise LabelsContractError("El destino no coincide con la tarifa aceptada.")

    assigned = fulfillment.get("assigned_location")
    origin_address = assigned.get("address") if isinstance(assigned, Mapping) else None
    origin_route = snapshot.get("origin_route")
    origin_zip = "".join(
        character
        for character in str(
            origin_address.get("zipcode")
            if isinstance(origin_address, Mapping)
            else ""
        )
        if character.isdigit()
    )
    expected_origin_zip = str(
        origin_route.get("postal_code") if isinstance(origin_route, Mapping) else ""
    )
    origin_country = _country_code(
        origin_address.get("country") if isinstance(origin_address, Mapping) else None
    )
    expected_origin_country = str(
        origin_route.get("country") if isinstance(origin_route, Mapping) else ""
    ).upper()
    expected_location = str(
        origin_route.get("location_id") if isinstance(origin_route, Mapping) else ""
    )
    assigned_location = str(
        assigned.get("location_id") if isinstance(assigned, Mapping) else ""
    )
    if (
        origin_zip != expected_origin_zip
        or origin_country != expected_origin_country
        or (expected_location and assigned_location != expected_location)
    ):
        raise LabelsContractError("El origen no coincide con la tarifa aceptada.")

    expected_quantity, expected_weight = _packages_totals(snapshot)
    try:
        total_quantity = int(fulfillment.get("total_quantity"))
    except (TypeError, ValueError):
        raise LabelsContractError("La cantidad total de la orden no es válida.") from None
    total_weight = _positive_decimal(
        fulfillment.get("total_weight"), "El peso total de la orden"
    )
    if total_quantity != expected_quantity or total_weight != expected_weight:
        raise LabelsContractError("Los bultos cambiaron desde la cotización aceptada.")

    if "paquetes_tienda" not in str(snapshot.get("pricing_mode") or ""):
        if _line_item_packages(fulfillment) != _snapshot_packages(snapshot):
            raise LabelsContractError(
                "El peso o las medidas cambiaron desde la cotización aceptada."
            )


def _bind_rate_quotes(
    operations: Sequence[LabelOperation],
    *,
    customer_id: str,
    quote_resolver: Callable[..., Mapping],
) -> tuple[LabelOperation, ...]:
    references: list[tuple[LabelOperation, str, Mapping]] = []
    for operation in operations:
        reference, fulfillment = _reference_from_generate(operation)
        references.append((operation, reference, fulfillment))
    resolved_batch: dict[str, Mapping] | None = None
    if quote_resolver is resolver_referencia:
        try:
            resolved_batch = resolver_referencias(
                operations[0].store_id,
                customer_id,
                tuple(reference for _operation, reference, _fulfillment in references),
            )
        except (RateQuoteContractError, RateQuoteNotFoundError) as exc:
            raise LabelsContractError("La cotización aceptada no es válida.") from exc
        except RateQuoteSnapshotError as exc:
            raise LabelsUnavailableError("No se pudo verificar la cotización.") from exc
    bound: list[LabelOperation] = []
    for operation, reference, fulfillment in references:
        try:
            snapshot = (
                resolved_batch[reference]
                if resolved_batch is not None
                else quote_resolver(
                    operation.store_id,
                    customer_id,
                    reference,
                )
            )
        except (RateQuoteContractError, RateQuoteNotFoundError) as exc:
            raise LabelsContractError("La cotización aceptada no es válida.") from exc
        except RateQuoteSnapshotError as exc:
            raise LabelsUnavailableError("No se pudo verificar la cotización.") from exc
        _validate_fulfillment_snapshot(fulfillment, snapshot)
        snapshot_id = str(snapshot.get("snapshot_id") or "").strip()
        if not snapshot_id:
            raise LabelsUnavailableError("La cotización no tiene identidad durable.")
        bound.append(replace(operation, rate_quote_snapshot_id=snapshot_id))
    return tuple(bound)


def registrar_fulfillment_orders(
    store_id: str,
    order_id: str,
    fulfillment_order_ids: Sequence[str],
) -> None:
    """Fija FFO→orden sin permitir que un ID se reasigne a otra compra."""
    store = _identifier(store_id, "store_id")
    order = _identifier(order_id, "order_id")
    fulfillment_ids = tuple(
        dict.fromkeys(
            _identifier(value, "fulfillment_order_id")
            for value in fulfillment_order_ids
        )
    )
    if not fulfillment_ids:
        return
    _ensure_tables()
    domain = f"{store}.tiendanube"
    with get_conn() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"tauro:tiendanube:{domain}",),
                )
                for fulfillment_id in fulfillment_ids:
                    cur.execute(
                        """
                        INSERT INTO tiendanube_fulfillment_order_orders
                            (store_id, fulfillment_order_id, order_id)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (store_id, fulfillment_order_id) DO NOTHING
                        """,
                        (store, fulfillment_id, order),
                    )
                    cur.execute(
                        """
                        SELECT order_id
                          FROM tiendanube_fulfillment_order_orders
                         WHERE store_id = %s AND fulfillment_order_id = %s
                         FOR UPDATE
                        """,
                        (store, fulfillment_id),
                    )
                    row = cur.fetchone()
                    if not row or str(row.get("order_id") or "") != order:
                        raise LabelsConflictError(
                            "El Fulfillment Order ya pertenece a otra orden."
                        )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _load_order_context(store_id: str, fulfillment_order_id: str) -> dict | None:
    _ensure_tables()
    domain = f"{store_id}.tiendanube"
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT m.order_id,
                           EXISTS (
                               SELECT 1
                                 FROM tiendanube_pedidos_redactados r
                                WHERE r.dominio = %s
                                  AND r.pedido_externo_id = m.order_id
                           ) AS redacted
                      FROM tiendanube_fulfillment_order_orders m
                     WHERE m.store_id = %s AND m.fulfillment_order_id = %s
                    """,
                    (domain, store_id, fulfillment_order_id),
                )
                row = cur.fetchone()
    except Exception as exc:
        raise LabelsUnavailableError(
            "No se pudo vincular la etiqueta con su orden."
        ) from exc
    return dict(row) if row else None


def _bind_order_context(
    operations: Sequence[LabelOperation],
    *,
    order_context_loader: Callable[[str, str], Mapping | None],
) -> tuple[LabelOperation, ...]:
    bound: list[LabelOperation] = []
    for operation in operations:
        try:
            context = order_context_loader(
                operation.store_id, operation.fulfillment_order_id
            )
        except LabelsContractError:
            raise
        except LabelsUnavailableError:
            raise
        except Exception as exc:
            raise LabelsUnavailableError(
                "No se pudo vincular la etiqueta con su orden."
            ) from exc
        order_id = str((context or {}).get("order_id") or "").strip()
        if not order_id:
            raise LabelsUnavailableError(
                "El Fulfillment Order todavía no está vinculado a su orden."
            )
        if bool((context or {}).get("redacted")):
            raise LabelsContractError("La orden fue eliminada por privacidad.")
        bound.append(replace(operation, order_id=_identifier(order_id, "order_id")))
    return tuple(bound)


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
                    for store_id in sorted({operation.store_id for operation in operations}):
                        cur.execute(
                            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                            (f"tauro:tiendanube:{store_id}.tiendanube",),
                        )
                    for operation in operations:
                        if operation.operation == "GENERATE" and operation.payload_complete:
                            cur.execute(
                                """
                                SELECT m.order_id,
                                       EXISTS (
                                           SELECT 1
                                             FROM tiendanube_pedidos_redactados r
                                            WHERE r.dominio = %s
                                              AND r.pedido_externo_id = m.order_id
                                       ) AS redacted
                                  FROM tiendanube_fulfillment_order_orders m
                                 WHERE m.store_id = %s
                                   AND m.fulfillment_order_id = %s
                                 FOR SHARE OF m
                                """,
                                (
                                    f"{operation.store_id}.tiendanube",
                                    operation.store_id,
                                    operation.fulfillment_order_id,
                                ),
                            )
                            context = cur.fetchone()
                            context = dict(context) if context else {}
                            if (
                                not operation.order_id
                                or str(context.get("order_id") or "")
                                != operation.order_id
                            ):
                                raise LabelsConflictError(
                                    "La etiqueta no pertenece a la orden registrada."
                                )
                            if context.get("redacted"):
                                raise LabelsContractError(
                                    "La orden fue eliminada por privacidad."
                                )
                            cur.execute(
                                """
                                INSERT INTO tiendanube_rate_quote_claims
                                    (store_id, snapshot_id, fulfillment_order_id)
                                VALUES (%s, %s, %s)
                                ON CONFLICT (
                                    store_id,
                                    snapshot_id,
                                    fulfillment_order_id
                                ) DO NOTHING
                                """,
                                (
                                    operation.store_id,
                                    operation.rate_quote_snapshot_id,
                                    operation.fulfillment_order_id,
                                ),
                            )
                            cur.execute(
                                """
                                SELECT 1 AS claim_exists
                                  FROM tiendanube_rate_quote_claims
                                 WHERE store_id = %s
                                   AND snapshot_id = %s
                                   AND fulfillment_order_id = %s
                                 FOR UPDATE
                                """,
                                (
                                    operation.store_id,
                                    operation.rate_quote_snapshot_id,
                                    operation.fulfillment_order_id,
                                ),
                            )
                            claim = cur.fetchone()
                            if not claim:
                                raise LabelsUnavailableError(
                                    "No se pudo registrar el uso de la cotización."
                                )
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
                                 rate_quote_snapshot_id, order_id,
                                 generate_payload, generate_fingerprint,
                                 generate_payload_complete, estado)
                            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                            ON CONFLICT (store_id, label_id) DO NOTHING
                            RETURNING store_id
                            """,
                            (
                                operation.store_id,
                                operation.label_id,
                                operation.fulfillment_order_id,
                                operation.rate_quote_snapshot_id,
                                operation.order_id,
                                generate_payload,
                                generate_fingerprint,
                                operation.payload_complete,
                                state,
                            ),
                        )
                        inserted_label = cur.fetchone()
                        cur.execute(
                            """
                            SELECT fulfillment_order_id, rate_quote_snapshot_id,
                                   order_id, generate_fingerprint,
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
                        current_order = str(current.get("order_id") or "")
                        if (
                            operation.order_id
                            and current_order
                            and current_order != operation.order_id
                        ):
                            raise LabelsConflictError(
                                "El label_id ya pertenece a otra orden."
                            )

                        current_snapshot = str(
                            current.get("rate_quote_snapshot_id") or ""
                        )
                        if (
                            operation.operation == "GENERATE"
                            and current_snapshot
                            and current_snapshot
                            != str(operation.rate_quote_snapshot_id or "")
                        ):
                            raise LabelsConflictError(
                                "El label_id ya pertenece a otra cotización."
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
                                           rate_quote_snapshot_id = %s,
                                           order_id = %s,
                                           estado = %s,
                                           actualizada_en = now()
                                     WHERE store_id = %s AND label_id = %s
                                    """,
                                    (
                                        generate_payload,
                                        operation.fingerprint,
                                        operation.payload_complete,
                                        operation.rate_quote_snapshot_id,
                                        operation.order_id,
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
    """Gate único del worker: apagado ante cualquier dato ausente o ambiguo."""
    enabled = lambda name: str(os.getenv(name) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not all(
        enabled(name)
        for name in (
            "TIENDANUBE_LABELS_WORKER_ENABLED",
            "TIENDANUBE_SHIPPING_ENABLED",
            "TAURO_NACIONAL_RATES_READY",
        )
    ):
        return False
    if len(str(os.getenv("TIENDANUBE_LABEL_DOWNLOAD_SECRET") or "").strip()) < 32:
        return False
    try:
        from servicios.oca_adapter import registration_status

        if not registration_status().get("fulfillment_ready"):
            return False
    except Exception:
        return False
    environment = str(os.getenv("OCA_ENVIRONMENT") or "qa").strip().lower()
    if environment == "production" and not enabled("TIENDANUBE_HOMOLOGATION_APPROVED"):
        return False
    return environment in {"qa", "production"}


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
    installation_loader: Callable[[str], dict | None] | None = None,
    quote_resolver: Callable[..., Mapping] = resolver_referencia,
    order_context_loader: Callable[[str, str], Mapping | None] = _load_order_context,
) -> PersistResult:
    store_id = _authenticated_store(callback_token, config_loader=config_loader)
    operations = _generate_operations(store_id, payload)
    ready = bool(execution_ready())
    if ready:
        if installation_loader is None:
            from servicios.tiendanube_app import instalacion

            installation_loader = instalacion
        try:
            installation = installation_loader(store_id)
        except Exception as exc:
            raise LabelsUnavailableError("No se pudo validar la tienda.") from exc
        customer_id = str((installation or {}).get("cliente_id") or "").strip()
        if not customer_id:
            raise LabelsUnavailableError("La tienda no está vinculada a TAURO.")
        operations = _bind_order_context(
            operations,
            order_context_loader=order_context_loader,
        )
        persisted_operations = _bind_rate_quotes(
            operations,
            customer_id=customer_id,
            quote_resolver=quote_resolver,
        )
    else:
        persisted_operations = _minimize_blocked_generate(operations)
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
    config_loader: Callable[[str], dict | None] = configuracion_por_label_token,
    execution_ready: Callable[[], bool] = labels_execution_ready,
    cancel_service: Callable[..., object] | None = None,
):
    store_id = _authenticated_store(callback_token, config_loader=config_loader)
    operations = _cancel_operations(store_id, payload)
    if not execution_ready():
        # No se crea una intención CANCEL separada mientras la capacidad está
        # apagada: un 4xx/5xx mantiene el estado en Tiendanube y el merchant
        # puede reintentar después de la homologación.
        raise LabelsBlockedError("La cancelación nacional todavía no está homologada.")
    if cancel_service is None:
        from servicios.tiendanube_label_cancel import cancel_labels

        cancel_service = cancel_labels
    try:
        return cancel_service(
            store_id,
            [dict(operation.payload) for operation in operations],
        )
    except LabelsContractError:
        raise
    except Exception as exc:
        from servicios.tiendanube_label_cancel import LabelCancelContractError

        if isinstance(exc, LabelCancelContractError):
            raise LabelsContractError("La cancelación no es válida.") from exc
        raise LabelsUnavailableError(
            "No se pudo procesar la cancelación con seguridad."
        ) from exc
