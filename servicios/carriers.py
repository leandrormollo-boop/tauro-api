"""
Registro multi-courier de TAURO.

Cada carrier declara sus REQUISITOS (las variables de entorno que necesita para
operar). Mientras falten, el cotizador lo muestra igual —con su logo— en estado
"próximamente". El día que se cargan las credenciales en Railway → Variables, el
carrier se enciende solo y empieza a cotizar en vivo. Cero cambios de código.

FedEx ya opera. UPS y DHL tienen su cliente escrito y listo (core/ups_client.py,
core/dhl_client.py); esperan credenciales.
"""
from __future__ import annotations

import os

from core.fedex_client import FedExClient
from core.ups_client import UPSClient
from core.dhl_client import DHLClient

# Orden = orden de aparición en la web.
CARRIERS = [
    {
        "id": "fedex",
        "nombre": "FedEx",
        "servicio": "International Priority",
        "logo": "/static/img/carriers/fedex.svg",
        "requisitos": ("FEDEX_API_KEY", "FEDEX_SECRET_KEY", "FEDEX_ACCOUNT_NUMBER"),
        "cliente": FedExClient,
    },
    {
        "id": "ups",
        "nombre": "UPS",
        "servicio": "Worldwide Express",
        "logo": "/static/img/carriers/ups.svg",
        "requisitos": ("UPS_CLIENT_ID", "UPS_CLIENT_SECRET", "UPS_ACCOUNT_NUMBER"),
        "cliente": UPSClient,
    },
    {
        "id": "dhl",
        "nombre": "DHL Express",
        "servicio": "Express Worldwide",
        "logo": "/static/img/carriers/dhl.svg",
        "requisitos": ("DHL_API_KEY", "DHL_API_SECRET", "DHL_ACCOUNT_NUMBER"),
        "cliente": DHLClient,
    },
]


def carrier_activo(carrier: dict) -> bool:
    """Un carrier está activo cuando TODAS sus variables de entorno están cargadas."""
    return all(os.getenv(v) for v in carrier["requisitos"])


def _precios(resultado: dict, dolar: float, markup_pct: float) -> dict:
    """Convierte el costo crudo del carrier a precio final (ARS + USD) con markup web."""
    if resultado.get("moneda", "USD") == "USD":
        costo_usd = resultado["costo"]
        costo_ars = round(costo_usd * dolar)
    else:
        costo_ars = resultado["costo"]
        costo_usd = round(costo_ars / dolar, 2)

    precio_ars = round(costo_ars * (1 + markup_pct / 100))
    precio_usd = round(precio_ars / dolar, 2)
    return {"precio_ars": precio_ars, "precio_usd": precio_usd}


def cotizar_carriers(origen: dict, destino: dict, paquete: dict,
                     dolar: float, markup_pct: float) -> list[dict]:
    """
    Cotiza los 3 carriers y devuelve una tarjeta por cada uno.

    estado:
      - "cotizado"     → tarifa real (precio_ars/precio_usd/dias_estimados)
      - "proximamente" → carrier sin credenciales todavía (se muestra con logo)
      - "sin_tarifa"   → activo pero sin cobertura para esa ruta
    """
    salida: list[dict] = []

    for c in CARRIERS:
        base = {
            "id": c["id"],
            "nombre": c["nombre"],
            "logo": c["logo"],
            "servicio": c["servicio"],
        }

        if not carrier_activo(c):
            salida.append({**base, "estado": "proximamente"})
            continue

        try:
            resultado = c["cliente"]().get_rates(origen, destino, paquete)
        except Exception as e:  # una caída de un carrier no tumba a los otros
            print(f"[carriers] {c['id']} get_rates excepción: {e}")
            resultado = {"encontrado": False}

        if not resultado.get("encontrado"):
            salida.append({**base, "estado": "sin_tarifa"})
            continue

        # "INTERNATIONAL_PRIORITY" → "International Priority" (prolijo para la web)
        servicio = (resultado.get("servicio") or c["servicio"]).replace("_", " ").title()

        salida.append({
            **base,
            "estado": "cotizado",
            "servicio": servicio,
            "dias_estimados": str(resultado.get("dias_estimados", "3-5")),
            **_precios(resultado, dolar, markup_pct),
        })

    return salida
