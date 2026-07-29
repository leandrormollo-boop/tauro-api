# ============================================================
# Tarifas pre-calculadas para el checkout
# ============================================================
# POR QUÉ EXISTE ESTO:
# Shopify le da ~10 segundos a un CarrierService para responder. Si no
# contesta a tiempo, el comprador NO VE opción de envío y la venta se
# pierde. Cotizar en vivo contra FedEx/UPS/DHL en ese momento es una
# ruleta: la API del courier puede tardar 15s, estar caída o cambiar.
#
# Entonces el checkout NUNCA cotiza en vivo: lee de esta tabla, que un
# job refresca todos los días de madrugada. Respuesta en milisegundos y
# cero dependencia de terceros en el momento más caro del negocio.
#
# Cascada de resiliencia (nunca devolvemos vacío):
#   1. tabla de tarifas (lo normal, instantáneo)
#   2. cotización en vivo con timeout corto (si la tabla no tiene la ruta)
#   3. tarifa de emergencia por fórmula (si todo lo demás falla)
# ============================================================
from __future__ import annotations

import os
from typing import Optional

from core.database import get_conn

# Escalones de peso que se precalculan. Un envío de 1,3 kg toma el de 2 kg
# (siempre se redondea PARA ARRIBA: nunca cobramos de menos).
ESCALONES_KG = [0.5, 1, 2, 3, 5, 10, 15, 20, 30, 45, 68]

# Los destinos que hoy soporta el cotizador público.
PAISES = ["US", "BR", "CL", "UY", "MX", "ES"]

# Fórmula de emergencia: sólo se usa si la tabla está vacía Y el courier
# no contesta. Deliberadamente CARA — es preferible cobrar de más y que
# el comerciante ajuste, a regalar un envío o perder la venta.
EMERGENCIA_BASE_USD = {
    "US": 45, "BR": 40, "CL": 38, "UY": 35, "MX": 50, "ES": 60,
}
EMERGENCIA_POR_KG_USD = 12

_tabla_lista = False


def _ensure_tabla() -> None:
    global _tabla_lista
    if _tabla_lista:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tarifas_cache (
                    id             SERIAL PRIMARY KEY,
                    carrier        TEXT NOT NULL,
                    nombre         TEXT NOT NULL,
                    logo           TEXT,
                    pais           TEXT NOT NULL,
                    peso_hasta_kg  NUMERIC(6,2) NOT NULL,
                    precio_ars     NUMERIC(14,2) NOT NULL,
                    precio_usd     NUMERIC(14,2) NOT NULL,
                    dias_estimados TEXT,
                    servicio       TEXT,
                    actualizado    TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (carrier, pais, peso_hasta_kg)
                );
                CREATE INDEX IF NOT EXISTS ix_tarifas_cache_busqueda
                    ON tarifas_cache (pais, peso_hasta_kg);
                -- `entorno` deja registrado con qué cuenta de FedEx se calculó
                -- cada tarifa. Sin esto, el día que se pase a producción el
                -- checkout seguiría sirviendo precios de sandbox (con el piso
                -- de seguridad apagado) hasta el refresco de las 4am.
                ALTER TABLE tarifas_cache ADD COLUMN IF NOT EXISTS entorno TEXT;
            """)
        conn.commit()
    _tabla_lista = True


def _entorno() -> str:
    return os.getenv("FEDEX_ENVIRONMENT", "sandbox").lower()


def guardar_tarifa(carrier: str, nombre: str, logo: str, pais: str,
                   peso_hasta_kg: float, precio_ars: float, precio_usd: float,
                   dias_estimados: str = "", servicio: str = "") -> None:
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO tarifas_cache
                    (carrier, nombre, logo, pais, peso_hasta_kg, precio_ars,
                     precio_usd, dias_estimados, servicio, actualizado, entorno)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s)
                ON CONFLICT (carrier, pais, peso_hasta_kg) DO UPDATE SET
                    nombre = EXCLUDED.nombre,
                    logo = EXCLUDED.logo,
                    precio_ars = EXCLUDED.precio_ars,
                    precio_usd = EXCLUDED.precio_usd,
                    dias_estimados = EXCLUDED.dias_estimados,
                    servicio = EXCLUDED.servicio,
                    actualizado = now(),
                    entorno = EXCLUDED.entorno
            """, (carrier, nombre, logo, pais.upper(), peso_hasta_kg,
                  precio_ars, precio_usd, dias_estimados, servicio, _entorno()))
        conn.commit()


def escalon_para(peso_kg: float) -> float:
    """El escalón que le toca a un peso: siempre para arriba."""
    for e in ESCALONES_KG:
        if peso_kg <= e:
            return e
    return ESCALONES_KG[-1]


def buscar_tarifas(pais: str, peso_kg: float, incluir_vencidas: bool = False) -> list[dict]:
    """
    Tarifas guardadas para esa ruta. Devuelve [] si no hay nada —
    el que llama decide qué hacer (cotizar en vivo o emergencia).

    Las tarifas VENCEN. Si el job de refresco muere, esta tabla sigue
    contestando con precios de hace semanas y nadie se entera: el envío se
    vende al precio de otro dólar y otra tarifa de FedEx. Por eso, pasado
    `TARIFAS_CACHE_TTL_HORAS`, se devuelve [] para que el flujo intente
    cotizar en vivo.

    `incluir_vencidas=True` las devuelve igual, marcadas: es el último
    recurso antes de la fórmula de emergencia, porque un precio viejo de un
    courier real es mejor que un precio inventado. Nunca se deja al
    comprador sin opción de envío.
    """
    _ensure_tabla()
    pais = (pais or "").upper()
    escalon = escalon_para(peso_kg)
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT carrier, nombre, logo, precio_ars, precio_usd,
                           dias_estimados, servicio, peso_hasta_kg, actualizado,
                           EXTRACT(EPOCH FROM (NOW() - actualizado)) / 3600 AS edad_horas
                    FROM tarifas_cache
                    WHERE pais = %s AND peso_hasta_kg = %s
                      -- una tarifa calculada con la cuenta de sandbox no sirve
                      -- en producción (ni al revés). Las filas viejas sin
                      -- `entorno` se asumen de sandbox, que es lo que son.
                      AND COALESCE(entorno, 'sandbox') = %s
                    ORDER BY precio_ars
                """, (pais, escalon, _entorno()))
                filas = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[tarifas_cache] error leyendo {pais}/{peso_kg}kg: {e}")
        return []

    ttl = float(os.getenv("TARIFAS_CACHE_TTL_HORAS", "48"))
    edad = max((float(f["edad_horas"] or 0) for f in filas), default=0.0)
    if filas and ttl > 0 and edad > ttl:
        if not incluir_vencidas:
            print(f"[tarifas_cache] VENCIDA {pais}/{escalon}kg: {edad:.0f}h de antigüedad "
                  f"(máximo {ttl:.0f}h) → se intenta cotizar en vivo. "
                  f"Si esto se repite, el job de refresco no está corriendo.")
            return []
        print(f"[tarifas_cache] usando tarifa VENCIDA de {edad:.0f}h para {pais}/{escalon}kg "
              f"(no hubo cotización en vivo; mejor esto que la fórmula de emergencia)")

    # Si el envío pesa más que el escalón más alto, prorrateamos el último
    # (mejor un precio alto y coherente que ninguna opción de envío).
    factor = 1.0
    if peso_kg > ESCALONES_KG[-1]:
        factor = peso_kg / ESCALONES_KG[-1]

    salida = []
    for f in filas:
        salida.append({
            "id": f["carrier"],
            "nombre": f["nombre"],
            "logo": f["logo"],
            "servicio": f["servicio"] or "",
            "dias_estimados": f["dias_estimados"] or "3-5",
            "precio_ars": round(float(f["precio_ars"]) * factor, 2),
            "precio_usd": round(float(f["precio_usd"]) * factor, 2),
            "estado": "cotizado",
            "origen_tarifa": "cache_vencida" if (ttl > 0 and edad > ttl) else "cache",
            "edad_horas": round(float(f["edad_horas"] or 0), 1),
        })
    return salida


def tarifa_emergencia(pais: str, peso_kg: float, dolar: float) -> list[dict]:
    """
    Último recurso: una tarifa por fórmula para que el comprador SIEMPRE
    tenga una opción de envío. Cara a propósito.
    """
    base = EMERGENCIA_BASE_USD.get((pais or "").upper(), 55)
    usd = base + EMERGENCIA_POR_KG_USD * max(peso_kg, 0.5)
    return [{
        "id": "tauro",
        "nombre": "TAURO Solutions",
        "logo": "/static/img/logo-mark-white.png",
        "servicio": "Envío internacional",
        "dias_estimados": "5-9",
        "precio_usd": round(usd, 2),
        "precio_ars": round(usd * dolar, 2),
        "estado": "cotizado",
        "origen_tarifa": "emergencia",
    }]


def refrescar_cache(paises: Optional[list[str]] = None) -> dict:
    """
    Job: recorre países × escalones de peso y guarda lo que cotizan los
    couriers. Corre de madrugada, cuando una demora no le cuesta una
    venta a nadie.
    """
    from servicios.carriers import cotizar_carriers

    _ensure_tabla()
    paises = paises or PAISES
    dolar = float(os.getenv("COTIZACION_DOLAR_ARS", "1450"))
    markup = float(os.getenv("WEB_MARKUP_PCT", "20"))

    destinos = {
        "US": {"city": "MIAMI", "state": "FL", "postal_code": "33101"},
        "BR": {"city": "SAO PAULO", "state": "SP", "postal_code": "01310100"},
        "CL": {"city": "SANTIAGO", "state": "RM", "postal_code": "8320000"},
        "UY": {"city": "MONTEVIDEO", "state": "MO", "postal_code": "11000"},
        "MX": {"city": "MEXICO", "state": "DF", "postal_code": "06600"},
        "ES": {"city": "MADRID", "state": "M", "postal_code": "28001"},
    }
    origen = {
        "street": "Av. Corrientes 1234", "city": "BUENOS AIRES",
        "state": "B", "postal_code": "1043", "country": "AR",
    }

    guardadas, fallidas = 0, 0
    for pais in paises:
        info = destinos.get(pais)
        if not info:
            continue
        destino = {**info, "country": pais}
        for peso in ESCALONES_KG:
            # Caja proporcional al peso: aproximación razonable para tarifar.
            lado = 20 if peso <= 2 else (30 if peso <= 10 else 45)
            paquete = {
                "peso_kg": peso, "largo": lado, "ancho": lado, "alto": int(lado * 0.7),
                "valor_declarado_usd": 100, "descripcion_en": "Merchandise",
            }
            try:
                opciones = cotizar_carriers(origen, destino, paquete, dolar, markup)
            except Exception as e:
                print(f"[tarifas_cache] {pais} {peso}kg falló: {e}")
                fallidas += 1
                continue
            for c in opciones:
                if c.get("estado") != "cotizado":
                    continue
                try:
                    guardar_tarifa(
                        carrier=c["id"], nombre=c["nombre"], logo=c.get("logo", ""),
                        pais=pais, peso_hasta_kg=peso,
                        precio_ars=float(c["precio_ars"]), precio_usd=float(c["precio_usd"]),
                        dias_estimados=str(c.get("dias_estimados", "")),
                        servicio=c.get("servicio", ""),
                    )
                    guardadas += 1
                except Exception as e:
                    print(f"[tarifas_cache] no pude guardar {c['id']} {pais} {peso}kg: {e}")
                    fallidas += 1

    print(f"[tarifas_cache] refresco terminado: {guardadas} tarifas guardadas, {fallidas} fallidas")
    return {"guardadas": guardadas, "fallidas": fallidas}


def estado_cache() -> dict:
    """Para el panel del admin: cuántas tarifas hay y de cuándo son."""
    _ensure_tabla()
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) AS n, MAX(actualizado) AS ultima,
                           COUNT(DISTINCT pais) AS paises
                    FROM tarifas_cache
                """)
                r = cur.fetchone()
                return {"tarifas": int(r["n"] or 0), "ultima": r["ultima"],
                        "paises": int(r["paises"] or 0)}
    except Exception as e:
        print(f"[tarifas_cache] estado: {e}")
        return {"tarifas": 0, "ultima": None, "paises": 0}
