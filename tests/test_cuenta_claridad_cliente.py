"""La cuenta del cliente muestra su saldo y sus pagos por envío. Lo que falta
facturar es un dato interno de TAURO y vive solo en el admin."""

from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
CUENTA = (RAIZ / "templates/portal/cuenta.html").read_text(encoding="utf-8")
MOVIMIENTOS = (RAIZ / "templates/portal/cuenta_movimientos.html").read_text(encoding="utf-8")
PAGOS = (RAIZ / "templates/portal/cuenta_pagos.html").read_text(encoding="utf-8")


def test_un_solo_camino_para_informar_un_pago():
    assert "Imputar un pago" not in CUENTA
    assert "Asignar un pago a envíos" not in CUENTA
    assert CUENTA.count('class="btn btn-primary" data-open-payment>Informar un pago</a>') == 1


def test_el_cliente_no_ve_lo_pendiente_de_facturar():
    for texto in ("A facturar", "pendiente_facturacion_ars", "facturado_ars",
                  "todavía no facturamos", "sin facturar"):
        assert texto not in CUENTA, texto
    assert "Pendiente de factura" not in MOVIMIENTOS
    assert '("cargos", "Envíos y facturas")' not in MOVIMIENTOS


def test_proximo_paso_es_pagar_el_saldo_eligiendo_envios():
    assert "Saldo a pagar" in CUENTA
    assert "elegí qué envíos estás pagando" in CUENTA
    assert "Asignar envíos" in PAGOS
