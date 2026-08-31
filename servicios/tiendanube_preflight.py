"""Preflight determinístico y sin secretos para la salida a Tiendanube."""
from __future__ import annotations

import os
from pathlib import Path

from servicios.carrier_contract import Ambito, carriers_for
from servicios.oca_adapter import registration_status as oca_registration_status


_TRUE = {"1", "true", "yes", "si", "sí", "on"}


def _present(name: str, env: dict[str, str]) -> bool:
    return bool(str(env.get(name) or "").strip())


def _enabled(name: str, env: dict[str, str]) -> bool:
    return str(env.get(name) or "").strip().lower() in _TRUE


def evaluate_preflight(
    env: dict[str, str] | None = None,
    *,
    repository_root: Path | None = None,
) -> dict:
    env = dict(os.environ if env is None else env)
    root = repository_root or Path(__file__).resolve().parent.parent
    checks: list[dict] = []

    def add(code: str, ok: bool, detail: str, *, release=True):
        checks.append(
            {"code": code, "ok": bool(ok), "detail": detail, "release": release}
        )

    base_url = str(env.get("BASE_URL") or "").strip().rstrip("/")
    add("base_url_https", base_url.startswith("https://"), "BASE_URL debe usar HTTPS.")
    add(
        "oauth_credentials",
        _present("TIENDANUBE_CLIENT_ID", env)
        and _present("TIENDANUBE_CLIENT_SECRET", env),
        "Client ID y Client Secret deben existir en el entorno.",
    )
    add(
        "token_encryption_key",
        _present("TIENDANUBE_TOKEN_ENCRYPTION_KEY", env),
        "Los access tokens necesitan una clave de cifrado exclusiva y rotatable.",
    )
    add(
        "shipping_access",
        _enabled("TIENDANUBE_SHIPPING_ACCESS_APPROVED", env),
        "El Platform Team debe habilitar Shipping API.",
    )
    add(
        "demo_store",
        _present("TIENDANUBE_DEMO_STORE_ID", env),
        "Debe existir una tienda demo argentina identificada.",
    )

    national_specs = carriers_for(Ambito.NACIONAL)
    operative = [spec for spec in national_specs if spec.implementacion == "operativa"]
    add(
        "national_adapter_code",
        bool(operative),
        "Al menos un adapter nacional debe estar marcado operativo después del UAT.",
    )
    credentials_ready = any(
        all(_present(name, env) for name in spec.variables_requeridas)
        for spec in operative
    )
    add(
        "national_adapter_credentials",
        credentials_ready,
        "El adapter operativo necesita todas sus credenciales contractuales.",
    )
    oca_status = oca_registration_status(env)
    add(
        "oca_adapter_enabled",
        oca_status["enabled"],
        "OCA_ADAPTER_ENABLED se activa sólo para el UAT controlado.",
    )
    add(
        "oca_adapter_configuration",
        oca_status["configuration_valid"],
        "CUIT, cuenta, operativa, credenciales, modalidades y timeouts deben ser válidos.",
    )
    add(
        "oca_uat",
        oca_status["uat_approved"],
        "La cotización OCA necesita evidencia de UAT aprobada.",
    )
    add(
        "oca_environment_gate",
        oca_status["environment_approved"],
        "Producción OCA requiere una aprobación independiente y explícita.",
    )

    bundle = root / "tiendanube_nube_app" / "dist" / "main.min.js"
    add(
        "nube_sdk_bundle",
        bundle.is_file() and bundle.stat().st_size > 0,
        "El bundle NubeSDK debe estar compilado.",
    )
    add(
        "rates_uat",
        _enabled("TAURO_NACIONAL_RATES_READY", env),
        "Tarifas, SLA y carrito mixto requieren UAT aprobado.",
    )
    add(
        "shipping_flag",
        _enabled("TIENDANUBE_SHIPPING_ENABLED", env),
        "El flag final se activa únicamente después del UAT.",
    )
    add(
        "homologation",
        _enabled("TIENDANUBE_HOMOLOGATION_APPROVED", env),
        "La homologación síncrona debe estar aprobada.",
    )

    blockers = [check for check in checks if check["release"] and not check["ok"]]
    return {
        "ready_for_release": not blockers,
        "checks": checks,
        "blockers": [check["code"] for check in blockers],
    }
