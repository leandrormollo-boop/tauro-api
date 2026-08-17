"""El ámbito viaja desde la ruta hasta la solicitud y su cargo."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from servicios import (
    api_b2b, catalogo, cuenta_corriente, direcciones, integraciones_tienda,
    recolecciones, solicitud_automatica, solicitudes_guia,
)
from servicios.couriers_urls import ambito_envio


class _Cursor:
    def __init__(self, respuestas):
        self.respuestas = iter(respuestas)
        self.ejecutadas = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        parametros = tuple(params or ())
        assert sql.count("%s") == len(parametros)
        self.ejecutadas.append((sql, parametros))

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


def _crear(monkeypatch, origen, destino):
    cursor = _Cursor([{"id": 9}])
    monkeypatch.setattr(solicitudes_guia, "get_conn", _conexion(cursor))
    resultado = solicitudes_guia.crear_solicitud_guia(
        cliente_id="TEST", producto_alias="CARGA", cantidad=1,
        remitente_pais=origen, destino_pais=destino,
        dest_nombre="Persona", dest_documento="", dest_email="",
        dest_telefono="", dest_direccion="Calle 1", dest_ciudad="Ciudad",
        dest_estado="", dest_zip="1000", peso_kg=1, largo_cm=10,
        ancho_cm=10, alto_cm=10, valor_declarado_usd=10,
        ruta_id="R", coti_id="C", precio_tauro_ars=100,
        precio_tauro_usd=1,
    )
    return resultado, cursor.ejecutadas[0]


def test_la_ruta_define_el_ambito_aunque_el_courier_diga_otra_cosa():
    assert ambito_envio({
        "courier": "DHL", "remitente_pais": "AR", "destino_pais": "AR",
    }) == "nacional"
    assert ambito_envio({
        "courier": "OCA", "remitente_pais": "US", "destino_pais": "AR",
    }) == "internacional"
    assert ambito_envio({"courier": "DHL"}) == "sin_clasificar"
    assert ambito_envio({
        "ambito": "INTERNACIONAL", "remitente_pais": "AR",
        "destino_pais": "AR", "courier": "DHL",
    }) == "sin_clasificar"


def test_crear_solicitud_persiste_nacional_o_internacional(monkeypatch):
    _, (sql_nacional, params_nacional) = _crear(monkeypatch, "AR", "AR")
    _, (sql_internacional, params_internacional) = _crear(monkeypatch, "CN", "AR")

    assert "ambito" in sql_nacional
    assert "NACIONAL" in params_nacional
    assert "INTERNACIONAL" in params_internacional
    assert sql_nacional == sql_internacional


@pytest.mark.parametrize(
    "origen,destino,origen_iso,destino_iso,ambito",
    [
        ("Argentina", "Estados Unidos", "AR", "US", "INTERNACIONAL"),
        ("EE.UU.", "Argentina", "US", "AR", "INTERNACIONAL"),
        ("argentina", "ARGENTINA", "AR", "AR", "NACIONAL"),
    ],
)
def test_crear_solicitud_normaliza_paises_sin_truncarlos(
    monkeypatch, origen, destino, origen_iso, destino_iso, ambito,
):
    _, (_sql, params) = _crear(monkeypatch, origen, destino)

    assert params[3] == destino_iso
    assert params[13] == origen_iso
    assert params[14] == ambito


def test_estados_unidos_jamas_se_persiste_como_espana(monkeypatch):
    _, (_sql, params) = _crear(monkeypatch, "Argentina", "ESTADOS UNIDOS")

    assert params[3] == "US"
    assert params[3] != "ES"


def test_validacion_pre_emision_normaliza_nombres_completos():
    assert solicitudes_guia._error_ambito_no_emitible({
        "ambito": "INTERNACIONAL",
        "remitente_pais": "United States",
        "destino_pais": "España",
    }) is None


def test_pais_desconocido_bloquea_antes_de_reservar_o_llamar_courier(monkeypatch):
    solicitud = {
        "id": 91, "estado": "SOLICITADO", "tracking": None,
        "ambito": "INTERNACIONAL", "courier": "DHL",
        "remitente_pais": "Argentina",
        "destino_pais": "Estados Unidos del Sur",
    }
    monkeypatch.setattr(
        solicitudes_guia, "obtener_solicitud", lambda _solicitud_id: solicitud,
    )

    def no_llamar(*_args, **_kwargs):
        raise AssertionError("no debe reservar ni llamar al courier")

    monkeypatch.setattr(solicitudes_guia, "_reservar_para_emitir", no_llamar)

    resultado = solicitudes_guia.generar_guia_internacional(91, courier="DHL")

    assert resultado["ok"] is False
    assert "países reconocidos" in resultado["error"]


def test_cargo_automatico_copia_el_ambito_de_la_solicitud(monkeypatch):
    cursor = _Cursor([
        {
            "cliente_id": "TEST", "precio_tauro_ars": 15000,
            "tracking": "123", "courier": "DHL", "ambito": "INTERNACIONAL",
            "producto_alias": "CARGA", "destino_pais": "US",
        },
        {"id": 44},
    ])
    monkeypatch.setattr(cuenta_corriente, "get_conn", _conexion(cursor))

    assert cuenta_corriente.cargar_guia_emitida(9) is True
    sql, params = cursor.ejecutadas[-1]
    assert "solicitud_id, ambito" in sql
    assert params[-1] == "INTERNACIONAL"


def test_cargo_sin_ruta_suficiente_no_se_inserta(monkeypatch):
    cursor = _Cursor([{
        "cliente_id": "TEST", "precio_tauro_ars": 15000,
        "tracking": "123", "courier": "DHL", "ambito": None,
        "producto_alias": "CARGA", "remitente_pais": None,
        "destino_pais": None,
    }])
    monkeypatch.setattr(cuenta_corriente, "get_conn", _conexion(cursor))

    with pytest.raises(ValueError, match="requiere revisión"):
        cuenta_corriente.cargar_guia_emitida(9)
    assert len(cursor.ejecutadas) == 1


def test_cargo_preexistente_en_otro_ambito_no_se_pisa(monkeypatch):
    cursor = _Cursor([
        {
            "cliente_id": "TEST", "precio_tauro_ars": 15000,
            "tracking": "123", "courier": "DHL", "ambito": "INTERNACIONAL",
            "producto_alias": "CARGA", "remitente_pais": "AR",
            "destino_pais": "US",
        },
        None,
    ])
    monkeypatch.setattr(cuenta_corriente, "get_conn", _conexion(cursor))

    with pytest.raises(ValueError, match="otro ámbito"):
        cuenta_corriente.cargar_guia_emitida(9)


def test_origen_o_destino_inventado_falla_antes_del_insert(monkeypatch):
    with pytest.raises(ValueError, match="países válidos"):
        _crear(monkeypatch, "ZZ", "AR")

    with pytest.raises(ValueError, match="países válidos"):
        _crear(monkeypatch, "Argentina", "Estados Unidos del Sur")


@pytest.mark.parametrize(
    "solicitud,mensaje",
    [
        ({"id": 7, "courier": "DHL", "ambito": "NACIONAL",
          "remitente_pais": "AR", "destino_pais": "AR"}, "nacionales"),
        ({"id": 8, "courier": "DHL", "ambito": "INTERNACIONAL",
          "remitente_pais": "AR", "destino_pais": "AR"}, "revisión"),
    ],
)
def test_despachador_bloquea_ambito_no_emitible_antes_del_courier(
    monkeypatch, solicitud, mensaje,
):
    monkeypatch.setattr(solicitudes_guia, "obtener_solicitud", lambda _id: solicitud)

    def no_llamar(*_args, **_kwargs):
        raise AssertionError("no debe llamar a una API de courier")

    monkeypatch.setattr(solicitudes_guia, "generar_guia_internacional", no_llamar)
    resultado = solicitudes_guia.generar_guia(solicitud["id"])

    assert resultado["ok"] is False
    assert mensaje in resultado["error"]


def test_reserva_cliente_bloquea_nacional_antes_de_tomar_credito(monkeypatch):
    cursor = _Cursor([{
        "cliente_id": "TEST", "tracking": None, "estado": "SOLICITADO",
        "precio_tauro_ars": 15000, "courier": "DHL", "ambito": "NACIONAL",
        "remitente_pais": "AR", "destino_pais": "AR", "activo": True,
        "puede_emitir": True, "tope_deuda_ars": None,
    }])
    monkeypatch.setattr(solicitudes_guia, "get_conn", _conexion(cursor))

    resultado = solicitudes_guia._reservar_credito_cliente(9, "TEST")

    assert resultado["ok"] is False
    assert "nacionales" in resultado["error"]
    assert len(cursor.ejecutadas) == 1


def test_cotizador_b2b_legado_no_cotiza_argentina_como_internacional(monkeypatch):
    def no_buscar_producto(*_args, **_kwargs):
        raise AssertionError("el bloqueo nacional debe ocurrir antes del catálogo/courier")

    monkeypatch.setattr(api_b2b, "get_producto", no_buscar_producto)

    simple = api_b2b.obtener_precio_envio(
        "TEST", "SKU", "AR", origen_pais="AR",
    )
    multi = api_b2b.obtener_precio_envio_multi(
        "TEST", "AR", [{"producto": "SKU"}], origen_pais="AR",
    )
    multicourier = api_b2b.cotizar_couriers_cliente(
        "TEST", "AR", [{"producto": "SKU"}],
        origen_real={"pais": "AR"},
    )

    assert simple["encontrado"] is False
    assert multi["encontrado"] is False
    assert multicourier["encontrado"] is False
    assert "nacional_no_disponible" in simple["motivo"]
    assert "nacional_no_disponible" in multi["motivo"]
    assert "nacional_no_disponible" in multicourier["motivo"]


def test_cotizador_b2b_usa_el_par_exacto_y_no_bloquea_importaciones(monkeypatch):
    rutas = [
        SimpleNamespace(
            ruta_id="US-AR", origen_pais="US", destino_pais="AR",
        ),
        SimpleNamespace(
            ruta_id="CN-AR", origen_pais="CN", destino_pais="AR",
        ),
    ]
    monkeypatch.setattr(api_b2b, "get_rutas_activas", lambda: rutas)

    assert api_b2b.buscar_ruta_para_destino("AR", "CN").ruta_id == "CN-AR"
    assert api_b2b.buscar_ruta_para_destino("AR", "AR") is None

    # Llegar a la validación de producto demuestra que CN→AR no fue
    # interceptado por la guarda nacional (AR→AR).
    monkeypatch.setattr(api_b2b, "get_producto", lambda *_args: None)
    resultado = api_b2b.obtener_precio_envio_multi(
        "TEST", "AR",
        [{"producto": "SKU", "cantidad": 1, "unidades_aduana": 1}],
        origen_pais="CN",
    )
    assert resultado["motivo"] == "producto_no_encontrado: SKU"


def test_tienda_importacion_hacia_argentina_propaga_el_origen(monkeypatch):
    pedido = {
        "id": 21, "cliente_id": "TEST", "estado": "PENDIENTE",
        "numero": "ORD-21", "solicitud_id": None,
        "destinatario": {
            "pais": "AR", "nombre": "Destino", "direccion": "Calle 1",
            "direccion2": "", "ciudad": "CABA", "estado": "C",
            "cp": "1000", "email": "destino@example.com", "telefono": "1",
        },
        "items": [{"sku": "SKU", "cantidad": 1}],
        "valor_total": 10, "moneda": "USD",
    }
    cursor = _Cursor([pedido])
    monkeypatch.setattr(solicitud_automatica, "get_conn", _conexion(cursor))
    monkeypatch.setattr(
        catalogo, "get_productos",
        lambda _cliente: [SimpleNamespace(alias_interno="SKU")],
    )
    monkeypatch.setattr(
        direcciones, "obtener_remitente_para_envio",
        lambda *_args: {
            "pais": "China", "nombre": "Proveedor", "direccion": "Road 1",
            "ciudad": "Shanghai", "cp": "200000", "email": "", "telefono": "",
        },
    )
    visto = {}

    def cotizar(_cliente, _pais, _filas, **kwargs):
        visto["filas"] = _filas
        visto.update(kwargs)
        return {
            "encontrado": True, "bultos": [{
                "producto_alias": "SKU", "cantidad": 1, "peso_kg": 1,
                "largo_cm": 10, "ancho_cm": 10, "alto_cm": 10,
            }],
            "peso_total_kg": 1, "valor_total_usd": 10,
            "ruta_id": "CN-AR", "coti_id": "C1",
            "precio_ars": 1000, "precio_usd": 1,
        }

    monkeypatch.setattr(api_b2b, "obtener_precio_envio_multi", cotizar)
    monkeypatch.setattr(
        solicitudes_guia, "crear_solicitud_guia",
        lambda **_kwargs: {"id": 99},
    )
    monkeypatch.setattr(integraciones_tienda, "marcar_convertido", lambda *_args, **_kwargs: None)

    resultado = solicitud_automatica.crear_desde_pedido(21)

    assert resultado["ok"] is True
    assert visto["origen_pais"] == "CN"
    assert visto["filas"] == [{
        "producto": "SKU", "cantidad": 1, "unidades_aduana": 1,
    }]


@pytest.mark.parametrize("cantidad", [None, "", 0, "0.500"])
def test_tienda_no_inventa_cantidad_ni_llega_al_cotizador(monkeypatch, cantidad):
    pedido = {
        "id": 22, "cliente_id": "TEST", "estado": "PENDIENTE",
        "numero": "ORD-22", "solicitud_id": None,
        "destinatario": {
            "pais": "US", "nombre": "Destino", "direccion": "Street 1",
            "direccion2": "", "ciudad": "Miami", "estado": "FL",
            "cp": "33101", "email": "destino@example.com", "telefono": "1",
        },
        "items": [{"sku": "SKU", "cantidad": cantidad}],
        "valor_total": 10, "moneda": "USD",
    }
    cursor = _Cursor([pedido])
    monkeypatch.setattr(solicitud_automatica, "get_conn", _conexion(cursor))
    monkeypatch.setattr(
        catalogo, "get_productos",
        lambda _cliente: [SimpleNamespace(alias_interno="SKU")],
    )

    def no_cotizar(*_args, **_kwargs):
        pytest.fail("Una cantidad inválida no debe llegar al cotizador")

    monkeypatch.setattr(api_b2b, "obtener_precio_envio_multi", no_cotizar)

    resultado = solicitud_automatica.crear_desde_pedido(22)

    assert resultado["ok"] is False
    assert "Cantidad del producto SKU" in resultado["motivo"]


def test_recoleccion_nacional_falla_antes_de_llamar_al_courier(monkeypatch):
    monkeypatch.setattr(recolecciones, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(
        solicitudes_guia,
        "obtener_solicitud_de_cliente",
        lambda *_args: {
            "id": 9, "tracking": "GUIA", "courier": "DHL",
            "ambito": "NACIONAL", "remitente_pais": "AR",
            "destino_pais": "AR",
        },
    )

    def no_llamar(*_args, **_kwargs):
        raise AssertionError("no debe instanciar ni llamar al courier")

    monkeypatch.setattr(recolecciones, "_cliente_pickup", no_llamar)
    resultado = recolecciones.crear(
        "TEST", "2026-08-17", "09:00", "17:00",
        1, 1.0, courier="DHL", solicitud_id=9,
    )

    assert resultado["ok"] is False
    assert "Andreani y OCA" in resultado["error"]


def test_schema_y_api_propagan_ambito_en_el_orden_seguro():
    raiz = Path(__file__).resolve().parent.parent
    schema = (raiz / "sql" / "schema.sql").read_text(encoding="utf-8")
    main = (raiz / "main.py").read_text(encoding="utf-8")

    alter = "solicitudes_guia ADD COLUMN IF NOT EXISTS ambito"
    indice = "idx_solicitudes_cliente_ambito_fecha"
    assert schema.index(alter) < schema.index(indice)
    assert "remitente_pais=origen_iso" in main
    assert "origen_pais=origen_iso" in main
    assert "APIs directas de Andreani y OCA" in main


def test_detalle_nacional_no_promete_una_guia_que_aun_no_puede_emitir():
    raiz = Path(__file__).resolve().parent.parent
    detalle = (raiz / "templates" / "portal" / "envio_detalle.html").read_text(
        encoding="utf-8"
    )

    assert "ambito == 'nacional'" in detalle
    assert "La emisión nacional se habilitará" in detalle
    assert "No se emitió ni se generó ningún cargo" in detalle
    assert "se habilitará con las conexiones directas de OCA y Andreani" in detalle
