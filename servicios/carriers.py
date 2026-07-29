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


def _precios(resultado: dict, dolar: float, markup_pct: float,
             descuento_pct: float = 0.0) -> dict:
    """
    Convierte el costo crudo del carrier a precio final (ARS + USD).

    Sin descuento: precio = tarifa × (1 + markup web).
    Con descuento (pedido de Leandro para FedEx): el precio final es la tarifa
    del carrier CON el descuento aplicado (sin markup encima), y se devuelve
    también la tarifa de lista para mostrarla tachada en la web.
    """
    # `costo` es lo que el carrier nos cobra a NOSOTROS (tarifa ACCOUNT en
    # FedEx). `costo_lista` es el precio público (LIST) cuando el courier
    # lo informa — es el correcto para mostrar tachado.
    es_usd = resultado.get("moneda", "USD") == "USD"
    costo_real_ars = round(resultado["costo"] * dolar) if es_usd else round(resultado["costo"])

    base = resultado.get("costo_lista") or resultado["costo"]
    if es_usd:
        lista_usd = base
        lista_ars = round(lista_usd * dolar)
    else:
        lista_ars = round(base)
        lista_usd = round(lista_ars / dolar, 2)

    if descuento_pct > 0:
        precio_ars = round(lista_ars * (1 - descuento_pct / 100))

        # ── PISO DE SEGURIDAD ──────────────────────────────────────────
        # OJO con lo que representa `resultado["costo"]`: para FedEx es la
        # tarifa ACCOUNT, o sea LO QUE TAURO LE PAGA al courier, no el
        # precio público. Aplicarle un descuento grande vende POR DEBAJO
        # DEL COSTO — hoy no se nota porque la cuenta está en sandbox y
        # ACCOUNT ≈ LIST, pero el día que entre la tarifa negociada de
        # producción cada envío pasaría a perder plata en silencio.
        #
        # El piso corre SÓLO con cuenta de producción: en sandbox FedEx
        # devuelve tarifas ficticias infladas (USD 415 por 1,2 kg a Miami),
        # así que aplicarlo ahí rompería el precio de vidriera sin proteger
        # nada real. Se enciende solo el día que entre la cuenta productiva
        # — que es exactamente cuando el costo pasa a ser plata de verdad.
        en_produccion = os.getenv("FEDEX_ENVIRONMENT", "sandbox").lower() == "production"
        margen_min = float(os.getenv("WEB_MARGEN_MINIMO_PCT", "15"))
        if en_produccion and margen_min > 0:
            piso_ars = round(costo_real_ars * (1 + margen_min / 100))
            if precio_ars < piso_ars:
                print(f"[carriers] PISO DE SEGURIDAD: el descuento daba ARS {precio_ars} "
                      f"pero el costo es ARS {costo_real_ars} → se cobra ARS {piso_ars} "
                      f"(costo + {margen_min:.0f}%). Revisá WEB_DESC_FEDEX_PCT.")
                precio_ars = piso_ars

        precio_usd = round(precio_ars / dolar, 2)
        return {
            "precio_ars": precio_ars,
            "precio_usd": precio_usd,
            "precio_lista_ars": lista_ars,
            "precio_lista_usd": round(lista_usd, 2),
            "descuento_pct": round(descuento_pct),
        }

    precio_ars = round(lista_ars * (1 + markup_pct / 100))
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

        # FedEx sale con descuento sobre su tarifa de lista (WEB_DESC_FEDEX_PCT,
        # tunable en Railway → Variables sin tocar código; 0 = sin descuento).
        #
        # OJO CON LA CALIBRACIÓN (corregido 28/07): este comentario decía que el
        # 90% dejaba el paquete de 1,2 kg a US en ~USD 40. Está MAL: esos USD 40
        # eran del paquete de 5 kg (el default del widget de la web, no el de
        # referencia). Medido contra producción, con 90% el paquete real de
        # 1,2 kg sale USD 24-27 — entre 30% y 40% POR DEBAJO del objetivo que
        # definió Leandro. No recalibrar este número contra las tarifas de
        # sandbox, que son ficticias: hacerlo recién con la cuenta de producción.
        descuento = float(os.getenv("WEB_DESC_FEDEX_PCT", "90")) if c["id"] == "fedex" else 0.0

        salida.append({
            **base,
            "estado": "cotizado",
            "servicio": servicio,
            "dias_estimados": str(resultado.get("dias_estimados", "3-5")),
            **_precios(resultado, dolar, markup_pct, descuento_pct=descuento),
        })

    return salida
