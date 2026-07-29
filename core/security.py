from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.background import BackgroundTask, BackgroundTasks
from starlette.middleware.base import BaseHTTPMiddleware

from servicios.rate_limit import check_rate, client_ip


SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
PRIVATE_PREFIXES = ("/admin", "/portal")
CSRF_PROTECTED_PREFIXES = ("/admin", "/portal")
JSON_POST_PATHS = {"/cotizar-web", "/cotizar", "/pedido"}

# Únicas rutas que aceptan archivos (multipart). El bloqueo general sigue
# vigente para todo lo demás: cada excepción acá es una superficie de ataque
# nueva, así que se agrega ruta por ruta, nunca por prefijo. El tope de 8 MB
# alcanza para cualquier comprobante escaneado; más que eso es otra cosa.
UPLOAD_PATHS = {
    "/portal/pagos/informar",   # cliente informa un pago con comprobante
    "/admin/pagos/nuevo",       # admin carga pago (comprobante opcional)
    "/admin/envios/nuevo",      # admin carga factura (PDF opcional)
}
UPLOAD_MAX_BYTES = 8 * 1024 * 1024


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _csv_env(name: str) -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in os.getenv(name, "").split(",")
        if item.strip()
    )


def is_production_env() -> bool:
    environment = (
        os.getenv("APP_ENV")
        or os.getenv("ENV")
        or os.getenv("RAILWAY_ENVIRONMENT_NAME")
        or ""
    ).strip().lower()
    running_on_railway = any(
        os.getenv(name)
        for name in (
            "RAILWAY_ENVIRONMENT",
            "RAILWAY_ENVIRONMENT_ID",
            "RAILWAY_PROJECT_ID",
            "RAILWAY_SERVICE_ID",
        )
    )
    return running_on_railway or environment in {"production", "prod"}


def normalize_origin(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


@dataclass(frozen=True)
class SecurityConfig:
    production: bool
    allowed_origins: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    max_body_bytes: int
    public_quote_limit: int
    api_limit: int
    portal_quote_limit: int
    hsts_max_age: int
    hsts_include_subdomains: bool


def load_security_config() -> SecurityConfig:
    production = is_production_env()

    default_origins = (
        "https://taurosolutions.ar",
        "https://www.taurosolutions.ar",
        "https://tauro-api-production.up.railway.app",
    )
    if not production:
        default_origins += (
            "http://localhost:8000",
            "http://localhost:8123",
            "http://127.0.0.1:8000",
            "http://127.0.0.1:8123",
        )

    configured_origins = tuple(
        origin
        for raw in _csv_env("CORS_ALLOWED_ORIGINS")
        if (origin := normalize_origin(raw))
    )
    allowed_origins = tuple(dict.fromkeys(default_origins + configured_origins))

    default_hosts = (
        "taurosolutions.ar",
        "www.taurosolutions.ar",
        "tauro-api-production.up.railway.app",
        "tauro-api.railway.internal",
    )
    if not production:
        default_hosts += (
            "localhost",
            "127.0.0.1",
            "0.0.0.0",
            "testserver",
        )
    allowed_hosts = tuple(
        dict.fromkeys(default_hosts + _csv_env("ALLOWED_HOSTS"))
    )

    return SecurityConfig(
        production=production,
        allowed_origins=allowed_origins,
        allowed_hosts=allowed_hosts,
        max_body_bytes=_env_int("SECURITY_MAX_BODY_BYTES", 1_048_576),
        public_quote_limit=_env_int("PUBLIC_QUOTE_RATE_LIMIT", 12),
        api_limit=_env_int("B2B_API_RATE_LIMIT", 120),
        portal_quote_limit=_env_int("PORTAL_QUOTE_RATE_LIMIT", 30),
        hsts_max_age=_env_int("SECURITY_HSTS_MAX_AGE", 31_536_000),
        hsts_include_subdomains=_env_bool(
            "SECURITY_HSTS_INCLUDE_SUBDOMAINS", True
        ),
    )


def _request_origin(request: Request) -> str | None:
    forwarded_proto = (
        request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    )
    scheme = forwarded_proto or request.url.scheme
    host = request.headers.get("host")
    return normalize_origin(f"{scheme}://{host}") if host else None


def _source_origin(request: Request) -> str | None:
    origin = request.headers.get("origin")
    if origin:
        return normalize_origin(origin)
    return normalize_origin(request.headers.get("referer"))


def _csrf_source_allowed(request: Request, config: SecurityConfig) -> bool:
    fetch_site = (request.headers.get("sec-fetch-site") or "").lower()
    if fetch_site == "cross-site":
        return False

    source = _source_origin(request)
    if not source:
        return False

    allowed = set(config.allowed_origins)
    if not config.production:
        current = _request_origin(request)
        if current:
            allowed.add(current)
    return source in allowed


def _credential_fingerprint(request: Request) -> str:
    credential = (
        request.headers.get("x-api-key")
        or request.cookies.get("token")
        or client_ip(request)
    )
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()[:20]


def _rate_rule(
    request: Request, config: SecurityConfig
) -> tuple[str, int, int] | None:
    path = request.scope.get("path", "")
    if request.method == "GET" and path == "/portal/api/precio":
        return ("portal_quote", config.portal_quote_limit, 60)
    if request.method != "POST":
        return None

    if path == "/cotizar-web":
        return ("public_quote", config.public_quote_limit, 60)
    if path in {"/cotizar", "/pedido"}:
        return ("b2b_api", config.api_limit, 60)
    if path.startswith("/portal/api/") or path == "/portal/cotizar":
        return ("portal_quote", config.portal_quote_limit, 60)
    return None


def _public_csp(production: bool) -> str:
    directives = [
        "default-src 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com data:",
        "img-src 'self' data: blob:",
        "connect-src 'self'",
        "media-src 'none'",
        "worker-src 'none'",
        "manifest-src 'self'",
    ]
    if production:
        directives.append("upgrade-insecure-requests")
    return "; ".join(directives)


def _private_csp(production: bool, nonce: str | None = None) -> str:
    script_source = "script-src 'self'"
    if nonce:
        script_source += f" 'nonce-{nonce}'"
    directives = [
        "default-src 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        script_source,
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com data:",
        "img-src 'self' data: blob:",
        "connect-src 'self'",
        "media-src 'self' blob:",
        "worker-src 'none'",
        "manifest-src 'self'",
    ]
    if production:
        directives.append("upgrade-insecure-requests")
    return "; ".join(directives)


def content_security_policy(
    path: str,
    production: bool,
    nonce: str | None = None,
) -> str:
    if (
        path in {"/web", "/app.js", "/styles.css", "/tweaks-panel.jsx"}
        or path.startswith("/components/")
    ):
        return _public_csp(production)
    return _private_csp(production, nonce)


def _is_html_request(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    path = request.scope.get("path", "")
    return "text/html" in accept or path.startswith(PRIVATE_PREFIXES)


def _valid_host_header(host: str) -> bool:
    if not host or len(host) > 255:
        return False
    if any(ord(char) < 33 for char in host):
        return False
    return not any(char in host for char in "/\\@?#")


def _error_response(
    request: Request,
    status_code: int,
    message: str,
    headers: dict[str, str] | None = None,
) -> Response:
    if _is_html_request(request):
        return HTMLResponse(
            (
                "<!doctype html><html lang=\"es\"><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                "<title>Solicitud rechazada - Tauro Solutions</title></head>"
                "<body><main><h1>Solicitud rechazada</h1>"
                f"<p>{message}</p><p><a href=\"/web\">Volver a Tauro</a></p>"
                "</main></body></html>"
            ),
            status_code=status_code,
            headers=headers,
        )
    return JSONResponse(
        {"ok": False, "error": message},
        status_code=status_code,
        headers=headers,
    )


def _add_background_audit(
    response: Response,
    *,
    event: str,
    request: Request,
    success: bool,
) -> None:
    from servicios.auditoria import registrar_evento

    path = request.scope.get("path", "")
    if path.startswith("/admin"):
        actor_type = "admin"
        actor_ref = "admin" if request.cookies.get("admin_token") else None
    elif path.startswith("/portal"):
        actor_type = "cliente"
        token = request.cookies.get("token")
        actor_ref = (
            hashlib.sha256(token.encode("utf-8")).hexdigest()[:20]
            if token
            else None
        )
    else:
        actor_type = "publico"
        actor_ref = None

    task = BackgroundTask(
        registrar_evento,
        event=event,
        actor_type=actor_type,
        actor_ref=actor_ref,
        ip=client_ip(request),
        method=request.method,
        path=path,
        status_code=response.status_code,
        success=success,
        request_id=getattr(request.state, "request_id", None),
        metadata={
            "user_agent": (request.headers.get("user-agent") or "")[:200],
        },
    )
    if response.background is None:
        response.background = task
        return

    tasks = BackgroundTasks()
    tasks.tasks.append(response.background)
    tasks.tasks.append(task)
    response.background = tasks


def _audit_event(request: Request) -> str | None:
    path = request.scope.get("path", "")
    if path == "/admin/login" and request.method == "POST":
        return "admin.login"
    if path == "/admin/logout":
        return "admin.logout"
    if path == "/portal/login" and request.method == "POST":
        return "portal.login"
    if path == "/portal/login/send" and request.method == "POST":
        return "portal.magic_link_request"
    if path == "/portal/auth":
        return "portal.magic_link_exchange"
    if path == "/portal/logout":
        return "portal.logout"
    if request.method not in SAFE_METHODS and path.startswith("/admin/"):
        return "admin.mutation"
    if request.method not in SAFE_METHODS and path.startswith(
        ("/portal/envios", "/portal/direcciones", "/portal/catalogo")
    ):
        return "portal.mutation"
    return None


class SecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, config: SecurityConfig):
        super().__init__(app)
        self.config = config

    async def dispatch(self, request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex
        request.state.csp_nonce = secrets.token_urlsafe(18)
        path = request.scope.get("path", "")
        host = request.headers.get("host", "")

        # Mitigates ambiguous Host/path parsing before any use of request.url.
        if not path.startswith("/") or not _valid_host_header(host):
            response = _error_response(
                request,
                400,
                "La solicitud contiene una URL invalida.",
            )
            self._apply_headers(request, response)
            return response

        content_length = request.headers.get("content-length")
        if content_length:
            try:
                parsed_length = int(content_length)
                too_large = (
                    parsed_length < 0
                    or parsed_length > self.config.max_body_bytes
                )
            except ValueError:
                too_large = True
            if too_large:
                response = _error_response(
                    request,
                    413,
                    "La solicitud supera el tamano permitido.",
                )
                self._apply_headers(request, response)
                return response

        if (
            request.method not in SAFE_METHODS
            and path.startswith(CSRF_PROTECTED_PREFIXES)
            and not _csrf_source_allowed(request, self.config)
        ):
            response = _error_response(
                request,
                403,
                "No pudimos validar el origen del formulario. "
                "Recarga la pagina e intenta nuevamente.",
            )
            self._apply_headers(request, response)
            event = _audit_event(request)
            if event:
                _add_background_audit(
                    response,
                    event=f"{event}.csrf_rejected",
                    request=request,
                    success=False,
                )
            return response

        content_type = (
            request.headers.get("content-type", "")
            .split(";", 1)[0]
            .strip()
            .lower()
        )
        managed_json_post = (
            request.method == "POST"
            and (
                path in JSON_POST_PATHS
                or path.startswith("/portal/api/")
            )
        )
        if managed_json_post and content_type != "application/json":
            response = _error_response(
                request,
                415,
                "Este endpoint acepta solamente JSON.",
            )
            self._apply_headers(request, response)
            return response
        if managed_json_post and not content_length:
            response = _error_response(
                request,
                411,
                "La solicitud debe indicar su tamano.",
            )
            self._apply_headers(request, response)
            return response

        if request.method not in SAFE_METHODS and path.startswith(
            PRIVATE_PREFIXES
        ):
            if content_type.startswith("multipart/"):
                # Comprobantes de pago y facturas: multipart permitido SOLO
                # en la lista blanca, con tope de tamaño propio. El resto de
                # las rutas privadas sigue rechazando archivos como siempre.
                if path in UPLOAD_PATHS:
                    if not content_length:
                        response = _error_response(
                            request, 411, "La solicitud debe indicar su tamano.")
                        self._apply_headers(request, response)
                        return response
                    try:
                        declarado = int(content_length)
                    except ValueError:
                        declarado = UPLOAD_MAX_BYTES + 1
                    if declarado > UPLOAD_MAX_BYTES:
                        response = _error_response(
                            request, 413,
                            "El archivo es demasiado grande (maximo 8 MB).")
                        self._apply_headers(request, response)
                        return response
                    # Dentro del límite: sigue al handler, que valida el TIPO
                    # (sólo JPG/PNG/PDF) — acá todavía no se leyó el cuerpo.
                else:
                    response = _error_response(
                        request,
                        415,
                        "Este formulario no acepta archivos.",
                    )
                    self._apply_headers(request, response)
                    return response

            if content_type == "application/x-www-form-urlencoded":
                private_form_limit = min(
                    self.config.max_body_bytes, 131_072
                )
                if not content_length:
                    response = _error_response(
                        request,
                        411,
                        "La solicitud debe indicar su tamano.",
                    )
                    self._apply_headers(request, response)
                    return response

                body = await request.body()
                if len(body) > private_form_limit:
                    response = _error_response(
                        request,
                        413,
                        "El formulario supera el tamano permitido.",
                    )
                    self._apply_headers(request, response)
                    return response
                if b";" in body or body.count(b"&") >= 200:
                    response = _error_response(
                        request,
                        400,
                        "El formulario contiene parametros invalidos.",
                    )
                    self._apply_headers(request, response)
                    return response

        rule = _rate_rule(request, self.config)
        if rule:
            label, limit, window = rule
            key = f"{label}:{_credential_fingerprint(request)}"
            if not check_rate(key, max_attempts=limit, window_seconds=window):
                response = _error_response(
                    request,
                    429,
                    "Demasiadas solicitudes. Espera un minuto e intenta nuevamente.",
                    headers={"Retry-After": str(window)},
                )
                self._apply_headers(request, response)
                return response

        response = await call_next(request)
        self._apply_headers(request, response)

        event = _audit_event(request)
        if event:
            _add_background_audit(
                response,
                event=event,
                request=request,
                success=200 <= response.status_code < 400,
            )
        return response

    def _apply_headers(self, request: Request, response: Response) -> None:
        path = request.scope.get("path", "/")
        headers = response.headers
        headers["X-Request-ID"] = request.state.request_id
        headers["X-Content-Type-Options"] = "nosniff"
        headers["X-Frame-Options"] = "DENY"
        headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
        headers["Cross-Origin-Resource-Policy"] = "same-site"
        headers["X-Permitted-Cross-Domain-Policies"] = "none"
        nonce = (
            getattr(request.state, "csp_nonce", None)
            if path.startswith(PRIVATE_PREFIXES)
            else None
        )
        headers["Content-Security-Policy"] = content_security_policy(
            path,
            self.config.production,
            nonce,
        )
        # MutableHeaders no tiene .pop() en esta versión de Starlette
        if "server" in headers:
            del headers["server"]

        if self.config.production:
            hsts = f"max-age={self.config.hsts_max_age}"
            if self.config.hsts_include_subdomains:
                hsts += "; includeSubDomains"
            headers["Strict-Transport-Security"] = hsts

        if path.startswith(PRIVATE_PREFIXES):
            headers["Cache-Control"] = "no-store, max-age=0"
            headers["Pragma"] = "no-cache"
            headers["X-Robots-Tag"] = "noindex, nofollow"
