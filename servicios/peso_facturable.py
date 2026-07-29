# ============================================================
# Peso facturable de un carrito de tienda (Shopify / Tiendanube).
# ============================================================
# Los couriers aéreos NO cobran por lo que pesa el paquete sino por
# lo que ocupa: facturan max(peso real, (L × A × H) / 5000). Una caña
# de pescar de 1,2 kg en una caja de 150×15×15 paga como 6,75 kg.
#
# El problema: Shopify manda el peso de cada item en el carrito, pero
# NO manda dimensiones. Sin ellas el checkout cotizaba por peso real y
# cobraba una fracción de la tarifa verdadera.
#
# De dónde salen las dimensiones: del catálogo del cliente, cruzando el
# SKU que manda la tienda contra `productos.alias_interno` (mismo criterio
# que el tax por SKU: comparación en MAYÚSCULAS de los dos lados, porque
# el alias lo escribe el comerciante a mano).
# ============================================================
from __future__ import annotations

import os

from core.database import get_conn
from modelos.cotizacion import calcular_peso_volumetrico

# Piso de Shopify: nunca cotizar por menos de esto.
PESO_MINIMO_KG = 0.5

# Caja por defecto cuando no se puede deducir ninguna dimensión.
# Es la que usaba el checkout hardcodeada; se mantiene como último recurso.
CAJA_FALLBACK = (30.0, 20.0, 15.0)


def _dimensiones_por_sku(cliente_id: str, skus: list[str]) -> dict:
    """{sku → (largo, ancho, alto) en cm} para los SKU del carrito."""
    if not cliente_id or not skus:
        return {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT alias_interno, largo_cm, ancho_cm, alto_cm
                FROM productos
                WHERE cliente_id = %s AND UPPER(TRIM(alias_interno)) = ANY(%s)
            """, (cliente_id.strip().upper(), skus))
            out = {}
            for r in cur.fetchall():
                l = float(r["largo_cm"] or 0)
                a = float(r["ancho_cm"] or 0)
                h = float(r["alto_cm"] or 0)
                if l > 0 and a > 0 and h > 0:
                    out[r["alias_interno"].strip().upper()] = (l, a, h)
            return out


def peso_facturable(cliente_id: str, items: list[dict]) -> dict:
    """
    Peso por el que hay que cotizar el carrito, y la caja estimada.

    Devuelve:
        peso_kg          → max(real, volumétrico), nunca menos de 0,5
        peso_real_kg     → suma de los gramos que mandó la tienda
        peso_volumetrico → (volumen total) / 5000
        dimensiones      → (largo, ancho, alto) de la caja estimada
        cobertura        → fracción de items con dimensiones conocidas (0..1)

    Cuando NINGÚN item tiene dimensiones cargadas se aplica
    `SHOPIFY_FACTOR_VOLUMEN` (default 1.0 = sólo peso real) para poder
    protegerse sin tener que cargar todo el catálogo de golpe.
    """
    peso_real = sum((it.get("grams") or 0) * (it.get("quantity") or 1)
                    for it in items) / 1000.0

    skus = [str(it.get("sku") or "").strip().upper() for it in items if it.get("sku")]
    try:
        dims = _dimensiones_por_sku(cliente_id, skus) if (skus and cliente_id) else {}
    except Exception as e:
        print(f"[peso_facturable] no pude leer dimensiones por SKU: {e}")
        dims = {}

    # Volumen total: se suman las cajas de cada unidad. Es la aproximación
    # estándar; apilar mejor sólo puede abaratar, nunca encarecer.
    volumen_cm3 = 0.0
    largo_max = 0.0
    con_dims = 0
    total_items = 0
    kg_sin_dims = 0.0
    for it in items:
        # requires_shipping=False es un producto digital (un ebook, una gift
        # card): no viaja, no pesa y no ocupa lugar en la caja.
        if it.get("requires_shipping") is False:
            continue
        cantidad = int(it.get("quantity") or 1)
        total_items += cantidad
        sku = str(it.get("sku") or "").strip().upper()
        if sku and sku in dims:
            l, a, h = dims[sku]
            volumen_cm3 += l * a * h * cantidad
            largo_max = max(largo_max, l, a, h)
            con_dims += cantidad
        else:
            kg_sin_dims += ((it.get("grams") or 0) * cantidad) / 1000.0

    cobertura = (con_dims / total_items) if total_items else 0.0

    # PESO FACTURABLE POR PARTES, no todo-o-nada: lo que tiene medidas aporta
    # su volumétrico real, y lo que no, aporta su peso real × el factor de
    # seguridad. Antes, un solo producto sin medidas anulaba el cálculo del
    # carrito entero y se cotizaba todo por peso real.
    factor = float(os.getenv("SHOPIFY_FACTOR_VOLUMEN", "1.0"))
    peso_volumetrico = round(volumen_cm3 / 5000.0, 2) if volumen_cm3 > 0 else 0.0
    estimado = round(peso_volumetrico + kg_sin_dims * factor, 2)

    if volumen_cm3 > 0:
        # Caja estimada. NO un cubo de la raíz cúbica del volumen: 40 cañas de
        # 100 cm darían un cubo de 117 cm, que FedEx rechaza por largo+contorno.
        # Se respeta la dimensión más larga conocida y se reparte el resto.
        largo = max(largo_max, 1.0)
        seccion = (volumen_cm3 / largo) ** 0.5
        lado = round(max(seccion, 1.0), 1)
        dimensiones = (round(largo, 1), lado, lado)
    else:
        dimensiones = CAJA_FALLBACK

    peso = max(peso_real, estimado)

    # CARRITO SIN PESO: si el comerciante no cargó los gramos en Shopify, el
    # carrito entero cotiza al escalón mínimo — un envío de 5 kg se cobra como
    # uno de 0,5. No se puede adivinar cuánto pesa, así que se avisa fuerte y
    # se deja una perilla para poner un piso más realista mientras se corrige
    # el catálogo de la tienda.
    if peso_real <= 0 and peso_volumetrico <= 0:
        piso = float(os.getenv("SHOPIFY_PESO_DEFAULT_KG", str(PESO_MINIMO_KG)))
        peso = max(peso, piso)
        print(f"[peso_facturable] ATENCIÓN: el carrito llegó SIN PESO "
              f"({total_items} unidad/es, ningún gramo declarado). Se cotiza "
              f"{peso:.2f} kg. El comerciante tiene que cargar el peso de sus "
              f"productos en Shopify o todos sus envíos se venden al mínimo.")

    # El factor ya entró en `estimado`; acá sólo se informa qué pasó, porque
    # cobrar de menos por falta de datos no puede ser silencioso.
    if cobertura < 1.0:
        faltan = total_items - con_dims
        print(f"[peso_facturable] {faltan} de {total_items} unidad/es SIN medidas en el "
              f"catálogo (cobertura {cobertura:.0%}, factor {factor}) → se cotiza "
              f"{peso:.2f} kg. Cargá largo/ancho/alto con el SKU EXACTO de la tienda: "
              f"mientras falten, esos productos se cobran por peso real.")
    if estimado > peso_real:
        print(f"[peso_facturable] volumétrico manda: real {peso_real:.2f} kg → "
              f"facturable {estimado:.2f} kg (cobertura {cobertura:.0%})")

    return {
        "peso_kg": max(round(peso, 2), PESO_MINIMO_KG),
        "peso_real_kg": round(peso_real, 2),
        "peso_volumetrico_kg": peso_volumetrico,
        "dimensiones": dimensiones,
        "cobertura": cobertura,
    }
