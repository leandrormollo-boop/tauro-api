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


def condiciones_cotizacion(op, *, nacional=False, tax_paga=None):
    """Sólo campos conocidos de esta opción, sin inferir reglas del courier."""
    peso = op.get("peso_usado_kg")
    real = op.get("peso_real_kg")
    volumen = op.get("cobra_por_volumen")
    if volumen is None and peso is not None and real is not None:
        volumen = Decimal(str(peso)) > Decimal(str(real))
    dias = op.get("dias_estimados")
    desconocido = "Se confirma al emitir"
    datos = [("Moneda", "ARS"),
             ("Peso facturable usado", f"{medida_cm(peso)} kg" if peso is not None else desconocido),
             ("Cobro por volumen", ("Sí" if volumen else "No") if volumen is not None else desconocido),
             ("Plazo estimado", f"{dias} días hábiles" if dias and dias != "A confirmar" else desconocido)]
    if not nacional:
        paga = {"CLIENTE": "Los pagás vos; se suman a tu cuenta",
                "DESTINATARIO": "Los paga quien recibe"}.get(tax_paga, desconocido)
        datos.append(("Impuestos y aranceles en destino", paga))
    return datos
