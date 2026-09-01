"""Preflight determinístico y sin secretos para la salida a Tiendanube."""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from servicios.carrier_contract import Ambito, Capacidad, carriers_for
from servicios.oca_adapter import registration_status as oca_registration_status
from servicios.tiendanube_labels import labels_execution_ready


_TRUE = {"1", "true", "yes", "si", "sí", "on"}


def _present(name: str, env: dict[str, str]) -> bool:
    return bool(str(env.get(name) or "").strip())


def _enabled(name: str, env: dict[str, str]) -> bool:
    return str(env.get(name) or "").strip().lower() in _TRUE


def _positive_decimal(name: str, env: dict[str, str]) -> bool:
    try:
        value = Decimal(str(env.get(name) or "").strip())
    except (InvalidOperation, ValueError):
        return False
    return value.is_finite() and value > 0


def _holiday_calendar_ready(env: dict[str, str]) -> bool:
    try:
        holidays = {
            date.fromisoformat(item.strip())
            for item in str(env.get("TAURO_NACIONAL_HOLIDAYS") or "").split(",")
            if item.strip()
        }
    except ValueError:
        return False
    return any(item.year == date.today().year for item in holidays)


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
    limit_names = (
        "TAURO_NACIONAL_MAX_PACKAGE_WEIGHT_KG",
        "TAURO_NACIONAL_MAX_LENGTH_CM",
        "TAURO_NACIONAL_MAX_WIDTH_CM",
        "TAURO_NACIONAL_MAX_HEIGHT_CM",
        "TAURO_NACIONAL_MAX_TOTAL_WEIGHT_KG",
        "TAURO_NACIONAL_MAX_TOTAL_VOLUME_M3",
    )
    add(
        "national_shipping_limits",
        all(_positive_decimal(name, env) for name in limit_names),
        "Peso, medidas y volumen máximos deben provenir del contrato nacional.",
    )
    add(
        "national_holiday_calendar",
        _holiday_calendar_ready(env),
        "El calendario de feriados argentinos debe cubrir el año en curso.",
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
    required_capabilities = {
        Capacidad.COTIZAR,
        Capacidad.EMITIR,
        Capacidad.ETIQUETA,
        Capacidad.CANCELAR,
        Capacidad.TRACKING,
    }
    complete_national = [
        spec for spec in operative
        if required_capabilities.issubset(spec.capacidades)
    ]
    add(
        "national_fulfillment_capabilities",
        bool(complete_national),
        "El operador nacional debe cotizar, emitir, etiquetar, cancelar y trackear.",
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
    icon = root / "docs" / "tiendanube" / "assets" / "tauro-nacional-icon-600.png"
    icon_ok = False
    if icon.is_file():
        raw = icon.read_bytes()[:24]
        icon_ok = (
            raw.startswith(b"\x89PNG\r\n\x1a\n")
            and len(raw) == 24
            and int.from_bytes(raw[16:20], "big") == 600
            and int.from_bytes(raw[20:24], "big") == 600
        )
    add(
        "publication_icon",
        icon_ok,
        "El icono de publicación debe ser PNG 600x600.",
    )
    app_source = (root / "servicios" / "tiendanube_app.py")
    portal_source = (root / "endpoints" / "portal_cliente.py")
    labels_source = (root / "servicios" / "tiendanube_labels.py")
    labels_endpoint_source = (root / "endpoints" / "tiendanube_shipping.py")
    source_text = app_source.read_text(encoding="utf-8") if app_source.is_file() else ""
    portal_text = portal_source.read_text(encoding="utf-8") if portal_source.is_file() else ""
    labels_text = (
        labels_source.read_text(encoding="utf-8") if labels_source.is_file() else ""
    )
    labels_endpoint_text = (
        labels_endpoint_source.read_text(encoding="utf-8")
        if labels_endpoint_source.is_file()
        else ""
    )
    add(
        "app_resumed_contract",
        '"app/resumed"' in source_text and "def reactivar(" in source_text,
        "La app debe reanudar API, binding y carrier sin reinstalar.",
    )
    add(
        "fulfillment_orders_contract",
        "/fulfillment-orders" in source_text and '"DISPATCHED"' in source_text,
        "Tracking y despacho deben usar Fulfillment Orders con fallback controlado.",
    )
    add(
        "admin_links_contract",
        '/tienda/tiendanube/pedidos' in portal_text,
        "Los admin links individual y masivo necesitan un destino autenticado.",
    )
    add(
        "labels_callback_contract",
        "tiendanube_label_outbox" in labels_text
        and '"/labels/{callback_token}/generate"' in labels_endpoint_text
        and '"/labels/{callback_token}/cancel"' in labels_endpoint_text,
        "Labels API necesita callbacks autenticados, idempotentes y durables.",
    )
    add(
        "labels_worker",
        labels_execution_ready(),
        "La aceptación de etiquetas requiere un worker nacional homologado.",
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
