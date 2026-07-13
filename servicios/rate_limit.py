# ============================================================
# Rate limiting simple en memoria (anti fuerza bruta)
# ============================================================
# Ventana deslizante por clave (ej. "login:<ip>"). Efímero: se resetea en
# cada restart y no se comparte entre workers. Suficiente para frenar fuerza
# bruta en logins con 1 worker; si algún día hay varios workers o se necesita
# algo serio, mover a Redis.
# ============================================================

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
    IP del cliente considerando el proxy de Railway (X-Forwarded-For).
    Devuelve 'unknown' si no se puede determinar.
    """
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"
