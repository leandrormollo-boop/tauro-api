"""Vocabulario único de estados operativos y físicos de un envío.

El estado operativo pertenece a TAURO (pedido, guía y despacho). El estado de
tracking describe lo que informa el courier. Se presentan siempre por separado
para no convertir, por ejemplo, una guía lista en un envío ya despachado.
"""

from __future__ import annotations

from typing import Any


ESTADOS_SOLICITUD = [
    "SOLICITADO",
    "EN_PROCESO",
    "VERIFICAR_COURIER",
    "GUIA_LISTA",
    "DESPACHADO",
    "ENTREGADO",
    "REEMPLAZADO",
    "CANCELADO",
]
ESTADO_EMITIENDO = "EMITIENDO"
ESTADOS_VALIDOS = ESTADOS_SOLICITUD + [ESTADO_EMITIENDO]

ESTADOS_OPERACION_UI = {
    "SOLICITADO": ("Solicitado", "warn"),
    "EN_PROCESO": ("Solicitado", "warn"),
    "EMITIENDO": ("Solicitado", "warn"),
    "VERIFICAR_COURIER": ("Solicitado", "warn"),
    "GUIA_LISTA": ("Guía lista", "accent"),
    # El código interno permanece estable para no romper integraciones ni
    # filtros. Para el cliente, el hito correcto es que el courier ya lo
    # recolectó; se distingue en violeta de la entrega final en verde.
    "DESPACHADO": ("Recolectado", "accent"),
    "ENTREGADO": ("Entregado", "ok"),
    "CANCELADO": ("Cancelado", "muted"),
    "REEMPLAZADO": ("Reemplazado", "muted"),
}

ESTADOS_TRACKING_UI = {
    "PROCESO_ENTREGA": ("Proceso de entrega", "warn"),
    "RETENIDO": ("Retenido", "error"),
    "ENTREGADO": ("Entregado", "ok"),
}


def _presentacion(codigo: Any, mapa: dict, *, vacio: tuple[str, str]) -> dict:
    codigo_normalizado = str(codigo or "").strip().upper()
    etiqueta, clase = mapa.get(codigo_normalizado, vacio)
    return {
        "codigo": codigo_normalizado,
        "label": etiqueta,
        "clase": clase,
    }


def presentar_estados_envio(envio: dict) -> dict:
    """Agrega las dos presentaciones canónicas sin borrar el dato original."""
    envio["estado_operacion_ui"] = _presentacion(
        envio.get("estado"),
        ESTADOS_OPERACION_UI,
        vacio=("Solicitado", "warn"),
    )
    envio["estado_tracking_ui"] = _presentacion(
        envio.get("tracking_estado"),
        ESTADOS_TRACKING_UI,
        vacio=("Sin movimientos", "muted"),
    )
    return envio
