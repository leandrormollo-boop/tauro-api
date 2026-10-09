#!/usr/bin/env python3
"""Capture deterministic, sanitized smoke-test evidence from TAURO staging.

The script deliberately uses only the Python standard library. It performs four
real HTTP requests, hashes each complete response before sanitizing a short
preview, and renders one PNG evidence card per request plus one card from the
sanitized Railway edge-log audit. Chrome uses an isolated, temporary profile.
Raw response bodies, request credentials, browser profiles, cookies, and
environment variables are never persisted.

Run only after Railway reports commit 803c496 as SUCCESS::

    python scripts/capture_staging_evidence.py \
      --confirm-success 803c496 \
      --base-url https://tauro-api-staging.up.railway.app
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


EXPECTED_DEPLOYMENT_REF = "803c496"
DEFAULT_STAGING_URL = "https://tauro-api-staging.up.railway.app"
EXPECTED_STAGING_HOST = "tauro-api-staging.up.railway.app"
APP_BRIDGE_URL = "https://cdn.shopify.com/shopifycloud/app-bridge.js"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_PREVIEW_CHARS = 500
USER_AGENT = "Tauro-Staging-Evidence/1.0"
EDGE_AUDIT_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "evidence"
    / "staging"
    / "tiendanube-rates-edge-audit.json"
)

SENSITIVE_KEY_RE = re.compile(
    r"(?:access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|"
    r"api[_-]?(?:key|secret)|secret|password|authorization|cookie|session|"
    r"hmac|signature|private[_-]?key|code)",
    re.IGNORECASE,
)
AUTH_VALUE_RE = re.compile(
    r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE
)
KEY_VALUE_RE = re.compile(
    r"(?i)\b(access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|"
    r"api[_-]?(?:key|secret)|secret|password|authorization|cookie|session|"
    r"hmac|signature|private[_-]?key|code)"
    r"(\s*[:=]\s*)(?:[\"']?)[^\s,;<>\"']+"
)
QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"client[_-]?secret|api[_-]?(?:key|secret)|secret|password|authorization|"
    r"hmac|signature|code)=)[^&\s]+"
)
JWT_RE = re.compile(
    r"\b[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"
)
LONG_CREDENTIAL_RE = re.compile(r"\b[A-Za-z0-9_+/=-]{40,}\b")
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)


class NoRedirectHandler(HTTPRedirectHandler):
    """Expose redirects as evidence instead of following them off-origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class VisibleTextParser(HTMLParser):
    """Extract visible text while ignoring scripts, styles, and attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._hidden_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "template", "svg"}:
            self._hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "template", "svg"}:
            self._hidden_depth = max(0, self._hidden_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._hidden_depth == 0 and data.strip():
            self.parts.append(data.strip())


class ScriptSrcParser(HTMLParser):
    """Collect script sources without executing or retaining page state."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attributes = {key.lower(): value for key, value in attrs}
        source = str(attributes.get("src") or "").strip()
        if source:
            self.sources.append(source)


@dataclass(frozen=True)
class SmokeCase:
    slug: str
    title: str
    method: str
    path: str
    expected_status: int
    body: bytes | None = None
    require_app_bridge: bool = False


SMOKE_CASES = (
    SmokeCase(
        slug="01-health-200",
        title="Health operativo",
        method="GET",
        path="/health",
        expected_status=200,
    ),
    SmokeCase(
        slug="02-shopify-app-200",
        title="Shopify App con App Bridge",
        method="GET",
        path="/shopify/app?shop=x.myshopify.com",
        expected_status=200,
        require_app_bridge=True,
    ),
    SmokeCase(
        slug="03-tiendanube-callback-400",
        title="Tiendanube callback inválido",
        method="GET",
        path="/integraciones/tiendanube/callback",
        expected_status=400,
    ),
    SmokeCase(
        slug="04-tiendanube-webhook-401",
        title="Tiendanube webhook sin firma",
        method="POST",
        path="/integraciones/tiendanube/webhook",
        expected_status=401,
        body=b"{}",
    ),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def redact_text(value: str) -> str:
    redacted = AUTH_VALUE_RE.sub("[REDACTED_AUTH]", value)
    redacted = KEY_VALUE_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", redacted)
    redacted = QUERY_SECRET_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", redacted)
    redacted = JWT_RE.sub("[REDACTED_JWT]", redacted)
    redacted = EMAIL_RE.sub("[REDACTED_EMAIL]", redacted)
    redacted = LONG_CREDENTIAL_RE.sub("[REDACTED_LONG_VALUE]", redacted)
    return redacted


def redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]"
            if SENSITIVE_KEY_RE.search(str(key))
            else redact_json(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_json(item) for item in value[:20]]
    if isinstance(value, str):
        return redact_text(value)
    return value


def sanitized_preview(body: bytes, content_type: str) -> str:
    decoded = body.decode("utf-8", errors="replace")
    if "json" in content_type.lower():
        try:
            decoded = json.dumps(
                redact_json(json.loads(decoded)),
                ensure_ascii=False,
                sort_keys=True,
                separators=(", ", ": "),
            )
        except (json.JSONDecodeError, TypeError):
            decoded = redact_text(decoded)
    elif "html" in content_type.lower() or "<html" in decoded[:500].lower():
        parser = VisibleTextParser()
        try:
            parser.feed(decoded)
            decoded = " · ".join(parser.parts)
        except Exception:  # A malformed preview must never expose raw markup.
            decoded = "[HTML no interpretable]"
        decoded = redact_text(decoded)
    else:
        decoded = redact_text(decoded)

    normalized = " ".join(html.unescape(decoded).split())
    if not normalized:
        return "[respuesta sin cuerpo visible]"
    if len(normalized) > MAX_PREVIEW_CHARS:
        return normalized[: MAX_PREVIEW_CHARS - 1] + "…"
    return normalized


def app_bridge_script_source(body: bytes) -> str:
    parser = ScriptSrcParser()
    try:
        parser.feed(body.decode("utf-8", errors="replace"))
    except Exception:
        return ""
    return next((source for source in parser.sources if source == APP_BRIDGE_URL), "")


def app_bridge_asset_available(source: str, timeout: float) -> bool:
    if source != APP_BRIDGE_URL:
        return False
    request = Request(
        source,
        headers={"Accept": "application/javascript", "User-Agent": USER_AGENT},
        method="GET",
    )
    try:
        with build_opener(NoRedirectHandler()).open(request, timeout=timeout) as response:
            response.read(1)
            return int(response.status) == 200
    except (HTTPError, URLError, TimeoutError, OSError):
        return False


def safe_content_type(headers: Any) -> str:
    value = headers.get("Content-Type", "unknown") if headers else "unknown"
    return redact_text(str(value))[:160]


def perform_request(base_url: str, case: SmokeCase, timeout: float) -> dict[str, Any]:
    url = urljoin(base_url.rstrip("/") + "/", case.path.lstrip("/"))
    headers = {
        "Accept": "application/json, text/html;q=0.9, text/plain;q=0.8",
        "Cache-Control": "no-cache",
        "User-Agent": USER_AGENT,
    }
    if case.body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=case.body, headers=headers, method=case.method)
    opener = build_opener(NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(response.status)
            content_type = safe_content_type(response.headers)
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        status = int(exc.code)
        content_type = safe_content_type(exc.headers)
        body = exc.read(MAX_RESPONSE_BYTES + 1)
    except (URLError, TimeoutError, OSError) as exc:
        received_at = utc_now()
        return {
            "slug": case.slug,
            "title": case.title,
            "method": case.method,
            "path": case.path,
            "expected_status": case.expected_status,
            "status": None,
            "received_at": received_at,
            "sha256": hashlib.sha256(b"").hexdigest(),
            "bytes": 0,
            "content_type": "unavailable",
            "preview": redact_text(f"Error de red: {type(exc).__name__}: {exc}"),
            "checks": {"status_matches": False},
            "passed": False,
        }

    received_at = utc_now()

    if len(body) > MAX_RESPONSE_BYTES:
        raise RuntimeError(
            f"{case.slug}: response exceeded the {MAX_RESPONSE_BYTES}-byte safety limit"
        )

    checks: dict[str, bool] = {"status_matches": status == case.expected_status}
    if case.require_app_bridge:
        app_bridge_source = app_bridge_script_source(body)
        checks["app_bridge_script_tag_exact"] = app_bridge_source == APP_BRIDGE_URL
        checks["app_bridge_asset_http_200"] = app_bridge_asset_available(
            app_bridge_source, timeout
        )

    return {
        "slug": case.slug,
        "title": case.title,
        "method": case.method,
        "path": case.path,
        "expected_status": case.expected_status,
        "status": status,
        "received_at": received_at,
        "sha256": hashlib.sha256(body).hexdigest(),
        "bytes": len(body),
        "content_type": content_type,
        "preview": sanitized_preview(body, content_type),
        "checks": checks,
        "passed": all(checks.values()),
    }


def load_edge_audit() -> dict[str, Any]:
    audit = json.loads(EDGE_AUDIT_PATH.read_text(encoding="utf-8"))
    request = audit.get("request", {})
    probe = audit.get("probe", {})
    log_inspection = audit.get("log_inspection", {})
    discrimination = audit.get("discrimination", {})
    fingerprint = str(probe.get("token_sha256", ""))

    invariants = {
        "environment_is_staging": audit.get("environment") == "staging",
        "deployment_matches": audit.get("deployment_ref") == EXPECTED_DEPLOYMENT_REF,
        "probe_discriminates_token_presence": (
            discrimination.get("passed") is True
            and discrimination.get("without_callback_token_status") == 422
            and discrimination.get("with_fictitious_callback_token_status") == 401
        ),
        "token_was_sent_in_query": probe.get("callback_token_sent_in_query") is True,
        "fixed_path_without_query": (
            request.get("path_recorded_by_edge")
            == "/integraciones/tiendanube/shipping/rates"
            and log_inspection.get("query_string_present_in_recorded_path") is False
        ),
        "one_edge_record_matched": (
            log_inspection.get("matching_http_edge_records_by_unique_user_agent") == 1
            and bool(str(request.get("request_id") or ""))
        ),
        "no_token_in_edge_logs": (
            log_inspection.get("exact_token_occurrences_in_http_edge_output") == 0
        ),
        "no_token_in_app_logs": (
            log_inspection.get(
                "exact_token_occurrences_in_application_or_deploy_output"
            )
            == 0
        ),
        "raw_token_not_persisted": probe.get("raw_token_persisted") is False,
        "fingerprint_is_sha256": bool(re.fullmatch(r"[0-9a-f]{64}", fingerprint)),
    }
    if not all(invariants.values()):
        failed = ", ".join(key for key, passed in invariants.items() if not passed)
        raise RuntimeError(f"La evidencia edge no supera sus invariantes: {failed}")

    duration = int(request.get("railway_total_duration_ms", 0))
    request_id = str(request.get("request_id") or "")
    return {
        "slug": "05-tiendanube-rates-token-redaction",
        "title": "Token de rates fuera de logs",
        "method": str(request.get("method", "POST")),
        "path": str(request["path_recorded_by_edge"]),
        "expected_status": 401,
        "status": int(request.get("http_status", 0)),
        "received_at": str(audit["probe_started_at"]),
        "sha256": fingerprint,
        "bytes": 0,
        "content_type": "Railway HTTP/edge",
        "hash_label": "SHA-256 del token ficticio del probe (sin guardar el valor)",
        "metadata": (
            f"Railway HTTP/edge · request {request_id} "
            f"· totalDuration {duration} ms"
        ),
        "preview": (
            "Control sin token: 422 · probe con token ficticio: 401. "
            "Railway registró sólo el path fijo, sin query. "
            "Ocurrencias exactas del token: edge 0 · aplicación/deploy 0."
        ),
        "checks": invariants,
        "passed": int(request.get("http_status", 0)) == 401 and all(invariants.values()),
        "source": "sanitized_railway_edge_audit",
        "deployment_id": str(audit["deployment_id"]),
    }


def locate_chrome(explicit_path: str | None) -> Path:
    candidates = [
        explicit_path,
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    raise RuntimeError("No se encontró Chrome/Chromium; use --chrome con su ejecutable.")


def playwright_command() -> list[str] | None:
    """Find a project/runtime Playwright CLI before an unrelated global CLI."""

    node = shutil.which("node")
    if node:
        probe = subprocess.run(
            [
                node,
                "-e",
                (
                    "const p=require('path');"
                    "const f=require.resolve('playwright/package.json');"
                    "process.stdout.write(p.join(p.dirname(f),'cli.js'))"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        candidate = Path(probe.stdout.strip()) if probe.returncode == 0 else None
        if candidate and candidate.is_file():
            return [node, str(candidate.resolve())]
    executable = shutil.which("playwright")
    return [executable] if executable else None


def browser_version(chrome: Path) -> str:
    playwright = playwright_command()
    if playwright:
        completed = subprocess.run(
            [*playwright, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return redact_text(completed.stdout.strip())[:120] + " · bundled Chromium"
    completed = subprocess.run(
        [str(chrome), "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return redact_text((completed.stdout or completed.stderr).strip())[:160]


def evidence_html(result: dict[str, Any], base_origin: str, deployment_ref: str) -> str:
    status_text = "SIN RESPUESTA" if result["status"] is None else str(result["status"])
    outcome = "APROBADO" if result["passed"] else "FALLÓ"
    outcome_class = "pass" if result["passed"] else "fail"
    check_rows = "".join(
        "<li><span>{}</span><strong class=\"{}\">{}</strong></li>".format(
            html.escape(check.replace("_", " ").title()),
            "ok" if passed else "bad",
            "SÍ" if passed else "NO",
        )
        for check, passed in result["checks"].items()
    )
    hash_label = html.escape(
        str(result.get("hash_label", "SHA-256 del cuerpo real (antes de sanitizar)"))
    )
    metadata_text = html.escape(
        str(
            result.get(
                "metadata",
                f"{result['bytes']} bytes · {result['content_type']}",
            )
        )
    )
    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(result['title'])}</title>
<style>
  :root {{ color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; min-height: 100vh; padding: 48px; background: #0b0912; color: #f8f6ff; }}
  main {{ max-width: 1420px; margin: 0 auto; padding: 44px; border: 1px solid #35294c;
          border-radius: 28px; background: linear-gradient(145deg, #15101f, #0f0c17); box-shadow: 0 28px 90px #0008; }}
  header {{ display: flex; justify-content: space-between; gap: 28px; align-items: flex-start; margin-bottom: 38px; }}
  .eyebrow {{ color: #bca7e8; font-size: 16px; letter-spacing: .16em; text-transform: uppercase; font-weight: 700; }}
  h1 {{ margin: 10px 0 0; font-size: 40px; line-height: 1.12; }}
  .badge {{ padding: 12px 18px; border-radius: 999px; font-size: 15px; font-weight: 800; letter-spacing: .08em; }}
  .pass {{ color: #76e6b0; background: #123e2c; border: 1px solid #2b9362; }}
  .fail {{ color: #ff9eaa; background: #471822; border: 1px solid #a93d50; }}
  .grid {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; }}
  .card {{ min-width: 0; padding: 22px; border: 1px solid #302640; border-radius: 18px; background: #0c0a12cc; }}
  .label {{ color: #978ca8; font-size: 13px; text-transform: uppercase; letter-spacing: .12em; margin-bottom: 9px; }}
  .value {{ min-width: 0; font-size: 18px; line-height: 1.45; overflow-wrap: anywhere; word-break: break-word; }}
  .mono {{ min-width: 0; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; overflow-wrap: anywhere; word-break: break-word; }}
  .status {{ min-width: 0; font-size: 30px; line-height: 1.25; font-weight: 800; color: #d6c4ff; overflow-wrap: anywhere; word-break: break-word; }}
  .status small {{ font-size: 20px; }}
  .wide {{ grid-column: 1 / -1; }}
  ul {{ list-style: none; padding: 0; margin: 0; }}
  li {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #292132; }}
  li:last-child {{ border-bottom: 0; }}
  .ok {{ color: #76e6b0; }} .bad {{ color: #ff9eaa; }}
  .preview {{ color: #d2cadf; font-size: 16px; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }}
  footer {{ margin-top: 24px; color: #7f748f; font-size: 13px; }}
</style>
</head>
<body>
<main>
  <header>
    <div><div class="eyebrow">TAURO · Evidencia real de staging</div><h1>{html.escape(result['title'])}</h1></div>
    <div class="badge {outcome_class}">{outcome}</div>
  </header>
  <section class="grid">
    <div class="card"><div class="label">Método y path</div><div class="value mono">{html.escape(result['method'])} {html.escape(result['path'])}</div></div>
    <div class="card"><div class="label">Estado HTTP</div><div class="status">{status_text} <small>/ esperado {result['expected_status']}</small></div></div>
    <div class="card wide"><div class="label">Timestamp UTC · origen · despliegue confirmado</div><div class="value mono">{html.escape(result['received_at'])} · {html.escape(base_origin)} · {html.escape(deployment_ref)}</div></div>
    <div class="card wide"><div class="label">{hash_label}</div><div class="value mono">{html.escape(result['sha256'])}</div></div>
    <div class="card"><div class="label">Metadatos no sensibles</div><div class="value">{metadata_text}</div></div>
    <div class="card"><div class="label">Comprobaciones</div><ul>{check_rows}</ul></div>
    <div class="card wide"><div class="label">Preview sanitizado</div><div class="preview">{html.escape(result['preview'])}</div></div>
  </section>
  <footer>No se guardó el cuerpo original. No se usaron cookies, credenciales ni perfiles del navegador del usuario.</footer>
</main>
</body>
</html>"""


def render_png(chrome: Path, source_html: Path, output_png: Path, profile_dir: Path) -> None:
    if output_png.exists():
        output_png.unlink()
    playwright = playwright_command()
    if playwright:
        # Playwright waits for a complete paint before capturing. This avoids
        # the partially painted compositor tiles that Chrome's bare
        # --screenshot mode can emit intermittently on macOS.
        command = [
            *playwright,
            "screenshot",
            "--browser",
            "chromium",
            "--color-scheme",
            "dark",
            "--viewport-size",
            "1600,1400",
            "--wait-for-timeout",
            "750",
            "--timeout",
            "20000",
            source_html.resolve().as_uri(),
            str(output_png),
        ]
    else:
        command = [
            str(chrome),
            "--headless=new",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-dev-shm-usage",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={profile_dir}",
            "--window-size=1600,1400",
            "--timeout=1000",
            f"--screenshot={output_png}",
            source_html.resolve().as_uri(),
        ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Chrome agotó el timeout al generar {output_png.name}") from exc

    if (
        completed.returncode != 0
        or not output_png.is_file()
        or output_png.stat().st_size == 0
    ):
        diagnostic = redact_text(
            (completed.stderr or completed.stdout or "sin diagnóstico").strip()
        )
        raise RuntimeError(f"Chrome no pudo generar {output_png.name}: {diagnostic[:500]}")


def validate_staging_url(value: str) -> str:
    parsed = urlsplit(value.rstrip("/"))
    if parsed.scheme != "https" or parsed.hostname != EXPECTED_STAGING_HOST:
        raise argparse.ArgumentTypeError(
            f"--base-url debe ser exactamente https://{EXPECTED_STAGING_HOST}"
        )
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise argparse.ArgumentTypeError(
            "--base-url no admite credenciales, puerto, path, query ni fragment"
        )
    return DEFAULT_STAGING_URL


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Captura cinco evidencias sanitizadas de staging."
    )
    parser.add_argument(
        "--confirm-success",
        required=True,
        choices=[EXPECTED_DEPLOYMENT_REF],
        help=(
            "Confirmación manual del ref desplegado. Use 803c496 únicamente después "
            "de comprobar estado SUCCESS en Railway."
        ),
    )
    parser.add_argument(
        "--base-url",
        type=validate_staging_url,
        default=DEFAULT_STAGING_URL,
        help=f"Origen HTTPS de staging (default: {DEFAULT_STAGING_URL}).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "docs" / "evidence" / "staging",
        help="Directorio para los PNG y el manifiesto JSON.",
    )
    parser.add_argument("--chrome", help="Ruta explícita a Chrome/Chromium.")
    parser.add_argument("--timeout", type=float, default=15.0, help="Timeout HTTP por request.")
    args = parser.parse_args()
    if not 1 <= args.timeout <= 60:
        parser.error("--timeout debe estar entre 1 y 60 segundos")
    return args


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    chrome = locate_chrome(args.chrome)

    results = [perform_request(args.base_url, case, args.timeout) for case in SMOKE_CASES]
    results.append(load_edge_audit())

    with tempfile.TemporaryDirectory(prefix="tauro-staging-evidence-") as temp_name:
        temp_dir = Path(temp_name)
        for index, result in enumerate(results, start=1):
            source_html = temp_dir / f"evidence-{index:02d}.html"
            source_html.write_text(
                evidence_html(result, args.base_url, args.confirm_success),
                encoding="utf-8",
            )
            output_png = output_dir / f"{result['slug']}.png"
            render_png(
                chrome,
                source_html,
                output_png,
                temp_dir / f"chrome-profile-{index:02d}",
            )
            result["screenshot"] = output_png.name
            result["screenshot_sha256"] = hashlib.sha256(output_png.read_bytes()).hexdigest()

    manifest = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "environment": "staging",
        "base_origin": args.base_url,
        "confirmed_deployment_ref": args.confirm_success,
        "browser": browser_version(chrome),
        "privacy": {
            "raw_bodies_persisted": False,
            "request_credentials_used": False,
            "user_browser_profile_used": False,
            "previews_sanitized": True,
        },
        "all_passed": all(result["passed"] for result in results),
        "results": results,
    }
    manifest_path = output_dir / "staging-smoke-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    for result in results:
        status = "PASS" if result["passed"] else "FAIL"
        print(f"{status} {result['method']} {result['path']} -> {result['status']}")
    print(f"Manifest: {manifest_path}")
    return 0 if manifest["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
