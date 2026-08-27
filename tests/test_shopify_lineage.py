"""Linaje durable de ventas Shopify hacia solicitudes y libreta TAURO."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from endpoints import portal_cliente as portal
from servicios import (
    api_b2b,
    catalogo,
    direcciones,
    integraciones_tienda,
    solicitud_automatica,
    solicitudes_guia,
)


class _Cursor:
    def __init__(self, respuestas):
        self.respuestas = iter(respuestas)
        self.ejecutadas = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        params = tuple(params or ())
        assert sql.count("%s") == len(params)
        self.ejecutadas.append((sql, params))

    def fetchone(self):
        return next(self.respuestas)


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _conexion(cursor):
    @contextmanager
    def get_conn():
        yield _Conn(cursor)

    return get_conn


def _request(path="/portal/envios/nuevo"):
    from starlette.requests import Request

    request = Request({
        "type": "http", "method": "POST", "path": path,
        "raw_path": path.encode(), "query_string": b"", "headers": [],
        "scheme": "http", "server": ("testserver", 80),
        "client": ("testclient", 1234), "root_path": "",
    })
    request.state.csp_nonce = "test"
    return request


def test_schema_migra_linaje_en_solicitudes_y_direcciones():
    schema = (Path(__file__).parents[1] / "sql" / "schema.sql").read_text()

    for tabla in ("solicitudes_guia", "direcciones"):
        assert f"idx_{tabla}_origen_tienda" in schema
    assert schema.count("ADD COLUMN IF NOT EXISTS origen_plataforma TEXT") >= 2
    assert schema.count("ADD COLUMN IF NOT EXISTS origen_dominio TEXT") >= 2
    assert schema.count("ADD COLUMN IF NOT EXISTS origen_pedido_externo_id TEXT") >= 2


def test_solicitud_persiste_origen_en_su_insert_inicial(monkeypatch):
    cursor = _Cursor([{"id": 12}, {"id": 91}])
    monkeypatch.setattr(solicitudes_guia, "get_conn", _conexion(cursor))

    creada = solicitudes_guia.crear_solicitud_guia(
        cliente_id="MELCIOR", producto_alias="LANA", cantidad=1,
        remitente_pais="AR", destino_pais="US",
        dest_nombre="Buyer", dest_documento="", dest_email="",
        dest_telefono="", dest_direccion="Street 1", dest_ciudad="Miami",
        dest_estado="FL", dest_zip="33101", peso_kg=1, largo_cm=10,
        ancho_cm=10, alto_cm=10, valor_declarado_usd=40,
        ruta_id="AR-US", coti_id="C-1", precio_tauro_ars=100,
        precio_tauro_usd=1, origen_plataforma="Shopify",
        origen_dominio="PESCA-JACKS.MYSHOPIFY.COM",
        origen_pedido_externo_id="gid://shopify/Order/123",
    )

    sql, params = next(
        (sql, params) for sql, params in cursor.ejecutadas
        if "INSERT INTO solicitudes_guia" in sql
    )
    assert creada["id"] == 91
    assert "origen_plataforma, origen_dominio, origen_pedido_externo_id" in sql
    assert params[-3:] == (
        "shopify", "pesca-jacks.myshopify.com", "gid://shopify/Order/123",
    )


def test_retry_de_la_misma_venta_recupera_la_solicitud_sin_duplicar(monkeypatch):
    clave = solicitudes_guia.idempotency_hash_origen_tienda(
        cliente_id="MELCIOR", origen_plataforma="shopify",
        origen_dominio="pesca-jacks.myshopify.com",
        origen_pedido_externo_id="123",
    )
    cursor = _Cursor([{"id": 12}, None, {
        "id": 91, "request_fingerprint": None,
        "idempotency_key_hash": clave,
    }])
    monkeypatch.setattr(solicitudes_guia, "get_conn", _conexion(cursor))

    creada = solicitudes_guia.crear_solicitud_guia(
        cliente_id="MELCIOR", producto_alias="LANA", cantidad=1,
        remitente_pais="AR", destino_pais="US",
        dest_nombre="Buyer", dest_documento="", dest_email="",
        dest_telefono="", dest_direccion="Street 1", dest_ciudad="Miami",
        dest_estado="FL", dest_zip="33101", peso_kg=1, largo_cm=10,
        ancho_cm=10, alto_cm=10, valor_declarado_usd=40,
        ruta_id="AR-US", coti_id="C-1", precio_tauro_ars=100,
        precio_tauro_usd=1, idempotency_key_hash=clave,
        origen_plataforma="shopify",
        origen_dominio="pesca-jacks.myshopify.com",
        origen_pedido_externo_id="123",
    )

    assert creada["id"] == 91
    assert creada["_idempotent_replay"] is True
    assert sum(
        "INSERT INTO solicitudes_guia" in sql
        for sql, _params in cursor.ejecutadas
    ) == 1
    insert_sql = next(
        sql for sql, _params in cursor.ejecutadas
        if "INSERT INTO solicitudes_guia" in sql
    )
    assert "ON CONFLICT (cliente_id, idempotency_key_hash)" in insert_sql
    assert any(
        "SELECT *" in sql and "idempotency_key_hash" in sql
        for sql, _params in cursor.ejecutadas
    )


def test_hash_de_venta_es_canonico_y_no_expone_identificadores():
    uno = solicitudes_guia.idempotency_hash_origen_tienda(
        cliente_id="melcior", origen_plataforma="SHOPIFY",
        origen_dominio="PESCA-JACKS.MYSHOPIFY.COM",
        origen_pedido_externo_id="123",
    )
    dos = solicitudes_guia.idempotency_hash_origen_tienda(
        cliente_id="MELCIOR", origen_plataforma="shopify",
        origen_dominio="pesca-jacks.myshopify.com",
        origen_pedido_externo_id="123",
    )

    assert uno == dos
    assert len(uno) == 64
    assert "pesca" not in uno and "123" not in uno


def test_direccion_guardada_desde_shopify_conserva_origen(monkeypatch):
    cursor = _Cursor([
        {"id": 12},
        {"id": 8, "nombre": "Buyer", "alias": "Buyer"},
    ])
    monkeypatch.setattr(direcciones, "get_conn", _conexion(cursor))

    direcciones.crear_direccion(
        cliente_id="MELCIOR", tipo="DESTINATARIO", nombre="Buyer",
        direccion="Street 1", ciudad="Miami", cp="33101", pais="US",
        origen_plataforma="SHOPIFY",
        origen_dominio="PESCA-JACKS.MYSHOPIFY.COM",
        origen_pedido_externo_id="123",
    )

    sql, params = next(
        (sql, params) for sql, params in cursor.ejecutadas
        if "INSERT INTO direcciones" in sql
    )
    assert "origen_plataforma, origen_dominio, origen_pedido_externo_id" in sql
    assert params[-3:] == ("shopify", "pesca-jacks.myshopify.com", "123")


def test_automatico_conserva_origen_aunque_falle_marcar_convertido(monkeypatch):
    pedido = {
        "id": 21, "cliente_id": "MELCIOR", "estado": "PENDIENTE",
        "numero": "#123", "solicitud_id": None,
        "origen_plataforma": "shopify",
        "origen_dominio": "pesca-jacks.myshopify.com",
        "origen_pedido_externo_id": "gid://shopify/Order/123",
        "destinatario": {
            "pais": "US", "nombre": "Buyer", "direccion": "Street 1",
            "direccion2": "", "ciudad": "Miami", "estado": "FL",
            "cp": "33101", "email": "buyer@example.com", "telefono": "1",
        },
        "items": [{"sku": "LANA", "cantidad": 1}],
        "valor_total": 40, "moneda": "USD",
    }
    monkeypatch.setattr(
        solicitud_automatica, "get_conn", _conexion(_Cursor([pedido])),
    )
    monkeypatch.setattr(
        catalogo, "get_productos",
        lambda _cliente: [SimpleNamespace(alias_interno="LANA")],
    )
    monkeypatch.setattr(
        direcciones, "obtener_remitente_para_envio",
        lambda *_args: {
            "pais": "AR", "nombre": "Melcior", "direccion": "Calle 1",
            "ciudad": "CABA", "cp": "1000", "email": "", "telefono": "",
        },
    )
    monkeypatch.setattr(api_b2b, "obtener_precio_envio_multi", lambda *_a, **_k: {
        "encontrado": True,
        "bultos": [{
            "producto_alias": "LANA", "cantidad": 1, "peso_kg": 1,
            "largo_cm": 10, "ancho_cm": 10, "alto_cm": 10,
        }],
        "peso_total_kg": 1, "valor_total_usd": 40,
        "ruta_id": "AR-US", "coti_id": "C-1",
        "precio_ars": 100, "precio_usd": 1,
    })
    persistido = {}

    def crear(**kwargs):
        persistido.update(kwargs)
        return {"id": 91}

    monkeypatch.setattr(solicitudes_guia, "crear_solicitud_guia", crear)
    monkeypatch.setattr(
        integraciones_tienda, "marcar_convertido",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("update caído")),
    )

    resultado = solicitud_automatica.crear_desde_pedido(21)

    assert resultado == {"ok": True, "solicitud_id": 91, "motivo": ""}
    assert persistido["origen_plataforma"] == "shopify"
    assert persistido["origen_dominio"] == "pesca-jacks.myshopify.com"
    assert persistido["origen_pedido_externo_id"] == "gid://shopify/Order/123"
    assert len(persistido["idempotency_key_hash"]) == 64


def test_automatico_falla_cerrado_si_un_pedido_legacy_no_tiene_origen(monkeypatch):
    pedido = {
        "id": 22, "cliente_id": "MELCIOR", "estado": "PENDIENTE",
        "numero": "#124", "solicitud_id": None,
        "origen_plataforma": "shopify", "origen_dominio": None,
        "origen_pedido_externo_id": "124",
    }
    cursor = _Cursor([pedido])
    monkeypatch.setattr(solicitud_automatica, "get_conn", _conexion(cursor))
    monkeypatch.setattr(
        catalogo, "get_productos",
        lambda *_args: pytest.fail("sin linaje no debe leer ni crear datos derivados"),
    )

    resultado = solicitud_automatica.crear_desde_pedido(22)

    assert resultado["ok"] is False
    assert "verificar el origen" in resultado["motivo"]


def test_portal_resuelve_origen_por_cliente_en_postgres(monkeypatch):
    cursor = _Cursor([{
        "id": 17, "estado": "PENDIENTE", "solicitud_id": None,
        "origen_plataforma": "shopify",
        "origen_dominio": "pesca-jacks.myshopify.com",
        "origen_pedido_externo_id": "123",
    }])
    monkeypatch.setattr(portal, "get_conn", _conexion(cursor))

    origen = portal._origen_pedido_tienda_verificado("melcior", "17")

    sql, params = cursor.ejecutadas[0]
    assert "JOIN tiendas_conectadas" in sql
    assert "p.cliente_id = %s" in sql
    assert params == (17, "MELCIOR")
    assert origen == {
        "pedido_id": 17,
        "origen_plataforma": "shopify",
        "origen_dominio": "pesca-jacks.myshopify.com",
        "origen_pedido_externo_id": "123",
    }


def test_portal_propaga_origen_a_solicitud_y_direccion(monkeypatch):
    monkeypatch.setattr(portal, "get_productos", lambda _cliente: [])
    monkeypatch.setattr(
        portal, "_paises_con_nacional",
        lambda: [("AR", "Argentina"), ("CN", "China"), ("US", "Estados Unidos")],
    )
    monkeypatch.setattr(portal, "listar_direcciones", lambda *_args: [])
    monkeypatch.setattr(portal, "obtener_remitente_para_envio", lambda *_args: None)
    monkeypatch.setattr(portal, "tax_paga_cliente", lambda _cliente: "DESTINATARIO")
    monkeypatch.setattr(portal, "courier_default_cliente", lambda _cliente: "dhl")
    origen = {
        "pedido_id": 17, "origen_plataforma": "shopify",
        "origen_dominio": "pesca-jacks.myshopify.com",
        "origen_pedido_externo_id": "123",
    }
    monkeypatch.setattr(
        portal, "_origen_pedido_tienda_verificado", lambda *_args: origen,
    )
    monkeypatch.setattr(portal, "cotizar_couriers_cliente", lambda *_a, **_k: {
        "encontrado": True,
        "opciones": [{
            "id": "dhl", "nombre": "DHL Express",
            "precio_ars": 195_000, "precio_usd": 195,
        }],
        "ruta_id": "CN-US", "coti_id": "C-2", "peso_total_kg": 3.9,
        "piezas_total": 1,
        "bultos": [{
            "producto_alias": "CARGA", "cantidad": 1,
            "unidades_aduana": 1, "peso_kg": 3.9,
            "largo_cm": 48, "ancho_cm": 47, "alto_cm": 20,
            "valor_unitario_usd": 40, "descripcion_en": "SWEATER",
            "hs_code": "611011", "pais_origen": "CN",
        }],
    })
    direccion_guardada = {}
    solicitud_guardada = {}
    marcado = []
    monkeypatch.setattr(portal, "crear_direccion", lambda **kw: direccion_guardada.update(kw))
    monkeypatch.setattr(
        portal, "crear_solicitud_guia",
        lambda **kw: solicitud_guardada.update(kw) or {"id": 91},
    )
    monkeypatch.setattr(
        portal, "marcar_convertido",
        lambda cliente, pedido_id, solicitud_id=None: marcado.append(
            (cliente, pedido_id, solicitud_id)
        ),
    )

    respuesta = portal.envio_nuevo_post(
        _request(), destino_pais="US",
        bulto_producto=[""], bulto_cantidad=["1"], bulto_unidades_aduana=["1"],
        bulto_peso=["3.9"], bulto_largo=["48"], bulto_ancho=["47"],
        bulto_alto=["20"], bulto_desc_en=["SWEATER"],
        bulto_valor_usd=["40"], bulto_hs=["611011"], bulto_pais_fab=["CN"],
        producto_alias="", cantidad="1", intl_courier="dhl",
        precio_cotizado_ars="195000", tax_paga="DESTINATARIO",
        remitente_id="", rem_nombre="Proveedor", rem_contacto="Contacto",
        rem_documento="CN-TAX", rem_email="ops@example.cn", rem_telefono="+86 10",
        rem_direccion="Road 1", rem_ciudad="Yiwu", rem_estado="Zhejiang",
        rem_zip="322000", rem_pais="CN", destinatario_id="",
        dest_nombre="Buyer", dest_contacto="Buyer", dest_documento="US-TAX",
        dest_email="buyer@example.com", dest_telefono="+1 305",
        dest_direccion="Street 1", dest_ciudad="Miami", dest_estado="FL",
        dest_zip="33101", dest_alias="Habitual", guardar_destinatario="1",
        precio_cliente_final_ars="", observaciones="Pedido Shopify",
        pedido_tienda_id="17", cliente="MELCIOR",
    )

    assert respuesta.status_code == 303
    for guardado in (direccion_guardada, solicitud_guardada):
        assert guardado["origen_plataforma"] == "shopify"
        assert guardado["origen_dominio"] == "pesca-jacks.myshopify.com"
        assert guardado["origen_pedido_externo_id"] == "123"
    assert len(solicitud_guardada["idempotency_key_hash"]) == 64
    assert marcado == [("MELCIOR", 17, 91)]


def test_portal_rechaza_id_oculto_ajeno_o_inexistente(monkeypatch):
    monkeypatch.setattr(portal, "get_conn", _conexion(_Cursor([None])))

    with pytest.raises(ValueError, match="no pertenece"):
        portal._origen_pedido_tienda_verificado("MELCIOR", "999")
