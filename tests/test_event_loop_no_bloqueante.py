"""Los handlers que hacen I/O bloqueante no deben congelar el event loop.

Un webhook de Shopify o un callback de tarifas de Tiendanube esperan base de
datos y red. Si eso corre directo en un handler ``async``, una ráfaga de
webhooks deja sin responder al resto de la API. Estas pruebas verifican que
el trabajo se delega al threadpool: varias llamadas concurrentes terminan en
paralelo y un latido del loop sigue avanzando mientras tanto.
"""
from __future__ import annotations

import asyncio
import json
import time

from fastapi.responses import JSONResponse


class _Req:
    def __init__(self, body: bytes, headers: dict | None = None):
        self._body = body
        self.headers = headers or {"content-length": str(len(body))}

    async def body(self):
        return self._body

    async def stream(self):
        yield self._body

    async def json(self):
        return json.loads(self._body)


async def _latido(stop: asyncio.Event) -> int:
    ticks = 0
    while not stop.is_set():
        await asyncio.sleep(0.02)
        ticks += 1
    return ticks


def _correr(coro_factory, n: int, espera: float):
    async def _main():
        stop = asyncio.Event()
        latido = asyncio.create_task(_latido(stop))
        inicio = time.monotonic()
        resultados = await asyncio.gather(*(coro_factory() for _ in range(n)))
        total = time.monotonic() - inicio
        stop.set()
        ticks = await latido
        return resultados, total, ticks

    return asyncio.run(_main())


def test_webhook_shopify_no_bloquea_el_loop(monkeypatch):
    from endpoints import integraciones

    def _lento(headers, cuerpo, topic):
        time.sleep(0.3)
        return JSONResponse({"ok": True})

    monkeypatch.setattr(integraciones, "_procesar_shopify_webhook_sync", _lento)
    req = lambda: _Req(b"{}", {"x-shopify-topic": "orders/create"})
    resultados, total, ticks = _correr(
        lambda: integraciones._procesar_shopify_webhook(req(), "orders/create"),
        n=5,
        espera=0.3,
    )
    assert all(r.status_code == 200 for r in resultados)
    # 5 × 0,3 s en serie serían 1,5 s; en paralelo, cerca de 0,3 s.
    assert total < 0.9, f"los webhooks se serializaron: {total:.2f}s"
    # Con el loop libre, el latido avanza durante toda la espera.
    assert ticks >= 8, f"el loop estuvo bloqueado: {ticks} ticks"


def test_webhook_tiendanube_no_bloquea_el_loop(monkeypatch):
    from endpoints import integraciones

    def _lento(headers, cuerpo, evento_ruta=""):
        time.sleep(0.3)
        return {"ok": True}

    monkeypatch.setattr(integraciones, "_recibir_webhook_tiendanube_sync", _lento)
    resultados, total, ticks = _correr(
        lambda: integraciones._recibir_webhook_tiendanube(_Req(b"{}")),
        n=5,
        espera=0.3,
    )
    assert all(r == {"ok": True} for r in resultados)
    assert total < 0.9
    assert ticks >= 8


def test_rates_tiendanube_no_bloquea_el_loop_y_respeta_techo(monkeypatch):
    from endpoints import tiendanube_shipping as endpoint

    def _lento(payload, token):
        time.sleep(0.3)
        return {"rates": []}

    monkeypatch.setattr(endpoint, "cotizar_callback", _lento)
    resultados, total, ticks = _correr(
        lambda: endpoint.rates(
            _Req(b'{"store_id":"1"}'), callback_token="token-prueba-largo-1234567890"
        ),
        n=5,
        espera=0.3,
    )
    assert all(r == {"rates": []} for r in resultados)
    assert total < 0.9
    assert ticks >= 8

    # Techo: una cotización colgada no puede exceder el SLA del checkout.
    monkeypatch.setattr(endpoint, "_RATES_ENDPOINT_TIMEOUT_SECONDS", 0.2)

    def _colgado(payload, token):
        time.sleep(1.0)
        return {"rates": []}

    monkeypatch.setattr(endpoint, "cotizar_callback", _colgado)

    async def _medir():
        # Se mide dentro del loop: asyncio.run() espera al hilo colgado al
        # cerrar el executor, pero la respuesta al cliente ya salió antes.
        inicio = time.monotonic()
        respuesta = await endpoint.rates(
            _Req(b'{"store_id":"1"}'), callback_token="token-prueba-largo-1234567890"
        )
        return respuesta, time.monotonic() - inicio

    respuesta, demora = asyncio.run(_medir())
    assert respuesta.status_code == 503
    assert demora < 0.8, f"el techo no cortó a tiempo: {demora:.2f}s"
