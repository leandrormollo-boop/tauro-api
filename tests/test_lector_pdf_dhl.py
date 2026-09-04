"""Datos sintéticos; ningún PDF ni dato de clientes forma parte del repositorio."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from servicios.entrada_facturas_dhl import ExtraccionDHLInvalida, preparar_factura_dhl
from servicios.lector_pdf_dhl import _leer_paginas, extraer_factura_dhl_pdf


CABECERA = '''Factura ORIGINAL
A 1700 - 00000001 FECHA: 31/08/2026
VENCIMIENTO: 15/09/2026
Codigo 01
DHL EXPRESS (ARGENTINA) S.A.
CUIT:30-58011131-5
EMPRESA: CLIENTE SINTETICO
CUIT: 20-12345678-6
Servicio transporte 100,00 0,00
Adicional combustible 30,00 0,00
Otros Servicios 2,00 10,00
Periodic fee 0,00 0,00
** VALORES EXPRESADOS EN DOLARES ** TOTAL CONCEPTO 132,00 10,00
SUBTOTAL U$S 142,00
CANTIDAD DE GUÍAS: 1
Tipo de Cambio Efectos Impositivos: 1500.00 IVA 21,0% 2,10
IVA 10,5% 0,00
Percep. IIBB-s/det. 3.00 % 4,26
TOTAL A PAGAU$S 148,36
El importe corresponde a $ 222540.00 a TC: 1500.000
'''
DETALLE = '''DETALLE Página 1 de 1
Factura
N° 1700A00000001
DHL EXPRESS (ARGENTINA) S.A. Fecha: 31/08/2026
'''


def paginas():
    def palabra(texto, x, y):
        return {'text': texto, 'x0': x, 'top': y}
    palabras = [palabra(texto, x, 85) for texto, x in [
        ('Guía', 19), ('Peso', 141), ('Detalle', 434), ('Importe', 487), ('Grav', 525)]]
    palabras += [palabra('0123456789', 18, 111), palabra('6.50', 131, 110), palabra('W', 157, 110)]
    for nombre, importe, gravado, y in [('Flete', '100,00', 'N', 111), ('FUEL', '30,00', 'N', 120),
                                       ('GoGreen Plus', '2,00', 'N', 129), ('VALUE', '10,00', 'S', 138)]:
        palabras += [palabra(nombre, 418, y), palabra(importe, 505, 109 + (y-111) * 5/6),
                     palabra(gravado, 529, y)]
    palabras.append(palabra('PROTECTION', 418, 147))
    return [SimpleNamespace(width=612, height=912, extract_text=lambda: CABECERA),
            SimpleNamespace(width=612, height=792, extract_text=lambda: DETALLE,
                            extract_words=lambda: palabras)]


def leer(p=None, **kwargs):
    return _leer_paginas(paginas() if p is None else p, **{
        'numero_esperado': '1700A00000001', 'cuit_esperado': '20123456786', **kwargs})


def test_lee_cargos_multilinea_con_interlineado_distinto():
    resultado = leer()
    assert resultado.requiere_revision
    datos = resultado.extraccion
    assert datos['total'] == Decimal('148.36')
    assert [i['importe'] for i in datos['items']] == list(map(Decimal, ['100', '30', '2', '10', '2.10', '4.26']))
    assert datos['items'][3]['concepto_tipo'] == 'SEGURO'
    assert datos['items'][0]['tracking'] == '0123456789'
    assert datos['items'][0]['peso_base'] == 'VOLUMETRICO'
    assert 'peso_real_kg' not in datos['items'][0]
    assert all(i['tracking'] is None for i in datos['items'][-2:])


def test_salida_compatible_con_preparador_sin_cargar_base():
    resultado = preparar_factura_dhl(leer().extraccion, archivo_pdf=b'%PDF-sintetico',
        archivo_nombre='prueba.pdf', cuenta_correo='prueba@example.invalid', mensaje_id='m-1', adjunto_id='a-1')
    assert resultado.requiere_revision
    assert resultado.datos_registro['total'] == Decimal('148.36')
    assert len(resultado.observaciones) == 2


@pytest.mark.parametrize(('campo', 'valor'), [('numero_esperado', '1700A99999999'),
                                           ('cuit_esperado', '20999999999')])
def test_rechaza_identidad_fuente_distinta(campo, valor):
    with pytest.raises(ExtraccionDHLInvalida):
        leer(**{campo: valor})


@pytest.mark.parametrize(('anterior', 'nuevo'), [
    ('Factura ORIGINAL', 'Nota de Credito ORIGINAL'),
    ('Factura ORIGINAL', 'Factura REIMPRESION'),
    ('Codigo 01', 'Codigo 03'),
    ('VALORES EXPRESADOS EN DOLARES', 'VALORES EXPRESADOS EN PESOS'),
    ('30-58011131-5', '30-99999999-9'),
    ('31/08/2026', '32/08/2026'),
    ('222540.00', '222541.00'),
    ('1500.000', '1501.000'),
    ('148,36', '148,37'),
    ('CANTIDAD DE GUÍAS: 1', 'CANTIDAD DE GUÍAS: 2'),
    ('Periodic fee 0,00 0,00', 'Periodic fee 1,00 0,00'),
    ('Servicio transporte 100,00 0,00', 'Servicio transporte 99,00 1,00'),
    ('TOTAL CONCEPTO 132,00 10,00', 'TOTAL CONCEPTO 131,00 10,00'),
])
def test_rechaza_documentos_inconsistentes(anterior, nuevo):
    p = paginas()
    p[0].extract_text = lambda: CABECERA.replace(anterior, nuevo)
    with pytest.raises(ExtraccionDHLInvalida):
        leer(p)


def test_no_acepta_total_ambiguo():
    p = paginas()
    p[0].extract_text = lambda: CABECERA + 'TOTAL A PAGAU$S 148,36\n'
    with pytest.raises(ExtraccionDHLInvalida, match='ambiguo'):
        leer(p)


@pytest.mark.parametrize('detalle', [DETALLE.replace('00000001', '00000002'),
    DETALLE.replace('31/08/2026', '30/08/2026'), DETALLE.replace('1 de 1', '1 de 2')])
def test_detalle_de_otro_documento_o_incompleto(detalle):
    p = paginas()
    p[1].extract_text = lambda: detalle
    with pytest.raises(ExtraccionDHLInvalida):
        leer(p)


def test_no_admite_paginas_faltantes_o_adicionales():
    for p in [paginas()[:1], paginas() + paginas()]:
        with pytest.raises(ExtraccionDHLInvalida):
            leer(p)


@pytest.mark.parametrize(('texto', 'cambio'), [('FUEL', 'RECARGO NUEVO'), ('6.50', '6,50'),
    ('W', 'X'), ('N', 'Z'), ('30,00', '30,01'), ('0123456789', '1234')])
def test_no_infiere_cargos_pesos_o_tracking(texto, cambio):
    p = paginas()
    words = p[1].extract_words()
    for word in words:
        if word['text'] == texto:
            word['text'] = cambio
    with pytest.raises(ExtraccionDHLInvalida):
        leer(p)


@pytest.mark.parametrize(('codigo', 'base', 'campo'), [
    ('A', 'DECLARADO', None), ('B', 'REAL', 'peso_real_kg'),
    ('V', 'VOLUMETRICO', 'peso_volumetrico_kg'), ('W', 'VOLUMETRICO', 'peso_volumetrico_kg'),
    ('M', 'OTRO', None),
])
def test_interpreta_codigos_de_peso_sin_inventar_peso_real(codigo, base, campo):
    p = paginas()
    next(w for w in p[1].extract_words() if w['text'] == 'W')['text'] = codigo
    item = leer(p).extraccion['items'][0]
    assert item['peso_base'] == base
    assert item['peso_facturado_kg'] == Decimal('6.50')
    assert set(item) & {'peso_real_kg', 'peso_volumetrico_kg'} == ({campo} if campo else set())


def test_columnas_movidas_se_rechazan():
    p = paginas()
    next(w for w in p[1].extract_words() if w['text'] == 'Importe')['x0'] = 300
    with pytest.raises(ExtraccionDHLInvalida, match='Columnas'):
        leer(p)


def test_no_acepta_geometria_distinta():
    p = paginas()
    p[1].width = 595
    with pytest.raises(ExtraccionDHLInvalida, match='Dimensiones'):
        leer(p)


@pytest.mark.parametrize('contenido', [b'no pdf', b'%PDF-no-es-documento-real',
                                      b'%PDF' + b' ' * (8 * 1024 * 1024), None])
def test_pdf_invalido_no_escapa_como_excepcion_interna(contenido):
    with pytest.raises(ExtraccionDHLInvalida):
        extraer_factura_dhl_pdf(contenido, numero_esperado='1700A00000001', cuit_esperado='20123456786')
