"""Compresión sólo de recursos públicos, nunca del HTML ni del streaming de tarifas."""

from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES, GZipMiddleware
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send


def accepts_gzip(value: str) -> bool:
    qualities = {}
    for item in value.split(","):
        encoding, *parameters = item.lower().strip().split(";")
        quality = 1.0
        for parameter in parameters:
            key, _, raw = parameter.partition("=")
            if key.strip() == "q":
                try:
                    quality = float(raw.strip())
                except ValueError:
                    quality = 0.0
        quality = quality if 0 <= quality <= 1 else 0.0
        encoding = encoding.strip()
        qualities[encoding] = min(qualities.get(encoding, quality), quality)
    return qualities.get("gzip", qualities.get("*", 0.0)) > 0


class PublicStaticGZipMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.compressed = GZipMiddleware(
            app, minimum_size=1024, compresslevel=6,
            # También excluir una página de error devuelta para un asset inexistente.
            exclude_content_types=DEFAULT_EXCLUDED_CONTENT_TYPES + (
                "text/html", "application/xhtml+xml", "application/pdf",
                "application/x-ndjson",
            ),
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        public_asset = path == "/styles.css" or (
            path.startswith("/static/")
            and path.endswith((".css", ".js", ".json", ".svg"))
        )
        if scope["type"] != "http" or scope.get("method") not in {"GET", "HEAD"} or not public_asset:
            await self.app(scope, receive, send)
            return

        async def send_asset(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if "accept-encoding" not in [item.strip().lower() for item in headers.get("vary", "").split(",")]:
                    headers.add_vary_header("Accept-Encoding")
                # Las variantes comprimida/identity tienen el mismo contenido,
                # pero no los mismos bytes: el validador compartido es débil.
                if message["status"] != 206 and "etag" in headers and not headers["etag"].startswith("W/"):
                    headers["etag"] = "W/" + headers["etag"]
            await send(message)

        if scope["method"] == "GET" and accepts_gzip(Headers(scope=scope).get("accept-encoding", "")):
            # Starlette usa una búsqueda por substring. Negociamos primero
            # para respetar q=0, mayúsculas y el comodín del cliente.
            gzip_scope = dict(scope, headers=[
                (key, value) for key, value in scope["headers"]
                if key.lower() != b"accept-encoding"
            ] + [(b"accept-encoding", b"gzip")])
            await self.compressed(gzip_scope, receive, send_asset)
        else:
            await self.app(scope, receive, send_asset)
