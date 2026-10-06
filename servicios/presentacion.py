"""Formato humano compartido por las pantallas server-rendered."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def numero_ars(valor) -> str:
    try:
        numero = Decimal(str(valor or 0))
    except (InvalidOperation, TypeError, ValueError):
        numero = Decimal("0")
    texto = f"{numero:,.2f}"
    return texto.replace(",", "_").replace(".", ",").replace("_", ".")


def dinero_ars(valor) -> str:
    return f"$ {numero_ars(valor)}"


def medida_cm(valor) -> str:
    """Medidas sin ceros decimales superfluos, conservando las fracciones."""
    numero = Decimal(str(valor or 0))
    texto = format(numero, "f")
    if "." in texto:
        texto = texto.rstrip("0").rstrip(".")
    return texto.replace(".", ",")
