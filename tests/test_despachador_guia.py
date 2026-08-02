"""
El despachador de emisión NUNCA puede caer a FedEx por descarte.

Antes era `if ENVIA: envia; else: fedex`, así que cualquier courier que no
fuera ENVIA —DHL, UPS, o el que venga— se emitía con una etiqueta de FedEx:
el cliente pagaba precio DHL, recibía una guía FedEx y el link de tracking
apuntaba al courier equivocado. Nadie se enteraba hasta que el paquete no
aparecía donde el cliente lo buscaba.

Hoy DHL cotiza pero no emite. Este test es el que impide que, al agregar DHL
al portal, la emisión se vaya en silencio por el canal que no es.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import servicios.solicitudes_guia as sg  # noqa: E402


def _emitir(courier):
    with mock.patch.object(sg, "obtener_solicitud", return_value={"id": 1, "courier": courier}), \
         mock.patch.object(sg, "generar_guia_envia", return_value={"ok": True, "via": "envia"}), \
         mock.patch.object(sg, "generar_guia_fedex", return_value={"ok": True, "via": "fedex"}):
        return sg.generar_guia(1)


def test_envia_va_por_envia():
    assert _emitir("ENVIA")["via"] == "envia"


def test_fedex_va_por_fedex():
    assert _emitir("FEDEX")["via"] == "fedex"


def test_sin_courier_va_por_fedex():
    """El default histórico: una solicitud vieja sin courier es FedEx."""
    assert _emitir(None)["via"] == "fedex"


def test_dhl_no_se_emite_por_fedex():
    """
    EL TEST QUE IMPORTA. Mientras create_shipment de DHL no exista, una
    solicitud DHL tiene que fallar a la vista — no salir con etiqueta FedEx.
    """
    r = _emitir("DHL")
    assert not r["ok"], "una solicitud DHL NO puede emitirse todavía"
    assert "via" not in r, "se emitió por otro courier"
    assert "DHL" in r["error"]


def test_ups_tampoco():
    r = _emitir("UPS")
    assert not r["ok"]
    assert "via" not in r


def test_un_courier_inventado_tampoco():
    """Cualquier valor raro en la columna cae del lado seguro."""
    r = _emitir("ANDREANI")
    assert not r["ok"]
    assert "via" not in r
