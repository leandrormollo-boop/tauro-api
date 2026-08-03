from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from starlette.requests import Request

from core.security import (
    _csrf_source_allowed,
    _rate_rule,
    _valid_host_header,
    content_security_policy,
    is_production_env,
    load_security_config,
    normalize_origin,
)
from servicios.api_b2b import hash_api_key
from servicios.auth import (
    MIN_PASSWORD_LENGTH,
    _token_lookup_values,
    _token_storage_value,
    validate_new_password,
)
from servicios.rate_limit import client_ip
from servicios.solicitudes_guia import _clean_url


def make_request(
    *,
    origin: str | None = None,
    referer: str | None = None,
    fetch_site: str | None = None,
    method: str = "POST",
    path: str = "/admin/login",
    host: str = "taurosolutions.ar",
    extra_headers: dict[str, str] | None = None,
) -> Request:
    headers = [(b"host", host.encode("ascii"))]
    for name, value in (
        ("origin", origin),
        ("referer", referer),
        ("sec-fetch-site", fetch_site),
        ("x-forwarded-proto", "https"),
    ):
        if value:
            headers.append((name.encode("ascii"), value.encode("ascii")))
    for name, value in (extra_headers or {}).items():
        headers.append((name.encode("ascii"), value.encode("ascii")))

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": headers,
            "client": ("203.0.113.10", 12345),
            "server": (host, 443),
        }
    )


class SecurityConfigTests(unittest.TestCase):
    def test_railway_always_uses_production_security(self):
        with patch.dict(
            os.environ,
            {"RAILWAY_PROJECT_ID": "project-test"},
            clear=True,
        ):
            self.assertTrue(is_production_env())

    def test_origin_normalization(self):
        self.assertEqual(
            normalize_origin("HTTPS://TauroSolutions.ar/path?q=1"),
            "https://taurosolutions.ar",
        )
        self.assertIsNone(normalize_origin("javascript:alert(1)"))

    def test_host_header_rejects_ambiguous_authority(self):
        self.assertTrue(_valid_host_header("taurosolutions.ar"))
        self.assertTrue(_valid_host_header("taurosolutions.ar:443"))
        self.assertFalse(_valid_host_header("taurosolutions.ar/portal"))
        self.assertFalse(_valid_host_header("tauro@attacker.example"))

    def test_cors_never_uses_wildcard(self):
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "CORS_ALLOWED_ORIGINS": "https://clientes.example",
            },
            clear=False,
        ):
            config = load_security_config()
        self.assertNotIn("*", config.allowed_origins)
        self.assertIn("https://taurosolutions.ar", config.allowed_origins)
        self.assertIn("https://clientes.example", config.allowed_origins)
        self.assertNotIn("*.up.railway.app", config.allowed_hosts)

    def test_csrf_accepts_same_origin(self):
        config = load_security_config()
        request = make_request(
            origin="https://taurosolutions.ar",
            fetch_site="same-origin",
        )
        self.assertTrue(_csrf_source_allowed(request, config))

    def test_csrf_rejects_cross_site_and_missing_source(self):
        config = load_security_config()
        cross_site = make_request(
            origin="https://attacker.example",
            fetch_site="cross-site",
        )
        self.assertFalse(_csrf_source_allowed(cross_site, config))
        self.assertFalse(_csrf_source_allowed(make_request(), config))

    def test_production_csrf_does_not_trust_forged_host(self):
        with patch.dict(
            os.environ,
            {"APP_ENV": "production"},
            clear=False,
        ):
            config = load_security_config()
        request = make_request(
            origin="https://attacker.up.railway.app",
            fetch_site="same-origin",
            host="attacker.up.railway.app",
        )
        self.assertFalse(_csrf_source_allowed(request, config))

    def test_public_and_private_csp_are_scoped(self):
        public = content_security_policy("/web", production=True)
        private = content_security_policy("/portal/home", production=True)
        private_with_nonce = content_security_policy(
            "/portal/home",
            production=True,
            nonce="test-nonce",
        )
        self.assertIn("frame-ancestors 'none'", public)
        self.assertIn("script-src 'self'", public)
        self.assertNotIn("https://unpkg.com", public)
        self.assertNotIn("'unsafe-eval'", public)
        self.assertNotIn("https://unpkg.com", private)
        self.assertNotIn("'unsafe-eval'", private)
        self.assertNotIn(
            "script-src 'self' 'unsafe-inline'",
            private,
        )
        self.assertIn("'nonce-test-nonce'", private_with_nonce)

    def test_portal_get_quote_is_rate_limited(self):
        config = load_security_config()
        request = make_request(method="GET", path="/portal/api/precio")
        self.assertEqual(
            _rate_rule(request, config),
            ("portal_quote", config.portal_quote_limit, 60),
        )

    def test_railway_ip_header_wins_over_spoofable_headers(self):
        request = make_request(
            extra_headers={
                "x-real-ip": "203.0.113.20",
                "cf-connecting-ip": "198.51.100.99",
                "x-forwarded-for": "198.51.100.98",
            }
        )
        self.assertEqual(client_ip(request), "203.0.113.20")


class CredentialStorageTests(unittest.TestCase):
    def test_session_storage_uses_one_way_hash(self):
        raw = "session-value-that-must-not-be-stored"
        stored = _token_storage_value(raw)
        self.assertTrue(stored.startswith("sha256:"))
        self.assertNotIn(raw, stored)
        self.assertEqual(_token_lookup_values(raw), (stored, raw))

    def test_api_key_hash_is_deterministic_and_not_raw(self):
        raw = "tauro_test_api_key_123456789"
        hashed = hash_api_key(raw)
        self.assertEqual(hashed, hash_api_key(raw))
        self.assertNotEqual(raw, hashed)
        self.assertEqual(len(hashed), 64)

    def test_new_password_minimum(self):
        with self.assertRaises(ValueError):
            validate_new_password("x" * (MIN_PASSWORD_LENGTH - 1))
        validate_new_password("x" * MIN_PASSWORD_LENGTH)

    def test_guide_urls_require_https(self):
        self.assertEqual(
            _clean_url("https://labels.example/guide.pdf"),
            "https://labels.example/guide.pdf",
        )
        with self.assertRaises(ValueError):
            _clean_url("javascript:alert(1)")
        with self.assertRaises(ValueError):
            _clean_url("http://labels.example/guide.pdf")


if __name__ == "__main__":
    unittest.main()
