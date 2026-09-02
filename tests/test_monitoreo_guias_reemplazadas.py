"""Riesgo operativo de una etiqueta DHL descartada por reemisión."""

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from starlette.requests import Request

from servicios import monitoreo_guias_reemplazadas as monitoreo


RAIZ = Path(__file__).resolve().parents[1]


def test_404_y_respuesta_sin_eventos_significan_sin_movimiento():
    no_encontrada = monitoreo.normalizar_consulta_reemplazada({
        "encontrado": False,
        "error": "DHL 404",
        "http_status": 404,
    })
    sin_eventos = monitoreo.normalizar_consulta_reemplazada({
        "encontrado": True,
        "eventos": [],
    })

    assert no_encontrada == {"ok": True, "movimiento": False}
    assert sin_eventos == {"ok": True, "movimiento": False}


def test_cualquier_evento_dhl_dispara_alerta_con_evidencia_minima():
    resultado = monitoreo.normalizar_consulta_reemplazada({
        "encontrado": True,
        "eventos": [
            {
                "date": "2026-09-01",
                "time": "10:12:00",
                "typeCode": "PU",
                "description": "Shipment picked up",
            }
        ],
    })

    assert resultado == {
        "ok": True,
        "movimiento": True,
        "estado_courier": "PU",
        "descripcion": "Shipment picked up",
        "evento_fecha": "2026-09-01 10:12:00",
    }


def test_error_de_api_no_se_disfraza_como_guia_sin_movimiento():
    resultado = monitoreo.normalizar_consulta_reemplazada({
        "encontrado": False,
        "error": "DHL 503",
        "http_status": 503,
    })

    assert resultado["ok"] is False
    assert resultado["error"] == "DHL 503"


def test_alerta_se_persiste_sin_tocar_la_cuenta_corriente(monkeypatch):
    class Cursor:
        def __init__(self):
            self.actual = None
            self.sql = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, _params=None):
            compacto = " ".join(sql.split())
            self.sql.append(compacto)
            if compacto.startswith("SELECT r.id, r.tracking_anterior"):
                self.actual = {
                    "id": 3,
                    "tracking_anterior": "1111111111",
                    "riesgo_estado": "VIGILAR",
                    "solicitud_anterior_id": 7,
                    "solicitud_nueva_id": 8,
                    "cliente_id": "MELCIOR",
                }
            elif compacto.startswith("WITH previo AS"):
                self.actual = {"id": 3, "alerta_nueva": True}
            else:
                self.actual = None

        def fetchone(self):
            return self.actual

    cursor = Cursor()

    @contextmanager
    def conexion():
        class Conn:
            def cursor(self):
                return cursor

        yield Conn()

    class DHL:
        def track(self, numero):
            assert numero == "1111111111"
            return {
                "encontrado": True,
                "eventos": [{
                    "typeCode": "PU",
                    "description": "Shipment picked up",
                }],
            }

    auditoria = []
    monkeypatch.setattr(monitoreo, "get_conn", conexion)
    monkeypatch.setattr(
        monitoreo,
        "registrar_evento_con_cursor",
        lambda _cur, **datos: auditoria.append(datos),
    )

    resultado = monitoreo.actualizar_tracking_reemplazado_dhl(
        3, cliente_dhl=DHL(), confirmar_sin_movimiento=True
    )

    assert resultado["movimiento"] is True
    assert resultado["alerta_nueva"] is True
    assert any("riesgo_estado='ALERTA_MOVIMIENTO'" in q for q in cursor.sql)
    assert not any("UPDATE envios" in q for q in cursor.sql)
    assert auditoria[0]["event"] == "dhl.guia_reemplazada_con_movimiento"


def test_no_consulta_antes_de_los_siete_dias(monkeypatch):
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, _sql, _params=None):
            return None

        def fetchone(self):
            return {
                "id": 3,
                "tracking_anterior": "1111111111",
                "riesgo_estado": "VIGILAR",
                "solicitud_anterior_id": 7,
                "solicitud_nueva_id": 8,
                "cliente_id": "MELCIOR",
            }

    @contextmanager
    def conexion():
        class Conn:
            def cursor(self):
                return Cursor()

        yield Conn()

    class DHL:
        def track(self, _numero):
            raise AssertionError("no debe consultar antes del control programado")

    monkeypatch.setattr(monitoreo, "get_conn", conexion)
    resultado = monitoreo.actualizar_tracking_reemplazado_dhl(
        3, cliente_dhl=DHL()
    )

    assert resultado["omitido"] is True
    assert resultado["motivo"] == "control_programado"


def test_control_a_siete_dias_confirma_cancelacion_una_sola_vez(monkeypatch):
    class Cursor:
        def __init__(self):
            self.actual = None
            self.sql = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, _params=None):
            compacto = " ".join(sql.split())
            self.sql.append(compacto)
            if compacto.startswith("SELECT r.id, r.tracking_anterior"):
                self.actual = {
                    "id": 3,
                    "tracking_anterior": "1111111111",
                    "riesgo_estado": "VIGILAR",
                    "solicitud_anterior_id": 7,
                    "solicitud_nueva_id": 8,
                    "cliente_id": "MELCIOR",
                }
            elif "SET riesgo_estado='CERRADA'" in compacto:
                self.actual = {"id": 3}
            else:
                self.actual = None

        def fetchone(self):
            return self.actual

    cursor = Cursor()

    @contextmanager
    def conexion():
        class Conn:
            def cursor(self):
                return cursor

        yield Conn()

    class DHL:
        def track(self, _numero):
            return {"encontrado": False, "error": "DHL 404", "http_status": 404}

    monkeypatch.setattr(monitoreo, "get_conn", conexion)
    resultado = monitoreo.actualizar_tracking_reemplazado_dhl(
        3,
        cliente_dhl=DHL(),
        confirmar_sin_movimiento=True,
    )

    assert resultado["cancelacion_confirmada"] is True
    cierre = next(q for q in cursor.sql if "SET riesgo_estado='CERRADA'" in q)
    assert "control de 7 días" in cierre
    assert not any("UPDATE envios" in q for q in cursor.sql)


def test_contrato_de_schema_scheduler_y_admin():
    schema = (RAIZ / "sql" / "schema.sql").read_text(encoding="utf-8")
    tracking = (RAIZ / "servicios" / "tracking_envios.py").read_text(
        encoding="utf-8"
    )
    admin = (RAIZ / "endpoints" / "admin.py").read_text(encoding="utf-8")
    menu = (RAIZ / "templates" / "admin" / "base_admin.html").read_text(
        encoding="utf-8"
    )
    pantalla = (
        RAIZ / "templates" / "admin" / "guias_reemplazadas.html"
    ).read_text(encoding="utf-8")

    assert "riesgo_estado" in schema
    assert "ALERTA_MOVIMIENTO" in schema
    assert "idx_reemisiones_riesgo_tracking" in schema
    assert "actualizar_trackings_reemplazados_dhl" in tracking
    servicio = (
        RAIZ / "servicios" / "monitoreo_guias_reemplazadas.py"
    ).read_text(encoding="utf-8")
    assert "INTERVAL '7 days'" in servicio
    assert "r.riesgo_estado='VIGILAR'" in servicio
    assert '@router.get("/guias-reemplazadas"' in admin
    assert "Guías descartadas" in menu
    assert "Guías reemplazadas y canceladas" in pantalla
    assert "no reactiva el cargo viejo" in pantalla
    assert "control único a 7 días" in pantalla
    assert "único control en la fecha programada" in pantalla
    assert "Descargar guía final" in pantalla


def test_pantalla_admin_renderiza_alerta_y_tracking_final(monkeypatch):
    from endpoints import admin

    ahora = datetime(2026, 9, 1, 15, 30, tzinfo=timezone.utc)
    fila = {
        "id": 3,
        "cliente_id": "MELCIOR",
        "cliente_nombre": "Melcior",
        "solicitud_anterior_id": 7,
        "solicitud_nueva_id": 8,
        "tracking_anterior": "1111111111",
        "tracking_nuevo": "2222222222",
        "riesgo_estado": "ALERTA_MOVIMIENTO",
        "tracking_anterior_estado_courier": "PU",
        "tracking_anterior_descripcion": "Shipment picked up",
        "tracking_anterior_evento_fecha": "2026-09-01 12:30:00",
        "tracking_anterior_consultado_at": ahora,
        "tracking_anterior_error": None,
        "alerta_movimiento_at": ahora,
        "completed_at": ahora,
        "motivo": "Corrección de domicilio",
        "anterior_tiene_label": True,
        "nueva_tiene_label": True,
        "cuenta_consistente": True,
        "cargo_anterior_estado": "CANCELADO",
        "cargo_nuevo_estado": "ACTIVO",
        "cargo_anterior_fc": "",
        "riesgo_resuelto_nota": None,
        "riesgo_resuelto_at": None,
    }
    monkeypatch.setattr(admin, "_is_auth", lambda _token: True)
    monkeypatch.setattr(monitoreo, "listar_reemisiones_admin", lambda **_kw: [fila])
    monkeypatch.setattr(
        monitoreo,
        "resumen_reemisiones_admin",
        lambda: {"vigilar": 0, "alertas": 1, "cerradas": 0},
    )
    request = Request({
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/admin/guias-reemplazadas",
        "raw_path": b"/admin/guias-reemplazadas",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    })
    request.state.csp_nonce = "test"

    respuesta = admin.admin_guias_reemplazadas(
        request, admin_token="autorizado"
    )
    html = respuesta.body.decode("utf-8")

    assert respuesta.status_code == 200
    assert "Movimiento detectado" in html
    assert "1111111111" in html and "2222222222" in html
    assert "Shipment picked up" in html
    assert "Descargar guía final" in html
