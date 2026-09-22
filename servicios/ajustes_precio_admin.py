"""Ajustes comerciales de ADMIN sobre el precio final de un envío.

El cargo original es inmutable. Cada cambio se registra como un movimiento
separado en ``ajustes_cliente`` para que cuenta corriente, facturación y
auditoría conserven el antes y el después.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from typing import Any

import psycopg2

from core.database import get_conn
from servicios.conciliacion_couriers import _registrar_auditoria


CENTAVO = Decimal("0.01")
_IDEMPOTENCIA_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


class AjustePrecioAdminError(ValueError):
    """La operación no cumple los controles comerciales o contables."""


def _dinero(valor: Any, campo: str) -> Decimal:
    try:
        numero = Decimal(str(valor)).quantize(CENTAVO, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        raise AjustePrecioAdminError(f"{campo}: ingresá un importe válido.") from None
    if not numero.is_finite() or numero < 0:
        raise AjustePrecioAdminError(f"{campo}: el importe no puede ser negativo.")
    return numero


def _texto(valor: Any) -> str:
    return " ".join(str(valor or "").strip().split())


def aplicar_nuevo_precio(
    *,
    cliente_id: str,
    envio_id: int,
    nuevo_precio_ars: Any,
    motivo: str,
    actor: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Aplica un nuevo precio final sin modificar el cargo original.

    Una baja crea un CREDITO y una suba un DEBITO. La clave idempotente evita
    que un doble click repita el movimiento.
    """
    cliente = _texto(cliente_id).upper()
    actor_limpio = _texto(actor)[:120] or "admin"
    motivo_limpio = _texto(motivo)[:500]
    clave = _texto(idempotency_key)
    nuevo = _dinero(nuevo_precio_ars, "Nuevo precio")
    if not cliente:
        raise AjustePrecioAdminError("Falta el cliente del envío.")
    if len(motivo_limpio) < 8:
        raise AjustePrecioAdminError(
            "Explicá el motivo del cambio con al menos 8 caracteres."
        )
    if not _IDEMPOTENCIA_RE.fullmatch(clave):
        raise AjustePrecioAdminError("La clave de la operación no es válida.")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, solicitud_id, precio_anterior_ars,
                       precio_nuevo_ars, monto_ars, tipo
                FROM ajustes_cliente
                WHERE idempotency_key=%s
                """,
                (clave,),
            )
            repetido = cur.fetchone()
            if repetido:
                cur.execute(
                    "SELECT cliente_id FROM envios WHERE solicitud_id=%s",
                    (repetido["solicitud_id"],),
                )
                duenio = cur.fetchone()
                if (
                    not duenio
                    or duenio["cliente_id"] != cliente
                    or Decimal(str(repetido["precio_nuevo_ars"])) != nuevo
                ):
                    raise AjustePrecioAdminError(
                        "La clave de operación ya fue usada con otros datos."
                    )
                return {
                    "ok": True,
                    "duplicado": True,
                    "ajuste_id": int(repetido["id"]),
                    "solicitud_id": int(repetido["solicitud_id"]),
                    "precio_anterior_ars": _dinero(
                        repetido["precio_anterior_ars"], "Precio anterior"
                    ),
                    "precio_nuevo_ars": nuevo,
                    "monto_ajuste_ars": _dinero(
                        abs(repetido["monto_ars"]), "Ajuste"
                    ),
                    "tipo": repetido["tipo"],
                }

            cur.execute(
                """
                SELECT e.id, e.solicitud_id, e.monto_ars, e.estado,
                       s.test AS solicitud_test,
                       s.estado AS solicitud_estado
                FROM envios e
                JOIN solicitudes_guia s
                  ON s.id=e.solicitud_id AND s.cliente_id=e.cliente_id
                WHERE e.id=%s AND e.cliente_id=%s
                FOR UPDATE OF e, s
                """,
                (int(envio_id), cliente),
            )
            envio = cur.fetchone()
            if not envio:
                raise AjustePrecioAdminError(
                    "El envío no existe o pertenece a otro cliente."
                )
            if envio["estado"] != "ACTIVO":
                raise AjustePrecioAdminError(
                    "Sólo se puede ajustar un cargo activo."
                )
            if envio["solicitud_test"]:
                raise AjustePrecioAdminError(
                    "Un envío de prueba no admite ajustes comerciales."
                )
            if envio["solicitud_estado"] in ("CANCELADO", "REEMPLAZADO"):
                raise AjustePrecioAdminError(
                    "El envío está cancelado o reemplazado y debe conciliarse."
                )

            cur.execute(
                """
                SELECT %s::numeric + COALESCE(SUM(monto_ars), 0) AS precio_vigente
                FROM ajustes_cliente
                WHERE solicitud_id=%s AND estado='APLICADO'
                """,
                (envio["monto_ars"], int(envio["solicitud_id"])),
            )
            precio_anterior = _dinero(
                cur.fetchone()["precio_vigente"], "Precio vigente"
            )
            delta = (nuevo - precio_anterior).quantize(
                CENTAVO, rounding=ROUND_HALF_UP
            )
            if delta == 0:
                return {
                    "ok": True,
                    "duplicado": False,
                    "sin_cambios": True,
                    "solicitud_id": int(envio["solicitud_id"]),
                    "precio_anterior_ars": precio_anterior,
                    "precio_nuevo_ars": nuevo,
                    "monto_ajuste_ars": CENTAVO * 0,
                    "tipo": None,
                }

            tipo = "DEBITO" if delta > 0 else "CREDITO"
            referencia = f"ADMIN-PRECIO-{cliente}-{int(envio_id)}"
            try:
                cur.execute(
                    """
                    INSERT INTO ajustes_cliente (
                        conciliacion_id, solicitud_id, tipo, monto_ars,
                        precio_anterior_ars, precio_nuevo_ars, estado,
                        idempotency_key, motivo, propuesto_por,
                        aprobado_por, aprobado_at, aplicado_por, aplicado_at,
                        referencia_aplicacion, origen
                    ) VALUES (
                        NULL, %s, %s, %s, %s, %s, 'APLICADO',
                        %s, %s, %s, %s, NOW(), %s, NOW(), %s,
                        'AJUSTE_COMERCIAL_ADMIN'
                    )
                    RETURNING id
                    """,
                    (
                        int(envio["solicitud_id"]), tipo, delta,
                        precio_anterior, nuevo, clave, motivo_limpio,
                        actor_limpio, actor_limpio, actor_limpio, referencia,
                    ),
                )
            except psycopg2.IntegrityError as exc:
                raise AjustePrecioAdminError(
                    "El precio cambió mientras se procesaba la operación. Recargá e intentá nuevamente."
                ) from exc
            ajuste_id = int(cur.fetchone()["id"])
            _registrar_auditoria(
                cur,
                evento="AJUSTE_COMERCIAL_ADMIN_APLICADO",
                actor=actor_limpio,
                solicitud_id=int(envio["solicitud_id"]),
                ajuste_id=ajuste_id,
                metadata={
                    "cliente_id": cliente,
                    "envio_id": int(envio_id),
                    "tipo": tipo,
                    "monto_ars": str(delta),
                    "precio_anterior_ars": str(precio_anterior),
                    "precio_nuevo_ars": str(nuevo),
                    "motivo": motivo_limpio,
                },
            )
            return {
                "ok": True,
                "duplicado": False,
                "sin_cambios": False,
                "ajuste_id": ajuste_id,
                "solicitud_id": int(envio["solicitud_id"]),
                "precio_anterior_ars": precio_anterior,
                "precio_nuevo_ars": nuevo,
                "monto_ajuste_ars": abs(delta),
                "tipo": tipo,
            }
