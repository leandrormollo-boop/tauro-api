# ============================================================
# Servicio de autenticación — Login mágico por email
# ============================================================
# Reemplaza completamente las llamadas a Google Sheets.
# Usa PostgreSQL (core.database).
# ============================================================

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt

from core.database import get_conn


SESSION_DAYS = 7


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


# ── Password hashing ────────────────────────────────────────

def hash_password(password: str) -> str:
    """Hashea una contraseña con bcrypt. Devuelve string utf-8 listo para guardar."""
    if not password:
        raise ValueError("La contraseña no puede estar vacía.")
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Verifica una contraseña contra su hash bcrypt. Retorna True/False."""
    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def set_cliente_password(cliente_id: str, password: str) -> bool:
    """Setea/cambia la contraseña de un cliente. Devuelve True si actualizó."""
    cliente_id = cliente_id.strip().upper()
    new_hash = hash_password(password)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE clientes SET password_hash = %s WHERE cliente_id = %s",
                (new_hash, cliente_id),
            )
            return cur.rowcount > 0


def autenticar_cliente(email: str, password: str) -> Optional[str]:
    """
    Verifica email + password contra la DB.
    Retorna cliente_id si OK, None si email no existe / inactivo / password incorrecto / sin password seteado.
    """
    email = (email or "").strip().lower()
    if not email or not password:
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cliente_id, password_hash FROM clientes "
                "WHERE email = %s AND activo = TRUE",
                (email,),
            )
            row = cur.fetchone()
    if not row:
        return None
    if not row["password_hash"]:
        # Cliente existe pero nunca le seteamos contraseña → no puede entrar con password
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return str(row["cliente_id"]).strip().upper()


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
