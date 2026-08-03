"""
Quién paga los impuestos de destino.

Regla de Leandro (01/08/2026): lo elige EL CLIENTE, y lo puede dejar
predefinido en su cuenta. Prete Rosso los paga siempre, así que no quiere
tildarlo envío por envío; otro cliente puede querer decidirlo cada vez.

  DESTINATARIO → los abona quien recibe el paquete (incoterm DAP)
  CLIENTE      → los prepaga TAURO y se los factura al cliente (incoterm DDP)

Por qué importa: hasta hoy el código mandaba `dutiesPayment: SENDER` con la
cuenta de TAURO fija, sin que nadie lo hubiera decidido. O sea que TAURO
pagaba los derechos de TODOS los envíos, incluidas las importaciones de
mercadería que ni siquiera es nuestra. Con Estados Unidos eso pesa: se
eliminó la franquicia de USD 800 y hoy toda caja paga arancel.
"""
from __future__ import annotations

DESTINATARIO = "DESTINATARIO"
CLIENTE = "CLIENTE"

OPCIONES = {
    DESTINATARIO: "Los paga quien recibe",
    CLIENTE: "Los pago yo (se suman a mi cuenta)",
}

# Default DESTINATARIO a propósito: es la opción que NO expone plata de
# TAURO. Que el cliente tenga que elegir explícitamente hacerse cargo.
DEFAULT = DESTINATARIO


def normalizar(valor: str, default: str = DEFAULT) -> str:
    """Un valor raro no puede terminar cobrándole los impuestos a nadie."""
    v = (valor or "").strip().upper()
    return v if v in OPCIONES else default


def incoterm(tax_paga: str) -> str:
    """
    El incoterm que le corresponde a cada opción.

    DDP = Delivered Duty Paid: el remitente (nosotros, con la cuenta del
    cliente detrás) prepaga derechos e impuestos.
    DAP = Delivered At Place: llegan hasta la puerta y el destinatario paga
    los impuestos para retirarlos.
    """
    return "DDP" if normalizar(tax_paga) == CLIENTE else "DAP"


def paga_el_remitente(tax_paga: str) -> bool:
    """Para el bloque dutiesPayment de FedEx y valueAddedServices de DHL."""
    return normalizar(tax_paga) == CLIENTE
