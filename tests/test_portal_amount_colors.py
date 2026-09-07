"""Estilos de importes: no alteran valores, estados ni colores del admin."""
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import re

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape


ROOT = Path(__file__).resolve().parent.parent


def render_account(balance, movement=None):
    """Página real con datos ficticios, sin acceder a cuentas ni bases reales."""
    balance = Decimal(balance)
    ledger = dict(
        debe_ars=Decimal("3000000") + balance, haber_ars=Decimal("3000000"),
        saldo_ars=balance, facturado_ars=Decimal("3000000") + balance,
        pendiente_facturacion_ars=0, envios_ars=Decimal("3000000") + balance,
        pagos_ars=Decimal("3000000"), diferencias_debito_ars=0,
        diferencias_credito_ars=0,
    )
    empty = {key: 0 for key in ledger}
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape())
    env.globals.update(
        saldo_menu=lambda *_: dict(al_dia=balance == 0, a_favor_ars=max(-balance, 0), pendiente_ars=max(balance, 0)),
        pendientes_menu=lambda *_: dict(envios=0, tienda=0),
        ayuda=lambda: dict(whatsapp_url=None, mail_url="mailto:demo@example.invalid"),
    )
    payment = dict(
        tipo="PAGO", estado="APROBADO", concepto="Transferencia de prueba",
        referencia="", numero_guia=None, solicitud_id=None, destinatario=None,
        remitente=None, fecha="04/09/2026", valor_envio_ars=None,
        ambito="internacional", debe_ars=0, haber_ars=Decimal("3000000"),
    )
    return env.get_template("portal/cuenta.html").render(
        request=SimpleNamespace(query_params={}, url=SimpleNamespace(path="/portal/cuenta"), state=SimpleNamespace(csp_nonce="test")),
        cliente="CLIENTE DEMO", resumen_cuenta=dict(
            consolidado=ledger, nacional=empty, internacional=ledger,
            credito_sin_imputar_ars=0, cargos_sin_clasificar_ars=0,
        ),
        ambito_filtro="consolidado", tipo_filtro="todos", vista_cuenta="movimientos",
        movimientos=dict(items=[movement or payment], total_resultados=1, pagina_desde=1, pagina_hasta=1, total_paginas=1),
        destinos_pago=[], today="2026-09-04", idempotency_key="test",
    )


@pytest.mark.parametrize("balance,state", [("23842294.17", "A pagar"), ("-3000000", "A favor"), ("0", "Al día")])
def test_saldo_metalico_conserva_valor_y_estado(balance, state):
    html = render_account(balance)
    expected = f"$ {abs(Decimal(balance)):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    amount = re.search(r'<strong class="account-total-value portal-balance-metal">\s*(.*?)\s*</strong>', html)
    assert amount.group(1) == expected
    status = re.search(r'<span class="account-total-state">\s*(.*?)\s*</span>', html)
    assert status.group(1) == state
    assert '<dt>Pagos aprobados</dt><dd class="portal-money-green">$ 3.000.000,00</dd>' in html
    assert 'data-label="Importe">' in html
    assert '<strong class="account-amount-credit">− $ 3.000.000,00</strong>' in html
    assert 'mono amount-column account-movement-amount' in html


def test_costos_verdes_conservan_adicionales_separados():
    html = (ROOT / "templates/portal/envios.html").read_text()
    for name in ("initial", "final"):
        assert f'class="envio-price-{name} portal-money-green"' in html
    for name in ("diferencia", "tax"):
        assert f'class="envio-price-extra {name}"' in html
        assert f'class="envio-price-extra {name} portal-money-green"' not in html
    detail = (ROOT / "templates/portal/envio_detalle.html").read_text()
    assert 'class="portal-money-green">{{ dinero_ars(s.precio_inicial_cliente_ars or s.precio_tauro_ars) }}' in detail
    assert 'class="portal-money-green">{{ dinero_ars(s.precio_final_cliente_ars or s.precio_tauro_ars) }}' in detail


def test_diferencia_muestra_una_sola_ecuacion_y_no_duplica_el_cargo():
    difference = dict(
        tipo="DIFERENCIA", estado="APLICADA", concepto="Diferencia de envío",
        referencia="", numero_guia="873258645592", numero_factura=None,
        solicitud_id=12, destinatario="DESTINO", remitente="ORIGEN",
        fecha="01/09/2026", valor_envio_ars=Decimal("122271.34"),
        ambito="internacional", debe_ars=Decimal("24571.34"), haber_ars=0,
        diferencia_detalle=dict(
            montos_completos=True, valor_inicial_ars=Decimal("97700"),
            diferencia_ars=Decimal("24571.34"), valor_final_ars=Decimal("122271.34"),
            es_credito=False, es_peso=False, concepto_courier="Diferencia del courier",
            motivo_legible="Diferencia del courier", leyenda="Sin margen adicional.",
        ),
    )

    html = render_account("24571.34", movement=difference)

    assert "Valor cotizado" in html
    assert "Diferencia" in html
    assert "Costo final" in html
    assert "$ 97.700,00" in html
    assert "$ 24.571,34" in html
    assert "$ 122.271,34" in html
    assert 'account-movement-amount is-summary-only' in html
    assert ">Cargo<" not in html


def test_saldos_contrastan_en_ambos_temas_sin_cambiar_metalico_de_marca():
    css = (ROOT / "static/css/tauro.css").read_text()
    assert '.shell .portal-money-green { color: var(--ok); }' in css
    assert 'html[data-theme="light"] .shell .portal-balance-metal {' in css
    assert '@media (forced-colors: active)' in css
    assert '-webkit-text-fill-color: currentColor;' in css
    assert '.t-metal {' in css
    for path in (ROOT / "templates/admin").glob("*.html"):
        assert 'portal-balance-metal' not in path.read_text()
        assert 'portal-money-green' not in path.read_text()


def test_formula_de_diferencias_no_trunca_importes_en_mobile():
    css = (ROOT / "static/css/tauro.css").read_text()

    bloque_valor = css.split(".account-difference-value strong {", 1)[1].split("}", 1)[0]
    assert "text-overflow: ellipsis" not in bloque_valor
    assert "white-space: nowrap" not in bloque_valor
    assert "overflow-wrap: anywhere" in bloque_valor
    assert "@media (max-width: 390px)" in css
    assert ".account-difference-flow { grid-template-columns: 1fr;" in css
