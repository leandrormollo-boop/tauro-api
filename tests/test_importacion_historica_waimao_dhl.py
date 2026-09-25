import json

import pytest

from servicios import importacion_historica_waimao_dhl as servicio


def _manifiesto(*, repetido=False):
    registros = [
        {
            "tracking": "9950745335",
            "factura": "1700A00028150",
            "fecha": "2026-09-11",
            "remitente": "INTERFASHION",
            "origen_pais": "BD",
            "destinatario": "LUCILA ELLI",
            "destino_pais": "AR",
            "peso_kg": "12.5",
            "fuente": "FC DHL 1700A00028150",
        }
    ]
    if repetido:
        registros.append(dict(registros[0]))
    return {
        "schema_version": 1,
        "cliente_id": "WAIMAO",
        "courier": "DHL",
        "source_sha256": "a" * 64,
        "registros": registros,
    }


def test_manifiesto_normaliza_y_rechaza_trackings_duplicados():
    lote = servicio.leer_manifiesto(json.dumps(_manifiesto()).encode())
    assert lote["registros"][0]["tracking"] == "9950745335"
    assert str(lote["registros"][0]["peso_kg"]) == "12.500"

    with pytest.raises(servicio.ImportacionHistoricaWaimaoError, match="duplicado"):
        servicio.leer_manifiesto(json.dumps(_manifiesto(repetido=True)).encode())


class _Cursor:
    def __init__(self):
        self.queries = []
        self._one = None
        self._all = []
        self._next_id = 700

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=()):
        compacto = " ".join(sql.split())
        assert compacto.count("%s") == len(params), (compacto, params)
        self.queries.append((compacto, params))
        self._one = None
        self._all = []
        if "SELECT cliente_id FROM clientes" in compacto:
            self._one = {"cliente_id": "WAIMAO"}
        elif "FROM facturas_courier f JOIN facturas_courier_items" in compacto:
            self._all = [{"id": 21}]
        elif "FROM solicitudes_guia" in compacto and "SELECT id, cliente_id" in compacto:
            self._all = []
        elif compacto.startswith("INSERT INTO solicitudes_guia"):
            self._next_id += 1
            self._one = {"id": self._next_id}
        elif "COUNT(DISTINCT m.item_id) AS lineas" in compacto:
            self._one = {"lineas": 4}

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class _Conn:
    def __init__(self):
        self.cur = _Cursor()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def cursor(self):
        return self.cur


def test_importacion_crea_solicitud_sin_movimiento_financiero(monkeypatch):
    conexion = _Conn()
    monkeypatch.setattr(servicio, "get_conn", lambda: conexion)
    monkeypatch.setattr(
        servicio, "matchear_items_exactos",
        lambda factura_id, actor, _conn: {"propuestos": 1, "sin_match": 0},
    )
    eventos = []
    monkeypatch.setattr(
        servicio, "registrar_evento_con_cursor",
        lambda *args, **kwargs: eventos.append(kwargs),
    )

    lote = servicio.leer_manifiesto(json.dumps(_manifiesto()).encode())
    resultado = servicio.importar_manifiesto(lote)

    assert resultado == {
        "envios_manifestados": 1,
        "envios_creados": 1,
        "envios_existentes": 0,
        "facturas_reprocesadas": 1,
        "guias_con_match": 1,
        "propuestas_nuevas": 1,
        "sin_cargos_cliente": 1,
    }
    sql = "\n".join(consulta for consulta, _ in conexion.cur.queries)
    assert "INSERT INTO solicitudes_guia" in sql
    assert "INSERT INTO envios" not in sql
    assert "INSERT INTO pagos" not in sql
    assert "INSERT INTO ajustes_cliente" not in sql
    assert "FALSE, FALSE" in sql
    assert eventos[0]["metadata"]["controls"][-1] == "match_exacto_verificado"
