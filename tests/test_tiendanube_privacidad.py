from __future__ import annotations

import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


class CursorFalso:
    def __init__(self, respuestas=None):
        self.respuestas = list(respuestas or [])
        self.consultas = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.consultas.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.respuestas.pop(0) if self.respuestas else None

    def fetchall(self):
        return self.respuestas.pop(0) if self.respuestas else []


class ConexionFalsa:
    def __init__(self, cursor):
        self.cursor_falso = cursor
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self.cursor_falso

    def commit(self):
        self.commits += 1


def test_listar_pendientes_muestra_solicitudes_y_cuarentenas_no_resueltas(monkeypatch):
    from servicios import tiendanube_privacidad

    esperado = [{
        "id": 4,
        "request_id": "req-1",
        "store_id": "123",
        "cantidad_recursos": 2,
        "estado": "PENDIENTE",
    }]
    cursor = CursorFalso([esperado])
    monkeypatch.setattr(
        tiendanube_privacidad, "get_conn", lambda: ConexionFalsa(cursor),
    )

    assert tiendanube_privacidad.listar_pendientes() == esperado
    sql, params = cursor.consultas[0]
    assert "tipo = 'customers/data_request'" not in sql
    assert "estado <> 'RESUELTO'" in sql
    assert "jsonb_array_length(recursos)" in sql
    assert params is None


def test_exportacion_limita_tenant_y_excluye_contabilidad_y_binarios(monkeypatch):
    from servicios import tiendanube_privacidad

    solicitud = {
        "request_id": "req-1",
        "store_id": "123",
        "tipo": "customers/data_request",
        "customer_id": "77",
        "recursos": ["1001", "1001", "1002"],
        "estado": "PENDIENTE",
        "creado_at": None,
        "resuelto_at": None,
    }
    pedido = {
        "pedido_externo_id": "1001",
        "destinatario": {"email": "persona@example.com"},
    }
    guia = {
        "id": 88,
        "dest_email": "persona@example.com",
        "tiene_etiqueta": True,
    }
    direccion = {
        "id": 12,
        "email": "persona@example.com",
        "origen_pedido_externo_id": "1001",
    }
    envio = {"id": 19, "solicitud_id": 88, "tracking": "TRACK-1"}
    cursor = CursorFalso([
        solicitud,
        [pedido],
        [guia],
        [direccion],
        [envio],
    ])
    monkeypatch.setattr(
        tiendanube_privacidad, "get_conn", lambda: ConexionFalsa(cursor),
    )

    exportacion = tiendanube_privacidad.generar_exportacion(4)

    assert exportacion["solicitud"] == {
        "request_id": "req-1",
        "store_id": "123",
        "customer_id": "77",
        "resources_requested": ["1001", "1002"],
        "received_at": None,
    }
    assert exportacion["datos_en_tauro"]["pedidos_vinculados"] == [pedido]
    assert exportacion["datos_en_tauro"]["solicitudes_guia_vinculadas"] == [guia]
    assert exportacion["datos_en_tauro"]["direcciones_tiendanube_derivadas"] == [direccion]
    assert exportacion["datos_en_tauro"]["envios_operativos_vinculados"] == [envio]
    texto = json.dumps(exportacion)
    assert "persona@example.com" in texto
    assert '"label_pdf":' not in texto
    assert '"factura_pdf":' not in texto
    assert '"monto_ars":' not in texto
    assert '"nro_fc":' not in texto

    consultas = cursor.consultas
    assert consultas[1][1] == ("123.tiendanube", ["1001", "1002"])
    assert "t.plataforma = 'tiendanube'" in consultas[1][0]
    assert "p.plataforma = 'tiendanube'" in consultas[1][0]
    assert consultas[2][1] == ("123.tiendanube", ["1001", "1002"])
    assert "origen_plataforma" in consultas[2][0]
    assert consultas[4][1] == ([88],)
    assert "monto_ars" not in consultas[4][0]
    assert "nro_fc" not in consultas[4][0]


def test_exportacion_sin_recursos_no_lee_tablas_operativas(monkeypatch):
    from servicios import tiendanube_privacidad

    solicitud = {
        "request_id": "req-2",
        "store_id": "123",
        "customer_id": "77",
        "recursos": [],
        "creado_at": None,
    }
    cursor = CursorFalso([solicitud])
    monkeypatch.setattr(
        tiendanube_privacidad, "get_conn", lambda: ConexionFalsa(cursor),
    )

    exportacion = tiendanube_privacidad.generar_exportacion(5)

    assert exportacion["solicitud"]["resources_requested"] == []
    assert exportacion["datos_en_tauro"]["pedidos_vinculados"] == []
    assert len(cursor.consultas) == 1


@pytest.mark.parametrize("valor", [None, True, 0, -1, "no"])
def test_identificador_invalido_no_consulta_db(monkeypatch, valor):
    from servicios import tiendanube_privacidad

    monkeypatch.setattr(
        tiendanube_privacidad,
        "get_conn",
        lambda: (_ for _ in ()).throw(AssertionError("no debe consultar")),
    )

    with pytest.raises(tiendanube_privacidad.SolicitudPrivacidadInvalida):
        tiendanube_privacidad.generar_exportacion(valor)


def test_marcar_resuelta_es_idempotente_y_acotada_al_tipo(monkeypatch):
    from servicios import tiendanube_privacidad

    fila = {
        "id": 4,
        "request_id": "req-1",
        "store_id": "123",
        "cantidad_recursos": 2,
    }
    cursor = CursorFalso([fila])
    conn = ConexionFalsa(cursor)
    monkeypatch.setattr(tiendanube_privacidad, "get_conn", lambda: conn)

    assert tiendanube_privacidad.marcar_resuelta(4) == fila
    sql, params = cursor.consultas[0]
    assert "resuelto_at = COALESCE(resuelto_at, NOW())" in sql
    assert "tipo = 'customers/data_request'" in sql
    assert params == (4,)
    assert conn.commits == 1


def test_resolver_cuarentena_preserva_instalacion_y_registra_accion(monkeypatch):
    from servicios import tiendanube_privacidad

    fila = {
        "id": 7,
        "request_id": "evt-1",
        "store_id": "123",
        "tipo": "store/redact",
        "resolucion": "MANTENER_INSTALACION_ACTUAL",
    }
    cursor = CursorFalso([fila])
    conn = ConexionFalsa(cursor)
    monkeypatch.setattr(tiendanube_privacidad, "get_conn", lambda: conn)

    assert tiendanube_privacidad.resolver_cuarentena(7) == fila
    sql, params = cursor.consultas[0]
    assert "estado = 'CUARENTENA'" in sql
    assert "MANTENER_INSTALACION_ACTUAL" in params
    assert params == ("MANTENER_INSTALACION_ACTUAL", 7)
    assert conn.commits == 1


def test_admin_y_template_cablean_operacion_tiendanube():
    admin = (ROOT / "endpoints/admin.py").read_text(encoding="utf-8")
    base = (ROOT / "templates/admin/base_admin.html").read_text(encoding="utf-8")
    template = (
        ROOT / "templates/admin/tiendanube_privacidad.html"
    ).read_text(encoding="utf-8")

    assert '@router.get("/tiendanube/privacidad"' in admin
    assert "tiendanube.privacy.download" in admin
    assert "tiendanube.privacy.resolve" in admin
    assert "Cache-Control\": \"private, no-store" in admin
    assert "/admin/tiendanube/privacidad" in base
    assert "/datos.json" in template
    assert "/resolver" in template
    assert "data-confirm" in template


def test_descarga_admin_no_cachea_y_sanea_nombre_de_archivo(monkeypatch):
    from endpoints import admin
    from servicios import auditoria, tiendanube_privacidad

    exportacion = {
        "solicitud": {
            "request_id": 'req-1\r\nmalicioso-ñ"',
            "store_id": "123",
            "resources_requested": ["1001"],
        },
        "datos_en_tauro": {},
    }
    monkeypatch.setattr(admin, "_is_auth", lambda _token: True)
    monkeypatch.setattr(
        tiendanube_privacidad,
        "generar_exportacion",
        lambda _solicitud_id: exportacion,
    )
    visto = {}
    monkeypatch.setattr(
        auditoria,
        "registrar_desde_request",
        lambda *_args, **kwargs: visto.update(kwargs),
    )

    respuesta = admin.admin_tiendanube_privacidad_descargar(
        4, request=object(), admin_token="token",
    )

    assert respuesta.status_code == 200
    assert respuesta.headers["cache-control"] == "private, no-store"
    assert respuesta.headers["pragma"] == "no-cache"
    assert respuesta.headers["x-content-type-options"] == "nosniff"
    assert respuesta.headers["content-disposition"] == (
        'attachment; filename="tiendanube-data-request-req-1malicioso-.json"'
    )
    assert visto["event"] == "tiendanube.privacy.download"
    assert visto["actor_ref"] == "123:4"
