from contextlib import contextmanager
from decimal import Decimal

import pytest

from servicios import cuenta_corriente as cuenta


class _Cursor:
    def __init__(self, fila):
        self.fila = fila
        self.ejecuciones = []

    def execute(self, consulta, parametros=None):
        self.ejecuciones.append((consulta, parametros))

    def fetchone(self):
        return self.fila

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Conexion:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def test_periodo_mensual_cierra_el_mes_sin_depender_del_dia_actual():
    assert cuenta.normalizar_periodo_mensual_cuenta("2026-02") == {
        "clave": "2026-02",
        "desde": "2026-02-01",
        "hasta": "2026-02-28",
        "label": "Febrero 2026",
    }
    assert cuenta.normalizar_periodo_mensual_cuenta("2028-02")["hasta"] == "2028-02-29"
    with pytest.raises(ValueError, match="mes seleccionado"):
        cuenta.normalizar_periodo_mensual_cuenta("02/2026")


def test_resumen_mensual_separa_flete_tax_retorno_y_diferencia(monkeypatch):
    cursor = _Cursor({
        "envios_realizados": 12,
        "fletes_ars": Decimal("1200000.00"),
        "tax_cargos": 1,
        "tax_cargos_ars": Decimal("18000.00"),
        "retornos": 1,
        "retornos_ars": Decimal("42000.00"),
        "otros": 0,
        "otros_ars": Decimal("0"),
        "cargos_ars": Decimal("1260000.00"),
        "tax_ajustes": 1,
        "tax_ajustes_ars": Decimal("2500.00"),
        "diferencias": 2,
        "diferencias_ars": Decimal("7000.00"),
        "ajustes_ars": Decimal("9500.00"),
        "tax_cantidad": 2,
        "tax_ars": Decimal("20500.00"),
        "total_mes_ars": Decimal("1269500.00"),
    })

    @contextmanager
    def conexion():
        yield _Conexion(cursor)

    monkeypatch.setattr(cuenta, "get_conn", conexion)

    resumen = cuenta.resumen_mensual_cuenta("melcior", "2026-05", "internacional")

    assert resumen["label"] == "Mayo 2026"
    assert resumen["envios_realizados"] == 12
    assert resumen["fletes_ars"] == Decimal("1200000.00")
    assert resumen["tax_cantidad"] == 2
    assert resumen["tax_ars"] == Decimal("20500.00")
    assert resumen["retornos_ars"] == Decimal("42000.00")
    assert resumen["diferencias_ars"] == Decimal("7000.00")
    assert resumen["total_mes_ars"] == Decimal("1269500.00")
    consulta, parametros = cursor.ejecuciones[-1]
    assert "tax_cliente_ars" in consulta
    assert "RETORNO" in consulta
    assert "WHEN s.id IS NULL THEN 'OTRO'" in consulta
    assert "COUNT(*) FILTER (WHERE categoria='FLETE') AS envios_realizados" in consulta
    assert parametros == (
        "MELCIOR", "2026-05-01", "2026-05-31", "INTERNACIONAL", "INTERNACIONAL",
        "MELCIOR", "2026-05-01", "2026-05-31", "INTERNACIONAL", "INTERNACIONAL",
    )
