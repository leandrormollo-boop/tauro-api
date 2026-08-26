# ============================================================
# Servicio de autenticación — Login mágico por email
# ============================================================
# Reemplaza completamente las llamadas a Google Sheets.
# Usa PostgreSQL (core.database).
# ============================================================

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt

from core.database import get_conn


SESSION_DAYS = 7
PASSWORD_RESET_MINUTES = 30
PASSWORD_MIN_CHARS = 12
PASSWORD_MAX_BYTES = 72  # bcrypt ignora silenciosamente lo que exceda este límite

_PASSWORDS_COMUNES = frozenset({
    "123456789012", "administrador", "contraseña123", "password1234",
    "qwertyuiop12", "taurosolutions", "tauro12345678",
})


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


# ── Restablecimiento real de contraseña ───────────────────────────

def _password_reset_hash(token: str) -> str:
    """Digest irreversible para persistir el token sin guardar el secreto."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def buscar_cliente_para_password_reset(identificador: str) -> Optional[dict]:
    """Resuelve email o ID a una referencia interna de cliente activo.

    El endpoint nunca expone el resultado: la respuesta pública es idéntica
    exista o no exista la cuenta. El email no sale de esta capa: el worker lo
    obtiene recién al reclamar la solicitud durable.
    """
    identificador = (identificador or "").strip()
    if not identificador:
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            if "@" in identificador:
                cur.execute(
                    "SELECT cliente_id FROM clientes "
                    "WHERE LOWER(BTRIM(email)) = LOWER(BTRIM(%s)) AND activo = TRUE "
                    "AND NULLIF(BTRIM(email), '') IS NOT NULL",
                    (identificador,),
                )
            else:
                cur.execute(
                    "SELECT cliente_id FROM clientes "
                    "WHERE cliente_id = %s AND activo = TRUE "
                    "AND NULLIF(BTRIM(email), '') IS NOT NULL",
                    (identificador.upper(),),
                )
            row = cur.fetchone()
    if not row:
        return None
    return {"cliente_id": str(row["cliente_id"]).strip().upper()}


def crear_password_reset_token(cliente_id: str) -> str:
    """Crea un token inactivo de 30 minutos y devuelve el secreto una vez.

    El token queda *inactivo* hasta que el remitente SMTP confirma el envío.
    Así un fallo de correo nunca deja una credencial utilizable huérfana.
    """
    token = secrets.token_urlsafe(32)
    token_hash = _password_reset_hash(token)
    creado = _now()
    expira = creado + timedelta(minutes=PASSWORD_RESET_MINUTES)
    cliente_id = (cliente_id or "").strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO password_reset_tokens
                    (token_hash, cliente_id, creado_at, expira_at)
                VALUES (%s, %s, %s, %s)
                """,
                (token_hash, cliente_id, creado, expira),
            )
            # Higiene acotada: no acumular secretos expirados durante años.
            cur.execute(
                "DELETE FROM password_reset_tokens "
                "WHERE expira_at < %s - INTERVAL '1 day'",
                (creado,),
            )
    return token


def activar_password_reset_token(token: str) -> bool:
    """Activa el token sólo después de que SMTP confirmó el envío.

    Al activarlo invalida los pedidos anteriores de la misma cuenta. La
    operación es transaccional: queda exactamente un link vigente.
    """
    if not token or len(token) < 32:
        return False
    digest = _password_reset_hash(token)
    ahora = _now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cliente_id
                FROM password_reset_tokens
                WHERE token_hash = %s
                  AND usado_at IS NULL
                  AND email_enviado_at IS NULL
                  AND expira_at > %s
                FOR UPDATE
                """,
                (digest, ahora),
            )
            row = cur.fetchone()
            if not row:
                return False
            cur.execute(
                """
                UPDATE password_reset_tokens
                SET usado_at = %s
                WHERE cliente_id = %s
                  AND token_hash <> %s
                  AND usado_at IS NULL
                """,
                (ahora, row["cliente_id"], digest),
            )
            cur.execute(
                """
                UPDATE password_reset_tokens
                SET email_enviado_at = %s
                WHERE token_hash = %s
                  AND usado_at IS NULL
                  AND email_enviado_at IS NULL
                """,
                (ahora, digest),
            )
            return cur.rowcount == 1


def finalizar_password_reset_entregado(
    token: str,
    *,
    request_id: int,
    claim_id: str,
    message_id: str = "",
) -> bool:
    """Activa el token y confirma la entrega conservando el mismo claim.

    El request se bloquea antes de invalidar tokens anteriores. Si otro
    worker perdió o recuperó el claim, esta transacción no toca ningún link.
    """
    if not token or len(token) < 32 or not claim_id:
        return False
    digest = _password_reset_hash(token)
    ahora = _now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cliente_id
                  FROM password_reset_requests
                 WHERE id = %s
                   AND estado = 'PROCESANDO'
                   AND claim_id = %s
                 FOR UPDATE
                """,
                (request_id, claim_id),
            )
            request_row = cur.fetchone()
            if not request_row:
                return False

            cur.execute(
                """
                SELECT cliente_id
                  FROM password_reset_tokens
                 WHERE token_hash = %s
                   AND usado_at IS NULL
                   AND email_enviado_at IS NULL
                   AND expira_at > %s
                 FOR UPDATE
                """,
                (digest, ahora),
            )
            token_row = cur.fetchone()
            if (
                not token_row
                or str(token_row["cliente_id"]).strip().upper()
                != str(request_row["cliente_id"]).strip().upper()
            ):
                return False

            cur.execute(
                """
                UPDATE password_reset_tokens
                   SET usado_at = %s
                 WHERE cliente_id = %s
                   AND token_hash <> %s
                   AND usado_at IS NULL
                """,
                (ahora, token_row["cliente_id"], digest),
            )
            cur.execute(
                """
                UPDATE password_reset_tokens
                   SET email_enviado_at = %s
                 WHERE token_hash = %s
                   AND usado_at IS NULL
                   AND email_enviado_at IS NULL
                """,
                (ahora, digest),
            )
            if cur.rowcount != 1:
                raise RuntimeError("No se pudo activar el token de recuperación")

            cur.execute(
                """
                UPDATE password_reset_requests
                   SET estado = 'ENVIADO',
                       enviado_at = NOW(),
                       ultimo_error_code = NULL,
                       email_message_id = %s,
                       claim_id = NULL,
                       claimed_at = NULL,
                       actualizado_at = NOW()
                 WHERE id = %s
                   AND estado = 'PROCESANDO'
                   AND claim_id = %s
                """,
                (message_id or None, request_id, claim_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError("Se perdió el claim de recuperación")
    return True


def revocar_password_reset_token(token: str) -> bool:
    """Inutiliza un token (por ejemplo, cuando SMTP falla)."""
    if not token or len(token) < 32:
        return False
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE password_reset_tokens
                SET usado_at = %s
                WHERE token_hash = %s AND usado_at IS NULL
                """,
                (_now(), _password_reset_hash(token)),
            )
            return cur.rowcount == 1


def password_reset_token_valido(token: str) -> bool:
    """Comprueba vigencia sin consumir ni devolver identidad de la cuenta."""
    if not token or len(token) < 32:
        return False
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM password_reset_tokens r
                JOIN clientes c ON c.cliente_id = r.cliente_id
                WHERE r.token_hash = %s
                  AND r.usado_at IS NULL
                  AND r.email_enviado_at IS NOT NULL
                  AND r.expira_at > %s
                  AND c.activo = TRUE
                """,
                (_password_reset_hash(token), _now()),
            )
            return cur.fetchone() is not None


def validar_nueva_password(password: str, confirmacion: str) -> Optional[str]:
    """Devuelve un mensaje apto para UI o ``None`` cuando es aceptable."""
    if password != confirmacion:
        return "Las contraseñas no coinciden."
    if password != password.strip():
        return "La contraseña no puede empezar ni terminar con espacios."
    if len(password) < PASSWORD_MIN_CHARS:
        return f"Usá al menos {PASSWORD_MIN_CHARS} caracteres."
    try:
        password_bytes = password.encode("utf-8")
    except (AttributeError, UnicodeEncodeError):
        return "La contraseña contiene caracteres no válidos."
    if len(password_bytes) > PASSWORD_MAX_BYTES:
        return "La contraseña es demasiado larga."
    if password.casefold() in _PASSWORDS_COMUNES:
        return "Esa contraseña es demasiado común. Elegí otra."
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        return "Incluí al menos una letra y un número."
    return None


def consumir_password_reset_token(token: str, password: str) -> bool:
    """Cambia la contraseña, consume el link y revoca sesiones atómicamente."""
    if not token or len(token) < 32:
        return False
    digest = _password_reset_hash(token)
    ahora = _now()
    password_hash = hash_password(password)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.cliente_id
                FROM password_reset_tokens r
                JOIN clientes c ON c.cliente_id = r.cliente_id
                WHERE r.token_hash = %s
                  AND r.usado_at IS NULL
                  AND r.email_enviado_at IS NOT NULL
                  AND r.expira_at > %s
                  AND c.activo = TRUE
                FOR UPDATE OF r
                """,
                (digest, ahora),
            )
            row = cur.fetchone()
            if not row:
                return False
            cliente_id = str(row["cliente_id"]).strip().upper()
            cur.execute(
                "UPDATE clientes SET password_hash = %s WHERE cliente_id = %s AND activo = TRUE",
                (password_hash, cliente_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError("No se pudo actualizar la cuenta activa.")
            cur.execute(
                """
                UPDATE password_reset_tokens
                SET usado_at = %s
                WHERE cliente_id = %s AND usado_at IS NULL
                """,
                (ahora, cliente_id),
            )
            cur.execute(
                "UPDATE sessions SET usado = TRUE WHERE cliente_id = %s AND usado = FALSE",
                (cliente_id,),
            )
    return True


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
