"""Artículos comerciales de una caja, independientes de sus piezas físicas.

Los importes se calculan con Decimal; se convierten sólo al serializar JSON.
Las solicitudes anteriores sin items_invoice mantienen su contrato escalar.
"""

from decimal import Decimal

from servicios.numeros_humanos import (
    parse_entero_humano, parse_importe_humano, parse_numero_humano,
)
from servicios.paises import normalizar

MAX_ITEMS_INVOICE = 100
CENTAVO = Decimal("0.01")


def normalizar_items_invoice(items, *, peso_total_kg):
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS_INVOICE:
        raise ValueError(f"La invoice debe tener entre 1 y {MAX_ITEMS_INVOICE} ítems.")
    peso_total = parse_numero_humano(peso_total_kg)
    if peso_total is None or peso_total <= 0:
        raise ValueError("Completá el peso de las cajas antes de declarar los ítems.")
    resultado = []
    for indice, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Ítem {indice}: formato inválido.")
        descripcion = str(item.get("descripcion_en") or "").strip()
        if not descripcion or len(descripcion) > 75:
            raise ValueError(f"Ítem {indice}: escribí una descripción de hasta 75 caracteres.")
        unidades = parse_entero_humano(item.get("unidades_aduana"))
        if unidades is None or not 1 <= unidades <= 9999:
            raise ValueError(f"Ítem {indice}: la cantidad debe ser un entero entre 1 y 9999.")
        valor = parse_importe_humano(item.get("valor_unitario_usd"))
        if valor is None or not CENTAVO <= valor <= Decimal("999999999.99"):
            raise ValueError(f"Ítem {indice}: completá un valor unitario válido mayor a cero.")
        if valor != valor.quantize(CENTAVO):
            raise ValueError(f"Ítem {indice}: el valor unitario admite hasta dos decimales.")
        pais_crudo = str(item.get("pais_origen") or "").strip()
        pais = normalizar(pais_crudo) if pais_crudo else ""
        if pais_crudo and not pais:
            raise ValueError(f"Ítem {indice}: elegí un país de fabricación válido.")
        hs = str(item.get("hs_code") or "").strip()
        hs_digitos = hs.replace(".", "")
        if hs and (not hs_digitos.isascii() or not hs_digitos.isdigit()
                   or not 2 <= len(hs_digitos) <= 18):
            raise ValueError(f"Ítem {indice}: revisá el HS code.")
        peso = parse_numero_humano(item.get("peso_neto_kg"))
        if peso is None and len(items) == 1:
            peso = peso_total
        if (peso is None or peso <= 0 or peso > peso_total
                or peso != peso.quantize(Decimal("0.001"))):
            raise ValueError(
                f"Ítem {indice}: indicá el peso neto total de sus unidades en kg "
                "(hasta tres decimales)."
            )
        resultado.append({
            "descripcion_en": descripcion, "unidades_aduana": unidades,
            "valor_unitario_usd": float(valor), "hs_code": hs,
            "pais_origen": pais, "peso_neto_kg": float(peso),
        })
    if sum(Decimal(str(i["peso_neto_kg"])) for i in resultado) > peso_total:
        raise ValueError("La suma del peso neto de los ítems supera el peso de las cajas.")
    return resultado


def total_items_invoice(items):
    return sum(
        (Decimal(str(i["valor_unitario_usd"])) * int(i["unidades_aduana"])
         for i in items), Decimal("0.00"),
    ).quantize(CENTAVO)
