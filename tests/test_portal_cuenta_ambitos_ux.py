"""Contrato del portal para la cuenta por ámbito y el aviso de pagos."""

import asyncio
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import endpoints.portal_cliente as portal
import servicios.cuenta_corriente as cuenta


RAIZ = Path(__file__).resolve().parent.parent
IDEMPOTENCY_KEY = "a" * 43


def _resumen():
    return {
        "consolidado": {
            "debe_ars": Decimal("1200"), "haber_ars": Decimal("700"),
            "saldo_ars": Decimal("500"), "facturado_ars": Decimal("1000"),
            "pendiente_facturacion_ars": Decimal("200"),
        },
        "nacional": {
            "debe_ars": Decimal("400"), "haber_ars": Decimal("300"),
            "saldo_ars": Decimal("100"), "facturado_ars": Decimal("400"),
            "pendiente_facturacion_ars": Decimal("0"),
        },
        "internacional": {
            "debe_ars": Decimal("700"), "haber_ars": Decimal("400"),
            "saldo_ars": Decimal("300"), "facturado_ars": Decimal("600"),
            "pendiente_facturacion_ars": Decimal("100"),
        },
        "credito_sin_imputar_ars": Decimal("0"),
        "cargos_sin_clasificar_ars": Decimal("100"),
    }


def _movimientos():
    return {
        "items": [], "total_resultados": 0, "pagina_actual": 1,
        "total_paginas": 1, "pagina_desde": 0, "pagina_hasta": 0,
        "paginas_visibles": [1],
    }


class _Upload:
    filename = "comprobante.pdf"

    async def read(self, _limite):
        return b"%PDF-1.7 prueba"


class _Request:
    def __init__(self, form):
        self._form = form

    async def form(self):
        return self._form


def _form_pago(**cambios):
    form = {
        "monto": "100.000", "metodo": "Transferencia",
        "referencia": "OP-1", "destino_pago": "SIN_IMPUTAR",
        "volver_ambito": "consolidado", "comprobante": _Upload(),
        "idempotency_key": IDEMPOTENCY_KEY,
    }
    form.update(cambios)
    return form


def test_cuenta_usa_solo_cliente_de_sesion_y_normaliza_filtros(monkeypatch):
    llamadas = []
    monkeypatch.setattr(portal, "resumen_cuenta_por_ambito", lambda cliente: (
        llamadas.append(("resumen", cliente)) or _resumen()
    ))
    monkeypatch.setattr(portal, "movimientos_cuenta_paginados", lambda *args: (
        llamadas.append(("movimientos", *args)) or _movimientos()
    ))
    monkeypatch.setattr(portal.templates, "TemplateResponse", lambda **kwargs: kwargs)

    respuesta = portal.cuenta_corriente(
        SimpleNamespace(), ambito="NACIONAL", tipo="PAGOS", pagina="3",
        cliente="CLIENTE_SESION",
    )

    assert llamadas == [
        ("resumen", "CLIENTE_SESION"),
        ("movimientos", "CLIENTE_SESION", "nacional", "pagos", 3, 3),
    ]
    assert respuesta["context"]["ambito_filtro"] == "nacional"
    assert respuesta["context"]["saldo"]["saldo_pendiente_ars"] == Decimal("500")
    assert portal._idempotency_key_form(
        respuesta["context"]["idempotency_key"]
    ) == respuesta["context"]["idempotency_key"]


def test_query_manipulada_vuelve_a_consolidado_y_pagina_uno(monkeypatch):
    recibidos = []
    monkeypatch.setattr(portal, "resumen_cuenta_por_ambito", lambda _cliente: _resumen())
    monkeypatch.setattr(portal, "movimientos_cuenta_paginados", lambda *args: (
        recibidos.append(args) or _movimientos()
    ))
    monkeypatch.setattr(portal.templates, "TemplateResponse", lambda **kwargs: kwargs)

    portal.cuenta_corriente(
        SimpleNamespace(), ambito="CLIENTE_AJENO", tipo="SQL", pagina="-8",
        cliente="CLIENTE_SESION",
    )

    assert recibidos == [("CLIENTE_SESION", "consolidado", "todos", 1, 3)]


def test_cuenta_genera_clave_opaca_nueva_por_render(monkeypatch):
    monkeypatch.setattr(portal, "resumen_cuenta_por_ambito", lambda _cliente: _resumen())
    monkeypatch.setattr(portal, "movimientos_cuenta_paginados", lambda *_args: _movimientos())
    monkeypatch.setattr(portal.templates, "TemplateResponse", lambda **kwargs: kwargs)

    primera = portal.cuenta_corriente(SimpleNamespace(), cliente="CLIENTE_SESION")
    segunda = portal.cuenta_corriente(SimpleNamespace(), cliente="CLIENTE_SESION")
    clave_1 = primera["context"]["idempotency_key"]
    clave_2 = segunda["context"]["idempotency_key"]

    assert clave_1 != clave_2
    assert portal._idempotency_key_form(clave_1) == clave_1
    assert portal._idempotency_key_form(clave_2) == clave_2


def test_pago_sin_imputar_es_default_y_no_acepta_cliente_del_form(monkeypatch):
    recibidos = []
    monkeypatch.setattr(cuenta, "registrar_pago", lambda **datos: recibidos.append(datos))

    respuesta = asyncio.run(portal.informar_pago(
        _Request(_form_pago(cliente_id="CLIENTE_AJENO")),
        cliente="CLIENTE_SESION",
    ))

    assert respuesta.status_code == 303
    assert recibidos[0]["cliente_id"] == "CLIENTE_SESION"
    assert recibidos[0]["aplicaciones"] == {}
    assert recibidos[0]["monto_ars"] == Decimal("100000.0")
    assert recibidos[0]["idempotency_key"] == IDEMPOTENCY_KEY


def test_pago_dividido_envia_decimal_y_conserva_remanente_sin_imputar(monkeypatch):
    recibidos = []
    monkeypatch.setattr(cuenta, "registrar_pago", lambda **datos: recibidos.append(datos))

    asyncio.run(portal.informar_pago(
        _Request(_form_pago(
            destino_pago="DIVIDIR", monto_nacional="30.000",
            monto_internacional="50.000",
        )),
        cliente="CLIENTE_SESION",
    ))

    assert recibidos[0]["aplicaciones"] == {
        "NACIONAL": Decimal("30000.0"),
        "INTERNACIONAL": Decimal("50000.0"),
    }
    assert sum(recibidos[0]["aplicaciones"].values()) < recibidos[0]["monto_ars"]


def test_pago_a_un_ambito_solicita_imputar_el_total(monkeypatch):
    recibidos = []
    monkeypatch.setattr(cuenta, "registrar_pago", lambda **datos: recibidos.append(datos))

    asyncio.run(portal.informar_pago(
        _Request(_form_pago(destino_pago="NACIONAL")),
        cliente="CLIENTE_SESION",
    ))

    assert recibidos[0]["aplicaciones"] == {"NACIONAL": Decimal("100000.0")}


def test_pago_dividido_no_puede_superar_total(monkeypatch):
    recibidos = []
    monkeypatch.setattr(cuenta, "registrar_pago", lambda **datos: recibidos.append(datos))

    respuesta = asyncio.run(portal.informar_pago(
        _Request(_form_pago(
            destino_pago="DIVIDIR", monto_nacional="80.000",
            monto_internacional="30.000",
        )),
        cliente="CLIENTE_SESION",
    ))

    assert recibidos == []
    assert respuesta.status_code == 303
    assert "no%20puede%20superar" in respuesta.headers["location"]


def test_pago_exige_idempotencia_valida_y_rechaza_mas_de_dos_decimales(monkeypatch):
    recibidos = []
    monkeypatch.setattr(cuenta, "registrar_pago", lambda **datos: recibidos.append(datos))

    sin_clave = asyncio.run(portal.informar_pago(
        _Request(_form_pago(idempotency_key="")),
        cliente="CLIENTE_SESION",
    ))
    demasiados_decimales = asyncio.run(portal.informar_pago(
        _Request(_form_pago(monto="0,015")),
        cliente="CLIENTE_SESION",
    ))

    assert recibidos == []
    assert "Recarg%C3%A1%20la%20p%C3%A1gina" in sin_clave.headers["location"]
    assert "m%C3%A1ximo%20dos%20decimales" in demasiados_decimales.headers["location"]


def test_template_muestra_invariantes_tabs_paginacion_y_copy_seguro():
    html = (RAIZ / "templates" / "portal" / "cuenta.html").read_text(encoding="utf-8")

    for texto in (
        "Saldo total consolidado", "Nacional", "Internacional", "Debe", "Haber",
        "Saldo", "Crédito sin imputar", "Cargos sin clasificar", "Sin imputar",
        "Dividir", "El pago no modifica tus saldos hasta que Tauro apruebe",
    ):
        assert texto in html
    assert 'name="destino_pago" value="SIN_IMPUTAR" checked' in html
    assert 'name="idempotency_key" value="{{ idempotency_key }}"' in html
    assert "data-open-payment" in html
    assert "paymentDetails.open = true" in html
    assert "movimientos.paginas_visibles" in html
    assert "&pagina={{ numero }}" in html
