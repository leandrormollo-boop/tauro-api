"""Vocabulario único de estados operativos y físicos de un envío.

El estado operativo pertenece a TAURO (pedido, guía y despacho). El estado de
tracking describe lo que informa el courier. Conservamos ambos datos y derivamos
un estado principal para el cliente sin modificar el historial operativo.
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
    "PROCESO_ENTREGA": ("En tránsito", "warn"),
    "RETENIDO": ("Retenido", "error"),
    "ENTREGADO": ("Entregado", "ok"),
}


def estado_principal_envio(estado: Any, tracking_estado: Any = None) -> str:
    """La anulación prevalece; el tracking confirmado supera hitos anteriores."""
    operacion = str(estado or "").strip().upper()
    tracking = str(tracking_estado or "").strip().upper()
    if operacion in {"CANCELADO", "REEMPLAZADO", "ENTREGADO"}:
        return operacion
    if tracking in ESTADOS_TRACKING_UI:
        return tracking
    return operacion


def _presentacion(codigo: Any, mapa: dict, *, vacio: tuple[str, str]) -> dict:
    codigo_normalizado = str(codigo or "").strip().upper()
    etiqueta, clase = mapa.get(codigo_normalizado, vacio)
    return {
        "codigo": codigo_normalizado,
        "label": etiqueta,
        "clase": clase,
    }


def presentar_estados_envio(envio: dict) -> dict:
    """Agrega las presentaciones canónicas sin borrar el dato original."""
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
    envio["estado_cliente_ui"] = _presentacion(
        estado_principal_envio(envio.get("estado"), envio.get("tracking_estado")),
        {**ESTADOS_OPERACION_UI, **ESTADOS_TRACKING_UI,
         "EMITIENDO": ("Generando guía", "warn"),
         "VERIFICAR_COURIER": ("Confirmando emisión", "warn")},
        vacio=("Por confirmar", "muted"),
    )
    return envio
