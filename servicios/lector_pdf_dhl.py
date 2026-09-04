"""Lector conservador del formato DHL Argentina FC A, USD, dos páginas.

Validado inicialmente contra tres originales de agosto de 2026. Formatos
distintos, NC/ND, escaneos, recargos desconocidos o documentos incompletos se
rechazan para revisión. No hay OCR/LLM, red, base de datos ni autorización de
cobro. Ejecutar el futuro worker con límites de memoria/tiempo antes de
exponerlo a adjuntos arbitrarios en producción.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from io import BytesIO
import re

import pdfplumber

from servicios.entrada_facturas_dhl import ExtraccionDHLInvalida


DINERO = r'(?:[0-9]+|[0-9]{1,3}(?:\.[0-9]{3})+),[0-9]{2}'
CONCEPTOS = {
    'FLETE': 'FLETE', 'FUEL': 'COMBUSTIBLE', 'GOGREEN PLUS': 'OTRO',
    '12:00 PREMIUM': 'OTRO', 'VALUE PROTECTION': 'SEGURO',
}
BASES_PESO = {'A': 'DECLARADO', 'B': 'REAL', 'V': 'VOLUMETRICO',
              'W': 'VOLUMETRICO', 'M': 'OTRO'}


@dataclass(frozen=True)
class LecturaPDFDHL:
    extraccion: dict
    observaciones: tuple[str, ...]
    requiere_revision: bool = True


def _exigir(condicion, mensaje):
    if not condicion:
        raise ExtraccionDHLInvalida(mensaje)


def _unico(patron, texto, campo):
    valores = re.findall(patron, texto, flags=re.MULTILINE)
    _exigir(len(valores) == 1, f'{campo}: dato faltante o ambiguo.')
    return valores[0]


def _dinero(texto):
    _exigir(bool(re.fullmatch(DINERO, texto)), 'Importe documental inválido.')
    return Decimal(texto.replace('.', '').replace(',', '.'))


def _fecha(texto):
    try:
        return datetime.strptime(texto, '%d/%m/%Y').date().isoformat()
    except ValueError as exc:
        raise ExtraccionDHLInvalida('Fecha documental inválida.') from exc


def _conceptos(texto):
    restantes = texto.upper().split()
    resultado = []
    while restantes:
        for nombre in CONCEPTOS:
            tokens = nombre.split()
            if restantes[:len(tokens)] == tokens:
                resultado.append(nombre)
                restantes = restantes[len(tokens):]
                break
        else:
            raise ExtraccionDHLInvalida('Concepto DHL desconocido; no se clasifica por inferencia.')
    return resultado


def _columna(palabras, desde, hasta):
    return sorted((w for w in palabras if desde <= w['x0'] < hasta),
                  key=lambda w: (w['top'], w['x0']))


def _leer_paginas(paginas, *, numero_esperado, cuit_esperado):
    _exigir(len(paginas) == 2, 'Formato no validado: se requieren cabecera y un detalle completos.')
    _exigir([(p.width, p.height) for p in paginas] == [(612, 912), (612, 792)],
            'Dimensiones del formato DHL no reconocidas.')
    cabecera = paginas[0].extract_text() or ''
    detalle = paginas[1].extract_text() or ''
    _exigir(cabecera.startswith('Factura ORIGINAL\n') and '\nCodigo 01\n' in cabecera,
            'Sólo se admite FC A original en este lector; NC/ND requieren otro formato.')
    _exigir('** VALORES EXPRESADOS EN DOLARES **' in cabecera, 'Moneda/formato no soportado.')
    serie, correlativo, fecha = _unico(
        r'^A ([0-9]{4}) - ([0-9]{8}) FECHA: ([0-9]{2}/[0-9]{2}/[0-9]{4})$',
        cabecera, 'Identidad de factura')
    numero = serie + 'A' + correlativo
    _exigir(numero == numero_esperado, 'El número del PDF no coincide con la fuente.')
    cuits = re.findall(r'\bCUIT:\s*([0-9]{2}-[0-9]{8}-[0-9])', cabecera)
    _exigir(cuits == ['30-58011131-5', cuit_esperado[:2] + '-' + cuit_esperado[2:10] + '-' + cuit_esperado[10:]],
            'Emisor o CUIT receptor distintos de los esperados.')
    _exigir(_unico(r'^N° ([0-9]{4}A[0-9]{8})$', detalle, 'Número del detalle') == numero,
            'El detalle pertenece a otro documento.')
    _exigir(_unico(r'Fecha: ([0-9]{2}/[0-9]{2}/[0-9]{4})$', detalle, 'Fecha del detalle') == fecha,
            'Fecha de detalle inconsistente.')
    _exigir(detalle.startswith('DETALLE Página 1 de 1\nFactura\n'), 'Detalle parcial o no soportado.')
    vencimiento = _unico(r'^VENCIMIENTO: ([0-9/]+)$', cabecera, 'Vencimiento')
    subtotal = _dinero(_unico(r'SUBTOTAL U\$S (' + DINERO + r')$', cabecera, 'Subtotal'))
    total = _dinero(_unico(r'^TOTAL A PAGA(?:R)?\s*U\$S (' + DINERO + r')$', cabecera, 'Total'))
    fx = Decimal(_unico(r'Tipo de Cambio Efectos Impositivos: ([0-9]+\.[0-9]{2})', cabecera, 'Cambio impositivo'))
    ars, cambio = _unico(r'^El importe corresponde a \$ ([0-9]+\.[0-9]{2}) a TC: ([0-9]+\.[0-9]{3})$',
                        cabecera, 'Equivalente documental')
    _exigir(fx > 0 and fx == Decimal(cambio) and total * fx == Decimal(ars),
            'No coincide el equivalente en ARS o el tipo de cambio documental.')
    impuestos_detalle = [
        ('IVA 21%', _dinero(_unico(r'IVA 21,0% (' + DINERO + r')$', cabecera, 'IVA 21'))),
        ('IVA 10.5%', _dinero(_unico(r'IVA 10,5% (' + DINERO + r')$', cabecera, 'IVA 10.5'))),
        ('PERCEPCION IIBB', _dinero(_unico(r'^Percep\. IIBB-s/det\. [0-9]+\.[0-9]{2} % (' + DINERO + r')$',
                                        cabecera, 'Percepción IIBB'))),
    ]
    impuestos = sum((importe for _, importe in impuestos_detalle), Decimal(0))
    _exigir(subtotal + impuestos == total, 'Subtotal e impuestos no cierran con el total.')
    resumen = {}
    for nombre in ('Servicio transporte', 'Adicional combustible', 'Otros Servicios', 'Periodic fee'):
        valores = _unico(r'^' + re.escape(nombre) + r' (' + DINERO + r') (' + DINERO + r')$', cabecera, nombre)
        resumen[nombre] = tuple(_dinero(v) for v in valores)
    _exigir(resumen['Periodic fee'] == (0, 0), 'Periodic fee requiere revisión específica.')
    exento, gravado = (_dinero(v) for v in _unico(r'TOTAL CONCEPTO (' + DINERO + r') (' + DINERO + r')$',
                                                cabecera, 'Resumen de conceptos'))
    _exigir(exento + gravado == subtotal, 'Exento y gravado no cierran con el subtotal.')
    cantidad = int(_unico(r'^CANTIDAD DE GUÍAS: ([0-9]+)$', cabecera, 'Cantidad de guías'))
    palabras = paginas[1].extract_words()
    # El formato tiene columnas fijas. Se comprueban anclas antes de usarlas.
    for nombre, desde, hasta in [('Guía', 15, 30), ('Peso', 135, 150),
                                 ('Detalle', 430, 460), ('Importe', 480, 515), ('Grav', 520, 540)]:
        _exigir(sum(w['text'] == nombre and desde <= w['x0'] < hasta and 80 <= w['top'] < 104
                    for w in palabras) == 1, 'Columnas del detalle DHL no reconocidas.')
    tabla = [w for w in palabras if 105 <= w['top'] < 730]
    guias = _columna(tabla, 8, 58)
    _exigir(len(guias) == cantidad and cantidad > 0 and
            all(re.fullmatch(r'[0-9]{10}', w['text']) for w in guias),
            'La cantidad/identidad de guías no coincide con el detalle.')
    _exigir(len({w['text'] for w in guias}) == cantidad, 'Guía repetida; revisar el detalle.')
    items = []
    acumulados = {clave: [Decimal(0), Decimal(0)] for clave in resumen}
    for indice, guia in enumerate(guias):
        superior = guia['top'] - 3
        inferior = guias[indice + 1]['top'] - 3 if indice + 1 < cantidad else 730
        bloque = [w for w in tabla if superior <= w['top'] < inferior]
        pesos = _columna(bloque, 125, 149)
        tipos = _columna(bloque, 150, 167)
        _exigir(len(pesos) == len(tipos) == 1, 'Peso faltante o ambiguo.')
        _exigir(bool(re.fullmatch(r'[0-9]+\.[0-9]{2}', pesos[0]['text'])), 'Peso no reconocido.')
        peso = Decimal(pesos[0]['text'])
        tipo_peso = tipos[0]['text']
        _exigir(peso > 0 and tipo_peso in BASES_PESO, 'Tipo de peso DHL desconocido.')
        nombres = _conceptos(' '.join(w['text'] for w in _columna(bloque, 415, 476)))
        importes = [_dinero(w['text']) for w in _columna(bloque, 480, 520)]
        indicadores = [w['text'] for w in _columna(bloque, 522, 540)]
        _exigir(len(nombres) == len(importes) == len(indicadores) and bool(nombres),
                'Conceptos, importes e indicadores no tienen la misma cantidad.')
        _exigir(all(v in {'N', 'S'} for v in indicadores), 'Indicador de gravado no reconocido.')
        # Las columnas monetaria y descriptiva usan interlineados diferentes:
        # se respeta el orden dentro del bloque, no la cercanía vertical.
        for nombre, importe, indicador in zip(nombres, importes, indicadores):
            categoria = ('Servicio transporte' if nombre == 'FLETE' else
                         'Adicional combustible' if nombre == 'FUEL' else 'Otros Servicios')
            acumulados[categoria][indicador == 'S'] += importe
            item = {'linea_numero': len(items) + 1, 'tracking': guia['text'],
                    'concepto_tipo': CONCEPTOS[nombre], 'concepto_codigo': nombre,
                    'descripcion': f'{nombre}; tipo peso DHL {tipo_peso}; gravado {indicador}',
                    'importe': importe, 'tipo_cambio_ars': fx,
                    'peso_facturado_kg': peso, 'peso_base': BASES_PESO[tipo_peso]}
            if tipo_peso in {'V', 'W'}:
                item['peso_volumetrico_kg'] = peso
            elif tipo_peso == 'B':
                item['peso_real_kg'] = peso
            items.append(item)
    _exigir(all(tuple(acumulados[k]) == resumen[k] for k in resumen),
            'Los conceptos del detalle no coinciden con el resumen exento/gravado.')
    _exigir(sum((i['importe'] for i in items), Decimal(0)) == subtotal,
            'Los cargos por guía no cierran con el subtotal.')
    for nombre, importe in impuestos_detalle:
        if importe:
            items.append({'linea_numero': len(items) + 1, 'tracking': None,
                          'concepto_tipo': 'IMPUESTO', 'concepto_codigo': nombre,
                          'descripcion': nombre + ' general de factura; sin prorrateo automático',
                          'importe': importe, 'tipo_cambio_ars': fx})
    return LecturaPDFDHL(
        {'tipo_documento': 'FC', 'numero': numero, 'moneda': 'USD',
         'fecha_emision': _fecha(fecha), 'fecha_vencimiento': _fecha(vencimiento),
         'subtotal': subtotal, 'impuestos': impuestos, 'total': total, 'items': items},
        ('Tipo de cambio impositivo del documento; no acredita el cambio efectivo de pago.',
         'Impuestos generales sin asignar a guías; revisar tratamiento antes de trasladarlos.',
         'Revisar el PDF original y la autenticidad de origen antes de registrar o conciliar.'),
    )


def extraer_factura_dhl_pdf(archivo_pdf: bytes, *, numero_esperado: str, cuit_esperado: str) -> LecturaPDFDHL:
    """Extrae una propuesta exacta del formato validado, siempre revisable."""
    _exigir(isinstance(archivo_pdf, bytes) and archivo_pdf.startswith(b'%PDF') and
            len(archivo_pdf) <= 8 * 1024 * 1024, 'PDF inválido o superior a 8 MB.')
    _exigir(isinstance(numero_esperado, str) and bool(re.fullmatch(r'[0-9]{4}A[0-9]{8}', numero_esperado)),
            'Número esperado inválido.')
    _exigir(isinstance(cuit_esperado, str) and bool(re.fullmatch(r'[0-9]{11}', cuit_esperado)),
            'CUIT esperado inválido.')
    try:
        with pdfplumber.open(BytesIO(archivo_pdf)) as documento:
            return _leer_paginas(documento.pages, numero_esperado=numero_esperado, cuit_esperado=cuit_esperado)
    except ExtraccionDHLInvalida:
        raise
    except Exception as exc:
        # No filtrar datos privados del parser a logs/respuestas públicas.
        raise ExtraccionDHLInvalida('No se pudo leer el PDF; requiere revisión manual.') from exc
