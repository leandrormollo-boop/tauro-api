"""Sesiones individuales, revocables y con vencimiento del administrador.

La base sólo recibe hashes. Rotar la contraseña o el segundo factor invalida
las sesiones anteriores, incluso si se conserva una copia de la cookie.
"""
import hashlib
import hmac
import secrets

from core.database import get_conn


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _firma(token: str, password: str, totp_secret: str) -> str:
    # El bearer aleatorio es la clave: el valor almacenado no permite probar
    # contraseñas offline a partir de una copia de la tabla de sesiones.
    return hmac.new(token.encode(), (password + "\0" + totp_secret).encode(),
                    hashlib.sha256).hexdigest()


def crear(password: str, totp_secret: str) -> str:
    token = secrets.token_urlsafe(32)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM admin_sesiones WHERE vence < NOW()")
            cur.execute("""
                INSERT INTO admin_sesiones (token_hash, firma, vence)
                VALUES (%s, %s, NOW() + INTERVAL '8 hours')
            """, (_hash(token), _firma(token, password, totp_secret)))
    return token


def verificar(token: str | None, password: str, totp_secret: str) -> bool:
    if not isinstance(token, str) or not 40 <= len(token) <= 128:
        return False
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT firma FROM admin_sesiones
                WHERE token_hash = %s AND vence > NOW()""", (_hash(token),))
            row = cur.fetchone()
    return bool(row and hmac.compare_digest(row["firma"], _firma(token, password, totp_secret)))


def revocar(token: str | None) -> None:
    if not isinstance(token, str) or not token:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM admin_sesiones WHERE token_hash = %s", (_hash(token),))


def verificar_totp(secret: str, codigo: str) -> bool:
    """Consume un paso una sola vez entre todos los workers y reinicios."""
    from servicios.totp import paso_valido
    paso = paso_valido(secret, codigo)
    if paso is None:
        return False
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO admin_totp_uso (secreto_hash, paso) VALUES (%s, %s)
                ON CONFLICT (secreto_hash) DO UPDATE SET paso = EXCLUDED.paso
                WHERE admin_totp_uso.paso < EXCLUDED.paso
                RETURNING paso
            """, (_hash(secret), paso))
            return cur.fetchone() is not None
