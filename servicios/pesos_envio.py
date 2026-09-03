"""Cálculo presentable de peso real, volumétrico y facturable por caja."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


DIVISOR_VOLUMETRICO = {
    "internacional": Decimal("5000"),
    "nacional": Decimal("4000"),
}


def _decimal(valor) -> Decimal:
    try:
        return Decimal(str(valor or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _redondear(valor: Decimal) -> Decimal:
    return valor.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calcular_pesos_bultos(bultos: list[dict], ambito: str) -> dict:
    """Calcula cada tipo de caja y el total; factura cada caja por separado."""
    ambito = str(ambito or "").strip().lower()
    divisor = DIVISOR_VOLUMETRICO.get(ambito, DIVISOR_VOLUMETRICO["internacional"])
    filas = []
    real_total = Decimal("0")
    volumetrico_total = Decimal("0")
    facturable_total = Decimal("0")

    for indice, bulto in enumerate(bultos or [], start=1):
        cantidad = max(1, int(_decimal(bulto.get("cantidad") or bulto.get("unidades") or 1)))
        real = _decimal(bulto.get("peso_kg") or bulto.get("peso_unitario_kg"))
        largo = _decimal(bulto.get("largo_cm") or bulto.get("largo"))
        ancho = _decimal(bulto.get("ancho_cm") or bulto.get("ancho"))
        alto = _decimal(bulto.get("alto_cm") or bulto.get("alto"))
        volumetrico = (largo * ancho * alto / divisor) if divisor else Decimal("0")
        facturable = max(real, volumetrico)
        real_total += real * cantidad
        volumetrico_total += volumetrico * cantidad
        facturable_total += facturable * cantidad
        filas.append({
            "indice": indice,
            "cantidad": cantidad,
            "real_unitario_kg": _redondear(real),
            "volumetrico_unitario_kg": _redondear(volumetrico),
            "facturable_unitario_kg": _redondear(facturable),
            "cobra_por_volumen": volumetrico > real,
        })

    return {
        "ambito": ambito,
        "divisor": int(divisor),
        "bultos": filas,
        "real_total_kg": _redondear(real_total),
        "volumetrico_total_kg": _redondear(volumetrico_total),
        "facturable_total_kg": _redondear(facturable_total),
        "cobra_por_volumen": facturable_total > real_total,
    }


def pesos_de_solicitud(solicitud: dict) -> dict:
    """Adapta una solicitud moderna o legacy al cálculo compartido."""
    from servicios.couriers_urls import ambito_envio

    bultos = solicitud.get("bultos")
    if not isinstance(bultos, list) or not bultos:
        bultos = [{
            # En el formato legacy peso_kg ya es el total consolidado.
            "cantidad": 1,
            "peso_kg": solicitud.get("peso_kg"),
            "largo_cm": solicitud.get("largo_cm"),
            "ancho_cm": solicitud.get("ancho_cm"),
            "alto_cm": solicitud.get("alto_cm"),
        }]
    return calcular_pesos_bultos(bultos, ambito_envio(solicitud))
