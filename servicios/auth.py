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


def autenticar_cliente(usuario: str, password: str) -> Optional[dict]:
    """
    Verifica usuario + password contra la DB. El usuario puede ser el EMAIL
    o el ID DE CLIENTE (pedido de Leandro 28/07: "la idea es que se pueda
    con mail y con id de usuario") — se distingue por la arroba, así que un
    ID nunca se confunde con un mail.

    Retorna {"cliente_id", "email"} si OK (el email hace falta para la
    sesión aunque hayan entrado con el ID), None en cualquier otro caso.
    """
    usuario = (usuario or "").strip()
    if not usuario or not password:
        return None

    with get_conn() as conn:
        with conn.cursor() as cur:
            if "@" in usuario:
                cur.execute(
                    "SELECT cliente_id, email, password_hash FROM clientes "
                    "WHERE email = %s AND activo = TRUE",
                    (usuario.lower(),),
                )
            else:
                cur.execute(
                    "SELECT cliente_id, email, password_hash FROM clientes "
                    "WHERE cliente_id = %s AND activo = TRUE",
                    (usuario.upper(),),
                )
            row = cur.fetchone()
    if not row:
        return None
    if not row["password_hash"]:
        # Cliente existe pero sin contraseña asignada (el alta del admin la
        # deja opcional). Antes esto devolvía None y el cliente veía
        # "incorrectos" — imposible de distinguir de una contraseña mal
        # tipeada, y terminaba en un WhatsApp a Leandro. Se devuelve el caso
        # explícito para que el login lo explique. Sí, esto le confirma a un
        # tercero que el ID existe: aceptable acá — los IDs son razones
        # sociales semi-públicas y el portal es de pocos clientes conocidos.
        return {"sin_password": True}
    if not verify_password(password, row["password_hash"]):
        return None
    return {
        "cliente_id": str(row["cliente_id"]).strip().upper(),
        "email": str(row["email"] or "").strip().lower(),
    }


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
                SELECT s.cliente_id
                FROM sessions s
                JOIN clientes c ON c.cliente_id = s.cliente_id
                WHERE s.token = %s
                  AND s.usado = FALSE
                  AND s.expira_at > %s
                  AND c.activo = TRUE
                """,
                (token, ahora),
            )
            row = cur.fetchone()
    return str(row["cliente_id"]).strip().upper() if row else None


def consumir_magic_token(token: str) -> Optional[dict]:
    """
    Canjea un token de magic link: lo valida y lo marca usado en una sola
    operación atómica (one-shot). Devuelve {cliente_id, email} o None si es
    inválido, expirado o ya usado. Evita que el link del email quede válido
    para siempre o se pueda reusar.
    """
    if not token or len(token) < 20:
        return None
    ahora = _now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sessions s SET usado = TRUE
                WHERE s.token = %s AND s.usado = FALSE AND s.expira_at > %s
                  AND EXISTS (
                      SELECT 1 FROM clientes c
                      WHERE c.cliente_id=s.cliente_id AND c.activo=TRUE
                  )
                RETURNING cliente_id, email
                """,
                (token, ahora),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "cliente_id": str(row["cliente_id"]).strip().upper(),
        "email": row["email"],
    }


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
