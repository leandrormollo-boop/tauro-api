from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from endpoints import portal_cliente
from servicios import catalogo, shopify_catalogo


def _json(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def test_sincronizar_catalogo_responde_json_sin_redireccion(monkeypatch):
    monkeypatch.setattr(
        shopify_catalogo,
        "solicitar_sincronizacion_cliente",
        lambda cliente: {
            "ok": True,
            "iniciada": True,
            "en_curso": False,
            "dominio": "pesca-jacks.myshopify.com",
            "sync_intento_id": "2026-10-09T18:20:00+00:00",
        },
    )
    request = SimpleNamespace(headers={"accept": "application/json"})

    respuesta = portal_cliente.tienda_sincronizar_catalogo(
        request,
        cliente="PESCA_JACKS",
    )

    assert respuesta.status_code == 200
    assert _json(respuesta) == {
        "ok": True,
        "iniciada": True,
        "en_curso": False,
        "sync_intento_id": "2026-10-09T18:20:00+00:00",
    }


def test_sincronizar_catalogo_conserva_fallback_sin_javascript(monkeypatch):
    monkeypatch.setattr(
        shopify_catalogo,
        "solicitar_sincronizacion_cliente",
        lambda _cliente: {"ok": True, "iniciada": True, "en_curso": False},
    )
    request = SimpleNamespace(headers={"accept": "text/html"})

    respuesta = portal_cliente.tienda_sincronizar_catalogo(
        request,
        cliente="PESCA_JACKS",
    )

    assert respuesta.status_code == 303
    assert respuesta.headers["location"].startswith("/portal/tienda?ok=")


def test_estado_sync_es_privado_y_serializable(monkeypatch):
    actualizada = datetime(2026, 10, 9, 18, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(
        catalogo,
        "estado_sincronizacion_cliente",
        lambda cliente: {
            "estado": "COMPLETADO",
            "ultimo_intento_at": actualizada,
            "ultima_sincronizacion_at": actualizada,
        },
    )
    monkeypatch.setattr(
        catalogo,
        "resumen_stock_cliente",
        lambda cliente: {
            "variantes": 26,
            "unidades_disponibles": 679,
            "agotadas": 3,
        },
    )

    respuesta = portal_cliente.catalogo_sync_estado(cliente="PESCA_JACKS")
    cuerpo = _json(respuesta)

    assert respuesta.status_code == 200
    assert respuesta.headers["cache-control"] == "private, no-store"
    assert cuerpo["sincronizacion"] == {
        "estado": "COMPLETADO",
        "sync_intento_id": "2026-10-09T18:30:00+00:00",
        "ultima_sincronizacion_at": "2026-10-09T18:30:00+00:00",
    }
    assert cuerpo["resumen"] == {
        "variantes": 26,
        "unidades_disponibles": 679,
        "agotadas": 3,
    }
