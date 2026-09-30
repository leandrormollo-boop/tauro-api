"""Snapshots inmutables de tarifas nacionales aceptadas por Tiendanube.

El identificador que sale al checkout nunca es el ``quote_id`` privado del
operador. Apunta a una fila inmutable que congela ruta mínima, bultos, valor,
servicio y los tres importes (operador, TAURO y comprador). Así la emisión de
la etiqueta puede reproducir exactamente la cotización aceptada sin volver a
cotizar ni persistir datos personales del destinatario.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Mapping

from core.database import get_conn
from servicios.carrier_adapter import OperationState, QuoteRequest, QuoteResult


REFERENCE_PREFIX = "tauro:"
_SNAPSHOT_ID_RE = re.compile(r"^tnq_[0-9a-f]{64}$")
_CARRIER_RE = re.compile(r"^[a-z0-9_-]{2,32}$")
_tabla_lista = False


class RateQuoteSnapshotError(RuntimeError):
    """Base segura para errores de persistencia o resolución."""


class RateQuoteContractError(RateQuoteSnapshotError):
    pass


class RateQuoteNotFoundError(RateQuoteSnapshotError):
    pass


def _decimal(value, field: str, *, allow_zero: bool = False) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise RateQuoteContractError(f"{field} no es un decimal válido.") from None
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        raise RateQuoteContractError(f"{field} no es un importe válido.")
    return result


def _decimal_text(value, field: str, *, allow_zero: bool = False) -> str:
    result = _decimal(value, field, allow_zero=allow_zero)
    if result == 0:
        return "0"
    return format(result.normalize(), "f")


def _route(raw: Mapping, field: str) -> dict:
    if not isinstance(raw, Mapping):
        raise RateQuoteContractError(f"{field} no es una ruta válida.")
    country = str(raw.get("country") or raw.get("pais") or "").strip().upper()
    postal_code = "".join(
        char
        for char in str(raw.get("postal_code") or raw.get("cp") or "")
        if char.isdigit()
    )
    if country != "AR" or len(postal_code) != 4:
        raise RateQuoteContractError(f"{field} debe ser una ruta nacional válida.")
    # Deliberadamente no se conservan calle, número, nombre, email ni teléfono.
    return {
        "country": country,
        "postal_code": postal_code,
        "location_id": str(raw.get("location_id") or "").strip()[:120],
    }


def _packages(request: QuoteRequest) -> list[dict]:
    result: list[dict] = []
    for package in request.packages:
        if isinstance(package.quantity, bool) or package.quantity <= 0:
            raise RateQuoteContractError("La cantidad de un bulto no es válida.")
        result.append(
            {
                "quantity": int(package.quantity),
                "weight_kg": _decimal_text(package.weight_kg, "El peso"),
                "length_cm": _decimal_text(package.length_cm, "El largo"),
                "width_cm": _decimal_text(package.width_cm, "El ancho"),
                "height_cm": _decimal_text(package.height_cm, "El alto"),
            }
        )
    if not result:
        raise RateQuoteContractError("La cotización no contiene bultos.")
    return result


def _expiry(raw: str) -> str | None:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RateQuoteContractError("El vencimiento de la tarifa no es válido.") from None
    if parsed.tzinfo is None:
        raise RateQuoteContractError("El vencimiento de la tarifa necesita zona horaria.")
    return parsed.astimezone(timezone.utc).isoformat()


def _currency(value, field: str) -> str:
    result = str(value or "").strip().upper()
    if len(result) != 3 or not result.isalpha():
        raise RateQuoteContractError(f"{field} no es una moneda válida.")
    return result


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
        raise RateQuoteContractError("La cotización no se puede canonicalizar.") from None


def _content(
    store_id: str,
    request: QuoteRequest,
    quote: QuoteResult,
    buyer_price: Decimal,
    pricing_mode: str,
    platform_additional_cost: Decimal = Decimal("0"),
    platform_option_id: str = "",
) -> dict:
    store = str(store_id or "").strip()
    customer = str(request.customer_id or "").strip()
    carrier = str(quote.carrier_id or "").strip().lower()
    mode = str(pricing_mode or "").strip().lower()
    if not store.isdigit() or not customer:
        raise RateQuoteContractError("La tienda o el cliente no son válidos.")
    if not _CARRIER_RE.fullmatch(carrier):
        raise RateQuoteContractError("El operador no es válido.")
    if quote.state != OperationState.COTIZADO:
        raise RateQuoteContractError("Sólo se pueden congelar tarifas cotizadas.")
    if not quote.quote_id or not quote.service_code or not quote.service_name:
        raise RateQuoteContractError("La tarifa no identifica el servicio.")
    if not mode or len(mode) > 80:
        raise RateQuoteContractError("El modo comercial no es válido.")
    additional_cost = _decimal(
        platform_additional_cost,
        "El costo adicional configurado en Tiendanube",
        allow_zero=True,
    )
    base_buyer_price = _decimal(
        buyer_price, "El precio comprador", allow_zero=True
    )
    # Tiendanube aplica el envío gratis después de recibir la tarifa. Para un
    # carrito 100% bonificado debemos devolver el costo real, pero el comprador
    # termina pagando cero. En los demás escenarios, la plataforma suma el
    # adicional configurado por el merchant al precio que devolvió TAURO.
    expected_consumer_price = (
        Decimal("0")
        if mode.split(":", 1)[0] == "gratis_total"
        else base_buyer_price + additional_cost
    )

    metadata = request.metadata if isinstance(request.metadata, Mapping) else {}
    metadata_store = str(metadata.get("store_id") or "").strip()
    if metadata_store and metadata_store != store:
        raise RateQuoteContractError("La cotización pertenece a otra tienda.")

    try:
        estimated_days = int(quote.estimated_days or 0)
    except (TypeError, ValueError):
        raise RateQuoteContractError("El plazo estimado no es válido.") from None
    if isinstance(quote.estimated_days, bool) or estimated_days <= 0:
        raise RateQuoteContractError("El plazo estimado no es válido.")

    return {
        "store_id": store,
        "customer_id": customer,
        "request_id": str(request.request_id or "").strip(),
        "cart_id": str(metadata.get("cart_id") or "").strip()[:200],
        "carrier_id": carrier,
        "carrier_quote_id": str(quote.quote_id).strip(),
        "service_code": str(quote.service_code).strip(),
        "service_name": str(quote.service_name).strip(),
        "origin_route": _route(request.origin, "El origen"),
        "destination_route": _route(request.destination, "El destino"),
        "packages": _packages(request),
        "declared_value": _decimal_text(request.declared_value, "El valor declarado"),
        "declared_currency": _currency(
            request.declared_currency, "La moneda declarada"
        ),
        "origin_mode": str(request.origin_mode or "").strip().lower(),
        "destination_mode": str(request.destination_mode or "").strip().lower(),
        "carrier_cost": _decimal_text(quote.carrier_cost, "El costo del operador"),
        "carrier_currency": _currency(
            quote.carrier_currency, "La moneda del operador"
        ),
        "tauro_price": _decimal_text(quote.customer_price, "El precio TAURO"),
        "buyer_price": _decimal_text(
            base_buyer_price, "El precio comprador", allow_zero=True
        ),
        "platform_additional_cost": _decimal_text(
            additional_cost,
            "El costo adicional configurado en Tiendanube",
            allow_zero=True,
        ),
        "expected_consumer_price": _decimal_text(
            expected_consumer_price,
            "El precio final esperado del comprador",
            allow_zero=True,
        ),
        "platform_option_id": str(platform_option_id or "").strip()[:120],
        "price_currency": _currency(quote.currency, "La moneda del precio"),
        "estimated_days": estimated_days,
        "carrier_expires_at": _expiry(quote.expires_at_iso),
        "pricing_mode": mode,
    }


def construir_snapshot(
    store_id: str,
    request: QuoteRequest,
    quote: QuoteResult,
    buyer_price: Decimal,
    pricing_mode: str,
    *,
    platform_additional_cost: Decimal = Decimal("0"),
    platform_option_id: str = "",
    quoted_at: datetime | None = None,
) -> dict:
    """Construye una representación canónica, mínima y libre de PII."""
    content = _content(
        store_id,
        request,
        quote,
        buyer_price,
        pricing_mode,
        platform_additional_cost,
        platform_option_id,
    )
    digest = hashlib.sha256(_canonical_json(content).encode("utf-8")).hexdigest()
    result = dict(content)
    result["snapshot_id"] = f"tnq_{digest}"
    result["snapshot_sha256"] = digest
    result["quoted_at"] = quoted_at or datetime.now(timezone.utc)
    return result


def _ensure_table() -> None:
    """Comprueba el esquema durable sin migrarlo durante el checkout.

    ``sql/schema.sql`` se aplica antes de habilitar tráfico. Si la tabla quedó
    ausente o incompleta, la cotización falla cerrada; el callback nunca toma
    locks DDL ni intenta reparar producción mientras responde a Tiendanube.
    """
    global _tabla_lista
    if _tabla_lista:
        return
    required_columns = (
        "snapshot_id", "store_id", "customer_id", "request_id", "cart_id",
        "carrier_id", "carrier_quote_id", "service_code", "service_name",
        "origin_route", "destination_route", "packages", "declared_value",
        "declared_currency", "origin_mode", "destination_mode",
        "carrier_cost", "carrier_currency", "tauro_price", "buyer_price",
        "platform_additional_cost", "expected_consumer_price",
        "platform_option_id", "price_currency", "estimated_days",
        "carrier_expires_at", "pricing_mode", "snapshot_sha256", "quoted_at",
    )
    required_checks = (
        "ck_tn_rate_quote_snapshot_id",
        "ck_tn_rate_quote_amounts",
        "ck_tn_rate_quote_carrier",
        "ck_tn_rate_quote_json",
        "ck_tn_rate_quote_days",
        "ck_tn_rate_quote_platform_amounts",
    )
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH relation AS (
                        SELECT to_regclass(
                            'tiendanube_rate_quote_snapshots'
                        ) AS oid
                    )
                    SELECT
                        r.oid IS NOT NULL
                        AND (
                            SELECT COUNT(*) = %s
                              FROM information_schema.columns
                             WHERE table_schema = CURRENT_SCHEMA()
                               AND table_name =
                                   'tiendanube_rate_quote_snapshots'
                               AND column_name = ANY(%s::text[])
                        )
                        AND (
                            SELECT COUNT(DISTINCT c.conname) = %s
                              FROM pg_constraint c
                             WHERE c.conrelid = r.oid
                               AND c.contype = 'c'
                               AND c.convalidated
                               AND c.conname = ANY(%s::text[])
                        )
                        AND EXISTS (
                            SELECT 1
                              FROM pg_constraint c
                             WHERE c.conrelid = r.oid
                               AND c.contype = 'p'
                               AND c.convalidated
                        )
                        AND EXISTS (
                            SELECT 1
                              FROM pg_constraint c
                             WHERE c.conrelid = r.oid
                               AND c.contype = 'u'
                               AND c.convalidated
                        )
                        AND EXISTS (
                            SELECT 1
                              FROM pg_trigger t
                             WHERE t.tgrelid = r.oid
                               AND t.tgname =
                                   'trg_bloquear_tn_rate_quote_update'
                               AND NOT t.tgisinternal
                               AND t.tgenabled IN ('O', 'A')
                        ) AS schema_ready
                      FROM relation r
                    """,
                    (
                        len(required_columns),
                        list(required_columns),
                        len(required_checks),
                        list(required_checks),
                    ),
                )
                row = cur.fetchone()
    except Exception as exc:
        raise RateQuoteSnapshotError(
            "No se pudo verificar el esquema de tarifas Tiendanube."
        ) from exc

    ready = bool(
        row.get("schema_ready")
        if hasattr(row, "get")
        else row[0] if row else False
    )
    if not ready:
        raise RateQuoteSnapshotError(
            "El esquema de tarifas Tiendanube no está migrado; aplicá "
            "sql/schema.sql antes de habilitar tráfico."
        )
    _tabla_lista = True


def ensure_rate_quote_storage() -> None:
    """Punto público de readiness para servicios dependientes."""
    _ensure_table()


def guardar_snapshot(
    store_id: str,
    request: QuoteRequest,
    quote: QuoteResult,
    buyer_price: Decimal,
    pricing_mode: str,
    *,
    platform_additional_cost: Decimal = Decimal("0"),
    platform_option_id: str = "",
) -> dict:
    """Inserta una vez; un replay idéntico conserva el mismo identificador."""
    snapshot = construir_snapshot(
        store_id,
        request,
        quote,
        buyer_price,
        pricing_mode,
        platform_additional_cost=platform_additional_cost,
        platform_option_id=platform_option_id,
    )
    try:
        _ensure_table()
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                """
                INSERT INTO tiendanube_rate_quote_snapshots (
                    snapshot_id, store_id, customer_id, request_id, cart_id,
                    carrier_id, carrier_quote_id, service_code, service_name,
                    origin_route, destination_route, packages, declared_value,
                    declared_currency, origin_mode, destination_mode,
                    carrier_cost, carrier_currency, tauro_price, buyer_price,
                    platform_additional_cost, expected_consumer_price,
                    platform_option_id, price_currency, estimated_days, carrier_expires_at,
                    pricing_mode, snapshot_sha256, quoted_at
                ) VALUES (
                    %(snapshot_id)s, %(store_id)s, %(customer_id)s,
                    %(request_id)s, %(cart_id)s, %(carrier_id)s,
                    %(carrier_quote_id)s, %(service_code)s, %(service_name)s,
                    %(origin_route)s::jsonb, %(destination_route)s::jsonb,
                    %(packages)s::jsonb, %(declared_value)s,
                    %(declared_currency)s, %(origin_mode)s,
                    %(destination_mode)s, %(carrier_cost)s,
                    %(carrier_currency)s, %(tauro_price)s, %(buyer_price)s,
                    %(platform_additional_cost)s, %(expected_consumer_price)s,
                    %(platform_option_id)s, %(price_currency)s, %(estimated_days)s,
                    %(carrier_expires_at)s, %(pricing_mode)s,
                    %(snapshot_sha256)s, %(quoted_at)s
                )
                ON CONFLICT (snapshot_id) DO NOTHING
                """,
                    {
                        **snapshot,
                        "origin_route": _canonical_json(snapshot["origin_route"]),
                        "destination_route": _canonical_json(
                            snapshot["destination_route"]
                        ),
                        "packages": _canonical_json(snapshot["packages"]),
                    },
                )
                cur.execute(
                """
                SELECT * FROM tiendanube_rate_quote_snapshots
                 WHERE snapshot_id = %s
                """,
                    (snapshot["snapshot_id"],),
                )
                row = cur.fetchone()
            conn.commit()
    except RateQuoteSnapshotError:
        raise
    except Exception as exc:
        raise RateQuoteSnapshotError(
            "No se pudo persistir la tarifa aceptada."
        ) from exc
    if not row:
        raise RateQuoteSnapshotError("No se pudo confirmar la tarifa congelada.")
    resolved = dict(row)
    if (
        str(resolved.get("store_id") or "") != snapshot["store_id"]
        or str(resolved.get("customer_id") or "") != snapshot["customer_id"]
        or str(resolved.get("snapshot_sha256") or "") != snapshot["snapshot_sha256"]
    ):
        raise RateQuoteSnapshotError("La referencia de tarifa está en conflicto.")
    return snapshot


def referencia_publica(snapshot: Mapping) -> str:
    snapshot_id = str((snapshot or {}).get("snapshot_id") or "")
    carrier_id = str((snapshot or {}).get("carrier_id") or "").lower()
    if not _SNAPSHOT_ID_RE.fullmatch(snapshot_id) or not _CARRIER_RE.fullmatch(carrier_id):
        raise RateQuoteContractError("El snapshot no admite una referencia pública.")
    return f"{REFERENCE_PREFIX}{carrier_id}:{snapshot_id}"


def _parse_reference(reference: str) -> tuple[str, str]:
    parts = str(reference or "").strip().split(":")
    if len(parts) != 3 or parts[0] != "tauro":
        raise RateQuoteContractError("La referencia de tarifa no es válida.")
    carrier_id, snapshot_id = parts[1].lower(), parts[2]
    if not _CARRIER_RE.fullmatch(carrier_id) or not _SNAPSHOT_ID_RE.fullmatch(snapshot_id):
        raise RateQuoteContractError("La referencia de tarifa no es válida.")
    return carrier_id, snapshot_id


def _row_content(row: Mapping) -> dict:
    return {
        "store_id": str(row["store_id"]),
        "customer_id": str(row["customer_id"]),
        "request_id": str(row["request_id"]),
        "cart_id": str(row.get("cart_id") or ""),
        "carrier_id": str(row["carrier_id"]),
        "carrier_quote_id": str(row["carrier_quote_id"]),
        "service_code": str(row["service_code"]),
        "service_name": str(row["service_name"]),
        "origin_route": dict(row["origin_route"]),
        "destination_route": dict(row["destination_route"]),
        "packages": list(row["packages"]),
        "declared_value": _decimal_text(row["declared_value"], "El valor declarado"),
        "declared_currency": str(row["declared_currency"]).strip(),
        "origin_mode": str(row["origin_mode"]),
        "destination_mode": str(row["destination_mode"]),
        "carrier_cost": _decimal_text(row["carrier_cost"], "El costo del operador"),
        "carrier_currency": str(row["carrier_currency"]).strip(),
        "tauro_price": _decimal_text(row["tauro_price"], "El precio TAURO"),
        "buyer_price": _decimal_text(
            row["buyer_price"], "El precio comprador", allow_zero=True
        ),
        "platform_additional_cost": _decimal_text(
            row.get("platform_additional_cost") or 0,
            "El costo adicional configurado en Tiendanube",
            allow_zero=True,
        ),
        "expected_consumer_price": _decimal_text(
            row.get("expected_consumer_price") or 0,
            "El precio final esperado del comprador",
            allow_zero=True,
        ),
        "platform_option_id": str(row.get("platform_option_id") or ""),
        "price_currency": str(row["price_currency"]).strip(),
        "estimated_days": int(row["estimated_days"]),
        "carrier_expires_at": (
            row["carrier_expires_at"].astimezone(timezone.utc).isoformat()
            if row.get("carrier_expires_at")
            else None
        ),
        "pricing_mode": str(row["pricing_mode"]),
    }


def resolver_referencia(
    store_id: str,
    customer_id: str,
    reference: str,
    *,
    expected_carrier_id: str | None = None,
) -> dict:
    """Resuelve y verifica una referencia antes de cualquier llamada al courier."""
    carrier_id, snapshot_id = _parse_reference(reference)
    expected = str(expected_carrier_id or carrier_id).strip().lower()
    if carrier_id != expected:
        raise RateQuoteNotFoundError("La tarifa pertenece a otro operador.")
    try:
        _ensure_table()
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                """
                SELECT * FROM tiendanube_rate_quote_snapshots
                 WHERE snapshot_id = %s
                   AND store_id = %s
                   AND customer_id = %s
                   AND carrier_id = %s
                """,
                    (snapshot_id, str(store_id), str(customer_id), carrier_id),
                )
                row = cur.fetchone()
    except RateQuoteSnapshotError:
        raise
    except Exception as exc:
        raise RateQuoteSnapshotError(
            "No se pudo resolver la tarifa aceptada."
        ) from exc
    if not row:
        raise RateQuoteNotFoundError("La tarifa aceptada no existe o no pertenece a la tienda.")
    resolved = dict(row)
    content = _row_content(resolved)
    digest = hashlib.sha256(_canonical_json(content).encode("utf-8")).hexdigest()
    if digest != str(resolved.get("snapshot_sha256") or ""):
        raise RateQuoteSnapshotError("La tarifa congelada no supera la verificación de integridad.")
    resolved["snapshot_id"] = snapshot_id
    resolved["snapshot_sha256"] = digest
    return resolved


def resolver_referencias(
    store_id: str,
    customer_id: str,
    references: list[str] | tuple[str, ...],
) -> dict[str, dict]:
    """Resuelve un lote en una sola consulta y verifica cada snapshot.

    El callback de Labels tiene un SLA de cinco segundos y puede agrupar muchas
    etiquetas. Abrir una conexión por etiqueta agotaría el presupuesto aunque
    todas las referencias fueran válidas.
    """
    parsed: dict[str, tuple[str, str]] = {}
    for reference in references:
        normalized = str(reference or "").strip()
        if normalized not in parsed:
            parsed[normalized] = _parse_reference(normalized)
    if not parsed:
        return {}
    snapshot_ids = [snapshot_id for _carrier, snapshot_id in parsed.values()]
    try:
        _ensure_table()
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT * FROM tiendanube_rate_quote_snapshots
                     WHERE store_id = %s
                       AND customer_id = %s
                       AND snapshot_id = ANY(%s::text[])
                    """,
                    (str(store_id), str(customer_id), snapshot_ids),
                )
                rows = [dict(row) for row in cur.fetchall()]
    except RateQuoteSnapshotError:
        raise
    except Exception as exc:
        raise RateQuoteSnapshotError(
            "No se pudieron resolver las tarifas aceptadas."
        ) from exc

    by_id = {str(row.get("snapshot_id") or ""): row for row in rows}
    resolved_by_reference: dict[str, dict] = {}
    for reference, (carrier_id, snapshot_id) in parsed.items():
        row = by_id.get(snapshot_id)
        if not row or str(row.get("carrier_id") or "").strip().lower() != carrier_id:
            raise RateQuoteNotFoundError(
                "Una tarifa aceptada no existe o no pertenece a la tienda."
            )
        content = _row_content(row)
        digest = hashlib.sha256(_canonical_json(content).encode("utf-8")).hexdigest()
        if digest != str(row.get("snapshot_sha256") or ""):
            raise RateQuoteSnapshotError(
                "Una tarifa congelada no supera la verificación de integridad."
            )
        row["snapshot_id"] = snapshot_id
        row["snapshot_sha256"] = digest
        resolved_by_reference[reference] = row
    return resolved_by_reference
