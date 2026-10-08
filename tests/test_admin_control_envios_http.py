"""Contrato HTTP del control ADMIN, con servicios aislados y sin base real."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from endpoints import admin
from endpoints import admin_control_envios as http
from servicios.control_envios_admin import ControlEnvioAdminError


CLAVE = "control_admin_000000000000000001"
OTRA_CLAVE = "control_admin_000000000000000002"
REVISION = "a" * 64
RUTA_PEDIDO = "/admin/pedidos/7/control"
RUTA_CARGO = "/admin/clientes/CLIENTE_A/envios/70/control"


def _control_base() -> dict:
    return {
        "solicitud": {
            "id": 7,
            "cliente_id": "CLIENTE_A",
            "estado": "GUIA_LISTA",
            "courier": "DHL",
            "tracking": "JD014600006841",
            "tracking_estado": None,
            "cargo_pendiente": False,
        },
        "cargo": {
            "id": 70,
            "cliente_id": "CLIENTE_A",
            "estado": "ACTIVO",
            "monto_ars": Decimal("10000.00"),
            "ambito": "INTERNACIONAL",
            "nro_fc": None,
        },
        "cancelacion": {
            "habilitada": True,
            "codigo": None,
            "motivo": None,
            "modo": "CANCELACION_COMERCIAL",
        },
        "precio": {
            "habilitada": True,
            "motivo": None,
            "original_ars": Decimal("10000.00"),
            "vigente_ars": Decimal("10000.00"),
            "ajustes_ars": Decimal("0.00"),
        },
        "revision": REVISION,
        "historial": {
            "ajustes": [],
            "facturas": [],
            "aplicaciones_pago": [],
            "recolecciones_activas": [],
            "controles_previos": [],
        },
        "aviso_alcance": (
            "La cancelación quita el cargo de la cuenta del cliente. "
            "No cancela la guía ante el courier."
        ),
    }


def _detalle_base() -> dict:
    return {
        "id": 7,
        "cliente_id": "CLIENTE_A",
        "estado": "GUIA_LISTA",
        "courier": "DHL",
        "tracking": "JD014600006841",
        "dest_nombre": "Destino de prueba",
        "dest_ciudad": "Miami",
        "destino_pais": "US",
        "remitente_ciudad": "Buenos Aires",
        "remitente_pais": "AR",
        "remitente_direccion": "Calle Original 123",
        "cantidad": 2,
        "peso_kg": 5.5,
        "tracking_descripcion": None,
        "tracking_estado": None,
        "cancelacion_comercial": False,
    }


def _retiro_base() -> dict:
    return {
        "habilitada": True,
        "motivo": "",
        "solicitud_id": 7,
        "cliente_id": "CLIENTE_A",
        "courier": "DHL",
        "tracking": "JD014600006841",
        "recoleccion_activa": None,
        "origen": {
            "calle": "Calle Original 123",
            "ciudad": "Buenos Aires",
            "pais": "AR",
        },
        "cajas": {"bultos": 2, "peso_kg": 5.5, "paquetes": []},
    }


@pytest.fixture
def web(monkeypatch):
    estado = SimpleNamespace(
        control=_control_base(),
        detalle=_detalle_base(),
        retiro=_retiro_base(),
    )

    def obtener_control(**identidad):
        if identidad.get("cliente_id") not in (None, "CLIENTE_A"):
            raise ControlEnvioAdminError(
                "El envío no pertenece al cliente indicado.", codigo="NO_ENCONTRADO"
            )
        if identidad.get("solicitud_id") not in (None, 7):
            raise ControlEnvioAdminError("El envío no existe.", codigo="NO_ENCONTRADO")
        if identidad.get("envio_id") not in (None, 70):
            raise ControlEnvioAdminError("El envío no existe.", codigo="NO_ENCONTRADO")
        return deepcopy(estado.control)

    lookup = Mock(side_effect=obtener_control)
    detalle = Mock(side_effect=lambda sid: deepcopy(estado.detalle) if sid == 7 else None)
    retiro = Mock(side_effect=lambda sid: deepcopy(estado.retiro) if sid == 7 else {})
    precio = Mock(return_value={"ok": True, "sin_cambios": False})
    cancelar = Mock(return_value={"ok": True})
    programar = Mock(
        return_value={
            "ok": True,
            "id": 11,
            "confirmation_code": "PU-HTTP-1",
        }
    )
    monkeypatch.setattr(admin, "_is_auth", lambda token: token == "sesion-admin")
    monkeypatch.setattr(http.control, "obtener_control_envio_admin", lookup)
    monkeypatch.setattr(http, "obtener_solicitud", detalle)
    monkeypatch.setattr(http.retiros, "contexto_recoleccion_admin", retiro)
    monkeypatch.setattr(http.precios, "aplicar_nuevo_precio", precio)
    monkeypatch.setattr(http.control, "cancelar_envio_admin", cancelar)
    monkeypatch.setattr(http.retiros, "programar_recoleccion_admin", programar)
    for nombre in (
        "pendientes_admin",
        "alertas_guias_reemplazadas",
        "pagos_pendientes_count",
        "productos_pendientes_count",
    ):
        monkeypatch.setitem(admin.templates.env.globals, nombre, lambda: 0)

    app = FastAPI()
    app.include_router(admin.router)

    @app.middleware("http")
    async def nonce(request, call_next):
        request.state.csp_nonce = "test"
        return await call_next(request)

    client = TestClient(app, follow_redirects=False)
    client.cookies.set("admin_token", "sesion-admin")
    return SimpleNamespace(
        client=client,
        estado=estado,
        lookup=lookup,
        detalle=detalle,
        retiro=retiro,
        precio=precio,
        cancelar=cancelar,
        programar=programar,
    )


def _scope(ruta: str, accion: str, esperado: str, clave: str) -> str:
    return http._alcance(ruta, accion, REVISION, esperado, clave)


def _post(
    web,
    *,
    ruta=RUTA_PEDIDO,
    accion="precio",
    esperado="10000.00",
    clave=CLAVE,
    csrf=None,
    confirmar="1",
    extra=None,
):
    datos = {
        "accion": accion,
        "revision": REVISION,
        "precio_esperado": esperado,
        "idempotency_key": clave,
        "csrf": csrf
        if csrf is not None
        else admin._csrf_dhl(_scope(ruta, accion, esperado, clave)),
        "confirmar": confirmar,
    }
    datos.update(extra or {})
    return web.client.post(ruta, data=datos)


@pytest.mark.parametrize("metodo", ["get", "post"])
def test_autenticacion_ocurre_antes_de_lookup_o_servicios(web, metodo):
    web.client.cookies.clear()
    respuesta = getattr(web.client, metodo)(RUTA_PEDIDO)

    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/admin/login"
    web.lookup.assert_not_called()
    web.detalle.assert_not_called()
    web.retiro.assert_not_called()
    web.precio.assert_not_called()
    web.cancelar.assert_not_called()
    web.programar.assert_not_called()


@pytest.mark.parametrize(
    ("ruta", "accion", "esperado", "clave"),
    [
        (RUTA_CARGO, "precio", "10000.00", CLAVE),
        (RUTA_PEDIDO, "cancelar", "10000.00", CLAVE),
        (RUTA_PEDIDO, "precio", "10001.00", CLAVE),
        (RUTA_PEDIDO, "precio", "10000.00", OTRA_CLAVE),
    ],
)
def test_csrf_esta_aislado_por_ruta_accion_precio_y_clave(
    web, ruta, accion, esperado, clave
):
    token_de_otro_alcance = admin._csrf_dhl(
        _scope(RUTA_PEDIDO, "precio", "10000.00", CLAVE)
    )
    respuesta = _post(
        web,
        ruta=ruta,
        accion=accion,
        esperado=esperado,
        clave=clave,
        csrf=token_de_otro_alcance,
    )

    assert respuesta.status_code == 403
    web.lookup.assert_not_called()
    web.precio.assert_not_called()
    web.cancelar.assert_not_called()
    web.programar.assert_not_called()


def test_confirmacion_es_obligatoria_antes_de_consultar_o_escribir(web):
    respuesta = _post(web, confirmar="")

    assert respuesta.status_code == 400
    web.lookup.assert_not_called()
    web.precio.assert_not_called()


@pytest.mark.parametrize("metodo", ["get", "post"])
def test_cliente_forjado_en_ruta_de_cargo_devuelve_404_sin_escribir(web, metodo):
    ruta = "/admin/clientes/OTRO/envios/70/control"
    if metodo == "get":
        respuesta = web.client.get(ruta)
    else:
        respuesta = _post(
            web,
            ruta=ruta,
            accion="cancelar",
            extra={"motivo": "Cancelación con evidencia"},
        )

    assert respuesta.status_code == 404
    web.precio.assert_not_called()
    web.cancelar.assert_not_called()
    web.programar.assert_not_called()


def test_precio_parsea_formato_argentino_y_pasa_snapshot_esperado(web):
    respuesta = _post(
        web,
        extra={
            "nuevo_precio_ars": "1.234,56",
            "motivo": "Corrección comercial documentada",
        },
    )

    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == RUTA_PEDIDO + "?ok=precio"
    llamada = web.precio.call_args.kwargs
    assert llamada["cliente_id"] == "CLIENTE_A"
    assert llamada["envio_id"] == 70
    assert llamada["nuevo_precio_ars"] == Decimal("1234.56")
    assert llamada["precio_esperado_ars"] == "10000.00"
    assert llamada["idempotency_key"] == CLAVE
    assert llamada["actor"].startswith("admin:")


def test_cancelacion_pasa_revision_e_identidad_resueltas_en_servidor(web):
    respuesta = _post(
        web,
        ruta=RUTA_CARGO,
        accion="cancelar",
        extra={"motivo": "Cancelación comercial documentada"},
    )

    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == RUTA_CARGO + "?ok=cancelado"
    llamada = web.cancelar.call_args.kwargs
    assert llamada["cliente_id"] == "CLIENTE_A"
    assert llamada["envio_id"] == 70
    assert llamada["revision"] == REVISION
    assert llamada["actor"].startswith("admin:")


def test_recoleccion_usa_solo_solicitud_resuelta_e_ignora_datos_forjados(web):
    respuesta = _post(
        web,
        accion="recoleccion",
        extra={
            "fecha": "2026-10-09",
            "ready_time": "09:00",
            "close_time": "17:00",
            "instrucciones": "Tocar timbre",
            "solicitud_id": "999",
            "cliente_id": "OTRO",
            "courier": "FEDEX",
            "origen": "Dirección adulterada",
            "bultos": "99",
        },
    )

    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == RUTA_PEDIDO + "?ok=recoleccion"
    assert web.programar.call_args.kwargs == {
        "solicitud_id": 7,
        "fecha": "2026-10-09",
        "ready_time": "09:00",
        "close_time": "17:00",
        "instrucciones": "Tocar timbre",
        "actor": web.programar.call_args.kwargs["actor"],
        "idempotency_key": CLAVE,
    }
    assert web.programar.call_args.kwargs["actor"].startswith("admin:")


def test_error_esperado_devuelve_409_escapado_y_preserva_formulario(web):
    web.programar.return_value = {
        "ok": False,
        "error": '<script>alert("secreto")</script>',
    }
    respuesta = _post(
        web,
        accion="recoleccion",
        extra={
            "fecha": "2026-10-09",
            "ready_time": "10:15",
            "close_time": "18:30",
            "instrucciones": "Portón azul",
        },
    )

    assert respuesta.status_code == 409
    assert '<script>alert("secreto")</script>' not in respuesta.text
    assert "&lt;script&gt;alert" in respuesta.text
    assert 'value="2026-10-09"' in respuesta.text
    assert 'value="10:15"' in respuesta.text
    assert 'value="18:30"' in respuesta.text
    assert "Portón azul" in respuesta.text
    assert f'value="{CLAVE}"' in respuesta.text


def test_error_interno_no_filtra_detalle_en_respuesta_ni_log(web, caplog):
    web.programar.side_effect = RuntimeError("TOKEN_PRIVADO_NO_FILTRAR")
    respuesta = _post(
        web,
        accion="recoleccion",
        extra={
            "fecha": "2026-10-09",
            "ready_time": "09:00",
            "close_time": "17:00",
        },
    )

    assert respuesta.status_code == 503
    assert "TOKEN_PRIVADO_NO_FILTRAR" not in respuesta.text
    assert "TOKEN_PRIVADO_NO_FILTRAR" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_get_muestra_numero_de_recoleccion_confirmada(web):
    web.estado.retiro = {
        **_retiro_base(),
        "habilitada": False,
        "recoleccion_activa": {
            "id": 11,
            "estado": "AGENDADA",
            "confirmation_code": "PU-CONFIRMADA-77",
            "fecha": date(2026, 10, 9),
            "ready_time": "09:00",
            "close_time": "17:00",
        },
    }

    respuesta = web.client.get(RUTA_PEDIDO)

    assert respuesta.status_code == 200
    assert respuesta.headers["cache-control"] == "private, no-store"
    assert "Número de recolección" in respuesta.text
    assert "PU-CONFIRMADA-77" in respuesta.text


def test_precio_cero_se_muestra_como_cero_y_no_como_sin_cargo(web):
    web.estado.control["precio"].update(
        original_ars=Decimal("0.00"),
        vigente_ars=Decimal("0.00"),
        ajustes_ars=Decimal("0.00"),
    )
    respuesta = web.client.get(RUTA_PEDIDO)

    assert respuesta.status_code == 200
    assert "$ 0,00" in respuesta.text
    assert "Sin cargo" not in respuesta.text
    assert 'name="precio_esperado" value="0.00"' in respuesta.text


def test_guia_reemplazada_se_presenta_como_reemplazada(web):
    web.estado.control["solicitud"]["estado"] = "REEMPLAZADO"
    web.estado.detalle["estado"] = "REEMPLAZADO"
    respuesta = web.client.get(RUTA_PEDIDO)

    assert respuesta.status_code == 200
    assert "Reemplazado" in respuesta.text
    assert ">Cargo activo<" not in respuesta.text
