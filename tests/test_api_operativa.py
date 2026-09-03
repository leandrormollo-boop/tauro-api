from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

import main
from servicios import rastreo
from servicios.solicitudes_guia import IdempotencyConflictError


def _pedido() -> main.PedidoRequest:
    return main.PedidoRequest(
        producto_id="REEL-QA",
        destino_pais="US",
        nombre_comprador="Cliente QA",
        direccion_exacta="Test 123",
        ciudad="Miami",
        estado="FL",
        zip_code="33101",
        pais="US",
        telefono="0000000000",
        email_comprador="qa@example.invalid",
    )


def _perfil() -> dict:
    return {
        "cliente_id": "PESCA_JACKS_QA",
        "nombre": "Pesca Jacks QA",
        "cuit": "",
        "direccion": "Origen QA",
        "cp": "1000",
        "ciudad": "Buenos Aires",
        "pais": "AR",
        "telefono": "0000000000",
        "email": "qa@example.invalid",
    }


def _precio() -> dict:
    return {
        "encontrado": True,
        "ruta_id": "AR-US-QA",
        "coti_id": "COTI-QA",
        "precio_ars": 100000,
        "precio_usd": 100,
        "tipo_cambio_usado": 1000,
    }


def _producto() -> dict:
    return {
        "encontrado": True,
        "nombre_es": "REEL-QA",
        "nombre_en": "Fishing reel",
        "hs_code": "9507.10.00",
        "valor_usd": 40,
        "unidades": 1,
        "peso_kg": 1.2,
        "largo": 12,
        "ancho": 20,
        "alto": 10,
    }


def _preparar_pedido(monkeypatch, *, replay: bool = False, email_ok: bool = True):
    monkeypatch.setattr(main, "autenticar", lambda _: _perfil())
    monkeypatch.setattr(main, "obtener_precio_envio", lambda *a, **k: _precio())
    monkeypatch.setattr(main, "obtener_datos_producto", lambda *a, **k: _producto())
    enviados = []
    monkeypatch.setattr(
        main, "enviar_email_pedido",
        lambda datos: enviados.append(datos) is None and email_ok,
    )
    capturados = []

    def crear(**kwargs):
        capturados.append(kwargs)
        return {
            "id": 91,
            "estado": "SOLICITADO",
            "api_referencia": "PESCA-EXTERNA-91",
            "_idempotent_replay": replay,
        }

    monkeypatch.setattr(main, "crear_solicitud_guia", crear)
    return capturados, enviados


def test_pedido_hashea_idempotency_key_y_no_duplica_notificacion(monkeypatch):
    capturados, enviados = _preparar_pedido(monkeypatch, replay=True)

    respuesta = main.registrar_pedido(
        _pedido(), x_api_key="tauro-qa", idempotency_key="orden-shopify-1001",
    )

    assert respuesta["solicitud_id"] == 91
    assert respuesta["idempotent_replay"] is True
    assert respuesta["idempotencia_protegida"] is True
    assert respuesta["notificacion_logistica"] == "omitida_reintento"
    assert enviados == []
    assert capturados[0]["idempotency_key_hash"] != "orden-shopify-1001"
    assert len(capturados[0]["idempotency_key_hash"]) == 64
    assert len(capturados[0]["request_fingerprint"]) == 64


def test_pedido_persiste_aunque_falle_el_mail(monkeypatch):
    capturados, enviados = _preparar_pedido(monkeypatch, email_ok=False)

    respuesta = main.registrar_pedido(
        _pedido(), x_api_key="tauro-qa", idempotency_key="orden-shopify-1002",
    )

    assert capturados and enviados
    assert respuesta["status"] == "success"
    assert respuesta["notificacion_logistica"] == "fallida_no_bloqueante"


def test_pedido_rechaza_reuso_de_clave_con_otro_payload(monkeypatch):
    _preparar_pedido(monkeypatch)

    def conflicto(**kwargs):
        raise IdempotencyConflictError(
            "La Idempotency-Key ya fue utilizada con datos diferentes."
        )

    monkeypatch.setattr(main, "crear_solicitud_guia", conflicto)
    with pytest.raises(HTTPException) as exc:
        main.registrar_pedido(
            _pedido(), x_api_key="tauro-qa", idempotency_key="orden-shopify-1003",
        )
    assert exc.value.status_code == 409


def test_estado_pedido_y_label_siempre_filtran_por_cliente(monkeypatch):
    monkeypatch.setattr(main, "autenticar", lambda _: _perfil())
    from servicios import solicitudes_guia

    consultas = []

    def obtener(solicitud_id, cliente_id):
        consultas.append((solicitud_id, cliente_id))
        return {
            "id": solicitud_id,
            "estado": "GUIA_LISTA",
            "ambito": "INTERNACIONAL",
            "courier": "DHL",
            "servicio_courier": "P",
            "tracking": "2634793766",
            "tiene_label": True,
            "tiene_factura_comercial": True,
            "guia_url": None,
            "api_referencia": "PESCA-91",
            "created_at": datetime(2026, 8, 27, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 8, 27, tzinfo=timezone.utc),
        }

    monkeypatch.setattr(solicitudes_guia, "obtener_solicitud_de_cliente", obtener)
    monkeypatch.setattr(
        solicitudes_guia, "obtener_label_de_cliente",
        lambda solicitud_id, cliente_id: b"%PDF-1.4\nQA",
    )
    monkeypatch.setattr(
        solicitudes_guia, "obtener_factura_comercial_pdf",
        lambda solicitud_id, cliente_id: b"%PDF-1.4\nINVOICE",
    )

    estado = main.estado_pedido(91, x_api_key="tauro-qa")
    pdf = main.descargar_guia_api(91, x_api_key="tauro-qa")
    factura = main.descargar_factura_comercial_api(91, x_api_key="tauro-qa")

    assert consultas == [(91, "PESCA_JACKS_QA")]
    assert estado["tracking"] == "2634793766"
    assert estado["guia_url"] == "/pedidos/91/guia.pdf"
    assert estado["factura_comercial_url"] == "/pedidos/91/factura-comercial.pdf"
    assert pdf.media_type == "application/pdf"
    assert bytes(pdf.body).startswith(b"%PDF")
    assert factura.media_type == "application/pdf"
    assert bytes(factura.body).startswith(b"%PDF")


def test_listado_envios_b2b_pagina_y_separa_ambito(monkeypatch):
    monkeypatch.setattr(main, "autenticar", lambda _: _perfil())
    from servicios import solicitudes_guia
    consultas = []

    def listar(cliente_id, **filtros):
        consultas.append((cliente_id, filtros))
        return ([{
            "id": 91,
            "api_referencia": "PESCA-91",
            "estado": "GUIA_LISTA",
            "ambito": "INTERNACIONAL",
            "courier": "DHL",
            "servicio_courier": "P",
            "producto_alias": "REEL-QA",
            "cantidad": 1,
            "destino_pais": "US",
            "dest_nombre": "Cliente QA",
            "dest_ciudad": "Miami",
            "dest_estado": "FL",
            "peso_kg": 1.2,
            "valor_declarado_usd": 40,
            "precio_tauro_ars": 100000,
            "precio_tauro_usd": 100,
            "tracking": "2634793766",
            "tiene_label": True,
            "created_at": datetime(2026, 8, 27, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 8, 27, tzinfo=timezone.utc),
        }], 3)

    monkeypatch.setattr(solicitudes_guia, "listar_envios_api", listar)

    respuesta = main.listar_envios_b2b(
        x_api_key="tauro-qa", limite=1, offset=1,
        ambito="internacional", estado="guia_lista",
    )

    assert consultas == [("PESCA_JACKS_QA", {
        "limite": 1, "offset": 1,
        "ambito": "INTERNACIONAL", "estado": "GUIA_LISTA",
    })]
    assert respuesta["total"] == 3
    assert respuesta["siguiente_offset"] == 2
    assert respuesta["anterior_offset"] == 0
    assert respuesta["envios"][0]["ambito"] == "internacional"
    assert respuesta["envios"][0]["guia_url"] == "/pedidos/91/guia.pdf"
    serializado = str(respuesta).lower()
    assert "margen" not in serializado
    assert "costo_" not in serializado


def test_listado_envios_rechaza_filtros_ambiguos(monkeypatch):
    monkeypatch.setattr(main, "autenticar", lambda _: _perfil())

    with pytest.raises(HTTPException) as exc:
        main.listar_envios_b2b(
            x_api_key="tauro-qa", limite=100, offset=0,
            ambito="todos", estado="",
        )
    assert exc.value.status_code == 400


class _Cursor:
    def __init__(self, row):
        self.row = row
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params):
        assert "cliente_id=%s" in query
        self.params = params

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self._cursor


def test_rastreo_b2b_es_del_cliente_y_normaliza_live(monkeypatch):
    cursor = _Cursor({
        "id": 91,
        "cliente_id": "PESCA_JACKS_QA",
        "courier": "DHL",
        "estado": "DESPACHADO",
        "ambito": "INTERNACIONAL",
        "destino_pais": "US",
        "dest_ciudad": "Miami",
        "remitente_ciudad": "Buenos Aires",
        "remitente_pais": "AR",
        "tracking": "2634793766",
        "guia_generada_at": datetime(2026, 8, 27, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 8, 27, tzinfo=timezone.utc),
    })
    monkeypatch.setattr(rastreo, "get_conn", lambda: _Conn(cursor))
    monkeypatch.setattr(rastreo, "_rastrear_en_courier", lambda *a: {
        "estado": "TRANSIT",
        "descripcion": "En tránsito",
        "eventos": [{"codigo": "PU", "descripcion": "Retirado"}],
    })

    resultado = rastreo.rastrear_cliente(
        "pesca_jacks_qa", "2634-793-766", actualizar=True,
    )

    assert cursor.params == ("PESCA_JACKS_QA", "2634793766")
    assert resultado["fuente"] == "courier"
    assert resultado["estado"] == "TRANSIT"
    assert resultado["solicitud_id"] == 91
    assert "precio" not in str(resultado).lower()
    assert "email" not in str(resultado).lower()


def test_rastreo_live_dhl_reduce_el_payload_a_campos_seguros(monkeypatch):
    from core.dhl_client import DHLClient

    monkeypatch.setattr(DHLClient, "track", lambda self, tracking: {
        "encontrado": True,
        "estado": "TRANSIT",
        "descripcion": "En tránsito",
        "eventos": [{
            "statusCode": "PU",
            "description": "Retirado",
            "timestamp": "2026-08-27T10:00:00-03:00",
            "location": {"address": {
                "addressLocality": "Buenos Aires", "countryCode": "AR",
            }},
            "payload_interno": "no debe salir",
        }],
        "credencial_interna": "no debe salir",
    })

    resultado = rastreo._rastrear_en_courier("DHL", "2634793766")

    assert resultado["estado"] == "TRANSIT"
    assert resultado["eventos"] == [{
        "codigo": "PU",
        "descripcion": "Retirado",
        "fecha": "2026-08-27T10:00:00-03:00",
        "ubicacion": "Buenos Aires, AR",
    }]
    assert "credencial" not in str(resultado).lower()
    assert "payload_interno" not in str(resultado)


def test_rastreo_live_no_expone_error_crudo_del_courier(monkeypatch):
    from core.dhl_client import DHLClient

    monkeypatch.setattr(DHLClient, "track", lambda self, tracking: {
        "encontrado": False,
        "error": "DHL_API_SECRET=valor-que-no-debe-salir",
    })

    assert rastreo._rastrear_en_courier("DHL", "2634793766") is None


def test_servicio_idempotente_usa_indice_atomico():
    fuente = open("servicios/solicitudes_guia.py", encoding="utf-8").read()
    schema = open("sql/schema.sql", encoding="utf-8").read()
    assert "ON CONFLICT (cliente_id, idempotency_key_hash)" in fuente
    assert "uq_solicitudes_cliente_idempotency" in schema


def test_listado_envios_sql_filtra_cliente_y_no_selecciona_costos():
    fuente = open("servicios/solicitudes_guia.py", encoding="utf-8").read()
    bloque = fuente.split("def listar_envios_api(", 1)[1].split(
        "def contar_guias_listas", 1
    )[0]
    assert (
        'condiciones = ["cliente_id=%s", "test=FALSE", '
        '"visible_cliente=TRUE"]'
    ) in bloque
    assert "costo_" not in bloque
    assert "margen" not in bloque
    assert "label_pdf," not in bloque
