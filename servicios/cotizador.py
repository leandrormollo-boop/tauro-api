# ============================================================
# Servicio de cotización — PostgreSQL
# ============================================================

import os
import uuid
from datetime import datetime, timedelta, timezone

from core.database import get_conn
from core.fedex_client import FedExClient
from modelos.cotizacion import (
    CotizacionInput, CotizacionOutput, calcular_peso_volumetrico,
)
from servicios.rutas import get_ruta, pais_a_iso2, ciudad_a_state


COTIZACION_VALIDA_HORAS = 24


def _get_dolar_ars() -> float:
    """Lee el tipo de cambio de la tabla config."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT valor FROM config WHERE parametro = 'COTIZACION_DOLAR_ARS'",
                )
                row = cur.fetchone()
        if row:
            return float(row["valor"])
    except Exception as e:
        print(f"[cotizador] Error leyendo tipo de cambio: {e}")
    # fallback al env var
    return float(os.getenv("COTIZACION_DOLAR_ARS", "1450"))


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

    # 3. Llamar FedEx
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

    # 4. Convertir a USD/ARS. FedEx sandbox suele devolver USD; producción ARS.
    dolar = _get_dolar_ars()
    costo = float(rate_resp.get("costo", 0))
    moneda = str(rate_resp.get("moneda", "USD")).upper()
    if moneda == "USD":
        costo_fedex_usd = round(costo, 2)
        costo_ars = round(costo * dolar, 2)
    else:
        costo_ars = costo
        costo_fedex_usd = round(costo_ars / dolar, 2) if dolar else 0.0

    # 5. Aplicar markup
    precio_final_usd = round(costo_fedex_usd * (1 + markup_pct / 100), 2)
    precio_final_ars = round(precio_final_usd * dolar, 0)

    # 6. UUID + validez
    coti_id = uuid.uuid4().hex[:16]
    valida_hasta = (
        datetime.now(tz=timezone.utc) + timedelta(hours=COTIZACION_VALIDA_HORAS)
    ).isoformat(timespec="seconds")

    # 7. Loguear en cotizaciones
    try:
        dimensiones = f"{input_data.largo_cm}x{input_data.ancho_cm}x{input_data.alto_cm}"
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO cotizaciones
                        (coti_id, cliente_id, ruta_id, peso_kg, dimensiones, peso_usado_kg,
                         costo_fedex_usd, markup_pct, precio_final_usd, precio_final_ars,
                         dias_estimados, valida_hasta)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        coti_id, cliente, ruta.ruta_id, input_data.peso_kg, dimensiones,
                        peso_usado, costo_fedex_usd, markup_pct,
                        precio_final_usd, precio_final_ars,
                        ruta.dias_estimados, valida_hasta,
                    ),
                )
    except Exception as e:
        print(f"[cotizador] No se pudo loguear cotización: {e}")

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
