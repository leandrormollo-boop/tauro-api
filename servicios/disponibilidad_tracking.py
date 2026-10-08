"""Estado de configuración del rastreo automático, sin consultar couriers."""

from __future__ import annotations

import os
from collections.abc import Mapping


def disponibilidad_tracking_fedex(
    entorno: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Describe si el job FedEx puede correr con la configuración actual.

    Replica las condiciones de elegibilidad de ``tracking_fedex_portal``:
    requiere key y secret, y bloquea sandbox salvo habilitación explícita.
    No valida las credenciales ni hace llamadas de red.
    """
    entorno = os.environ if entorno is None else entorno
    tiene_credenciales = bool(
        entorno.get("FEDEX_API_KEY") and entorno.get("FEDEX_SECRET_KEY")
    )
    ambiente = (entorno.get("FEDEX_ENVIRONMENT", "sandbox") or "sandbox").lower()
    permite_sandbox = entorno.get("FEDEX_TRACKING_PERMITIR_SANDBOX") == "1"

    base: dict[str, object] = {
        "autenticacion_verificada": False,
        "titulo": "Rastreo FedEx sin activar",
    }
    if not tiene_credenciales:
        return {
            **base,
            "disponible": False,
            "motivo": "credenciales_faltantes",
            "detalle": (
                "Faltan los datos de acceso de FedEx. TAURO debe completar la "
                "configuración para actualizar estas guías."
            ),
        }
    if ambiente == "sandbox" and not permite_sandbox:
        return {
            **base,
            "disponible": False,
            "motivo": "sandbox_bloqueado",
            "detalle": (
                "FedEx está en modo de pruebas. TAURO debe configurar el rastreo "
                "real para actualizar estas guías."
            ),
        }

    return {
        **base,
        "disponible": True,
        "motivo": (
            "sandbox_habilitado"
            if ambiente == "sandbox"
            else "produccion_configurada"
        ),
        "detalle": (
            "La configuración permite ejecutar el rastreo. El acceso se confirma "
            "al consultar FedEx."
        ),
    }
