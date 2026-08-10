"""La cuenta distingue cargos facturados de los aún no facturados."""
from contextlib import contextmanager
from datetime import date
from unittest import mock

import servicios.cuenta_corriente as cc


@contextmanager
def _conexion_con(filas):
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, query, params=None):
            self.query = query
            self.params = params

        def fetchall(self):
            return filas

    class Conexion:
        def cursor(self):
            return Cursor()

    yield Conexion()


def test_factura_y_pendiente_conservan_su_estado_real():
    filas = [
        {
            "id": 1,
            "fecha": date(2026, 8, 9),
            "nro_fc": "FC 0004-123",
            "monto_ars": 100000,
            "descripcion": "Guía 1",
            "tiene_pdf": True,
        },
        {
            "id": 2,
            "fecha": date(2026, 8, 8),
            "nro_fc": None,
            "monto_ars": 50000,
            "descripcion": "Guía 2",
            "tiene_pdf": False,
        },
    ]

    with mock.patch.object(cc, "get_conn", lambda: _conexion_con(filas)):
        cargos = cc.get_facturas_recientes("TEST", limite=None)

    assert cargos[0]["facturado"] is True
    assert cargos[1]["facturado"] is False
    assert cargos[1]["nro_fc"] == "Guía 2"

    with mock.patch.object(cc, "get_pagos", return_value=[]):
        tipos = [m["tipo"] for m in cc.movimientos("TEST", cargos)]
    assert "FC" in tipos
    assert "PENDIENTE_FACTURA" in tipos


def test_resumen_separa_facturado_pendiente_y_total_cargos():
    resumen = cc.resumir_facturacion([
        {"monto_ars": 100000, "facturado": True},
        {"monto_ars": 50000, "facturado": False},
        {"monto_ars": "250.25", "facturado": False},
    ])

    assert resumen == {
        "facturado_ars": 100000.0,
        "pendiente_ars": 50250.25,
        "total_cargos_ars": 150250.25,
    }
