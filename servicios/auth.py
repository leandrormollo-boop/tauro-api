# ============================================================
# Servicio de autenticación — Login mágico por email
# ============================================================
# Reemplaza completamente las llamadas a Google Sheets.
# Usa PostgreSQL (core.database).
# ============================================================

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from core.database import get_conn


SESSION_DAYS = 7


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def buscar_cliente_por_email(email: str) -> Optional[str]:
    """
    Busca en la tabla clientes y devuelve el cliente_id (UPPERCASE) o None.
    Solo devuelve clientes activos.
    """
    email = email.strip().lower()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cliente_id FROM clientes WHERE email = %s AND activo = TRUE",
                (email,),
            )
            row = cur.fetchone()
    return str(row["cliente_id"]).strip().upper() if row else None


def generar_token(email: str, cliente: str) -> str:
    """Crea token y lo guarda en sessions. Devuelve el token."""
    token = secrets.token_urlsafe(32)
    creado = _now()
    expira = creado + timedelta(days=SESSION_DAYS)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sessions (token, email, cliente_id, creado_at, expira_at, usado)
                VALUES (%s, %s, %s, %s, %s, FALSE)
                """,
                (token, email.lower(), cliente.strip().upper(), creado, expira),
            )
    return token


def validar_token(token: str) -> Optional[str]:
    """Valida token. Si es válido y no expiró, devuelve cliente_id. Si no, None."""
    if not token or len(token) < 20:
        return None

    ahora = _now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cliente_id FROM sessions
                WHERE token = %s
                  AND usado = FALSE
                  AND expira_at > %s
                """,
                (token, ahora),
            )
            row = cur.fetchone()
    return str(row["cliente_id"]).strip().upper() if row else None


def revocar_token(token: str) -> bool:
    """Marca un token como usado (logout)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET usado = TRUE WHERE token = %s",
                (token,),
            )
            return cur.rowcount > 0


def limpiar_sessions_expiradas() -> int:
    """Elimina sesiones expiradas o usadas. Devuelve la cantidad eliminada."""
    ahora = _now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM sessions WHERE expira_at < %s OR usado = TRUE",
                (ahora,),
            )
            return cur.rowcount


def link_magico_url(base_url: str, token: str) -> str:
    """URL que va en el email del login mágico."""
    return f"{base_url.rstrip('/')}/portal/auth?token={token}"


def get_markup_pct(cliente: str, default: float = 25.0) -> float:
    """Lee MARKUP_PCT del cliente desde la tabla clientes."""
    cliente = cliente.strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT markup_pct FROM clientes WHERE cliente_id = %s AND activo = TRUE",
                (cliente,),
            )
            row = cur.fetchone()
    if row and row["markup_pct"] is not None:
        return float(row["markup_pct"])
    return default
