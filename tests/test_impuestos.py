"""
Quién paga los impuestos de destino: lo elige el cliente, no el código.

Antes de esto, fedex_client mandaba `dutiesPayment: SENDER` con la cuenta de
TAURO FIJA, sin que nadie lo hubiera decidido. O sea que TAURO pagaba los
derechos de TODOS los envíos — incluidas las importaciones de mercadería que
ni siquiera es nuestra. Con EE.UU. pesa: se eliminó la franquicia de USD 800
y hoy toda caja paga arancel.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from servicios.impuestos import (  # noqa: E402
    CLIENTE, DEFAULT, DESTINATARIO, incoterm, normalizar, paga_el_remitente,
)


def test_el_default_no_expone_plata_de_tauro():
    """
    Sin elección explícita, paga quien recibe. Que hacerse cargo sea una
    decisión del cliente, no un descuido nuestro.
    """
    assert DEFAULT == DESTINATARIO
    assert not paga_el_remitente(None)
    assert not paga_el_remitente("")


def test_un_valor_raro_cae_del_lado_seguro():
    """Un dato corrupto no puede terminar cobrándole los impuestos a TAURO."""
    for basura in ("basura", "SENDER", "ddp", None, "", "   "):
        assert normalizar(basura) == DESTINATARIO


def test_el_cliente_puede_hacerse_cargo():
    assert normalizar("CLIENTE") == CLIENTE
    assert normalizar("cliente") == CLIENTE
    assert paga_el_remitente("CLIENTE")


def test_cada_opcion_tiene_su_incoterm():
    """
    DDP = lo prepagamos y se lo facturamos. DAP = paga quien recibe.
    Es lo que después va en la guía y define quién abre la billetera en
    la puerta.
    """
    assert incoterm(CLIENTE) == "DDP"
    assert incoterm(DESTINATARIO) == "DAP"
    assert incoterm(None) == "DAP"


def test_fedex_ya_no_tiene_el_pagador_hardcodeado():
    """
    Test de cableado: el servicio puede estar perfecto y el cliente de FedEx
    seguir mandando SENDER fijo, que es como estaba.
    """
    import inspect

    from core.fedex_client import FedExClient

    fuente = inspect.getsource(FedExClient.create_shipment)
    assert "paga_el_remitente" in fuente, (
        "create_shipment no consulta quién paga: sigue cobrándole a TAURO"
    )
    assert '"paymentType": "RECIPIENT"' in fuente, (
        "falta la rama en la que paga el destinatario"
    )
