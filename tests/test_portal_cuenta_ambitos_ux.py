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
        ("movimientos", "CLIENTE_SESION", "nacional", "pagos", 3, 10),
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

    assert recibidos == [("CLIENTE_SESION", "consolidado", "todos", 1, 10)]


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
        "Saldo total consolidado", "Nacional", "Internacional", "Envíos", "Pagos",
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


def test_etiquetas_envios_y_pagos_conservan_los_importes_contables():
    html = (RAIZ / "templates" / "portal" / "cuenta.html").read_text(encoding="utf-8")

    for origen in ("ledger", "total"):
        assert f"<dt>Envíos</dt><dd>{{{{ dinero({origen}.debe_ars) }}}}</dd>" in html
        assert f'<dt>Pagos</dt><dd class="portal-money-green">{{{{ dinero({origen}.haber_ars) }}}}</dd>' in html
    assert '<th scope="col" class="amount-column">Envíos</th>' in html
    assert '<th scope="col" class="amount-column">Pagos</th>' in html
    assert 'data-label="Envíos">{% if m.debe_ars %}{{ dinero(m.debe_ars) }}' in html
    assert 'data-label="Pagos">{% if m.haber_ars %}{{ dinero(m.haber_ars) }}' in html
    for etiqueta in ("Debe", "Haber"):
        assert f">{etiqueta}<" not in html
        assert f'data-label="{etiqueta}"' not in html


def test_movimientos_separan_y_ordenan_los_datos_del_envio():
    portal_html = (
        RAIZ / "templates" / "portal" / "cuenta.html"
    ).read_text(encoding="utf-8")
    admin_html = (
        RAIZ / "templates" / "admin" / "cliente_detail.html"
    ).read_text(encoding="utf-8")
    admin_py = (RAIZ / "endpoints" / "admin.py").read_text(encoding="utf-8")
    css = (RAIZ / "static" / "css" / "tauro.css").read_text(encoding="utf-8")

    columnas = (
        "Concepto", "N.º de guía", "Destinatario", "Fecha", "Remitente",
        "Valor del envío",
    )
    tablas = (
        portal_html[portal_html.index('<table class="account-table">'):],
        admin_html[admin_html.index('<table class="admin-shipments-table">'):],
    )
    for html in tablas:
        posiciones = [html.index(f">{columna}<") for columna in columnas]
        assert posiciones == sorted(posiciones)

    assert 'data-label="Concepto"' in portal_html
    assert 'data-label="N.º de guía"' in portal_html
    assert 'data-label="Destinatario"' in portal_html
    assert 'data-label="Remitente"' in portal_html
    assert 'data-label="Valor del envío"' in portal_html
    assert "NULLIF(BTRIM(s.dest_nombre), '') AS destinatario" in admin_py
    assert "NULLIF(BTRIM(s.remitente_nombre), '') AS remitente" in admin_py
    assert "WHEN e.solicitud_id IS NOT NULL THEN 'Flete'" in admin_py
    assert ".admin-shipments-table" in css
    assert '.account-table td[data-label="Concepto"]' in css


def test_resumen_consolidado_no_desborda_sobre_los_ambitos():
    css = (RAIZ / "static" / "css" / "tauro.css").read_text(encoding="utf-8")

    assert "grid-template-columns: minmax(300px, 1fr) minmax(0, 2fr);" in css
    assert ".account-total-card > div { min-width: 0; }" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in css
    assert "text-overflow: ellipsis;" in css
