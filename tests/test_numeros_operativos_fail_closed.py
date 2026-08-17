"""Datos numéricos corruptos no pueden generar acciones irreversibles."""

from unittest import mock

import pytest

import servicios.cotizador as cot
import servicios.recolecciones as rec
import servicios.solicitudes_guia as sg
from servicios.parser_pedidos import _a_float


def test_parser_de_pedidos_comparte_formato_humano_para_medidas_e_importes():
    assert _a_float("5,5") == _a_float("5.5") == 5.5
    assert _a_float("100.000", importe=True) == 100_000
    assert _a_float("100,000", importe=True) == 100_000
    # Una medida con tres decimales ambiguos se deja para corrección humana.
    assert _a_float("10.000") is None


def _solicitud_con_bulto(**bulto):
    return {
        "id": 44, "cliente_id": "WAIMAO", "estado": "SOLICITADO",
        "tracking": None, "courier": "DHL", "cliente_nombre": "WAIMAO",
        "cliente_pais": "AR", "remitente_nombre": "WAIMAO",
        "remitente_contacto": "Lean", "remitente_direccion": "Calle 1",
        "remitente_ciudad": "CABA", "remitente_zip": "1000", "remitente_pais": "AR",
        "dest_nombre": "Destino", "dest_contacto": "Ana",
        "dest_direccion": "Street 1", "dest_ciudad": "Miami", "dest_zip": "33101",
        "destino_pais": "US", "cantidad": 1, "peso_kg": 1,
        "largo_cm": 10, "ancho_cm": 10, "alto_cm": 10,
        "valor_declarado_usd": 10, "producto_alias": "CARGA",
        "bultos": [{
            "cantidad": 1, "unidades_aduana": 1, "peso_kg": 1,
            "largo_cm": 10, "ancho_cm": 10, "alto_cm": 10,
            "valor_unitario_usd": 10, "descripcion_en": "SHOES",
            "hs_code": "640399", "pais_origen": "AR", **bulto,
        }],
    }


def test_guia_con_peso_cero_no_contacta_courier(monkeypatch):
    liberar = mock.Mock()
    dhl = mock.Mock()
    monkeypatch.setattr(sg, "obtener_solicitud", lambda _id: _solicitud_con_bulto(peso_kg="0"))
    monkeypatch.setattr(sg, "_reservar_para_emitir", lambda _id: True)
    monkeypatch.setattr(sg, "_liberar_reserva", liberar)
    monkeypatch.setattr("core.dhl_client.DHLClient", dhl)

    salida = sg.generar_guia_internacional(44, courier="DHL")

    assert not salida["ok"]
    assert "No llamamos al courier" in salida["error"]
    liberar.assert_called_once_with(44)
    dhl.assert_not_called()


def test_pickup_desde_guia_corrupta_no_reserva_ni_contacta_courier(monkeypatch):
    sol = _solicitud_con_bulto(peso_kg="0")
    sol.update({"tracking": "DHL123", "estado": "GUIA_LISTA"})
    monkeypatch.setattr(rec, "_ensure_tabla", lambda: None)
    monkeypatch.setattr("servicios.solicitudes_guia.obtener_solicitud_de_cliente", lambda *_: sol)
    cliente = mock.Mock()
    monkeypatch.setattr(rec, "_cliente_pickup", lambda _courier: cliente)

    salida = rec.crear("WAIMAO", "2026-08-17", "09:00", "17:00", 1, 1,
                        courier="DHL", solicitud_id=44)

    assert not salida["ok"]
    assert "No llamamos al courier" in salida["error"]
    cliente.create_pickup.assert_not_called()


@pytest.mark.parametrize(
    "cambio",
    [
        {"cantidad": ""},
        {"peso_kg": ""},
        {"largo_cm": ""},
        {"ancho_cm": "0"},
        {"alto_cm": "dato"},
        {"valor_unitario_usd": ""},
        {"unidades_aduana": "1,5"},
    ],
)
def test_recotizacion_dhl_no_inventa_datos_de_bulto(monkeypatch, cambio):
    cotizar = mock.Mock()
    monkeypatch.setattr("servicios.api_b2b.cotizar_couriers_cliente", cotizar)

    with pytest.raises(ValueError):
        sg._recotizar_dhl_antes_de_emitir(_solicitud_con_bulto(**cambio))

    cotizar.assert_not_called()


@pytest.mark.parametrize(
    "campo, valor",
    [
        ("cantidad", None),
        ("peso_kg", None),
        ("largo_cm", None),
        ("ancho_cm", 0),
        ("alto_cm", "dato"),
        ("valor_declarado_usd", None),
    ],
)
def test_recotizacion_dhl_legacy_tambien_falla_antes_del_courier(
    monkeypatch, campo, valor
):
    cotizar = mock.Mock()
    monkeypatch.setattr("servicios.api_b2b.cotizar_couriers_cliente", cotizar)
    solicitud = _solicitud_con_bulto()
    solicitud["bultos"] = []
    solicitud[campo] = valor

    with pytest.raises(ValueError):
        sg._recotizar_dhl_antes_de_emitir(solicitud)

    cotizar.assert_not_called()


def test_recotizacion_dhl_legacy_valida_conserva_cajas_como_unidades(monkeypatch):
    solicitud = _solicitud_con_bulto()
    solicitud["bultos"] = []
    solicitud["precio_tauro_ars"] = 100_000
    filas_vistas = []

    def cotizar(_cliente, _destino, filas, **_kwargs):
        filas_vistas.extend(filas)
        return {"opciones": [{
            "id": "dhl", "precio_ars": 100_000,
            "precio_usd": 100, "servicio": "Express",
        }]}

    monkeypatch.setattr("servicios.api_b2b.cotizar_couriers_cliente", cotizar)

    salida = sg._recotizar_dhl_antes_de_emitir(solicitud)

    assert salida == {"ok": True}
    assert filas_vistas[0]["cantidad"] == 1
    assert filas_vistas[0]["unidades_aduana"] == 1


@pytest.mark.parametrize(
    "bulto",
    [
        {},
        {"unidades": 1, "peso_kg": 1, "largo_cm": 10, "ancho_cm": 10,
         "alto_cm": 10},
        {"unidades": 1, "peso_kg": 1, "largo_cm": 300, "ancho_cm": 20,
         "alto_cm": 20, "valor_unitario_usd": 10},
    ],
)
def test_cotizador_multibulto_no_inventa_datos_antes_de_fedex(monkeypatch, bulto):
    fedex = mock.Mock()
    monkeypatch.setattr(cot, "_pricing_courier_cliente", lambda *_: {"tipo": "PCT", "valor": 20})
    monkeypatch.setattr(cot, "get_ruta", lambda _id: mock.Mock())
    monkeypatch.setattr(cot, "FedExClient", fedex)

    with pytest.raises(ValueError):
        cot.cotizar_bultos("WAIMAO", 20, "AR-US", [bulto])

    fedex.assert_not_called()


@pytest.mark.parametrize("cantidad", [None, "", 0, -1, "1,5"])
def test_alta_de_solicitud_no_inventa_cantidad_antes_de_la_base(
    monkeypatch, cantidad,
):
    conectar = mock.Mock()
    monkeypatch.setattr(sg, "get_conn", conectar)

    with pytest.raises(ValueError):
        sg.crear_solicitud_guia(
            cliente_id="WAIMAO", producto_alias="CARGA", cantidad=cantidad,
            destino_pais="US", dest_nombre="Destino", dest_documento="",
            dest_email="", dest_telefono="", dest_direccion="Street 1",
            dest_ciudad="Miami", dest_estado="FL", dest_zip="33101",
            peso_kg=1, largo_cm=10, ancho_cm=10, alto_cm=10,
            valor_declarado_usd=10, ruta_id="AR-US", coti_id="TEST",
            precio_tauro_ars=100000, precio_tauro_usd=100,
            remitente_pais="AR",
        )

    conectar.assert_not_called()


@pytest.mark.parametrize("campo", ["unidades_aduana", "valor_unitario_usd"])
def test_retiro_desde_guia_no_inventa_datos_aduaneros(campo):
    sol = _solicitud_con_bulto()
    sol["bultos"][0].pop(campo)

    with pytest.raises(ValueError):
        rec.datos_retiro_desde_solicitud(sol)
