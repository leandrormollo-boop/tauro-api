"""HTTP compartido exclusivamente por consultas de tarifas.

Las operaciones que pueden crear, cancelar o cobrar recursos no usan este
transporte: conservan sus reglas estrictas en cada cliente de courier.
"""

from __future__ import annotations

import threading
import time

import requests
from requests.adapters import HTTPAdapter


# Separar conexión y lectura evita que un handshake lento consuma todo el
# presupuesto de una cotización. requests interpreta la tupla en ese orden.
QUOTE_TIMEOUT = (3.05, 12.0)
QUOTE_TOTAL_TIMEOUT = 30.0
QUOTE_POOL_CONNECTIONS = 8
QUOTE_POOL_MAXSIZE = 8

_thread_local = threading.local()
_monotonic = time.monotonic
_sleep = time.sleep


class QuoteDeadlineExceeded(requests.Timeout):
    """La consulta agotó su presupuesto HTTP compartido."""


class QuoteBudget:
    """Presupuesto monotónico compartido por todos los HTTP de una tarifa."""

    def __init__(self, deadline: float):
        self.deadline = deadline

    @classmethod
    def start(cls, total_seconds: float = QUOTE_TOTAL_TIMEOUT) -> "QuoteBudget":
        return cls(_monotonic() + total_seconds)

    def remaining(self) -> float:
        return max(self.deadline - _monotonic(), 0.0)

    def ensure_remaining(self) -> float:
        remaining = self.remaining()
        if remaining <= 0.002:
            raise QuoteDeadlineExceeded(
                "Se agotó el tiempo total de consulta de tarifa."
            )
        return remaining

    def timeout(self) -> tuple[float, float]:
        remaining = self.ensure_remaining()
        # requests mide connect y read por separado. Repartir el remanente
        # mantiene la suma de ambos dentro del presupuesto pendiente.
        connect = min(QUOTE_TIMEOUT[0], remaining / 2)
        read = min(QUOTE_TIMEOUT[1], remaining - connect)
        return (max(connect, 0.001), max(read, 0.001))

    def sleep_before_retry(self, seconds: float) -> None:
        # Si el backoff no entra completo, fallar ya deja más tiempo a los
        # demás couriers y evita dormir para una llamada que no podrá salir.
        if seconds >= self.remaining():
            raise QuoteDeadlineExceeded(
                "Se agotó el tiempo total de consulta de tarifa."
            )
        _sleep(seconds)


def quote_session() -> requests.Session:
    """Devuelve una Session persistente por thread para llamadas de tarifa."""
    session = getattr(_thread_local, "quote_session", None)
    if session is None:
        session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=QUOTE_POOL_CONNECTIONS,
            pool_maxsize=QUOTE_POOL_MAXSIZE,
            max_retries=0,
            pool_block=False,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _thread_local.quote_session = session
    return session
