# ============================================================
# Servicio de cotización
# ============================================================
# Orquesta:
#   1. Lee la ruta de RUTAS_DEFAULT (origen/destino con ZIP)
#   2. Calcula peso volumétrico vs real (usa el mayor)
#   3. Llama a fedex_client.get_rates()
#   4. Aplica MARKUP_% del cliente
#   5. Loguea en COTI
#   6. Devuelve CotizacionOutput con UUID para crear pedido después
# ============================================================

import os
import uuid
from datetime import datetime, timedelta
from typing import Optional

from core.fedex_client import FedExClient
from core.sheets_client import _abrir_sheet
from modelos.cotizacion import (
    CotizacionInput, CotizacionOutput, calcular_peso_volumetrico,
)
from servicios.rutas import get_ruta, pais_a_iso2, ciudad_a_state


COTIZACION_VALIDA_HORAS = 24


def _get_dolar_ars() -> float:
    """Lee el tipo de cambio del CONFIG. Default 1450 si no existe."""
    try:
        sh = _abrir_sheet()
        hoja = sh.worksheet("CONFIG")
        rows = hoja.get_all_records()
        for r in rows:
            if str(r.get("PARAMETRO", "")).strip().upper() == "COTIZACION_DOLAR_ARS":
                return float(r.get("VALOR", 1450))
    except Exception:
        pass
    return 1450.0


def cotizar(
    cliente: str,
    markup_pct: float,
    input_data: CotizacionInput,
) -> CotizacionOutput:
    """Cotiza un envío. Lanza ValueError si la ruta no existe."""

    # 1. Resolver ruta
    ruta = get_ruta(input_data.ruta_id)
    if not ruta:
        raise ValueError(f"Ruta '{input_data.ruta_id}' no existe o está inactiva")

    # 2. Pesos
    peso_volumetrico = calcular_peso_volumetrico(
        input_data.largo_cm, input_data.ancho_cm, input_data.alto_cm
    )
    peso_usado = max(input_data.peso_kg, peso_volumetrico)

    # 3. Llamar FedEx (firma: origen, destino, paquete como dicts)
    # FedEx pide ISO-2 ('AR', 'US') y state code ('B', 'FL').
    fedex = FedExClient()
    rate_resp = fedex.get_rates(
        origen={
            "city": ruta.origen_ciudad,
            "state": ciudad_a_state(ruta.origen_ciudad),
            "postal_code": ruta.origen_zip,
            "country": pais_a_iso2(ruta.origen_pais),
        },
        destino={
            "city": ruta.destino_ciudad,
            "state": ciudad_a_state(ruta.destino_ciudad),
            "postal_code": ruta.destino_zip,
            "country": pais_a_iso2(ruta.destino_pais),
        },
        paquete={
            "peso_kg": peso_usado,
            "largo": input_data.largo_cm,
            "ancho": input_data.ancho_cm,
            "alto": input_data.alto_cm,
        },
    )

    if not rate_resp.get("encontrado"):
        raise ValueError(
            f"FedEx no devolvió tarifa: {rate_resp.get('error', 'sin detalles')}"
        )

    # 4. FedEx Argentina cotiza en ARS — convertir a USD para el negocio
    dolar = _get_dolar_ars()
    costo_ars = float(rate_resp.get("costo_ars", 0))
    costo_fedex_usd = round(costo_ars / dolar, 2) if dolar else 0.0

    # 5. Aplicar markup
    precio_final_usd = round(costo_fedex_usd * (1 + markup_pct / 100), 2)
    precio_final_ars = round(precio_final_usd * dolar, 0)

    # 6. UUID + validez
    coti_id = uuid.uuid4().hex[:16]
    valida_hasta = (datetime.now() + timedelta(hours=COTIZACION_VALIDA_HORAS)).isoformat(timespec="seconds")

    # 7. Loguear en COTI
    try:
        sh = _abrir_sheet()
        hoja = sh.worksheet("COTI")
        hoja.append_row([
            datetime.now().isoformat(timespec="seconds"),
            cliente, ruta.ruta_id, input_data.peso_kg,
            f"{input_data.largo_cm}x{input_data.ancho_cm}x{input_data.alto_cm}",
            peso_usado, costo_fedex_usd, markup_pct,
            precio_final_usd, precio_final_ars,
            ruta.dias_estimados, valida_hasta, "-", "-",
        ], value_input_option="USER_ENTERED")
    except Exception as e:
        print(f"[cotizador] No se pudo loguear COTI: {e}")

    return CotizacionOutput(
        coti_id=coti_id,
        ruta=ruta.ruta_id,
        peso_real_kg=input_data.peso_kg,
        peso_volumetrico_kg=peso_volumetrico,
        peso_usado_kg=peso_usado,
        costo_fedex_usd=costo_fedex_usd,
        markup_pct=markup_pct,
        precio_final_usd=precio_final_usd,
        precio_final_ars=precio_final_ars,
        dias_estimados=ruta.dias_estimados,
        valida_hasta=valida_hasta,
    )
