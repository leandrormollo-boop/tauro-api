"""La baja comercial conserva el rastreo fisico sin reactivar el envio."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from servicios import tracking_envios, tracking_fedex_portal


RAIZ = Path(__file__).resolve().parents[1]


class _CursorDHL:
    def __init__(self, *, comercial: bool):
        self.comercial = comercial
        self.fila = None
        self.consultas: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, consulta, params=None):
        sql = " ".join(consulta.split())
        self.consultas.append(sql)
        if sql.startswith("SELECT id, tracking"):
            self.fila = (
                {"id": 17, "tracking": "DHL-COMERCIAL"}
                if self.comercial
                else None
            )
        elif sql.startswith("UPDATE solicitudes_guia"):
            # Simula el predicado de ownership/estado que PostgreSQL evalua.
            habilitada = self.comercial and (
                "estado='CANCELADO' AND cancelacion_comercial=TRUE" in sql
            )
            self.fila = {"id": 17} if habilitada else None
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.fila


@contextmanager
def _conexion(cursor):
    yield type("Conexion", (), {"cursor": lambda _self: cursor})()


def _respuesta_dhl_entregada():
    return {
        "encontrado": True,
        "eventos": [{
            "typeCode": "OK",
            "description": "Delivered",
            "dateTime": "2026-10-08T15:00:00-03:00",
        }],
    }


def test_dhl_cancelado_comercial_actualiza_snapshot_sin_reactivar(monkeypatch):
    cursor = _CursorDHL(comercial=True)
    monkeypatch.setattr(tracking_envios, "get_conn", lambda: _conexion(cursor))
    cliente = type("DHL", (), {"track": lambda _self, _tracking: _respuesta_dhl_entregada()})()

    resultado = tracking_envios.actualizar_tracking_dhl(17, cliente_dhl=cliente)

    assert resultado["guardado"] is True
    assert resultado["estado"] == "ENTREGADO"
    update = next(sql for sql in cursor.consultas if sql.startswith("UPDATE"))
    assert "OR (estado='CANCELADO' AND cancelacion_comercial=TRUE)" in update
    # El CASE solo avanza estados operativos vigentes. CANCELADO cae en ELSE.
    assert "estado IN ('GUIA_LISTA', 'DESPACHADO')" in update
    assert "ELSE estado" in update


def test_dhl_cancelado_operativo_sigue_excluido(monkeypatch):
    cursor = _CursorDHL(comercial=False)
    monkeypatch.setattr(tracking_envios, "get_conn", lambda: _conexion(cursor))
    cliente = type("DHL", (), {"track": lambda *_args: (_ for _ in ()).throw(
        AssertionError("no debe consultar el courier")
    )})()

    resultado = tracking_envios.actualizar_tracking_dhl(17, cliente_dhl=cliente)

    assert resultado == {"ok": True, "omitido": True, "solicitud_id": 17}


def test_fedex_guardado_acepta_solo_cancelacion_comercial():
    class Cursor:
        def __init__(self):
            self.sql = ""
            self.fila = None

        def execute(self, consulta, _params=None):
            self.sql = " ".join(consulta.split())
            self.fila = {"id": 22}

        def fetchone(self):
            return self.fila

    cursor = Cursor()
    normalizado = {
        "ok": True,
        "estado": "ENTREGADO",
        "estado_courier": "DL",
        "descripcion": "Delivered",
        "evento_at": None,
    }

    assert tracking_fedex_portal._guardar(
        cursor, 22, "FEDEX-COMERCIAL", normalizado
    ) is True
    assert "OR (estado='CANCELADO' AND cancelacion_comercial=TRUE)" in cursor.sql
    assert "estado IN ('GUIA_LISTA', 'DESPACHADO')" in cursor.sql
    assert "ELSE estado" in cursor.sql


def test_schema_migra_flag_e_indice_parcial_idempotentes():
    schema = (RAIZ / "sql" / "schema.sql").read_text(encoding="utf-8")

    assert "cancelacion_comercial    BOOLEAN NOT NULL DEFAULT FALSE" in schema
    assert (
        "ADD COLUMN IF NOT EXISTS cancelacion_comercial "
        "BOOLEAN NOT NULL DEFAULT FALSE"
    ) in schema
    assert "idx_solicitudes_tracking_comercial_pendiente" in schema
    indice = schema.split(
        "idx_solicitudes_tracking_comercial_pendiente", 1
    )[1].split(";", 1)[0]
    assert "cancelacion_comercial=TRUE" in indice
    assert "estado='CANCELADO'" in indice
    assert "tracking_estado" in indice and "ENTREGADO" in indice


def test_lotes_dhl_y_fedex_incluyen_baja_comercial_sin_abrir_canceladas_comunes():
    dhl = Path(tracking_envios.__file__).read_text(encoding="utf-8")
    fedex = Path(tracking_fedex_portal.__file__).read_text(encoding="utf-8")
    predicado = "OR (estado='CANCELADO' AND cancelacion_comercial=TRUE)"

    # Candidato individual, UPDATE exitoso/fallido y lote diario.
    assert dhl.count(predicado) == 4
    # UPDATE exitoso/fallido y lote diario.
    assert fedex.count(predicado) == 3
    for fuente in (dhl, fedex):
        assert "tracking_estado IS DISTINCT FROM 'ENTREGADO'" in fuente
        assert "estado NOT IN ('CANCELADO', 'ENTREGADO')" in fuente
        assert "estado <> 'REEMPLAZADO'" in fuente
