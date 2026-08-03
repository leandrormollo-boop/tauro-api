"""
El despachador de emisión NUNCA puede caer a FedEx por descarte.

Antes era `if ENVIA: envia; else: fedex`, así que cualquier courier que no
fuera ENVIA —DHL, UPS, o el que venga— se emitía con una etiqueta de FedEx:
el cliente pagaba precio DHL, recibía una guía FedEx y el link de tracking
apuntaba al courier equivocado. Nadie se enteraba hasta que el paquete no
aparecía donde el cliente lo buscaba.

Desde el 02/08 DHL TAMBIÉN emite (mismo camino, otro cliente). El test
cambió de significado pero no de espíritu: cada courier tiene que emitirse
POR SU CANAL, y el que no esté implementado tiene que fallar a la vista.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import servicios.solicitudes_guia as sg  # noqa: E402


def _emitir(courier):
    """Devuelve lo que el despachador decidió, sin tocar la base ni el courier."""
    def _fake_internacional(solicitud_id, courier="FEDEX"):
        # `via` guarda el courier REAL con el que se habría emitido: es lo
        # que permite detectar que una solicitud DHL salga como FedEx.
        return {"ok": True, "via": courier.lower()}

    with mock.patch.object(sg, "obtener_solicitud", return_value={"id": 1, "courier": courier}), \
         mock.patch.object(sg, "generar_guia_envia", return_value={"ok": True, "via": "envia"}), \
         mock.patch.object(sg, "generar_guia_internacional", side_effect=_fake_internacional):
        return sg.generar_guia(1)


def test_envia_va_por_envia():
    assert _emitir("ENVIA")["via"] == "envia"


def test_fedex_va_por_fedex():
    assert _emitir("FEDEX")["via"] == "fedex"


def test_sin_courier_va_por_fedex():
    """El default histórico: una solicitud vieja sin courier es FedEx."""
    assert _emitir(None)["via"] == "fedex"


def test_dhl_se_emite_por_dhl():
    """
    EL TEST QUE IMPORTA. Una solicitud DHL tiene que emitirse POR DHL: si
    sale por FedEx, el cliente paga precio DHL, recibe etiqueta FedEx y el
    tracking apunta al courier equivocado.
    """
    r = _emitir("DHL")
    assert r["ok"], "DHL ya emite: no puede fallar"
    assert r["via"] == "dhl", f"se emitió por {r['via']}, no por DHL"


def test_ups_tampoco():
    r = _emitir("UPS")
    assert not r["ok"]
    assert "via" not in r


def test_un_courier_inventado_tampoco():
    """Cualquier valor raro en la columna cae del lado seguro."""
    r = _emitir("ANDREANI")
    assert not r["ok"]
    assert "via" not in r
