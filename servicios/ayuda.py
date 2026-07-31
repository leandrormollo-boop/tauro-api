# ============================================================
# Canal de ayuda de TAURO — un solo lugar para "¿a dónde escribo?"
# ============================================================
# El hallazgo más repetido de la revisión de claridad: el portal decía
# "escribinos" sin decir a dónde. Este módulo define el destino UNA vez.
#
# El WhatsApp se lee de la tabla `config` (parámetro WHATSAPP_TAURO,
# editable en /admin/config sin deploy) con la variable de entorno como
# respaldo. Mientras no esté cargado, el botón de WhatsApp simplemente no
# aparece y queda el mail — nada se rompe por esperar el número.
# ============================================================
from __future__ import annotations

import os
import re
import time

MAIL_TAURO = "taurosolutionsar@gmail.com"

_cache: dict = {"valor": None, "hasta": 0.0}


def _leer_whatsapp_config() -> str:
    from core.database import get_conn
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT valor FROM config WHERE parametro = 'WHATSAPP_TAURO'")
                row = cur.fetchone()
                return str(row["valor"] or "").strip() if row else ""
    except Exception:
        return ""


def whatsapp_tauro() -> str:
    """
    Número normalizado a dígitos (formato wa.me: 549XXXXXXXXXX) o "".
    Cacheado 60s: esto se evalúa en cada render del layout y no amerita
    un viaje a la base por página.
    """
    ahora = time.time()
    if _cache["valor"] is not None and ahora < _cache["hasta"]:
        return _cache["valor"]

    crudo = _leer_whatsapp_config() or os.getenv("WHATSAPP_TAURO", "")
    numero = re.sub(r"\D", "", crudo)
    # Un número argentino para wa.me arranca con 549 y tiene 12-13 dígitos;
    # si cargaron "11 5555-4444" se le antepone el 549 solo.
    if numero and not numero.startswith("549") and len(numero) in (10, 11):
        numero = "549" + numero.lstrip("0")
    if numero and len(numero) < 11:
        numero = ""          # basura tipeada: mejor sin botón que un botón roto

    _cache["valor"] = numero
    _cache["hasta"] = ahora + 60
    return numero


def ayuda_info() -> dict:
    """Para los templates: {whatsapp_url | "", mail, mail_url}."""
    numero = whatsapp_tauro()
    return {
        "whatsapp_url": f"https://wa.me/{numero}" if numero else "",
        "mail": MAIL_TAURO,
        "mail_url": f"mailto:{MAIL_TAURO}",
    }
