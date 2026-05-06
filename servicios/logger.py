# ============================================================
# Servicio de logging a Sheets
# ============================================================
# Loguea todas las requests y errores a las hojas LOG_REQUESTS y LOG_ERRORES.
# Si Sheets falla → fallback a print() local (no rompe el response del endpoint).
# ============================================================

import json
import traceback as tb_mod
from datetime import datetime
from typing import Any, Optional

from core.sheets_client import _abrir_sheet


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_json(obj: Any, max_len: int = 500) -> str:
    """Serializa a JSON, recorta si es muy largo."""
    try:
        s = json.dumps(obj, default=str, ensure_ascii=False)
    except Exception:
        s = str(obj)
    return s[:max_len] + ("..." if len(s) > max_len else "")


def log_request(
    cliente: str,
    endpoint: str,
    method: str,
    input_data: Any,
    output_data: Any,
    status: int,
    duracion_ms: float,
    ip: Optional[str] = None,
):
    """Log de cada request al API. Va a hoja LOG_REQUESTS."""
    try:
        sh = _abrir_sheet()
        hoja = sh.worksheet("LOG_REQUESTS")
        hoja.append_row([
            _now_iso(), cliente or "-", endpoint, method,
            _safe_json(input_data), _safe_json(output_data),
            status, round(duracion_ms, 1), ip or "-",
        ], value_input_option="USER_ENTERED")
    except Exception as e:
        # No queremos romper la response por un fallo de logging
        print(f"[logger] Error logueando request: {e}")
        print(f"  → {endpoint} {method} {status} cliente={cliente}")


def log_error(
    cliente: str,
    endpoint: str,
    input_data: Any,
    exception: Exception,
):
    """Log de error 500 con traceback completo. Va a hoja LOG_ERRORES."""
    try:
        sh = _abrir_sheet()
        hoja = sh.worksheet("LOG_ERRORES")
        hoja.append_row([
            _now_iso(), cliente or "-", endpoint,
            _safe_json(input_data),
            str(exception)[:500],
            tb_mod.format_exc()[:5000],
            "FALSE",  # RESUELTO
            "",       # NOTAS
        ], value_input_option="USER_ENTERED")
    except Exception as e:
        print(f"[logger] Error logueando error: {e}")
        print(f"  → {endpoint} cliente={cliente}")
        print(f"  → Original: {exception}")
        print(tb_mod.format_exc())


def log_health(servicio: str, estado: str, duracion_ms: float, detalle: str = ""):
    """Log de health check. Va a hoja LOG_HEALTH."""
    try:
        sh = _abrir_sheet()
        hoja = sh.worksheet("LOG_HEALTH")
        hoja.append_row([
            _now_iso(), servicio, estado,
            round(duracion_ms, 1), detalle[:300],
        ], value_input_option="USER_ENTERED")
    except Exception as e:
        print(f"[logger] Health log fallo: {e}")


def log_job(job: str, estado: str, duracion_s: float, detalle: str = ""):
    """Log de cron job. Va a hoja LOG_JOBS."""
    try:
        sh = _abrir_sheet()
        hoja = sh.worksheet("LOG_JOBS")
        hoja.append_row([
            _now_iso(), job, estado,
            round(duracion_s, 2), detalle[:500],
        ], value_input_option="USER_ENTERED")
    except Exception as e:
        print(f"[logger] Job log fallo: {e}")
