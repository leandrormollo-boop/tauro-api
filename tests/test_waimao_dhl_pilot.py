"""Controles del piloto WAIMAO → DHL, sin llamadas ni costos reales."""
from __future__ import annotations

import os
from contextlib import contextmanager
from unittest import mock

import pytest

from core.dhl_client import DHLClient
from servicios import solicitudes_guia as sg
from servicios.recolecciones import datos_retiro_desde_solicitud


def _dhl() -> DHLClient:
    c = DHLClient()
    c.api_key, c.api_secret = "key", "secret"
    c.account_number, c.account_import = "EXPO123", "IMPO456"
    c.environment, c.configuration_error = "sandbox", None
    c.base_url = c.SANDBOX_URL
    return c


def _respuesta(json_data=None, status=200):
    r = mock.Mock(status_code=status, text="respuesta simulada")
    r.json.return_value = json_data or {
        "dispatchConfirmationNumbers": ["CN-PICKUP-1"]
    }
    return r


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


def test_un_typo_de_entorno_nunca_activa_produccion():
    with mock.patch.dict(os.environ, {
        "DHL_API_KEY": "k", "DHL_API_SECRET": "s",
        "DHL_ACCOUNT_NUMBER_EXPO": "123", "DHL_ENVIRONMENT": "sandox",
    }, clear=True), mock.patch("core.dhl_client.requests.get") as get:
        cliente = DHLClient()
        salida = cliente.get_rates(
            {"country": "AR"}, {"country": "US"}, {"peso_kg": 1}
        )

    assert cliente.base_url == cliente.SANDBOX_URL
    assert cliente.environment == "invalid"
    assert not salida["encontrado"] and "DHL_ENVIRONMENT" in salida["error"]
    get.assert_not_called()


def test_una_caja_se_cotiza_con_las_medidas_reales_del_wizard():
    productos = [{
        "productCode": "P", "productName": "DHL Express Worldwide",
        "totalPrice": [{"price": 100, "priceCurrency": "USD"}],
        "deliveryCapabilities": {"totalTransitDays": 3},
    }]
    with mock.patch("core.dhl_client.requests.get",
                    return_value=_respuesta({"products": productos})) as get:
        salida = _dhl().get_rates(
            {"country": "CN", "city": "YIWU", "postal_code": "322000"},
            {"country": "AR", "city": "CABA", "postal_code": "1000"},
            {"peso_kg": 3.9, "largo_cm": 48, "ancho_cm": 47, "alto_cm": 20},
        )

    assert salida["encontrado"]
    params = get.call_args.kwargs["params"]
    assert (params["length"], params["width"], params["height"]) == (48, 47, 20)
    assert params["accountNumber"] == "IMPO456"


def test_pickup_chino_usa_hora_local_y_cajas_exactas():
    datos = {
        "origen": {
            "nombre": "Jeff Jang", "empresa": "Yiwu Hailu Garment",
            "telefono": "15057802211", "calle": "88 Fabric Road",
            "ciudad": "YIWU", "zip": "322000", "pais": "CN",
        },
        "fecha": "2026-08-11", "ready_time": "09:30", "close_time": "17:00",
        "paquetes": [{
            "peso_kg": 3.9, "largo_cm": 48, "ancho_cm": 47,
            "alto_cm": 20, "cantidad": 2, "unidades_aduana": 8,
            "valor_unitario_usd": 15,
        }],
    }
    with mock.patch("core.dhl_client.requests.post", return_value=_respuesta()) as post:
        salida = _dhl().create_pickup(datos)

    assert salida["encontrado"]
    body = post.call_args.kwargs["json"]
    assert body["plannedPickupDateAndTime"] == "2026-08-11T09:30:00+08:00"
    assert body["accounts"][0]["number"] == "IMPO456"
    paquetes = body["shipmentDetails"][0]["packages"]
    assert paquetes == [
        {"weight": 3.9, "dimensions": {"length": 48.0, "width": 47.0, "height": 20.0}},
        {"weight": 3.9, "dimensions": {"length": 48.0, "width": 47.0, "height": 20.0}},
    ]


def test_pickup_manual_no_inventa_piezas_ni_llama_dhl():
    datos = {
        "origen": {"nombre": "WAIMAO", "telefono": "1145678900",
                   "calle": "Calle 1", "ciudad": "CABA",
                   "zip": "1000", "pais": "AR"},
        "fecha": "2026-08-11", "ready_time": "09:00", "close_time": "17:00",
        "peso_kg": 4.5, "bultos": 3,
    }
    with mock.patch("core.dhl_client.requests.post", return_value=_respuesta()) as post:
        salida = _dhl().create_pickup(datos)

    assert salida["encontrado"] is False
    assert "guía emitida" in salida["error"]
    post.assert_not_called()


def test_post_manipulado_no_expande_millones_de_cajas():
    from servicios.api_b2b import _piezas_del_catalogo

    piezas, detalle, error = _piezas_del_catalogo("WAIMAO", [{
        "cantidad": 10**9, "unidades_aduana": 1,
        "peso_kg": 1, "largo_cm": 10, "ancho_cm": 10, "alto_cm": 10,
        "descripcion_en": "SHIRTS", "valor_unitario_usd": 1,
    }])

    assert piezas == [] and detalle == []
    assert "máximo 20 cajas" in error


def test_pickup_manipulado_con_mas_de_20_bultos_no_llama_dhl():
    datos = {
        "origen": {"nombre": "WAIMAO", "telefono": "1145678900",
                   "calle": "Calle 1", "ciudad": "CABA",
                   "zip": "1000", "pais": "AR"},
        "fecha": "2026-08-11", "ready_time": "09:00", "close_time": "17:00",
        "paquetes": [{
            "cantidad": 21, "unidades_aduana": 21, "peso_kg": 1,
            "largo_cm": 10, "ancho_cm": 10, "alto_cm": 10,
            "valor_unitario_usd": 1,
        }],
    }
    with mock.patch("core.dhl_client.requests.post") as post:
        salida = _dhl().create_pickup(datos)

    assert not salida["encontrado"] and "máximo 20" in salida["error"]
    post.assert_not_called()


def test_unidades_aduaneras_fuera_de_tope_no_llaman_shipments():
    datos = {
        "shipper": {"nombre": "Proveedor", "telefono": "+86 10",
                    "calle": "88 Fabric Road", "ciudad": "YIWU",
                    "zip": "322000", "pais": "CN"},
        "recipient": {"nombre": "WAIMAO", "telefono": "+54 11",
                      "calle": "Calle 1", "ciudad": "CABA",
                      "zip": "1000", "pais": "AR"},
        "bultos": [{"cantidad": 1, "unidades_aduana": 10000,
                    "peso_kg": 1, "largo_cm": 10, "ancho_cm": 10,
                    "alto_cm": 10, "valor_unitario_usd": 1,
                    "descripcion_en": "SHIRTS", "hs_code": "620530"}],
    }
    with mock.patch("core.dhl_client.requests.post") as post:
        salida = _dhl().create_shipment(datos)

    assert not salida["encontrado"] and "entre 1 y 9999" in salida["error"]
    post.assert_not_called()


def test_retiro_ligado_a_guia_usa_el_proveedor_no_la_cuenta_general():
    salida = datos_retiro_desde_solicitud({
        "id": 77, "cliente_id": "WAIMAO", "courier": "DHL", "tracking": "123",
        "remitente_nombre": "Yiwu Hailu Garment", "remitente_contacto": "Jeff Jang",
        "remitente_telefono": "+86 10", "remitente_direccion": "88 Fabric Road",
        "remitente_ciudad": "YIWU", "remitente_estado": "Zhejiang",
        "remitente_zip": "322000", "remitente_pais": "CN",
        "bultos": [{"cantidad": 2, "unidades_aduana": 8,
                    "valor_unitario_usd": 15, "peso_kg": 3.9,
                    "largo_cm": 48, "ancho_cm": 47, "alto_cm": 20}],
    })

    assert salida["origen"]["empresa"] == "Yiwu Hailu Garment"
    assert salida["origen"]["calle"] == "88 Fabric Road"
    assert salida["origen"]["pais"] == "CN"
    assert salida["courier"] == "DHL"
    assert salida["bultos"] == 2 and salida["peso_kg"] == 7.8


def test_retiro_legacy_preserva_totales_no_divisibles_hasta_dhl():
    retiro = datos_retiro_desde_solicitud({
        "id": 78, "cliente_id": "WAIMAO", "courier": "DHL", "tracking": "124",
        "remitente_nombre": "WAIMAO", "remitente_contacto": "Lean",
        "remitente_telefono": "1145678900", "remitente_direccion": "Calle 1",
        "remitente_ciudad": "CABA", "remitente_zip": "1000",
        "remitente_pais": "AR", "cantidad": 3, "peso_kg": 1,
        "largo_cm": 10, "ancho_cm": 20, "alto_cm": 30,
        "valor_declarado_usd": 100, "bultos": [],
    })

    assert [p["peso_kg"] for p in retiro["paquetes"]] == [0.334, 0.333, 0.333]
    assert [p["valor_unitario_usd"] for p in retiro["paquetes"]] == [33.34, 33.33, 33.33]
    assert sum(p["peso_kg"] for p in retiro["paquetes"]) == 1
    assert sum(p["valor_unitario_usd"] for p in retiro["paquetes"]) == 100

    datos = {
        "origen": retiro["origen"], "fecha": "2026-08-18",
        "ready_time": "09:00", "close_time": "17:00",
        "paquetes": retiro["paquetes"],
    }
    with mock.patch("core.dhl_client.requests.post", return_value=_respuesta()) as post:
        salida = _dhl().create_pickup(datos)

    assert salida["encontrado"] is True
    detalle = post.call_args.kwargs["json"]["shipmentDetails"][0]
    assert sum(p["weight"] for p in detalle["packages"]) == 1
    assert detalle["declaredValue"] == 100


def test_retiro_legacy_rechaza_cantidad_gigante_antes_de_crear_listas():
    with pytest.raises(ValueError, match="máximo es 20"):
        datos_retiro_desde_solicitud({
            "id": 79, "cliente_id": "WAIMAO", "courier": "DHL",
            "tracking": "125", "remitente_pais": "AR",
            "cantidad": 10**9, "peso_kg": 10**9,
            "largo_cm": 10, "ancho_cm": 20, "alto_cm": 30,
            "valor_declarado_usd": 10**9, "bultos": [],
        })


def test_timeout_de_pickup_queda_marcado_como_incierto():
    datos = {
        "origen": {"nombre": "WAIMAO", "telefono": "1145678900",
                   "calle": "Calle 1", "ciudad": "CABA",
                   "zip": "1000", "pais": "AR"},
        "fecha": "2026-08-11", "ready_time": "09:00", "close_time": "17:00",
        "paquetes": [{
            "cantidad": 1, "unidades_aduana": 1, "peso_kg": 1,
            "largo_cm": 10, "ancho_cm": 10, "alto_cm": 10,
            "valor_unitario_usd": 1,
        }],
    }
    with mock.patch("core.dhl_client.requests.post", side_effect=TimeoutError("timeout")):
        salida = _dhl().create_pickup(datos)

    assert salida["incierto"] is True
    assert salida["message_reference"].startswith("tauro-pickup-")
    assert "Verificá en MyDHL" in salida["error"]


def test_timeout_de_emision_bloquea_reintento_y_conserva_referencia():
    sol = {
        "id": 8, "cliente_id": "WAIMAO", "estado": "SOLICITADO", "tracking": None,
        "courier": "DHL", "cliente_nombre": "WAIMAO", "cliente_pais": "AR",
        "remitente_nombre": "Yiwu Hailu Garment", "remitente_contacto": "Jeff Jang",
        "remitente_documento": "91330782MA2DCHET04", "remitente_email": "jeff@hailu.cn",
        "remitente_telefono": "+86 10", "remitente_direccion": "88 Fabric Road",
        "remitente_ciudad": "YIWU", "remitente_zip": "322000", "remitente_pais": "CN",
        "dest_nombre": "WAIMAO", "dest_contacto": "Ana", "dest_documento": "30-123",
        "dest_email": "ops@waimao.com", "dest_telefono": "+54 11",
        "dest_direccion": "Calle 1", "dest_ciudad": "CABA", "dest_zip": "1000",
        "destino_pais": "AR", "cantidad": 1, "peso_kg": 3.9,
        "largo_cm": 48, "ancho_cm": 47, "alto_cm": 20,
        "valor_declarado_usd": 120, "producto_alias": "CARGA",
        "bultos": [{"cantidad": 1, "unidades_aduana": 8, "peso_kg": 3.9,
                    "largo_cm": 48, "ancho_cm": 47, "alto_cm": 20,
                    "valor_unitario_usd": 15, "descripcion_en": "SHIRTS",
                    "hs_code": "620530", "pais_origen": "CN"}],
    }
    cliente = mock.Mock()
    cliente.return_value.create_shipment.return_value = {
        "encontrado": False, "incierto": True,
        "message_reference": "tauro-ship-ref", "error": "timeout",
    }
    with mock.patch.object(sg, "obtener_solicitud", return_value=sol), \
         mock.patch.object(sg, "_reservar_para_emitir", return_value=True), \
         mock.patch.object(sg, "_persistir_referencia_courier", return_value=True), \
         mock.patch.object(sg, "_marcar_verificacion_courier") as marcar, \
         mock.patch.object(sg, "_liberar_reserva") as liberar, \
         mock.patch("servicios.catalogo.get_producto", return_value=None), \
         mock.patch("core.dhl_client.DHLClient", cliente):
        salida = sg.generar_guia_internacional(8, courier="DHL")

    assert not salida["ok"] and "no vuelvas a emitir" in salida["error"]
    marcar.assert_called_once()
    assert marcar.call_args.args[1]["message_reference"] == "tauro-ship-ref"
    liberar.assert_not_called()


def test_despachador_entrega_identidad_aduanera_y_contactos_a_dhl():
    sol = {
        "id": 9, "cliente_id": "WAIMAO", "estado": "SOLICITADO", "tracking": None,
        "courier": "DHL", "cliente_nombre": "WAIMAO", "cliente_pais": "AR",
        "remitente_nombre": "Yiwu Hailu Garment", "remitente_contacto": "Jeff Jang",
        "remitente_documento": "CN-TAX-9", "remitente_email": "jeff@hailu.cn",
        "remitente_telefono": "+86 10", "remitente_direccion": "88 Fabric Road",
        "remitente_ciudad": "YIWU", "remitente_zip": "322000", "remitente_pais": "CN",
        "dest_nombre": "WAIMAO", "dest_contacto": "Ana", "dest_documento": "AR-TAX-3",
        "dest_email": "ops@waimao.com", "dest_telefono": "+54 11",
        "dest_direccion": "Calle 1", "dest_ciudad": "CABA", "dest_zip": "1000",
        "destino_pais": "AR", "cantidad": 1, "peso_kg": 3.9,
        "largo_cm": 48, "ancho_cm": 47, "alto_cm": 20,
        "valor_declarado_usd": 120, "producto_alias": "CARGA",
        "bultos": [{"cantidad": 1, "unidades_aduana": 8, "peso_kg": 3.9,
                    "largo_cm": 48, "ancho_cm": 47, "alto_cm": 20,
                    "valor_unitario_usd": 15, "descripcion_en": "SHIRTS",
                    "hs_code": "620530", "pais_origen": "CN"}],
    }
    api = mock.Mock()
    api.return_value.create_shipment.return_value = {
        "encontrado": True, "tracking": "DHL123", "label_pdf": b"%PDF",
    }
    with mock.patch.object(sg, "obtener_solicitud", return_value=sol), \
         mock.patch.object(sg, "_reservar_para_emitir", return_value=True), \
         mock.patch.object(sg, "_persistir_referencia_courier", return_value=True), \
         mock.patch.object(sg, "guardar_guia_generada"), \
         mock.patch("servicios.catalogo.get_producto", return_value=None), \
         mock.patch("core.dhl_client.DHLClient", api):
        assert sg.generar_guia_internacional(9, courier="DHL")["ok"]

    datos = api.return_value.create_shipment.call_args.args[0]
    assert datos["shipper"]["documento"] == "CN-TAX-9"
    assert datos["shipper"]["email"] == "jeff@hailu.cn"
    assert datos["recipient"]["documento"] == "AR-TAX-3"
    assert datos["recipient"]["email"] == "ops@waimao.com"
    assert datos["message_reference"].startswith("tauro-dhl-ship-9-")


def test_markup_waimao_es_exactamente_95000_y_no_doble():
    from servicios.pricing import aplicar_pricing

    precio = aplicar_pricing(
        costo_usd=100, costo_ars=100_000, dolar=1_000,
        pricing={"tipo": "FIJO_ARS", "valor": 95_000},
    )
    assert precio["precio_final_ars"] == 195_000
    assert precio["precio_final_ars"] - 100_000 == 95_000


def test_si_dhl_cambia_la_tarifa_no_crea_hasta_nueva_confirmacion(monkeypatch):
    import endpoints.portal_cliente as pc

    monkeypatch.setattr(pc.templates, "TemplateResponse", lambda **kw: kw)
    monkeypatch.setattr(pc, "get_productos", lambda cliente: [])
    monkeypatch.setattr(pc, "_paises_con_nacional",
                        lambda: [("AR", "Argentina"), ("CN", "China"), ("MX", "México")])
    monkeypatch.setattr(pc, "listar_direcciones", lambda *args: [])
    monkeypatch.setattr(pc, "obtener_remitente_para_envio", lambda *args: None)
    monkeypatch.setattr(pc, "tax_paga_cliente", lambda cliente: "DESTINATARIO")
    monkeypatch.setattr(pc, "courier_default_cliente", lambda cliente: "dhl")
    monkeypatch.setattr(pc, "cotizar_couriers_cliente", lambda *args, **kwargs: {
        "encontrado": True,
        "opciones": [{"id": "dhl", "nombre": "DHL Express",
                      "precio_ars": 195_000, "precio_usd": 195}],
        "ruta_id": None, "coti_id": None, "peso_total_kg": 3.9,
        "piezas_total": 1,
        "bultos": [{"producto_alias": "CARGA", "cantidad": 1,
                    "unidades_aduana": 8,
                    "peso_kg": 3.9, "largo_cm": 48, "ancho_cm": 47,
                    "alto_cm": 20, "valor_unitario_usd": 15,
                    "descripcion_en": "SHIRTS", "hs_code": "620530",
                    "pais_origen": "CN"}],
    })
    monkeypatch.setattr(
        pc, "crear_solicitud_guia",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("no debe crear")),
    )

    respuesta = pc.envio_nuevo_post(
        _request(), destino_pais="MX",
        bulto_producto=[""], bulto_cantidad=["1"], bulto_unidades_aduana=["8"],
        bulto_peso=["3.9"],
        bulto_largo=["48"], bulto_ancho=["47"], bulto_alto=["20"],
        bulto_desc_en=["SHIRTS"], bulto_valor_usd=["15"],
        bulto_hs=["620530"], bulto_pais_fab=["CN"], producto_alias="", cantidad=1,
        intl_courier="dhl", precio_cotizado_ars="190000", tax_paga="DESTINATARIO",
        remitente_id="",
        rem_nombre="Yiwu Hailu Garment", rem_contacto="Jeff Jang",
        rem_documento="CN-TAX-9", rem_email="jeff@hailu.cn", rem_telefono="+86 10",
        rem_direccion="88 Fabric Road", rem_ciudad="YIWU", rem_estado="Zhejiang",
        rem_zip="322000", rem_pais="CN", destinatario_id="", dest_nombre="WAIMAO",
        dest_contacto="Ana", dest_documento="AR-TAX", dest_email="ops@waimao.com",
        dest_telefono="+54 11", dest_direccion="Calle 1", dest_ciudad="CABA",
        dest_estado="C", dest_zip="1000", dest_alias="", guardar_destinatario=None,
        precio_cliente_final_ars="", observaciones="", pedido_tienda_id="",
        cliente="WAIMAO",
    )

    assert "cambió de $ 190.000 a $ 195.000" in respuesta["context"]["error"]
    assert "no creamos ni cobramos nada" in respuesta["context"]["error"]
    assert respuesta["context"]["form"]["initial_step"] == 4
    assert respuesta["context"]["form"]["bultos"][0]["cantidad"] == 1
    assert respuesta["context"]["form"]["bultos"][0]["unidades_aduana"] == 8
    assert respuesta["context"]["form"]["bultos"][0]["valor_unitario_usd"] == 15


def test_el_limite_suma_deuda_reservas_y_la_nueva_guia(monkeypatch):
    consultas = []

    class Cursor:
        def __init__(self):
            self.respuestas = iter([
                {"cliente_id": "WAIMAO", "tracking": None, "estado": "SOLICITADO",
                 "precio_tauro_ars": 95_000, "activo": True, "puede_emitir": True,
                 "courier": "DHL", "ambito": "INTERNACIONAL",
                 "remitente_pais": "AR", "destino_pais": "US",
                 "tope_deuda_ars": 250_000},
                {"deuda": 100_000, "reservado": 80_000},
            ])

        def __enter__(self): return self
        def __exit__(self, *args): return None
        def execute(self, sql, params=None): consultas.append(" ".join(sql.split()))
        def fetchone(self): return next(self.respuestas)

    class Conn:
        def __init__(self): self.cur = Cursor()
        def cursor(self): return self.cur
        def commit(self): pass

    @contextmanager
    def conexion():
        yield Conn()

    monkeypatch.setattr(sg, "get_conn", conexion)
    monkeypatch.setattr(
        "servicios.configuracion_couriers_cliente.estado_integracion",
        lambda _courier: {"operativa": True},
    )
    salida = sg._reservar_credito_cliente(9, "WAIMAO")

    assert not salida["ok"]
    assert "275.000" in salida["error"]
    assert "FOR UPDATE OF c, s" in consultas[0]
    assert "cargo_pendiente=TRUE" in consultas[1]
    assert not any("SET estado='EMITIENDO'" in q for q in consultas)


def test_recoleccion_directa_de_waimao_arranca_en_dhl(monkeypatch):
    import endpoints.portal_cliente as pc
    import servicios.recolecciones as rec

    monkeypatch.setattr(pc.templates, "TemplateResponse", lambda **kw: kw)
    monkeypatch.setattr(rec, "listar", lambda cliente: [])
    monkeypatch.setattr(pc, "obtener_remitente_para_envio", lambda *args: {
        "nombre": "WAIMAO", "direccion": "Calle 1", "ciudad": "CABA",
        "cp": "1000", "pais": "AR",
    })
    monkeypatch.setattr(pc, "courier_default_cliente", lambda cliente: "dhl")

    respuesta = pc.recolecciones_view(_request("/portal/recolecciones"), cliente="WAIMAO")

    assert respuesta["context"]["courier_default"] == "DHL"


def test_retiro_de_envio_ajeno_no_llega_al_courier(monkeypatch):
    import servicios.recolecciones as rec

    monkeypatch.setattr(rec, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(rec, "_cliente_pickup",
                        lambda courier: (_ for _ in ()).throw(AssertionError("no llamar")))
    monkeypatch.setattr(sg, "obtener_solicitud_de_cliente", lambda sid, cliente: None)

    salida = rec.crear(
        "WAIMAO", "2026-08-11", "09:00", "17:00", 1, 1,
        courier="DHL", solicitud_id=999,
    )

    assert not salida["ok"] and "no es de tu cuenta" in salida["error"]


def test_cambio_de_tarifa_antes_de_emitir_libera_sin_llamar_shipments(monkeypatch):
    monkeypatch.setattr(sg, "_reservar_credito_cliente", lambda *args: {"ok": True})
    monkeypatch.setattr(sg, "obtener_solicitud",
                        lambda sid: {"id": sid, "cliente_id": "WAIMAO", "courier": "DHL"})
    monkeypatch.setattr(sg, "_recotizar_dhl_antes_de_emitir", lambda sol: {
        "ok": False, "precio_cambio": True,
        "error": "La tarifa DHL cambió; volvé a emitir.",
    })
    liberar = mock.Mock()
    monkeypatch.setattr(sg, "_liberar_reserva", liberar)
    monkeypatch.setattr(
        sg, "generar_guia",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no emitir")),
    )

    salida = sg.emitir_guia_como_cliente(12, "WAIMAO")

    assert not salida["ok"] and salida["precio_cambio"]
    liberar.assert_called_once_with(12)
