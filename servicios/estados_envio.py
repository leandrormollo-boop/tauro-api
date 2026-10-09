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
    # El código interno conserva el hito de despacho; la etiqueta del portal
    # coincide con la de tracking mientras no haya entrega confirmada.
    "DESPACHADO": ("En tránsito", "warn"),
    "ENTREGADO": ("Entregado", "ok"),
    "CANCELADO": ("Cancelado", "muted"),
    "REEMPLAZADO": ("Reemplazado", "muted"),
}

ESTADOS_TRACKING_UI = {
    "PROCESO_ENTREGA": ("En tránsito", "warn"),
    "RETENIDO": ("Retenido", "error"),
    "ENTREGADO": ("Entregado", "ok"),
}

HITOS_ENVIO_UI = tuple(
    {"codigo": codigo, "label": ESTADOS_OPERACION_UI[codigo][0]}
    for codigo in ("SOLICITADO", "GUIA_LISTA", "DESPACHADO", "ENTREGADO")
)


def es_envio_realizado(envio: dict) -> bool:
    """Procedencia explícita del alta Admin; nunca se deduce por antigüedad."""
    return str(envio.get("coti_id") or "").strip().startswith("EXT-")


def estado_operativo_presentado(envio: dict) -> str:
    """Presenta el despacho declarado por Admin sin reescribir su historial."""
    estado = str(envio.get("estado") or "").strip().upper()
    if (estado == "GUIA_LISTA" and es_envio_realizado(envio)
            and str(envio.get("tracking") or "").strip()):
        return "DESPACHADO"
    return estado


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
    operacion = estado_operativo_presentado(envio)
    envio["estado_operacion_ui"] = _presentacion(
        operacion,
        ESTADOS_OPERACION_UI,
        vacio=("Solicitado", "warn"),
    )
    envio["estado_tracking_ui"] = _presentacion(
        envio.get("tracking_estado"),
        ESTADOS_TRACKING_UI,
        vacio=("Sin movimientos", "muted"),
    )
    envio["estado_cliente_ui"] = _presentacion(
        estado_principal_envio(operacion, envio.get("tracking_estado")),
        {**ESTADOS_OPERACION_UI, **ESTADOS_TRACKING_UI,
         "EMITIENDO": ("Generando guía", "warn"),
         "VERIFICAR_COURIER": ("Confirmando emisión", "warn")},
        vacio=("Por confirmar", "muted"),
    )
    envio["seguimiento_pendiente_tauro"] = (
        es_envio_realizado(envio)
        and operacion == "DESPACHADO"
        and not envio.get("tracking_estado")
    )
    if envio["seguimiento_pendiente_tauro"]:
        envio["estado_cliente_ui"]["label"] = "Despachado"
        envio["estado_operacion_ui"]["label"] = "Despachado"
    # Este aviso de DHL cierra la disponibilidad del rastreo, no acredita
    # entrega. Se conserva el último estado conocido y el mensaje original.
    envio["seguimiento_sin_actualizaciones"] = (
        "final status for this shipment tracking number"
        in str(envio.get("tracking_descripcion") or "").casefold()
        and envio["estado_cliente_ui"]["codigo"] not in {"ENTREGADO", "CANCELADO", "REEMPLAZADO"}
    )
    return envio
