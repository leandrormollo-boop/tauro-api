# ============================================================
# Rate limiting simple en memoria (anti fuerza bruta)
# ============================================================
# Ventana deslizante por clave (ej. "login:<ip>"). Efímero: se resetea en
# cada restart y no se comparte entre workers. Suficiente para frenar fuerza
# bruta en logins con 1 worker; si algún día hay varios workers o se necesita
# algo serio, mover a Redis.
# ============================================================

import hashlib
import threading
import time
from collections import defaultdict, deque

_lock = threading.Lock()
_hits: dict[str, deque] = defaultdict(deque)


def check_rate(key: str, max_attempts: int = 5, window_seconds: int = 300) -> bool:
    """
    Registra un intento y devuelve True si está permitido, False si la clave
    ya superó max_attempts dentro de la ventana window_seconds.
    """
    now = time.monotonic()
    cutoff = now - window_seconds
    with _lock:
        dq = _hits[key]
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= max_attempts:
            return False
        dq.append(now)
        return True


def reset_rate(key: str) -> None:
    """Limpia los intentos de una clave (ej. tras un login exitoso)."""
    with _lock:
        _hits.pop(key, None)


def client_ip(request) -> str:
    """
    IP real del cliente para el rate limit (y sólo para eso).

    OJO con X-Forwarded-For: el CLIENTE controla lo que ponga a la IZQUIERDA;
    los proxies confiables (Cloudflare, Railway) APPENDEAN la IP real a la
    derecha. Tomar el primer valor (como se hacía) dejaba spoofear la clave
    del rate limit rotando el header en cada request → fuerza bruta sin tope.

    Orden de confianza:
    1. `CF-Connecting-IP`: lo pone Cloudflare y no viaja del cliente. Si el
       tráfico pasa por Cloudflare (setup documentado), es la IP verdadera.
    2. El valor MÁS A LA DERECHA de X-Forwarded-For: el que agregó el último
       proxy, no el que inyectó el cliente.
    3. `request.client.host` como último recurso.
    """
    cf = request.headers.get("cf-connecting-ip", "").strip()
    if cf:
        return cf
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        partes = [p.strip() for p in fwd.split(",") if p.strip()]
        if partes:
            return partes[-1]
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def check_auth_rate(key: str, max_attempts: int = 5, window_seconds: int = 300) -> bool:
    """Límite durable y atómico. Un fallo de DB no habilita el login."""
    from core.database import get_conn
    digest = hashlib.sha256(key.encode()).hexdigest()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM auth_intentos WHERE vence < NOW() - INTERVAL '1 day'")
            cur.execute("""
                INSERT INTO auth_intentos (clave_hash, cantidad, vence)
                VALUES (%s, 1, NOW() + %s * INTERVAL '1 second')
                ON CONFLICT (clave_hash) DO UPDATE SET
                    cantidad = CASE WHEN auth_intentos.vence <= NOW() THEN 1
                                    ELSE auth_intentos.cantidad + 1 END,
                    vence = CASE WHEN auth_intentos.vence <= NOW() THEN EXCLUDED.vence
                                 ELSE auth_intentos.vence END
                WHERE auth_intentos.vence <= NOW() OR auth_intentos.cantidad < %s
                RETURNING cantidad
            """, (digest, window_seconds, max_attempts))
            return cur.fetchone() is not None


def reset_auth_rate(key: str) -> None:
    from core.database import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM auth_intentos WHERE clave_hash = %s",
                        (hashlib.sha256(key.encode()).hexdigest(),))
