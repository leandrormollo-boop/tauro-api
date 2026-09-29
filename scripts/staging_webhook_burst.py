#!/usr/bin/env python3
"""Exercise the Shopify webhook path and health endpoint in staging only.

The webhook secret can be supplied with ``--webhook-secret`` or, preferably,
through ``TAURO_STAGING_SHOPIFY_WEBHOOK_SECRET``.  The secret, request bodies,
signatures, and response bodies are never included in output.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request

import requests


SECRET_ENV_VAR = "TAURO_STAGING_SHOPIFY_WEBHOOK_SECRET"
APP_SECRET_ENV_VAR = "SHOPIFY_PUBLIC_API_SECRET"
WEBHOOK_PATH = "/integraciones/shopify/webhook/inventory-levels-update"
WEBHOOK_TOPIC = "inventory_levels/update"
HEALTH_PATH = "/health"
HEALTH_INTERVAL_SECONDS = 0.1
MINIMUM_HEALTH_SECONDS = 10.0
REQUEST_TIMEOUT_SECONDS = 5.0
MAX_COUNT = 1_000
STAGING_HOST_RE = re.compile(r"(?:^|[.-])staging(?:[.-]|$)", re.IGNORECASE)
SHOP_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")
NON_PRODUCTION_SHOP_RE = re.compile(
    r"(?:^|-)(?:staging|stage|qa|dev|test|sandbox)(?:-|$)"
)


_thread_state = threading.local()


def http_session() -> requests.Session:
    """Keep one TLS connection pool per worker without sharing mutable state."""

    existing = getattr(_thread_state, "session", None)
    if existing is not None:
        return existing
    session = requests.Session()
    session.trust_env = False
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=1,
        pool_maxsize=1,
        max_retries=0,
        pool_block=True,
    )
    session.mount("https://", adapter)
    _thread_state.session = session
    return session


@dataclass(frozen=True)
class Observation:
    """Sanitized timing and outcome for one request."""

    started_at: float
    finished_at: float
    elapsed_ms: float
    status: int | None
    error: str | None
    redirect_rejected: bool


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0 or parsed > MAX_COUNT:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_COUNT}")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return parsed


def staging_base_url(value: str) -> str:
    """Validate and normalize a base URL without ever probing it."""

    parsed = urlsplit(value.strip())
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https":
        raise argparse.ArgumentTypeError("base URL must use HTTPS")
    if not hostname or not STAGING_HOST_RE.search(hostname):
        raise argparse.ArgumentTypeError("base URL host must be an explicit staging host")
    if parsed.username is not None or parsed.password is not None:
        raise argparse.ArgumentTypeError("base URL must not contain credentials")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise argparse.ArgumentTypeError("base URL must not contain a path, query, or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise argparse.ArgumentTypeError("base URL has an invalid port") from exc
    netloc = hostname if port is None else f"{hostname}:{port}"
    return urlunsplit(("https", netloc, "", "", ""))


def shop_domain(value: str) -> str:
    normalized = value.strip().lower().rstrip(".")
    if not SHOP_DOMAIN_RE.fullmatch(normalized):
        raise argparse.ArgumentTypeError("shop domain must be a *.myshopify.com hostname")
    shop_name = normalized.removesuffix(".myshopify.com")
    if not NON_PRODUCTION_SHOP_RE.search(shop_name):
        raise argparse.ArgumentTypeError(
            "shop domain must contain an explicit non-production marker "
            "(staging, stage, qa, dev, test, or sandbox)"
        )
    return normalized


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Send a signed inventory webhook burst while sampling /health. "
            "Only explicit HTTPS staging hosts are accepted; redirects are never followed."
        )
    )
    result.add_argument("--base-url", required=True, type=staging_base_url)
    result.add_argument("--shop-domain", required=True, type=shop_domain)
    result.add_argument(
        "--webhook-secret",
        help=f"Shopify app secret (prefer environment variable {SECRET_ENV_VAR})",
    )
    result.add_argument("--count", type=positive_int, default=200)
    result.add_argument(
        "--concurrency",
        type=positive_int,
        default=8,
        help=(
            "maximum simultaneous webhook requests (default: 8, leaving "
            "capacity in the 10-connection staging DB pool)"
        ),
    )
    result.add_argument("--window-seconds", type=positive_float, default=10.0)
    result.add_argument(
        "--output",
        choices=("json", "markdown"),
        default="json",
        help="sanitized result format written to stdout (default: json)",
    )
    result.add_argument(
        "--json-file",
        type=Path,
        help="optional path for the sanitized JSON evidence",
    )
    result.add_argument(
        "--markdown-file",
        type=Path,
        help="optional path for the sanitized Markdown evidence",
    )
    return result


def utc_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def webhook_request(
    base_url: str,
    domain: str,
    secret: str,
    index: int,
    event_at: str,
) -> Request:
    """Build one unique request; callers must not log or persist the result."""

    payload = {
        "available": index % 100,
        # These explicit staging-only identifiers satisfy the signed payload
        # contract but are not valid Shopify GIDs or numeric resource IDs.
        # The app therefore exercises HMAC, installation lookup, temporal
        # classification and threadpool isolation without making a request to
        # a real Shopify store or mutating catalog state.
        "inventory_item_id": f"tauro-staging-item-{index:06d}",
        "location_id": f"tauro-staging-location-{index:06d}",
        "updated_at": event_at,
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = base64.b64encode(
        hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    ).decode("ascii")
    return Request(
        f"{base_url}{WEBHOOK_PATH}",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Tauro-Staging-Webhook-Burst/1.0",
            "X-Shopify-Shop-Domain": domain,
            "X-Shopify-Topic": WEBHOOK_TOPIC,
            "X-Shopify-Triggered-At": event_at,
            "X-Shopify-Webhook-Id": f"tauro-staging-burst-{index:06d}",
            "X-Shopify-Hmac-SHA256": signature,
        },
    )


def health_request(base_url: str) -> Request:
    return Request(
        f"{base_url}{HEALTH_PATH}",
        method="GET",
        headers={"User-Agent": "Tauro-Staging-Webhook-Burst/1.0"},
    )


def observe(_opener, request: Request, scheduled_at: float | None = None) -> Observation:
    """Perform a request and retain no headers or body from either direction."""

    started_at = time.monotonic()
    status: int | None = None
    error: str | None = None
    redirect_rejected = False
    try:
        response = http_session().request(
            method=request.get_method(),
            url=request.full_url,
            data=request.data,
            headers=dict(request.header_items()),
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
            stream=True,
        )
        status = int(response.status_code)
        redirect_rejected = 300 <= status < 400
        response.close()
    except (requests.RequestException, TimeoutError, OSError) as exc:
        # The exception class is useful for a distribution and cannot contain
        # the URL, secret, signed headers, or response content.
        error = type(exc).__name__
    except Exception as exc:  # pragma: no cover - defensive aggregation only
        error = type(exc).__name__
    finished_at = time.monotonic()
    latency_origin = scheduled_at if scheduled_at is not None else started_at
    return Observation(
        started_at=started_at,
        finished_at=finished_at,
        elapsed_ms=(finished_at - latency_origin) * 1_000,
        status=status,
        error=error,
        redirect_rejected=redirect_rejected,
    )


def percentile_95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return round(ordered[rank], 3)


def distribution(observations: list[Observation]) -> tuple[dict[str, int], dict[str, int]]:
    statuses = Counter(str(item.status) for item in observations if item.status is not None)
    errors = Counter(item.error for item in observations if item.error is not None)
    return dict(sorted(statuses.items())), dict(sorted(errors.items()))


def summarize(
    webhook_observations: list[Observation],
    health_observations: list[Observation],
    requested_count: int,
    concurrency: int,
    window_seconds: float,
    health_monitor_seconds: float,
) -> dict[str, Any]:
    webhook_statuses, webhook_errors = distribution(webhook_observations)
    health_statuses, health_errors = distribution(health_observations)
    webhook_2xx = sum(
        count for status, count in webhook_statuses.items() if 200 <= int(status) < 300
    )
    health_2xx = sum(
        count for status, count in health_statuses.items() if 200 <= int(status) < 300
    )
    if webhook_observations:
        first_start = min(item.started_at for item in webhook_observations)
        last_start = max(item.started_at for item in webhook_observations)
        last_finish = max(item.finished_at for item in webhook_observations)
        dispatch_ms = round((last_start - first_start) * 1_000, 3)
        duration_ms = round((last_finish - first_start) * 1_000, 3)
    else:
        dispatch_ms = None
        duration_ms = None
    webhook_redirects = sum(item.redirect_rejected for item in webhook_observations)
    health_redirects = sum(item.redirect_rejected for item in health_observations)
    health_p95 = percentile_95([item.elapsed_ms for item in health_observations])
    required_monitor_seconds = max(MINIMUM_HEALTH_SECONDS, window_seconds)
    expected_health_count = (
        math.floor(required_monitor_seconds / HEALTH_INTERVAL_SECONDS) + 1
    )
    checks = {
        "at_least_200_webhook_2xx": webhook_2xx >= 200,
        "all_requested_webhooks_attempted": len(webhook_observations) == requested_count,
        "burst_dispatched_within_window": (
            dispatch_ms is not None and dispatch_ms <= window_seconds * 1_000
        ),
        "health_all_2xx": health_2xx == len(health_observations),
        "health_p95_below_300_ms": health_p95 is not None and health_p95 < 300.0,
        "health_sample_count_met": len(health_observations) >= expected_health_count,
        "no_redirect_responses": webhook_redirects + health_redirects == 0,
    }
    return {
        "schema_version": 1,
        "configuration": {
            "health_interval_ms": int(HEALTH_INTERVAL_SECONDS * 1_000),
            "health_monitor_seconds": round(health_monitor_seconds, 3),
            "requested_webhooks": requested_count,
            "webhook_concurrency": concurrency,
            "webhook_topic": WEBHOOK_TOPIC,
            "window_seconds": window_seconds,
        },
        "webhooks": {
            "attempted": len(webhook_observations),
            "burst_dispatch_ms": dispatch_ms,
            "burst_duration_ms": duration_ms,
            "errors": webhook_errors,
            "http_2xx": webhook_2xx,
            "http_statuses": webhook_statuses,
            "latency_p95_ms": percentile_95(
                [item.elapsed_ms for item in webhook_observations]
            ),
            "redirects_rejected": webhook_redirects,
        },
        "health": {
            "count": len(health_observations),
            "errors": health_errors,
            "http_2xx": health_2xx,
            "http_statuses": health_statuses,
            "latency_p95_ms": health_p95,
            "redirects_rejected": health_redirects,
        },
        "checks": checks,
        "passed": all(checks.values()),
    }


def markdown_report(result: dict[str, Any]) -> str:
    def compact(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    webhooks = result["webhooks"]
    health = result["health"]
    lines = [
        "# TAURO staging webhook burst",
        "",
        f"Result: **{'PASS' if result['passed'] else 'FAIL'}**",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Webhooks attempted | {webhooks['attempted']} |",
        f"| Webhook HTTP 2xx | {webhooks['http_2xx']} |",
        f"| Burst dispatch (ms) | {webhooks['burst_dispatch_ms']} |",
        f"| Burst duration (ms) | {webhooks['burst_duration_ms']} |",
        f"| Webhook p95 (ms) | {webhooks['latency_p95_ms']} |",
        f"| Health count | {health['count']} |",
        f"| Health p95 (ms) | {health['latency_p95_ms']} |",
        "",
        f"Webhook statuses: `{compact(webhooks['http_statuses'])}`",
        "",
        f"Webhook errors: `{compact(webhooks['errors'])}`",
        "",
        f"Health statuses: `{compact(health['http_statuses'])}`",
        "",
        f"Health errors: `{compact(health['errors'])}`",
        "",
        "## Checks",
        "",
    ]
    lines.extend(
        f"- {'PASS' if passed else 'FAIL'} — `{name}`"
        for name, passed in sorted(result["checks"].items())
    )
    return "\n".join(lines)


def run(args: argparse.Namespace, secret: str) -> dict[str, Any]:
    opener = None
    event_at = utc_timestamp()
    monitor_seconds = max(MINIMUM_HEALTH_SECONDS, args.window_seconds)
    webhook_futures: list[Future[Observation]] = []
    health_futures: list[Future[Observation]] = []

    # Separate pools prevent a saturated webhook burst from starving health
    # probes in the local load generator. Scheduling latency is included in
    # each health measurement so event-loop stalls remain visible.
    with (
        ThreadPoolExecutor(
            max_workers=min(args.concurrency, args.count), thread_name_prefix="webhook"
        ) as webhook_pool,
        ThreadPoolExecutor(max_workers=32, thread_name_prefix="health") as health_pool,
    ):
        health_started_at = time.monotonic()
        health_futures.append(
            health_pool.submit(
                observe,
                opener,
                health_request(args.base_url),
                health_started_at,
            )
        )
        for index in range(args.count):
            request = webhook_request(
                args.base_url, args.shop_domain, secret, index, event_at
            )
            webhook_futures.append(webhook_pool.submit(observe, opener, request))

        tick = 1
        while True:
            scheduled_at = health_started_at + tick * HEALTH_INTERVAL_SECONDS
            delay = scheduled_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            health_futures.append(
                health_pool.submit(
                    observe,
                    opener,
                    health_request(args.base_url),
                    scheduled_at,
                )
            )
            tick += 1
            if tick * HEALTH_INTERVAL_SECONDS > monitor_seconds:
                break
        health_scheduled_until = time.monotonic()

        webhook_observations = [future.result() for future in webhook_futures]
        health_observations = [future.result() for future in health_futures]

    measured_monitor_seconds = max(
        monitor_seconds, health_scheduled_until - health_started_at
    )
    result = summarize(
        webhook_observations,
        health_observations,
        args.count,
        args.concurrency,
        args.window_seconds,
        measured_monitor_seconds,
    )
    result.update(
        {
            "generated_at": utc_timestamp(),
            "environment": "staging",
            "target": {
                "base_origin": args.base_url,
                "shop_domain": args.shop_domain,
            },
            "privacy": {
                "secret_persisted": False,
                "request_bodies_persisted": False,
                "request_headers_persisted": False,
                "response_bodies_persisted": False,
                "redirects_followed": False,
            },
        }
    )
    return result


def write_evidence(path: Path, contents: str) -> None:
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(contents.rstrip() + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    secret = args.webhook_secret or os.getenv(SECRET_ENV_VAR, "")
    if not secret and os.getenv("ENV", "").strip().upper() == "STAGING":
        # Allows a Railway staging instance to run the saved script in-region
        # without copying the secret into a command line or evidence file.
        secret = os.getenv(APP_SECRET_ENV_VAR, "")
    if not secret:
        parser().error(
            f"provide --webhook-secret or set {SECRET_ENV_VAR}; "
            f"{APP_SECRET_ENV_VAR} is accepted only when ENV=STAGING"
        )
    result = run(args, secret)
    json_output = json.dumps(result, indent=2, sort_keys=True)
    markdown_output = markdown_report(result)
    if args.json_file:
        write_evidence(args.json_file, json_output)
    if args.markdown_file:
        write_evidence(args.markdown_file, markdown_output)
    if args.output == "markdown":
        print(markdown_output)
    else:
        print(json_output)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
