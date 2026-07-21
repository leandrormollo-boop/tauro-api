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
from servicios.pricing import aplicar_pricing, get_pricing_config, parse_monto_ars
from servicios.rutas import get_ruta, pais_a_iso2, ciudad_a_state


COTIZACION_VALIDA_HORAS = 24


def _get_dolar_ars() -> float:
    """
    Lee el tipo de cambio de config, tolerando formato argentino (1.450 -> 1450)
    y con guarda de rango: un dólar ARS realista no baja de 100 ni supera 100.000.
    Si el valor guardado es basura (0, mal tipeado, fuera de rango), usa el fallback
    y deja una alerta en el log en vez de romper todos los precios en silencio.
    """
    fallback = float(os.getenv("COTIZACION_DOLAR_ARS", "1450"))
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT valor FROM config WHERE parametro = 'COTIZACION_DOLAR_ARS'",
                )
                row = cur.fetchone()
        if row and row["valor"] is not None:
            valor = parse_monto_ars(row["valor"])
            if valor is not None and 100 <= valor <= 100_000:
                return valor
            print(
                f"[cotizador] ALERTA: COTIZACION_DOLAR_ARS fuera de rango "
                f"({row['valor']!r} -> {valor}); usando fallback {fallback}"
            )
    except Exception as e:
        print(f"[cotizador] Error leyendo tipo de cambio: {e}")
    return fallback


def cotizar_opciones(
    cliente: str,
    markup_pct: float,
    input_data: CotizacionInput,
) -> list:
    """
    Cotiza TODOS los servicios FedEx disponibles para la ruta en una sola
    llamada (Priority, Economy, etc.) y aplica el pricing del cliente a cada
    uno. Devuelve una lista de dicts ordenada por precio (más barato primero):
      {servicio, servicio_nombre, precio_final_ars, precio_final_usd,
       tarifa_lista_ars, dias_estimados, peso_usado_kg, coti_id, valida_hasta}
    Lanza ValueError si la ruta no existe o FedEx no devuelve tarifas.
    """
    ruta = get_ruta(input_data.ruta_id)
    if not ruta:
        raise ValueError(f"Ruta '{input_data.ruta_id}' no existe o está inactiva")

    peso_volumetrico = calcular_peso_volumetrico(
        input_data.largo_cm, input_data.ancho_cm, input_data.alto_cm
    )
    peso_usado = max(input_data.peso_kg, peso_volumetrico)

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
            "valor_declarado_usd": input_data.valor_declarado_usd or 100,
            "hs_code": input_data.hs_code or "",
            "descripcion_en": input_data.descripcion_en or "Merchandise",
            "unidades": input_data.unidades or 1,
        },
        todos_los_servicios=True,
    )
    if not rate_resp.get("encontrado"):
        raise ValueError(
            f"FedEx no devolvió tarifas: {rate_resp.get('error', 'sin detalles')}"
        )

    dolar = _get_dolar_ars()
    pricing = get_pricing_config(cliente, fallback_pct=markup_pct)
    valida_hasta = (
        datetime.now(tz=timezone.utc) + timedelta(hours=COTIZACION_VALIDA_HORAS)
    ).isoformat(timespec="seconds")

    opciones = []
    for op in rate_resp.get("opciones") or []:
        costo = float(op["costo"])
        moneda = str(op.get("moneda", "USD")).upper()
        if moneda == "USD":
            costo_usd = round(costo, 2)
            costo_ars = round(costo * dolar, 2)
        else:
            costo_ars = costo
            costo_usd = round(costo_ars / dolar, 2) if dolar else 0.0

        precio = aplicar_pricing(
            costo_usd=costo_usd, costo_ars=costo_ars, dolar=dolar, pricing=pricing,
        )

        lista_ars = None
        if op.get("costo_lista"):
            lista = float(op["costo_lista"])
            lista_ars = round(lista * dolar, 2) if moneda == "USD" else round(lista, 2)
            if lista_ars <= precio["precio_final_ars"]:
                lista_ars = None

        coti_id = uuid.uuid4().hex[:16]
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO cotizaciones
                            (coti_id, cliente_id, ruta_id, peso_kg, dimensiones, peso_usado_kg,
                             costo_fedex_usd, markup_pct, markup_tipo, markup_valor,
                             precio_final_usd, precio_final_ars, dias_estimados, valida_hasta)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            coti_id, cliente, ruta.ruta_id, input_data.peso_kg,
                            f"{input_data.largo_cm}x{input_data.ancho_cm}x{input_data.alto_cm}",
                            peso_usado, costo_usd, precio["markup_pct_equivalente"],
                            precio["markup_tipo"], precio["markup_valor"],
                            precio["precio_final_usd"], precio["precio_final_ars"],
                            ruta.dias_estimados, valida_hasta,
                        ),
                    )
        except Exception as e:
            print(f"[cotizador] No se pudo loguear cotización de opción: {e}")

        opciones.append({
            "servicio": op["servicio"],
            "servicio_nombre": op["servicio_nombre"],
            "precio_final_ars": precio["precio_final_ars"],
            "precio_final_usd": precio["precio_final_usd"],
            "tarifa_lista_ars": lista_ars,
            "dias_estimados": op.get("dias_estimados") or ruta.dias_estimados,
            "peso_usado_kg": peso_usado,
            "peso_real_kg": input_data.peso_kg,
            "peso_volumetrico_kg": peso_volumetrico,
            "ruta": ruta.ruta_id,
            "coti_id": coti_id,
            "valida_hasta": valida_hasta,
        })

    opciones.sort(key=lambda o: o["precio_final_ars"])
    return opciones


def cotizar_bultos(
    cliente: str,
    markup_pct: float,
    ruta_id: str,
    bultos: list,
) -> dict:
    """
    Cotiza un envío MULTI-BULTO: N cajas (posiblemente de productos distintos)
    en una sola guía FedEx. Cada bulto:
      {peso_kg (por caja), largo_cm, ancho_cm, alto_cm,
       valor_unitario_usd, unidades (cajas idénticas), hs_code, descripcion_en}

    El peso facturable se calcula POR CAJA (máx entre real y volumétrico de
    cada una) y FedEx tarifa el conjunto. Devuelve un dict estilo
    CotizacionOutput + piezas_total/peso_total_kg. Lanza ValueError si la
    ruta no existe o FedEx no tarifa.
    """
    ruta = get_ruta(ruta_id)
    if not ruta:
        raise ValueError(f"Ruta '{ruta_id}' no existe o está inactiva")

    piezas_fedex = []
    peso_real_total = 0.0
    peso_facturable_total = 0.0
    piezas_total = 0
    for b in bultos:
        unidades = max(int(b.get("unidades", 1) or 1), 1)
        peso_caja = float(b.get("peso_kg", 0.5) or 0.5)
        vol = calcular_peso_volumetrico(
            b.get("largo_cm", 30), b.get("ancho_cm", 20), b.get("alto_cm", 10)
        )
        peso_usado_caja = max(peso_caja, vol)
        piezas_total += unidades
        peso_real_total += peso_caja * unidades
        peso_facturable_total += peso_usado_caja * unidades
        piezas_fedex.append({
            "peso_kg": peso_usado_caja,
            "largo": b.get("largo_cm", 30),
            "ancho": b.get("ancho_cm", 20),
            "alto": b.get("alto_cm", 10),
            "valor_unitario_usd": b.get("valor_unitario_usd", 100),
            "unidades": unidades,
            "hs_code": b.get("hs_code", ""),
            "descripcion_en": b.get("descripcion_en", "Merchandise"),
        })

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
        paquetes=piezas_fedex,
    )
    if not rate_resp.get("encontrado"):
        raise ValueError(
            f"FedEx no devolvió tarifa: {rate_resp.get('error', 'sin detalles')}"
        )

    dolar = _get_dolar_ars()
    costo = float(rate_resp.get("costo", 0))
    moneda = str(rate_resp.get("moneda", "USD")).upper()
    if moneda == "USD":
        costo_fedex_usd = round(costo, 2)
        costo_ars = round(costo * dolar, 2)
    else:
        costo_ars = costo
        costo_fedex_usd = round(costo_ars / dolar, 2) if dolar else 0.0

    tarifa_lista_ars = None
    if rate_resp.get("costo_lista"):
        lista = float(rate_resp["costo_lista"])
        tarifa_lista_ars = round(lista * dolar, 2) if moneda == "USD" else round(lista, 2)

    pricing = get_pricing_config(cliente, fallback_pct=markup_pct)
    precio = aplicar_pricing(
        costo_usd=costo_fedex_usd, costo_ars=costo_ars, dolar=dolar, pricing=pricing,
    )

    coti_id = uuid.uuid4().hex[:16]
    valida_hasta = (
        datetime.now(tz=timezone.utc) + timedelta(hours=COTIZACION_VALIDA_HORAS)
    ).isoformat(timespec="seconds")

    try:
        dimensiones = " + ".join(
            f"{b.get('unidades', 1)}x({b.get('largo_cm')}x{b.get('ancho_cm')}x{b.get('alto_cm')})"
            for b in bultos
        )
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO cotizaciones
                        (coti_id, cliente_id, ruta_id, peso_kg, dimensiones, peso_usado_kg,
                         costo_fedex_usd, markup_pct, markup_tipo, markup_valor,
                         precio_final_usd, precio_final_ars, dias_estimados, valida_hasta)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        coti_id, cliente, ruta.ruta_id, round(peso_real_total, 2),
                        dimensiones[:200], round(peso_facturable_total, 2),
                        costo_fedex_usd, precio["markup_pct_equivalente"],
                        precio["markup_tipo"], precio["markup_valor"],
                        precio["precio_final_usd"], precio["precio_final_ars"],
                        ruta.dias_estimados, valida_hasta,
                    ),
                )
    except Exception as e:
        print(f"[cotizador] No se pudo loguear cotización multi-bulto: {e}")

    if tarifa_lista_ars and tarifa_lista_ars <= precio["precio_final_ars"]:
        tarifa_lista_ars = None

    return {
        "coti_id": coti_id,
        "ruta_id": ruta.ruta_id,
        "piezas_total": piezas_total,
        "peso_total_kg": round(peso_real_total, 2),
        "peso_facturable_kg": round(peso_facturable_total, 2),
        "costo_fedex_usd": costo_fedex_usd,
        "precio_final_usd": precio["precio_final_usd"],
        "precio_final_ars": precio["precio_final_ars"],
        "tarifa_lista_ars": tarifa_lista_ars,
        "markup_tipo": precio["markup_tipo"],
        "markup_valor": precio["markup_valor"],
        "markup_pct": precio["markup_pct_equivalente"],
        "dias_estimados": ruta.dias_estimados,
        "valida_hasta": valida_hasta,
    }


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
            # Valuación aduanera: usa el valor real del producto si vino;
            # si no, cae al default histórico (evita subdeclarar envíos caros).
            "valor_declarado_usd": input_data.valor_declarado_usd or 100,
            "hs_code": input_data.hs_code or "",
            "descripcion_en": input_data.descripcion_en or "Merchandise",
            "unidades": input_data.unidades or 1,
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

    # 4b. Tarifa pública (LIST) de FedEx en ARS — para mostrar el ahorro real.
    # Solo se expone si supera el precio final (si no, no hay ahorro que mostrar).
    tarifa_lista_ars = None
    costo_lista = rate_resp.get("costo_lista")
    if costo_lista:
        lista = float(costo_lista)
        tarifa_lista_ars = round(lista * dolar, 2) if moneda == "USD" else round(lista, 2)

    # 5. Aplicar regla de pricing del cliente.
    pricing = get_pricing_config(cliente, fallback_pct=markup_pct)
    precio = aplicar_pricing(
        costo_usd=costo_fedex_usd,
        costo_ars=costo_ars,
        dolar=dolar,
        pricing=pricing,
    )
    precio_final_usd = precio["precio_final_usd"]
    precio_final_ars = precio["precio_final_ars"]
    markup_pct_equivalente = precio["markup_pct_equivalente"]

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
                         costo_fedex_usd, markup_pct, markup_tipo, markup_valor, precio_final_usd, precio_final_ars,
                         dias_estimados, valida_hasta)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        coti_id, cliente, ruta.ruta_id, input_data.peso_kg, dimensiones,
                        peso_usado, costo_fedex_usd, markup_pct_equivalente,
                        precio["markup_tipo"], precio["markup_valor"],
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
        markup_pct=markup_pct_equivalente,
        markup_tipo=precio["markup_tipo"],
        markup_valor=precio["markup_valor"],
        precio_final_usd=precio_final_usd,
        precio_final_ars=precio_final_ars,
        tarifa_lista_ars=(
            tarifa_lista_ars
            if tarifa_lista_ars and tarifa_lista_ars > precio_final_ars
            else None
        ),
        dias_estimados=ruta.dias_estimados,
        valida_hasta=valida_hasta,
    )
