"""Nombre privado del envío: persistencia, aislamiento y presentación."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest
from starlette.requests import Request

from endpoints import portal_cliente
from servicios import auditoria, solicitudes_guia


RAIZ = Path(__file__).resolve().parents[1]


class _Cursor:
    def __init__(self):
        self.consultas = []
        self.fila = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        params = tuple(params or ())
        assert sql.count("%s") == len(params)
        self.consultas.append((" ".join(sql.split()), params))
        self.fila = {
            "id": 41,
            "cliente_id": "MELCIOR",
            "etiqueta_cliente": params[0],
            "updated_at": "ahora",
        }

    def fetchone(self):
        return self.fila


@contextmanager
def _conexion(cursor):
    yield type("Conn", (), {"cursor": lambda _self: cursor})()


def _request():
    request = Request({
        "type": "http", "method": "POST", "path": "/portal/envios/41/etiqueta",
        "raw_path": b"/portal/envios/41/etiqueta", "query_string": b"",
        "headers": [], "scheme": "http", "server": ("testserver", 80),
        "client": ("testclient", 1234), "root_path": "",
    })
    request.state.csp_nonce = "test"
    return request


def test_nombre_se_normaliza_y_tiene_limite_explicito():
    assert solicitudes_guia.normalizar_etiqueta_cliente(
        "  Pedido   21036 \n Enrique Vila  "
    ) == "Pedido 21036 Enrique Vila"
    assert solicitudes_guia.normalizar_etiqueta_cliente("") == ""
    with pytest.raises(ValueError, match="hasta 80 caracteres"):
        solicitudes_guia.normalizar_etiqueta_cliente("x" * 81)


def test_actualizacion_solo_afecta_envio_visible_del_cliente(monkeypatch):
    cursor = _Cursor()
    monkeypatch.setattr(
        solicitudes_guia, "get_conn", lambda: _conexion(cursor)
    )

    resultado = solicitudes_guia.actualizar_etiqueta_cliente(
        41, "melcior", "  Pedido   agosto  "
    )

    sql, params = cursor.consultas[0]
    assert resultado["etiqueta_cliente"] == "Pedido agosto"
    assert "SET etiqueta_cliente=%s, updated_at=NOW()" in sql
    assert "id=%s AND cliente_id=%s" in sql
    assert "test=FALSE AND visible_cliente=TRUE" in sql
    assert "tracking" not in sql
    assert params == ("Pedido agosto", 41, "MELCIOR")


def test_endpoint_no_copia_el_nombre_privado_al_log(monkeypatch):
    eventos = []
    monkeypatch.setattr(
        portal_cliente, "actualizar_etiqueta_cliente",
        lambda *_args: {"etiqueta_cliente": "Pedido privado", "id": 41},
    )
    monkeypatch.setattr(
        auditoria, "registrar_desde_request",
        lambda _request, **datos: eventos.append(datos),
    )

    respuesta = portal_cliente.etiquetar_envio_portal(
        _request(), 41, "Pedido privado", cliente="MELCIOR"
    )

    assert respuesta.status_code == 303
    assert respuesta.headers["location"].endswith("/41?ok=etiqueta")
    assert eventos[0]["event"] == "portal.etiqueta_envio_actualizada"
    assert eventos[0]["metadata"] == {
        "solicitud_id": 41, "etiqueta_presente": True, "longitud": 14,
    }
    assert "Pedido privado" not in str(eventos[0])


def test_schema_y_vistas_exponen_el_nombre_sin_confundirlo_con_la_guia():
    schema = (RAIZ / "sql/schema.sql").read_text()
    nuevo = (RAIZ / "templates/portal/envio_nuevo.html").read_text()
    detalle = (RAIZ / "templates/portal/envio_detalle.html").read_text()
    envios = (RAIZ / "templates/portal/envios.html").read_text()
    cuenta = (RAIZ / "templates/portal/cuenta.html").read_text()

    assert "ADD COLUMN IF NOT EXISTS etiqueta_cliente TEXT" in schema
    assert "ck_solicitudes_guia_etiqueta_cliente" in schema
    assert 'name="etiqueta_cliente"' in nuevo
    assert 'maxlength="80"' in nuevo
    assert 'action="/portal/envios/{{ s.id }}/etiqueta"' in detalle
    assert "Nombre para reconocer este envío" in detalle
    assert "La guía anterior quedó descartada" in detalle
    assert "s.etiqueta_cliente" in envios
    assert "m.etiqueta_envio or m.destinatario" in cuenta
    assert "Guía / tracking" in cuenta
    assert ">Importe<" not in cuenta


def test_integraciones_conservan_el_nombre_en_exportaciones_de_privacidad():
    shopify = (RAIZ / "servicios/shopify_gdpr.py").read_text()
    tiendanube = (RAIZ / "servicios/tiendanube_privacidad.py").read_text()
    automatica = (RAIZ / "servicios/solicitud_automatica.py").read_text()
    api = (RAIZ / "servicios/solicitudes_guia.py").read_text()

    assert "etiqueta_cliente" in shopify
    assert "etiqueta_cliente" in tiendanube
    assert 'f"Pedido {str(ped[\'numero\']).strip()}"' in automatica
    assert "SELECT id, api_referencia, etiqueta_cliente" in api
