"""Cómo le mostramos el tracking del courier al cliente.

El snapshot persistido conserva el mensaje original del courier. Acá lo
traducimos por código de evento, mostramos fechas en hora argentina y
detectamos envíos que llevan días sin un movimiento nuevo. No inventa estados:
si no reconocemos el código, mostramos el mensaje original tal cual.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo


_AR = ZoneInfo("America/Argentina/Buenos_Aires")

# Códigos de evento de DHL Express (events[].typeCode).
EVENTOS_DHL = {
    "SA": "DHL aceptó el envío",
    "PU": "DHL retiró el envío",
    "PL": "Procesado en un centro de DHL",
    "DF": "Salió de un centro de DHL",
    "AF": "Llegó a un centro de DHL",
    "TR": "En tránsito entre centros de DHL",
    "AR": "Llegó al centro de DHL de destino",
    "CC": "En aduana: DHL actualizó el estado del despacho",
    "RR": "En aduana: DHL actualizó el estado del despacho",
    "CR": "La aduana liberó el envío",
    "CI": "En inspección aduanera",
    "WC": "Salió a reparto",
    "FD": "En camino a la entrega",
    "AD": "Entrega coordinada con el destinatario",
    "OK": "Entregado",
    "PD": "Entrega parcial",
    "OH": "Retenido",
    "HP": "Retenido: hay un pago pendiente en destino",
    "NH": "No había nadie para recibirlo",
    "CA": "El domicilio estaba cerrado",
    "BA": "La dirección está incompleta o es incorrecta",
    "CM": "El destinatario se mudó",
    "RD": "El destinatario rechazó la entrega",
    "RT": "Devuelto al remitente",
    "MS": "DHL lo envió a otro centro por error y lo está corrigiendo",
}

ESTADOS_FINALES_CLIENTE = {"ENTREGADO", "CANCELADO", "REEMPLAZADO"}


def dias_sin_avance_alerta() -> int:
    try:
        valor = int(os.getenv("TRACKING_DIAS_SIN_AVANCE", "4"))
    except ValueError:
        return 4
    return valor if 1 <= valor <= 30 else 4


def mensaje_cliente(courier: Any, codigo: Any, descripcion: Any) -> str:
    """Mensaje en castellano; si no conocemos el código, el original."""
    original = " ".join(str(descripcion or "").split())
    if str(courier or "").strip().upper() == "DHL":
        traducido = EVENTOS_DHL.get(str(codigo or "").strip().upper())
        if traducido:
            return traducido
    return original


def _en_argentina(valor: Any) -> Optional[datetime]:
    if not isinstance(valor, datetime):
        return None
    if valor.tzinfo is None:
        # Las columnas TIMESTAMPTZ llegan con zona; un valor sin zona es UTC.
        valor = valor.replace(tzinfo=timezone.utc)
    return valor.astimezone(_AR)


def fecha_hora_ar(valor: Any) -> str:
    instante = _en_argentina(valor)
    return instante.strftime("%d/%m/%Y %H:%M") if instante else ""


def presentar_tracking(envio: dict, *, ahora: Optional[datetime] = None) -> dict:
    """Agrega ``tracking_ui`` sin modificar los datos originales."""
    ahora = _en_argentina(ahora or datetime.now(timezone.utc))
    descripcion = envio.get("tracking_descripcion") or ""
    mensaje = mensaje_cliente(
        envio.get("courier"), envio.get("tracking_estado_courier"), descripcion,
    )
    original = " ".join(str(descripcion).split())
    ultimo_movimiento = _en_argentina(
        envio.get("tracking_evento_at") or envio.get("tracking_cambio_at")
    )
    estado_cliente = ((envio.get("estado_cliente_ui") or {}).get("codigo")
                      or str(envio.get("estado") or "").upper())
    dias = (ahora.date() - ultimo_movimiento.date()).days if ultimo_movimiento else None
    alerta = bool(
        envio.get("tracking")
        and dias is not None
        and dias >= dias_sin_avance_alerta()
        and estado_cliente not in ESTADOS_FINALES_CLIENTE
        and str(envio.get("tracking_estado") or "").upper() != "RETENIDO"
    )
    envio["tracking_ui"] = {
        "mensaje": mensaje,
        # Mostramos el texto del courier solo si lo traducimos; si no, ya es
        # el mensaje principal y repetirlo sería ruido.
        "original": original if original and original != mensaje else "",
        "evento_label": fecha_hora_ar(envio.get("tracking_evento_at")),
        "consulta_label": fecha_hora_ar(
            envio.get("tracking_actualizado_at") or envio.get("tracking_consultado_at")
        ),
        "dias_sin_movimiento": dias,
        "alerta_sin_avance": alerta,
    }
    return envio
