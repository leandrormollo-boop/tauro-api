from copy import deepcopy
from decimal import Decimal

import pytest

from servicios import conciliacion_couriers
from servicios.entrada_facturas_dhl import (
    ExtraccionDHLInvalida, preparar_factura_dhl,
)


def ejemplo():
    return {
        'tipo_documento': 'FC', 'numero': 'DHL-0001', 'moneda': 'ARS',
        'fecha_emision': '2026-09-03', 'subtotal': '1000',
        'impuestos': '210', 'total': '1210',
        'items': [
            {'linea_numero': 1, 'tracking': '0123456789', 'concepto_tipo': 'FLETE',
             'importe': '1000', 'peso_facturado_kg': '5.5', 'peso_base': 'REAL'},
            {'linea_numero': 2, 'tracking': '0123456789', 'concepto_tipo': 'IMPUESTO',
             'importe': '210'},
        ],
    }


def preparar(datos=None, **overrides):
    fuente = {
        'archivo_pdf': b'%PDF-1.7\nevidencia de prueba',
        'archivo_nombre': 'factura.pdf', 'cuenta_correo': 'facturas@example.invalid',
        'mensaje_id': 'mensaje-1', 'adjunto_id': 'adjunto-1',
    }
    return preparar_factura_dhl(ejemplo() if datos is None else datos, **(fuente | overrides))


def test_prepara_sin_efectos_y_conserva_tracking_y_documento(monkeypatch):
    def prohibido():
        pytest.fail('El preparador no puede abrir la base de datos')
    monkeypatch.setattr(conciliacion_couriers, 'get_conn', prohibido)
    original = ejemplo()
    copia = deepcopy(original)
    resultado = preparar(original)
    assert original == copia
    assert resultado.requiere_revision is True
    assert resultado.observaciones == ()
    assert resultado.datos_registro['courier'] == 'DHL'
    assert resultado.datos_registro['total'] == Decimal('1210')
    assert resultado.datos_registro['items'][0]['tracking'] == '0123456789'
    assert resultado.datos_registro['items'][0]['peso_facturado_kg'] == Decimal('5.500')
    assert resultado.datos_registro['archivo_contenido'].startswith(b'%PDF')
    assert len(resultado.datos_registro['archivo_sha256']) == 64


@pytest.mark.parametrize('valor', [True, 1210.0, '1.210,00', '1,210.00', 'NaN', 'Infinity', '1e3', '-1'])
def test_rechaza_importes_ambiguos_o_no_exactos(valor):
    datos = ejemplo()
    datos['total'] = valor
    with pytest.raises(ExtraccionDHLInvalida):
        preparar(datos)


@pytest.mark.parametrize('campo', ['cliente_id', 'solicitud_id', 'saldo', 'margen', 'actor', 'metadatos_origen'])
def test_lector_no_puede_decidir_cliente_saldo_o_procedencia(campo):
    datos = ejemplo() | {campo: 'dato no autorizado'}
    with pytest.raises(ExtraccionDHLInvalida, match='no permitidos'):
        preparar(datos)


def test_nc_permanece_nc_y_exige_revision_de_referencia():
    datos = ejemplo() | {'tipo_documento': 'NC'}
    resultado = preparar(datos)
    assert resultado.datos_registro['tipo_documento'] == 'NC'
    assert resultado.datos_registro['total'] == Decimal('1210')
    assert resultado.requiere_revision
    assert 'documento original' in resultado.observaciones[0]
    assert conciliacion_couriers.signo_documento('NC') == -1


@pytest.mark.parametrize('linea', [1, True, 1.5, '2', -1])
def test_no_trunca_ni_duplica_numeros_de_linea(linea):
    datos = ejemplo()
    datos['items'][1]['linea_numero'] = linea
    with pytest.raises(ExtraccionDHLInvalida, match='línea'):
        preparar(datos)


def test_sin_tracking_se_observa_sin_inventar_asignacion():
    datos = ejemplo()
    datos['items'][1].pop('tracking')
    resultado = preparar(datos)
    assert resultado.datos_registro['items'][1]['tracking'] is None
    assert 'asignación manual' in resultado.observaciones[0]


def test_no_completa_diferencias_para_forzar_el_total():
    datos = ejemplo()
    datos['items'][0]['importe'] = '999'
    with pytest.raises(ExtraccionDHLInvalida, match='no suman'):
        preparar(datos)


def test_subtotal_impuestos_y_total_deben_cerrar():
    datos = ejemplo() | {'impuestos': '209'}
    with pytest.raises(ExtraccionDHLInvalida, match='Subtotal'):
        preparar(datos)


def test_no_inventa_tipo_de_cambio_extranjero():
    datos = ejemplo() | {'moneda': 'USD'}
    with pytest.raises(ExtraccionDHLInvalida, match='tipo de cambio documentado'):
        preparar(datos)
    for item in datos['items']:
        item['tipo_cambio_ars'] = '1500.25'
    assert preparar(datos).datos_registro['items'][0]['importe_ars'] == Decimal('1500250.0000')


def test_ars_no_se_convierte_nuevamente():
    datos = ejemplo()
    datos['items'][0]['tipo_cambio_ars'] = '1500'
    with pytest.raises(ExtraccionDHLInvalida, match='cambio 1'):
        preparar(datos)


@pytest.mark.parametrize('cambio', [{'archivo_pdf': b'no pdf'}, {'mensaje_id': ''}, {'adjunto_id': ''}, {'cuenta_correo': ''}])
def test_evidencia_y_fuente_son_obligatorias(cambio):
    with pytest.raises(ExtraccionDHLInvalida):
        preparar(**cambio)


def test_mensaje_y_cuenta_distinguen_fuente_sin_alterar_hash_documento():
    primero = preparar().datos_registro
    repetido = preparar().datos_registro
    reenviado = preparar(mensaje_id='mensaje-2').datos_registro
    otra_cuenta = preparar(cuenta_correo='otra@example.invalid').datos_registro
    assert primero['mensaje_origen_id'] == repetido['mensaje_origen_id']
    assert primero['mensaje_origen_id'] != reenviado['mensaje_origen_id']
    assert primero['mensaje_origen_id'] != otra_cuenta['mensaje_origen_id']
    assert primero['archivo_sha256'] == reenviado['archivo_sha256']
    assert 'facturas@example.invalid' not in str(primero['metadatos_origen'])
