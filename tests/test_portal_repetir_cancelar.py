from contextlib import contextmanager
from pathlib import Path

import pytest

from endpoints import portal_cliente
from servicios import solicitudes_guia as solicitudes
from servicios import auditoria


RAIZ = Path(__file__).resolve().parents[1]


def _guia_cancelable(**cambios):
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
        "control_existente_id": None,
        "tiene_ajustes_contables": False,
        "tiene_recoleccion_activa": False,
    }
    base.update(cambios)
    return base


@pytest.mark.parametrize(
    "cambio,texto",
    [
        ({"tracking_estado": "PROCESO_ENTREGA"}, "movimientos"),
        ({"cargo_nro_fc": "FC-1"}, "facturada"),
        ({"tiene_ajustes_contables": True}, "ajustes contables"),
        ({"tiene_recoleccion_activa": True}, "recolección activa"),
        ({"control_existente_id": 9}, "anteriormente"),
        ({"courier": "FEDEX"}, "sólo se pueden cancelar"),
    ],
)
def test_cancelacion_falla_cerrada_ante_riesgo(cambio, texto):
    resultado = solicitudes._validar_cancelacion_desde_fila(
        _guia_cancelable(**cambio)
    )
    assert resultado["ok"] is False
    assert texto in resultado["error"]


def test_solicitud_sin_emitir_puede_cancelarse_sin_cargo():
    resultado = solicitudes._validar_cancelacion_desde_fila(
        _guia_cancelable(
            estado="SOLICITADO", courier="DHL", tracking=None,
            cargo_id=None, cargo_estado=None,
        )
    )
    assert resultado == {
        "ok": True, "modo": "SOLICITUD", "tracking_anterior": ""
    }


class _CursorCancelacion:
    def __init__(self):
        self.solicitud = _guia_cancelable()
        self.cargo = {
            "cargo_id": 70, "cargo_estado": "ACTIVO",
            "cargo_nro_fc": "", "cargo_monto_ars": 14000,
        }
        self.one = None
        self.consultas = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=None):
        compacto = " ".join(sql.split())
        self.consultas.append((compacto, params))
        self.one = None
        if compacto.startswith("SELECT s.id"):
            self.one = dict(self.solicitud)
        elif compacto.startswith("SELECT id AS cargo_id"):
            self.one = dict(self.cargo)
        elif compacto.startswith("UPDATE envios"):
            self.cargo["cargo_estado"] = "CANCELADO"
            self.one = {"id": 70}
        elif compacto.startswith("UPDATE solicitudes_guia"):
            self.solicitud["estado"] = "CANCELADO"
            self.one = {"id": 7}
        elif compacto.startswith("INSERT INTO solicitudes_guia_reemisiones"):
            self.one = {"id": 91}

    def fetchone(self):
        return self.one


def test_cancelar_guia_es_atomico_auditable_y_programa_control(monkeypatch):
    cursor = _CursorCancelacion()

    @contextmanager
    def conexion():
        yield type("Conn", (), {"cursor": lambda _self: cursor})()

    eventos = []
    monkeypatch.setattr(solicitudes, "get_conn", conexion)
    monkeypatch.setattr(
        auditoria, "registrar_evento_con_cursor",
        lambda _cur, **datos: eventos.append(datos),
    )

    resultado = solicitudes.cancelar_solicitud_cliente(7, "melcior")

    assert resultado["ok"] is True
    assert resultado["control_id"] == 91
    assert cursor.solicitud["estado"] == "CANCELADO"
    assert cursor.cargo["cargo_estado"] == "CANCELADO"
    sql = "\n".join(consulta for consulta, _ in cursor.consultas)
    assert "SET estado='CANCELADO'" in sql
    assert "'CANCELACION'" in sql
    assert "'EMITIDA', 'VIGILAR'" in sql
    assert len(eventos) == 1
    assert eventos[0]["event"] == "portal.envio_cancelado"
    assert eventos[0]["metadata"]["tracking"] == "OLD123"


def test_repetir_precarga_datos_pero_no_enlaza_ni_conserva_precio():
    form, remitente = portal_cliente._precargar_envio_existente({
        "id": 7,
        "courier": "DHL",
        "remitente_nombre": "Origen",
        "remitente_pais": "AR",
        "dest_nombre": "Destino",
        "destino_pais": "US",
        "precio_tauro_ars": 99999,
        "bultos": [{
            "producto_alias": "Lana", "cantidad": 1,
            "peso_kg": 2, "largo_cm": 10, "ancho_cm": 20, "alto_cm": 30,
            "valor_unitario_usd": 40,
        }],
    })

    assert form["reemplaza_solicitud_id"] == ""
    assert form["precio_cotizado_ars"] == ""
    assert form["intl_courier"] == "dhl"
    assert form["dest_nombre"] == "Destino"
    assert form["bultos"][0]["producto"] == "Lana"
    assert remitente["nombre"] == "Origen"


def test_endpoint_cancelar_consulta_dhl_antes_de_mutar(monkeypatch):
    consultas = []
    mutaciones = []
    monkeypatch.setattr(
        portal_cliente,
        "validar_cancelacion_cliente",
        lambda sid, cliente, **kwargs: (
            consultas.append((sid, cliente, kwargs)) or {"ok": True}
        ),
    )
    monkeypatch.setattr(
        portal_cliente,
        "cancelar_solicitud_cliente",
        lambda sid, cliente: (
            mutaciones.append((sid, cliente)) or {"ok": True}
        ),
    )

    respuesta = portal_cliente.cancelar_envio_portal(7, cliente="MELCIOR")

    assert respuesta.status_code == 303
    assert respuesta.headers["location"].endswith("/7?ok=cancelado")
    assert consultas == [(7, "MELCIOR", {"consultar_courier": True})]
    assert mutaciones == [(7, "MELCIOR")]


def test_contrato_portal_y_schema_para_repetir_y_cancelar():
    detalle = (RAIZ / "templates/portal/envio_detalle.html").read_text()
    listado = (RAIZ / "templates/portal/envios.html").read_text()
    schema = (RAIZ / "sql/schema.sql").read_text()
    monitor = (
        RAIZ / "servicios/monitoreo_guias_reemplazadas.py"
    ).read_text()

    assert "Repetir envío" in detalle
    assert "/cancelar" in detalle
    assert "?repetir={{ s.id }}" in detalle and "?repetir={{ s.id }}" in listado
    assert "ALTER COLUMN solicitud_nueva_id DROP NOT NULL" in schema
    assert "CANCELACION" in schema
    assert "LEFT JOIN solicitudes_guia nueva" in monitor
