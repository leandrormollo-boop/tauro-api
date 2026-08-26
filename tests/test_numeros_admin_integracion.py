"""Integracion HTTP de importes localizados en factura y pago del admin."""

import asyncio
import inspect
from types import SimpleNamespace

import pytest

from endpoints import admin
from servicios import cuenta_corriente


IDEMPOTENCY_KEY = "c" * 43


@pytest.fixture
def admin_aislado(monkeypatch):
    monkeypatch.setattr(admin, "_is_auth", lambda _token: True)

    async def archivo_vacio(_archivo):
        return b""

    monkeypatch.setattr(cuenta_corriente, "leer_comprobante_con_tope", archivo_vacio)
    return SimpleNamespace()


@pytest.mark.parametrize("texto", ["100.000", "100,000"])
def test_factura_admin_interpreta_ambos_separadores_como_cien_mil(
    monkeypatch, admin_aislado, texto,
):
    guardados = []
    monkeypatch.setattr(admin, "registrar_envio", lambda **datos: guardados.append(datos))

    anotacion = inspect.signature(admin.admin_envio_nuevo).parameters["monto_ars"].annotation
    assert anotacion in (str, "str")
    respuesta = asyncio.run(admin.admin_envio_nuevo(
        request=admin_aislado,
        cliente_id="MELCIOR",
        fecha="2026-08-10",
        nro_fc="",
        monto_ars=texto,
        ambito="INTERNACIONAL",
        idempotency_key=IDEMPOTENCY_KEY,
        descripcion="",
        tracking="",
        estado="ACTIVO",
        factura_pdf=None,
        admin_token="token-falso",
    ))

    assert respuesta.status_code == 303
    assert respuesta.status_code != 422
    assert len(guardados) == 1
    assert guardados[0]["monto_ars"] == 100_000.0


@pytest.mark.parametrize("texto", ["100.000", "100,000"])
def test_pago_admin_interpreta_ambos_separadores_como_cien_mil(
    monkeypatch, admin_aislado, texto,
):
    guardados = []
    monkeypatch.setattr(admin, "registrar_pago", lambda **datos: guardados.append(datos))

    anotacion = inspect.signature(admin.admin_pago_nuevo).parameters["monto_ars"].annotation
    assert anotacion in (str, "str")
    respuesta = asyncio.run(admin.admin_pago_nuevo(
        request=admin_aislado,
        cliente_id="MELCIOR",
        fecha="2026-08-10",
        monto_ars=texto,
        idempotency_key=IDEMPOTENCY_KEY,
        metodo="transferencia",
        referencia="",
        nota="",
        comprobante=None,
        admin_token="token-falso",
    ))

    assert respuesta.status_code == 303
    assert respuesta.status_code != 422
    assert len(guardados) == 1
    assert guardados[0]["monto_ars"] == 100_000.0


def test_importe_admin_invalido_no_registra_factura(monkeypatch, admin_aislado):
    guardados = []
    monkeypatch.setattr(admin, "registrar_envio", lambda **datos: guardados.append(datos))
    monkeypatch.setattr(admin, "_get_clientes_lista", lambda: [])
    monkeypatch.setattr(
        admin.templates,
        "TemplateResponse",
        lambda *, context, status_code=200, **_kw: SimpleNamespace(
            status_code=status_code, context=context
        ),
    )

    respuesta = asyncio.run(admin.admin_envio_nuevo(
        request=admin_aislado,
        cliente_id="MELCIOR",
        fecha="2026-08-10",
        nro_fc="",
        monto_ars="100.00.0",
        ambito="INTERNACIONAL",
        idempotency_key=IDEMPOTENCY_KEY,
        descripcion="",
        tracking="",
        estado="ACTIVO",
        factura_pdf=None,
        admin_token="token-falso",
    ))

    assert respuesta.status_code == 200
    assert "ingresá un número válido" in respuesta.context["flash_error"]
    assert guardados == []
