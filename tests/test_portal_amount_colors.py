"""Estilos de importes: no alteran valores, estados ni colores del admin."""
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import re

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape


ROOT = Path(__file__).resolve().parent.parent


def render_account(balance):
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
        movimientos=dict(items=[payment], total_resultados=1, pagina_desde=1, pagina_hasta=1, total_paginas=1),
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
    assert '<dt>Pagos</dt><dd class="portal-money-green">$ 3.000.000,00</dd>' in html
    assert 'data-label="Pagos">$ 3.000.000,00</td>' in html
    assert 'mono amount-column account-movement-credit' in html


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
