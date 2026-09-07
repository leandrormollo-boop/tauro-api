"""Presentación pública de diferencias, con una allowlist sin costos internos."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping


MOTIVOS_PESO = frozenset({"PESO_REAL", "PESO_VOLUMETRICO", "MIXTO"})
_MOTIVOS = {
    "PESO_REAL": "Peso real",
    "PESO_VOLUMETRICO": "Peso volumétrico",
    "MIXTO": "Peso y recargos del courier",
    "IMPUESTOS": "Impuestos del courier",
    "RECARGO": "Recargo del courier",
    "DESCUENTO": "Descuento del courier",
    "OTRO": "Diferencia del courier",
}


def _decimal_opcional(valor: Any) -> Decimal | None:
    if valor in (None, ""):
        return None
    try:
        numero = Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not numero.is_finite():
        return None
    return numero.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def _dinero_opcional(valor: Any) -> Decimal | None:
    if valor in (None, ""):
        return None
    try:
        numero = Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not numero.is_finite():
        return None
    return numero.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def presentar_diferencia(datos: Mapping[str, Any] | None) -> dict[str, Any]:
    """Devuelve sólo campos comerciales permitidos para portal/cliente."""
    fuente = datos or {}
    motivo = str(
        fuente.get("motivo") or fuente.get("motivo_diferencia") or "OTRO"
    ).strip().upper()
    inicial = _decimal_opcional(
        fuente.get("peso_inicial_kg", fuente.get("peso_cotizado_kg"))
    )
    facturado = _decimal_opcional(
        fuente.get(
            "peso_facturado_kg", fuente.get("peso_final_facturado_kg")
        )
    )
    concepto = str(
        fuente.get("concepto_courier", fuente.get("conceptos_courier")) or ""
    ).strip()[:500]
    valor_inicial = _dinero_opcional(fuente.get("valor_inicial_ars"))
    diferencia = _dinero_opcional(fuente.get("diferencia_ars"))
    valor_final = _dinero_opcional(fuente.get("valor_final_ars"))
    montos_completos = all(
        valor is not None for valor in (valor_inicial, diferencia, valor_final)
    ) and valor_inicial + diferencia == valor_final
    es_peso = motivo in MOTIVOS_PESO and inicial is not None and facturado is not None
    return {
        "montos_completos": montos_completos,
        "valor_inicial_ars": valor_inicial,
        "diferencia_ars": diferencia,
        "valor_final_ars": valor_final,
        "es_credito": diferencia is not None and diferencia < 0,
        "es_peso": es_peso,
        "peso_inicial_kg": inicial,
        "peso_facturado_kg": facturado,
        "diferencia_peso_kg": (
            (facturado - inicial).quantize(Decimal("0.001"))
            if es_peso else None
        ),
        "base_peso": str(fuente.get("base_peso") or fuente.get("peso_base_facturado") or "").strip().upper(),
        "motivo": motivo,
        "motivo_legible": _MOTIVOS.get(motivo, motivo.replace("_", " ").title()),
        "concepto_courier": concepto,
        "leyenda": "TAURO traslada la diferencia del courier sin agregar margen.",
    }
