from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DOMINIO = "tauro-qa.myshopify.com"


class RequestFalso:
    def __init__(self, payload: dict, dominio: str = DOMINIO,
                 topic: str = "customers/data_request"):
        self._body = json.dumps(payload).encode()
        self.headers = {
            "x-shopify-hmac-sha256": "firma-valida",
            "x-shopify-shop-domain": dominio,
            "x-shopify-topic": topic,
        }

    async def body(self):
        return self._body


class CursorFalso:
    def __init__(self, respuestas=None):
        self.respuestas = list(respuestas or [])
        self.consultas = []
        self.rowcount = 1

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
    def __init__(self, cursor, falla_commit=False):
        self.cursor_falso = cursor
        self.commits = 0
        self.falla_commit = falla_commit

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self.cursor_falso

    def commit(self):
        self.commits += 1
        if self.falla_commit:
            raise RuntimeError("commit caido")


def payload_data_request(*, orders=None, email="comprador@example.com", shop_id=55,
                         request_id=9001, dominio=DOMINIO):
    return {
        "shop_id": shop_id,
        "shop_domain": dominio,
        "orders_requested": [] if orders is None else orders,
        "customer": {"id": 77, "email": email, "phone": "+54 11 0000 0000"},
        "data_request": {"id": request_id},
    }


def test_schema_gdpr_no_persiste_pii_y_es_idempotente_por_tienda():
    schema = (ROOT / "sql/schema.sql").read_text(encoding="utf-8")
    inicio = schema.index("CREATE TABLE IF NOT EXISTS shopify_gdpr_solicitudes")
    fin = schema.index(");", inicio) + 2
    tabla = schema[inicio:fin].lower()

    assert "orders_requested" in tabla
    assert "jsonb_array_length(orders_requested) <= 500" in tabla
    assert "unique (shop_id, request_id)" in tabla
    assert "customer_email" not in tabla
    assert "telefono" not in tabla
    assert "payload" not in tabla
    assert "claim_id" in tabla
    assert "message_id" in tabla
    assert "resuelto_at" in tabla


def test_normalizador_usa_email_solo_como_dato_efimero():
    from servicios.shopify_gdpr import normalizar_payload_data_request

    resultado = normalizar_payload_data_request(payload_data_request())

    assert resultado["orders_requested"] == []
    assert resultado["customer_email_memoria"] == "comprador@example.com"
    assert "phone" not in resultado
    assert "customer" not in resultado


def test_order_ids_exige_lista_numerica_y_acotada():
    from servicios.shopify_gdpr import (
        MAX_ORDER_IDS, SolicitudGDPRInvalida, normalizar_order_ids,
    )

    with pytest.raises(SolicitudGDPRInvalida):
        normalizar_order_ids({"101"}, campo="orders_requested")
    with pytest.raises(SolicitudGDPRInvalida):
        normalizar_order_ids(["101", "no-numerico"], campo="orders_to_redact")
    with pytest.raises(SolicitudGDPRInvalida):
        normalizar_order_ids(
            list(range(MAX_ORDER_IDS + 1)), campo="orders_requested",
        )


def test_data_request_vacio_resuelve_ids_por_email_y_encola_sin_pii(monkeypatch):
    from endpoints import shopify
    from servicios import shopify_gdpr

    visto = {}
    monkeypatch.setattr(shopify, "_firma_valida_app", lambda *_args: True)
    monkeypatch.setattr(
        shopify_gdpr,
        "resolver_order_ids_por_email",
        lambda dominio, email: visto.update(busqueda=(dominio, email)) or ["101", "202"],
    )
    monkeypatch.setattr(
        shopify_gdpr,
        "encolar_data_request",
        lambda **kwargs: visto.update(encolado=kwargs) or {
            "id": 8, "request_id": kwargs["request_id"], "estado": "PENDIENTE",
        },
    )

    respuesta = asyncio.run(shopify.gdpr_data_request(RequestFalso(payload_data_request())))

    assert respuesta == {"ok": True}
    assert visto["busqueda"] == (DOMINIO, "comprador@example.com")
    assert visto["encolado"]["orders_requested"] == ["101", "202"]
    assert "customer_email_memoria" not in visto["encolado"]
    assert "comprador@example.com" not in repr(visto["encolado"])


def test_resolver_email_incluye_huerfano_y_linaje_downstream(monkeypatch):
    from servicios import integraciones_tienda, shopify_gdpr

    cursor = CursorFalso([[{"pedido_externo_id": "101"}]])
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(
        shopify_gdpr, "get_conn", lambda: ConexionFalsa(cursor),
    )

    assert shopify_gdpr.resolver_order_ids_por_email(
        DOMINIO, "Comprador@Example.com",
    ) == ["101"]
    sql, params = cursor.consultas[0]
    assert "payload->'destinatario'->>'email'" in sql
    assert "FROM solicitudes_guia" in sql
    assert "FROM direcciones" in sql
    assert params == (
        DOMINIO, "comprador@example.com",
        DOMINIO, "comprador@example.com",
        DOMINIO, "comprador@example.com",
        DOMINIO, "comprador@example.com",
    )


def test_data_request_rechaza_dominio_header_distinto_antes_de_db(monkeypatch):
    from endpoints import shopify
    from servicios import shopify_gdpr

    monkeypatch.setattr(shopify, "_firma_valida_app", lambda *_args: True)
    monkeypatch.setattr(
        shopify_gdpr, "encolar_data_request",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("no debe encolar")),
    )
    respuesta = asyncio.run(shopify.gdpr_data_request(RequestFalso(
        payload_data_request(), dominio="otra.myshopify.com",
    )))

    assert respuesta.status_code == 400


def test_customer_y_shop_redact_rechazan_dominio_header_distinto(monkeypatch):
    from endpoints import shopify
    from servicios import shopify_app

    monkeypatch.setattr(shopify, "_firma_valida_app", lambda *_args: True)
    monkeypatch.setattr(
        shopify_app, "cliente_app_para_webhook", lambda *_args: "app-publica",
    )
    payload = payload_data_request()
    payload.pop("data_request")
    payload["orders_to_redact"] = ["101"]
    customer = asyncio.run(shopify.gdpr_customer_redact(RequestFalso(
        payload, dominio="otra.myshopify.com", topic="customers/redact",
    )))
    tienda = asyncio.run(shopify.gdpr_shop_redact(RequestFalso(
        {"shop_id": payload["shop_id"], "shop_domain": payload["shop_domain"]},
        dominio="otra.myshopify.com", topic="shop/redact",
    )))

    assert customer.status_code == 400
    assert tienda.status_code == 400


def test_data_request_devuelve_503_si_commit_no_se_puede_confirmar(monkeypatch):
    from endpoints import shopify
    from servicios import shopify_gdpr

    monkeypatch.setattr(shopify, "_firma_valida_app", lambda *_args: True)
    monkeypatch.setattr(
        shopify_gdpr, "encolar_data_request",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("db")),
    )
    respuesta = asyncio.run(shopify.gdpr_data_request(RequestFalso(
        payload_data_request(orders=[101]),
    )))

    assert respuesta.status_code == 503


def test_encolado_confirma_commit_y_conflicto_es_compuesto(monkeypatch):
    from servicios import shopify_gdpr

    cursor = CursorFalso([{"id": 7, "request_id": "9001", "estado": "PENDIENTE"}])
    conn = ConexionFalsa(cursor)
    monkeypatch.setattr(shopify_gdpr, "get_conn", lambda: conn)

    resultado = shopify_gdpr.encolar_data_request(
        request_id="9001", dominio=DOMINIO, shop_id="55",
        orders_requested=["101"],
    )

    assert resultado["id"] == 7
    assert conn.commits == 1
    assert "ON CONFLICT (shop_id, request_id) DO NOTHING" in cursor.consultas[0][0]


def test_encolado_rechaza_retry_con_otro_conjunto_de_ordenes(monkeypatch):
    from servicios import shopify_gdpr

    cursor = CursorFalso([
        None,
        {
            "id": 7, "request_id": "9001", "dominio": DOMINIO,
            "shop_id": "55", "orders_requested": ["101"],
            "estado": "PENDIENTE",
        },
    ])
    conn = ConexionFalsa(cursor)
    monkeypatch.setattr(shopify_gdpr, "get_conn", lambda: conn)

    with pytest.raises(shopify_gdpr.SolicitudGDPRConflictiva):
        shopify_gdpr.encolar_data_request(
            request_id="9001", dominio=DOMINIO, shop_id="55",
            orders_requested=["202"],
        )

    assert conn.commits == 0


def test_webhooks_rechazan_arrays_de_ordenes_invalidos(monkeypatch):
    from endpoints import shopify

    monkeypatch.setattr(shopify, "_firma_valida_app", lambda *_args: True)
    data_request = payload_data_request(orders={"101": True})
    respuesta_data = asyncio.run(
        shopify.gdpr_data_request(RequestFalso(data_request)),
    )

    redact = payload_data_request()
    redact.pop("data_request")
    redact["orders_to_redact"] = ["101", "invalido"]
    respuesta_redact = asyncio.run(
        shopify.gdpr_customer_redact(RequestFalso(
            redact, topic="customers/redact",
        )),
    )

    assert respuesta_data.status_code == 400
    assert respuesta_redact.status_code == 400


def test_customers_redact_vacio_resuelve_email_sin_convertirlo_en_toda_tienda(monkeypatch):
    from endpoints import shopify
    from servicios import integraciones_tienda, shopify_gdpr

    visto = {}
    monkeypatch.setattr(shopify, "_firma_valida_app", lambda *_args: True)
    monkeypatch.setattr(
        shopify_gdpr, "resolver_order_ids_por_email",
        lambda dominio, email: visto.update(busqueda=(dominio, email)) or ["303"],
    )
    monkeypatch.setattr(
        integraciones_tienda, "anonimizar_pedidos",
        lambda dominio, ids: visto.update(anonimizado=(dominio, ids)) or 1,
    )
    payload = payload_data_request()
    payload.pop("data_request")
    payload.pop("orders_requested")
    payload["orders_to_redact"] = []

    respuesta = asyncio.run(shopify.gdpr_customer_redact(RequestFalso(
        payload, topic="customers/redact",
    )))

    assert respuesta == {"ok": True}
    assert visto["busqueda"] == (DOMINIO, "comprador@example.com")
    assert visto["anonimizado"] == (DOMINIO, ["303"])


def test_worker_notifica_sin_pii_y_con_message_id_determinista(monkeypatch):
    from core.email_transport import EmailDeliveryResult, OPERATIONS_EMAIL
    from servicios import shopify_gdpr

    visto = {}
    monkeypatch.setattr(
        shopify_gdpr, "send_transactional_email",
        lambda **kwargs: visto.update(mail=kwargs) or EmailDeliveryResult(
            True, "ACCEPTED", message_id="<msg@tauro>",
        ),
    )
    monkeypatch.setattr(
        shopify_gdpr, "_marcar_notificada",
        lambda fila, message_id: visto.update(marcada=(fila["id"], message_id)) or True,
    )
    fila = {
        "id": 9, "shop_id": "55", "request_id": "9001", "dominio": DOMINIO,
        "orders_requested": ["101", "202"], "claim_id": "claim",
    }

    assert shopify_gdpr._notificar_reclamada(fila) == "NOTIFICADO"
    assert visto["mail"]["recipient"] == OPERATIONS_EMAIL
    assert visto["mail"]["dedupe_key"] == "shopify-gdpr:55:9001"
    contenido = visto["mail"]["text_body"] + visto["mail"]["html_body"]
    assert DOMINIO in contenido and "9001" in contenido and "2" in contenido
    assert "comprador@" not in contenido


def test_exportacion_incluye_guia_sin_binario_de_etiqueta(monkeypatch):
    from servicios import shopify_gdpr

    solicitud = {
        "request_id": "9001", "shop_id": "55", "dominio": DOMINIO,
        "orders_requested": ["101"], "estado": "NOTIFICADO", "creado_at": None,
        "notificado_at": None, "resuelto_at": None,
    }
    pedido = {
        "pedido_externo_id": "101", "solicitud_id": 77,
        "destinatario": {"email": "comprador@example.com"},
    }
    guia = {
        "id": 88, "dest_email": "comprador@example.com", "tiene_label": True,
    }
    direccion = {
        "id": 12, "email": "comprador@example.com",
        "origen_pedido_externo_id": "101",
    }
    envio = {
        "id": 19, "solicitud_id": 88, "monto_ars": "1200.00",
        "tiene_factura_pdf": True,
    }
    cursor = CursorFalso([solicitud, [pedido], [], [guia], [direccion], [envio]])
    monkeypatch.setattr(shopify_gdpr, "get_conn", lambda: ConexionFalsa(cursor))

    exportacion = shopify_gdpr.generar_exportacion(12)

    assert exportacion["datos_en_tauro"]["solicitudes_guia_vinculadas"] == [guia]
    assert exportacion["datos_en_tauro"]["direcciones_shopify_derivadas"] == [direccion]
    assert exportacion["datos_en_tauro"]["envios_financieros_vinculados"] == [envio]
    texto = json.dumps(exportacion, default=str)
    assert '"label_pdf":' not in texto
    assert '"factura_pdf":' not in texto
    assert '"tiene_label": true' in texto
    assert '"tiene_factura_pdf": true' in texto
    assert "registro contable y financiero" in texto
    consulta_guias = cursor.consultas[-3][0]
    assert "AS tiene_label" in consulta_guias
    assert "SELECT id, estado" in consulta_guias
    assert "origen_pedido_externo_id = ANY" in consulta_guias


def test_customer_redact_anonimiza_guia_por_linaje_aunque_falle_vinculo(monkeypatch):
    from servicios import integraciones_tienda

    cursor = CursorFalso([[], [{"id": 88}]])
    conn = ConexionFalsa(cursor)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "get_conn", lambda: conn)

    afectados = integraciones_tienda.anonimizar_pedidos(DOMINIO, ["101"])

    sql = [consulta for consulta, _params in cursor.consultas]
    update_guia = next(i for i, query in enumerate(sql) if "UPDATE solicitudes_guia" in query)
    update_envio = next(i for i, query in enumerate(sql) if "UPDATE envios" in query)
    update_recoleccion = next(i for i, query in enumerate(sql) if "UPDATE recolecciones" in query)
    delete_origen = next(i for i, query in enumerate(sql) if "DELETE FROM pedidos_huerfanos" in query)
    assert afectados == 7
    assert cursor.consultas[update_guia][1] == ([88],)
    assert cursor.consultas[update_envio][1] == ([88],)
    assert cursor.consultas[update_recoleccion][1] == ([88],)
    assert "label_pdf = NULL" in sql[update_guia]
    assert "tracking = NULL" in sql[update_guia]
    assert "tracking = NULL" in sql[update_envio]
    assert "descripcion = NULL" in sql[update_envio]
    assert "direccion = NULL" in sql[update_recoleccion]
    assert "instrucciones = NULL" in sql[update_recoleccion]
    assert "ubicacion = NULL" in sql[update_recoleccion]
    assert "error_operativo = NULL" in sql[update_recoleccion]
    assert "THEN 'CANCELADO'" in sql[update_guia]
    assert any("DELETE FROM direcciones" in query for query in sql)
    assert update_guia < update_envio < update_recoleccion < delete_origen
    assert conn.commits == 1


def test_shop_redact_anonimiza_antes_del_cascade_y_preserva_obligaciones(monkeypatch):
    from servicios import integraciones_tienda

    cursor = CursorFalso([[{"solicitud_id": 77}], [{"id": 88}]])
    conn = ConexionFalsa(cursor)
    monkeypatch.setattr(integraciones_tienda, "_ensure_tablas", lambda: None)
    monkeypatch.setattr(integraciones_tienda, "get_conn", lambda: conn)

    integraciones_tienda.borrar_datos_tienda(DOMINIO)

    sql = [consulta for consulta, _params in cursor.consultas]
    update_guia = next(i for i, query in enumerate(sql) if "UPDATE solicitudes_guia" in query)
    update_envio = next(i for i, query in enumerate(sql) if "UPDATE envios" in query)
    update_recoleccion = next(i for i, query in enumerate(sql) if "UPDATE recolecciones" in query)
    delete_tienda = next(i for i, query in enumerate(sql) if "DELETE FROM tiendas_conectadas" in query)
    assert cursor.consultas[update_guia][1] == ([77, 88],)
    assert cursor.consultas[update_envio][1] == ([77, 88],)
    assert cursor.consultas[update_recoleccion][1] == ([77, 88],)
    assert "ubicacion = NULL" in sql[update_recoleccion]
    assert "error_operativo = NULL" in sql[update_recoleccion]
    assert update_guia < update_envio < update_recoleccion < delete_tienda
    assert not any("DELETE FROM shopify_gdpr_solicitudes" in query for query in sql)
    assert conn.commits == 1


def test_retencion_resueltas_es_90_dias_y_no_toca_pendientes(monkeypatch):
    from servicios import shopify_gdpr

    cursor = CursorFalso()
    cursor.rowcount = 4
    conn = ConexionFalsa(cursor)
    monkeypatch.setattr(shopify_gdpr, "get_conn", lambda: conn)

    assert shopify_gdpr.limpiar_resueltas() == 4
    sql, params = cursor.consultas[0]
    assert "estado = 'RESUELTO'" in sql
    assert params == (90,)
    assert conn.commits == 1


def test_scheduler_y_admin_cablean_flujo_gdpr():
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    admin = (ROOT / "endpoints/admin.py").read_text(encoding="utf-8")
    template = (ROOT / "templates/admin/shopify_privacidad.html").read_text(
        encoding="utf-8",
    )

    assert "procesar_gdpr_shopify" in main
    assert "limpiar_gdpr_shopify" in main
    assert '@router.get("/shopify/privacidad"' in admin
    assert "shopify.gdpr.download" in admin
    assert "shopify.gdpr.resolve" in admin
    assert "/datos.json" in template
    assert "/resolver" in template


def test_install_fallback_no_pide_dominio_manual():
    shopify = (ROOT / "endpoints/shopify.py").read_text(encoding="utf-8")
    inicio = shopify.index('"Abrí TAURO desde tu tienda"')
    fin = shopify.index("# Reintento explícito", inicio)
    fallback = shopify[inicio:fin]

    assert "abrí TAURO Solutions desde Apps" in fallback
    assert "?shop=tutienda" not in fallback
    assert "agregá tu dominio" not in fallback
