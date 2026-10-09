import asyncio
import gzip
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.static_compression import PublicStaticGZipMiddleware


def _response(path, *, accept="gzip", status=200, existing_encoding=None, content_type="text/css"):
    """Captura bytes ASGI reales: el cliente HTTP descomprime automáticamente."""
    body = b"/* fixture css */ .demo { color: #a78bfa; }\n" * 500
    messages = []

    async def app(scope, receive, send):
        headers = [(b"content-type", content_type.encode()), (b"content-length", str(len(body)).encode())]
        if existing_encoding:
            headers.append((b"content-encoding", existing_encoding.encode()))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body[:12000], "more_body": True})
        await send({"type": "http.response.body", "body": body[12000:]})

    async def send(message):
        messages.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    asyncio.run(PublicStaticGZipMiddleware(app)(
        {"type": "http", "method": "GET", "path": path,
         "headers": [(b"accept-encoding", accept.encode())]}, receive, send,
    ))
    headers = dict(messages[0]["headers"])
    return body, headers, [m["body"] for m in messages[1:]]


@pytest.mark.parametrize("path", ["/static/css/tauro.css", "/static/js/app.js",
    "/static/data/quote-map/world.json", "/static/img/demo.svg", "/styles.css"])
def test_comprime_recursos_publicos_sin_cambiar_sus_bytes(path):
    original, headers, chunks = _response(path)
    packed = b"".join(chunks)
    assert headers[b"content-encoding"] == b"gzip"
    assert b"Accept-Encoding" in headers[b"vary"]
    assert gzip.decompress(packed) == original
    assert len(packed) < len(original) / 4


@pytest.mark.parametrize("path", ["/web", "/portal/cotizar", "/portal/api/cotizar",
    "/cotizar", "/portal/cotizaciones/cliente.pdf", "/static/img/logo.png",
    "/static/demo.pdf", "/admin/home"])
def test_no_intercepta_html_documentos_ni_streams_de_tarifas(path):
    original, headers, chunks = _response(path)
    assert b"content-encoding" not in headers
    # Los fragmentos salen tal como los entregó la aplicación, sin buffer gzip.
    assert chunks == [original[:12000], original[12000:]]


def test_respeta_identidad_respuesta_parcial_y_compresion_previa():
    for kwargs in ({"accept": "identity"}, {"status": 206}, {"existing_encoding": "br"}):
        original, headers, chunks = _response("/static/js/app.js", **kwargs)
        assert b"".join(chunks) == original
        assert headers.get(b"content-encoding") != b"gzip"


def test_pagina_de_error_de_asset_no_se_comprime():
    original, headers, chunks = _response("/static/inexistente.js", status=404, content_type="text/html")
    assert b"content-encoding" not in headers
    assert b"".join(chunks) == original


@pytest.mark.parametrize("accept", ["gzip;q=0", "br, gzip;q=0", "*;q=1, gzip;q=0", "xgzip", "gzip;q=invalid", "gzip;q=2"])
def test_no_comprime_si_el_cliente_rechaza_gzip(accept):
    original, headers, chunks = _response("/static/js/app.js", accept=accept)
    assert b"content-encoding" not in headers
    assert b"".join(chunks) == original


@pytest.mark.parametrize("accept", ["GZip", "br, gzip;q=0.5", "*;q=0.8"])
def test_negocia_gzip_por_token_calidad_y_comodin(accept):
    original, headers, chunks = _response("/static/js/app.js", accept=accept)
    assert headers[b"content-encoding"] == b"gzip"
    assert gzip.decompress(b"".join(chunks)) == original


def test_css_real_en_app_y_condicional_http_sin_arrancar_jobs():
    from main import app
    client = TestClient(app)
    response = client.get("/static/css/tauro.css", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["etag"].startswith("W/")
    assert response.content == (Path(__file__).resolve().parents[1] / "static/css/tauro.css").read_bytes()
    cached = client.get("/static/css/tauro.css", headers={"If-None-Match": response.headers["etag"]})
    assert cached.status_code == 304
    assert not cached.content
    assert "Accept-Encoding" in cached.headers["vary"]
    head = client.head("/static/css/tauro.css")
    assert head.headers["etag"] == response.headers["etag"]
    assert "Accept-Encoding" in head.headers["vary"]
