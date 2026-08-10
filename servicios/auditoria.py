from __future__ import annotations

import os
from typing import Any

from psycopg2.extras import Json

from core.database import get_conn


def registrar_evento_con_cursor(
    cur,
    *,
    event: str,
    actor_type: str,
    actor_ref: str | None,
    ip: str | None,
    method: str | None,
    path: str | None,
    status_code: int | None,
    success: bool,
    request_id: str | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Inserta un evento dentro de la transacción del caller.

    A diferencia de ``registrar_evento``, no suprime errores: se usa cuando
    la acción sensible y su trazabilidad deben confirmarse o revertirse juntas.
    """
    cur.execute(
        """
        INSERT INTO security_audit
            (event, actor_type, actor_ref, ip, method, path,
             status_code, success, request_id, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            event[:80],
            actor_type[:30],
            actor_ref[:120] if actor_ref else None,
            ip[:80] if ip else None,
            method[:10] if method else None,
            path[:300] if path else None,
            status_code,
            success,
            request_id[:64] if request_id else None,
            Json(metadata or {}),
        ),
    )


def registrar_evento(
    *,
    event: str,
    actor_type: str,
    actor_ref: str | None,
    ip: str | None,
    method: str | None,
    path: str | None,
    status_code: int | None,
    success: bool,
    request_id: str | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Persist a security event without request bodies or credentials."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                registrar_evento_con_cursor(
                    cur,
                    event=event,
                    actor_type=actor_type,
                    actor_ref=actor_ref,
                    ip=ip,
                    method=method,
                    path=path,
                    status_code=status_code,
                    success=success,
                    request_id=request_id,
                    metadata=metadata,
                )
    except Exception as exc:
        print(
            f"[security-audit] no se pudo guardar {event}: "
            f"{type(exc).__name__}"
        )


def registrar_desde_request(
    request,
    *,
    event: str,
    actor_type: str,
    actor_ref: str | None = None,
    success: bool = True,
    status_code: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """
    Atajo para los endpoints: saca IP, método y path del request y registra.
    Nunca lanza — un fallo del log jamás debe tumbar la acción que audita.
    """
    try:
        from servicios.rate_limit import client_ip
        ip = client_ip(request)
    except Exception:
        ip = None
    try:
        registrar_evento(
            event=event,
            actor_type=actor_type,
            actor_ref=actor_ref,
            ip=ip,
            method=getattr(request, "method", None),
            path=request.url.path if getattr(request, "url", None) else None,
            status_code=status_code,
            success=success,
            request_id=request.headers.get("x-request-id") if getattr(request, "headers", None) else None,
            metadata=metadata,
        )
    except Exception as exc:
        print(f"[security-audit] {event}: {type(exc).__name__}")


def registrar_desde_request_con_cursor(
    cur,
    request,
    *,
    event: str,
    actor_type: str,
    actor_ref: str | None = None,
    success: bool = True,
    status_code: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Versión transaccional para permisos, precios y otras acciones críticas."""
    try:
        from servicios.rate_limit import client_ip
        ip = client_ip(request)
    except Exception:
        ip = None
    registrar_evento_con_cursor(
        cur,
        event=event,
        actor_type=actor_type,
        actor_ref=actor_ref,
        ip=ip,
        method=getattr(request, "method", None),
        path=request.url.path if getattr(request, "url", None) else None,
        status_code=status_code,
        success=success,
        request_id=(
            request.headers.get("x-request-id")
            if getattr(request, "headers", None) else None
        ),
        metadata=metadata,
    )


def limpiar_auditoria_antigua() -> int:
    try:
        retention_days = max(
            30, int(os.getenv("SECURITY_AUDIT_RETENTION_DAYS", "365"))
        )
    except ValueError:
        retention_days = 365

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM security_audit
                WHERE created_at < NOW() - (%s * INTERVAL '1 day')
                """,
                (retention_days,),
            )
            return cur.rowcount
