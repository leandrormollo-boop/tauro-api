from contextlib import contextmanager
from pathlib import Path

import pytest

import servicios.cuenta_corriente as cuenta
import servicios.solicitudes_guia as solicitudes


ROOT = Path(__file__).resolve().parents[1]


class _CursorUnaFila:
    def __init__(self, fila):
        self.fila = fila

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, *_args, **_kwargs):
        return None

    def fetchone(self):
        return self.fila


def _conexion_una_fila(fila):
    @contextmanager
    def _conexion():
        class Conn:
            def cursor(self):
                return _CursorUnaFila(dict(fila))

        yield Conn()

    return _conexion


def _guia_elegible(**cambios):
    base = {
        "id": 7,
        "cliente_id": "MELCIOR",
        "estado": "GUIA_LISTA",
        "courier": "DHL",
        "tracking": "OLD123",
        "tracking_estado": None,
        "cargo_pendiente": False,
        "cargo_id": 70,
        "cargo_estado": "ACTIVO",
        "cargo_nro_fc": "",
        "cargo_monto_ars": 14000,
        "reemision_existente_id": None,
        "tiene_ajustes_contables": False,
        "tiene_recoleccion_activa": False,
    }
    base.update(cambios)
    return base


def test_reemision_solo_avanza_si_dhl_no_tiene_movimientos(monkeypatch):
    monkeypatch.setattr(
        solicitudes, "get_conn", _conexion_una_fila(_guia_elegible())
    )

    class DHLNuevo:
        def track(self, _tracking):
            return {"encontrado": False, "error": "DHL 404", "http_status": 404}

    assert solicitudes.validar_reemision_cliente(
        7, "MELCIOR", consultar_courier=True, cliente_dhl=DHLNuevo()
    )["ok"] is True

    class DHLConMovimiento:
        def track(self, _tracking):
            return {
                "encontrado": True,
                "eventos": [{"typeCode": "PU", "description": "Picked up"}],
            }

    bloqueada = solicitudes.validar_reemision_cliente(
        7, "MELCIOR", consultar_courier=True, cliente_dhl=DHLConMovimiento()
    )
    assert bloqueada["ok"] is False
    assert "movimientos" in bloqueada["error"]


@pytest.mark.parametrize(
    "cambio, texto",
    [
        ({"cargo_nro_fc": "FC-1"}, "facturada"),
        ({"tiene_ajustes_contables": True}, "ajustes contables"),
        ({"tiene_recoleccion_activa": True}, "recolección activa"),
        ({"reemision_existente_id": 8}, "ya tiene una corrección"),
    ],
)
def test_reemision_falla_cerrada_ante_riesgo_contable_u_operativo(
    monkeypatch, cambio, texto
):
    monkeypatch.setattr(
        solicitudes, "get_conn", _conexion_una_fila(_guia_elegible(**cambio))
    )
    resultado = solicitudes.validar_reemision_cliente(7, "MELCIOR")
    assert resultado["ok"] is False
    assert texto in resultado["error"]


def test_pdf_anterior_y_pdf_nuevo_pendiente_se_ocultan():
    anterior = solicitudes._sin_label({
        "estado": "REEMPLAZADO", "label_pdf": b"%PDF", "cargo_pendiente": False,
    })
    assert anterior["tiene_label"] is False

    nueva_incompleta = solicitudes._sin_label({
        "estado": "GUIA_LISTA", "label_pdf": b"%PDF",
        "reemplaza_solicitud_id": 7, "reemision_estado": "VERIFICAR_COURIER",
        "cargo_pendiente": True,
    })
    assert nueva_incompleta["tiene_label"] is False


class _CursorReemplazo:
    def __init__(self):
        self.consultas = []
        self.actual = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=None):
        compacto = " ".join(sql.split())
        self.consultas.append((compacto, params))
        if compacto.startswith("SELECT s.cliente_id"):
            self.actual = {
                "cliente_id": "MELCIOR", "precio_tauro_ars": 15000,
                "tracking": "NEW456", "courier": "DHL", "ambito": "INTERNACIONAL",
                "producto_alias": "LANA", "remitente_pais": "AR",
                "destino_pais": "US", "solicitud_anterior_id": 7,
                "tracking_anterior": "OLD123",
            }
        elif compacto.startswith("SELECT s.id"):
            self.actual = {
                "id": 7, "cliente_id": "MELCIOR", "estado": "GUIA_LISTA",
                "tracking": "OLD123", "cargo_id": 70,
                "cargo_estado": "ACTIVO", "cargo_nro_fc": "",
            }
        elif compacto.startswith("INSERT INTO envios"):
            self.actual = {"id": 80}
        elif compacto.startswith("UPDATE envios"):
            self.actual = {"id": 70}
        elif compacto.startswith("UPDATE solicitudes_guia "):
            self.actual = {"id": 7}
        elif compacto.startswith("UPDATE solicitudes_guia_reemisiones"):
            self.actual = {"id": 9}
        else:
            self.actual = None

    def fetchone(self):
        return self.actual


def test_cuenta_corriente_reemplaza_cargo_en_una_transaccion(monkeypatch):
    cursor = _CursorReemplazo()

    @contextmanager
    def conexion():
        class Conn:
            def cursor(self):
                return cursor

        yield Conn()

    auditoria = []
    monkeypatch.setattr(cuenta, "get_conn", conexion)
    monkeypatch.setattr(
        cuenta, "registrar_evento_con_cursor",
        lambda _cur, **datos: auditoria.append(datos),
    )

    assert cuenta.cargar_guia_emitida(8) is True
    sql = "\n".join(q for q, _ in cursor.consultas)
    assert "INSERT INTO envios" in sql
    assert "SET estado='CANCELADO'" in sql
    assert "SET estado='REEMPLAZADO'" in sql
    assert "SET estado='EMITIDA', tracking_nuevo=%s" in sql
    assert auditoria[0]["metadata"]["tracking_anterior"] == "OLD123"
    assert auditoria[0]["metadata"]["tracking_nuevo"] == "NEW456"


def test_contrato_de_schema_y_portal_para_reemplazo():
    schema = (ROOT / "sql/schema.sql").read_text(encoding="utf-8")
    detalle = (ROOT / "templates/portal/envio_detalle.html").read_text(
        encoding="utf-8"
    )
    editor = (ROOT / "templates/portal/envio_nuevo.html").read_text(
        encoding="utf-8"
    )
    servicio = (ROOT / "servicios/solicitudes_guia.py").read_text(
        encoding="utf-8"
    )

    assert "CREATE TABLE IF NOT EXISTS solicitudes_guia_reemisiones" in schema
    assert "solicitud_anterior_id" in schema and "solicitud_nueva_id" in schema
    assert "Corregir datos y reemplazar guía" in detalle
    assert "Guardar cambios y emitir guía nueva" in editor
    assert "estado <> 'REEMPLAZADO'" in servicio
    assert "deuda - reemplazado" in servicio
