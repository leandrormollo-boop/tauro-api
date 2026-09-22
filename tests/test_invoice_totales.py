"""Totales exactos de mercadería, sin emitir guías ni usar cuentas reales."""
from copy import deepcopy
from decimal import Decimal
from unittest import mock

import pytest

from servicios.invoice_comercial import normalizar_items_invoice, total_items_invoice
from servicios import api_b2b as b2b, solicitudes_guia as sg
from test_dhl_client import _emitir
from test_dhl_invoice_multiitems import caja, portal, submit


def caja_total(total=100, unidades=3):
    b = caja()
    b['items_invoice'] = [{**b['items_invoice'][0], 'unidades_aduana': unidades,
                           'valor_total_usd': total, 'peso_neto_kg': 3.5}]
    b['unidades_aduana'] = unidades
    b['valor_declarado_caja_usd'] = total
    b.pop('valor_unitario_usd')
    return b


@pytest.mark.parametrize('total,unidades,unitario', [
    (100, 3, '33.333'), (25, 18, '1.389'), (10, 25, '0.400'),
    ('1.000,50', 27, '37.056'), ('1,000.50', 27, '37.056'), (1, 300, '0.003'),
])
def test_total_exacto_sobrevive_normalizacion_y_payload(total, unidades, unitario):
    b = caja_total(total, unidades)
    piezas, detalle, error = b2b._piezas_del_catalogo('WAIMAO', [b])
    assert error is None
    item = detalle[0]['items_invoice'][0]
    assert Decimal(str(item['valor_unitario_usd'])) == Decimal(unitario)
    # Revalidar una declaración ya guardada no multiplica ni redondea su total.
    assert normalizar_items_invoice([item], peso_total_kg=4) == [item]
    payload = _emitir({'bultos': detalle, 'asegurar_carga': True})
    exacto = item['valor_total_usd']
    declaration = payload['content']['exportDeclaration']
    assert declaration['lineItems'][0]['preCalculatedLineItemTotalValue'] == exacto
    assert declaration['lineItems'][0]['price'] == float(unitario)
    assert declaration['lineItems'][0]['quantity']['value'] == unidades
    assert payload['content']['declaredValue'] == exacto
    assert declaration['invoice']['preCalculatedTotalValues'] == {
        'preCalculatedTotalGoodsValue': exacto, 'preCalculatedTotalInvoiceValue': exacto,
    }
    assert declaration['invoice']['indicativeCustomsValues'] == {'totalWithImportDutiesAndTaxes': exacto}
    assert next(v for v in payload['valueAddedServices'] if v['serviceCode'] == 'II')['value'] == exacto


@pytest.mark.parametrize('total', ['', None, 0, -1, 'NaN', 'Infinity', '0.001', '1.2345'])
def test_total_invalido_no_cae_en_unitario_legado(total):
    b = caja_total(total)
    b['items_invoice'][0]['valor_unitario_usd'] = 100
    with pytest.raises(ValueError):
        normalizar_items_invoice(b['items_invoice'], peso_total_kg=4)


@pytest.mark.parametrize('diferencia', [0.01, 0.02, 1])
def test_total_no_oculta_una_diferencia_real(diferencia):
    b = caja_total()
    b['valor_declarado_caja_usd'] = 100 + diferencia
    assert 'valor_declarado_no_coincide' in b2b._piezas_del_catalogo('WAIMAO', [b])[2]


def test_lineas_nuevas_y_anteriores_conservan_sus_totales():
    nuevo = caja_total()
    legado = caja()
    _, detalle, error = b2b._piezas_del_catalogo('WAIMAO', [nuevo, legado])
    assert error is None
    p = _emitir({'bultos': detalle})
    assert p['content']['declaredValue'] == 300
    assert [i['preCalculatedLineItemTotalValue'] for i in p['content']['exportDeclaration']['lineItems']] == [100, 100, 100]


def test_submit_por_total_guarda_y_recotiza_sin_alterarlo(portal):
    created, quotes = portal
    response = submit(bulto_valor_usd=[], bulto_total_usd=['100'],
                      bulto_valor_caja_usd=['100'], bulto_unidades_aduana=['3'],
                      bulto_peso_neto=[''], bulto_items_extra=['[]'])
    assert response.status_code == 303
    saved = created.call_args.kwargs
    assert saved['valor_declarado_usd'] == 100
    item = saved['bultos'][0]['items_invoice'][0]
    assert item['valor_total_usd'] == 100 and item['valor_unitario_usd'] == 33.333
    from endpoints.portal_cliente import _precargar_envio_existente
    sol = dict(saved, id=99, courier='DHL')
    form, _ = _precargar_envio_existente(sol)
    assert form['bultos'][0]['items_invoice'][0]['valor_total_usd'] == 100
    captured = mock.Mock(return_value={'encontrado': False, 'motivo': 'test'})
    with mock.patch.object(b2b, 'cotizar_couriers_cliente', captured):
        sg._recotizar_dhl_antes_de_emitir(sol)
    assert captured.call_args.args[2][0]['items_invoice'][0]['valor_total_usd'] == 100


def test_error_conserva_total_ingresado(portal):
    created, quotes = portal
    response = submit(bulto_valor_usd=[], bulto_total_usd=['100,50'],
                      bulto_valor_caja_usd=['100'], bulto_unidades_aduana=['3'],
                      bulto_peso_neto=[''], bulto_items_extra=['[]'])
    assert response['context']['form']['bultos'][0]['valor_total_usd'] == '100,50'
    assert response['context']['form']['initial_step'] == 4
    created.assert_not_called()


@pytest.mark.parametrize('unidades,total,requiere', [(1, 100, False), (3, 100, True), (25, 100, True)])
def test_no_degrada_la_declaracion_de_otros_couriers(unidades, total, requiere):
    from servicios.invoice_comercial import invoice_requiere_dhl
    _, detalle, error = b2b._piezas_del_catalogo('WAIMAO', [caja_total(total, unidades)])
    assert error is None
    assert invoice_requiere_dhl(detalle) is requiere
