"""Contrato de entrada para el futuro lector de facturas DHL por correo.

Sólo prepara datos: no lee correo, no llama modelos, no escribe en la base y
no modifica saldos. La salida debe revisarse antes de entregarla al escritor
canónico ``registrar_factura_courier``. Una extracción es evidencia propuesta,
no una autorización financiera ni una instrucción ejecutable.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import hashlib
import re
from typing import Any, Mapping

from servicios.conciliacion_couriers import (
    CENTAVO_CONTROL, ConciliacionCourierError, _preparar_item,
    normalizar_numero_documento, normalizar_tipo_documento,
)


class ExtraccionDHLInvalida(ValueError):
    """La propuesta requiere corrección; nunca debe importarse parcialmente."""


@dataclass(frozen=True)
class PreparacionFacturaDHL:
    datos_registro: dict[str, Any]
    observaciones: tuple[str, ...]
    requiere_revision: bool = True


def _decimal_exacto(valor: Any, campo: str) -> Decimal:
    # El extractor debe entregar números canónicos, no floats ni separadores
    # de miles ambiguos. Es el parser específico quien interpretará el PDF.
    if isinstance(valor, bool) or not isinstance(valor, (str, int, Decimal)):
        raise ExtraccionDHLInvalida(f"{campo}: se requiere un decimal exacto.")
    texto = str(valor)
    if not re.fullmatch(r"-?\d+(?:\.\d{1,6})?", texto):
        raise ExtraccionDHLInvalida(f"{campo}: formato decimal no canónico.")
    numero = Decimal(texto)
    if numero < 0:
        raise ExtraccionDHLInvalida(f"{campo}: el importe/peso no puede ser negativo.")
    return numero


def _texto_requerido(datos: Mapping[str, Any], campo: str) -> str:
    valor = datos.get(campo)
    if not isinstance(valor, str) or not valor.strip():
        raise ExtraccionDHLInvalida(f"Falta {campo}.")
    return valor.strip()


def preparar_factura_dhl(
    extraccion: Mapping[str, Any],
    *,
    archivo_pdf: bytes,
    archivo_nombre: str,
    cuenta_correo: str,
    mensaje_id: str,
    adjunto_id: str,
) -> PreparacionFacturaDHL:
    """Valida la salida no confiable del lector, sin efectos externos.

    Los identificadores del correo provienen del conector, NUNCA del PDF ni
    del modelo. No se acepta cliente, saldo, margen, precio final ni un ID de
    solicitud: el dueño sólo podrá resolverse contra la base por tracking.
    """
    permitidos = {
        'tipo_documento', 'numero', 'moneda', 'subtotal', 'impuestos',
        'total', 'fecha_emision', 'fecha_vencimiento', 'items',
    }
    if not isinstance(extraccion, Mapping) or set(extraccion) - permitidos:
        raise ExtraccionDHLInvalida('La extracción contiene campos no permitidos.')
    if not isinstance(archivo_pdf, bytes) or not archivo_pdf.startswith(b'%PDF'):
        raise ExtraccionDHLInvalida('Se requiere el PDF original como evidencia.')
    if len(archivo_pdf) > 8 * 1024 * 1024:
        raise ExtraccionDHLInvalida('El PDF supera el máximo de 8 MB.')
    for campo, valor in {
        'archivo_nombre': archivo_nombre, 'cuenta_correo': cuenta_correo,
        'mensaje_id': mensaje_id, 'adjunto_id': adjunto_id,
    }.items():
        if not isinstance(valor, str) or not valor.strip():
            raise ExtraccionDHLInvalida(f'Falta {campo} de la fuente original.')
    try:
        tipo = normalizar_tipo_documento(_texto_requerido(extraccion, 'tipo_documento'))
        numero = normalizar_numero_documento(_texto_requerido(extraccion, 'numero'))
        emision = date.fromisoformat(_texto_requerido(extraccion, 'fecha_emision'))
        vencimiento = (
            date.fromisoformat(_texto_requerido(extraccion, 'fecha_vencimiento'))
            if extraccion.get('fecha_vencimiento') is not None else None
        )
    except (ValueError, ConciliacionCourierError) as exc:
        raise ExtraccionDHLInvalida('Tipo, número o fecha documental inválidos.') from exc
    moneda = _texto_requerido(extraccion, 'moneda').upper()
    if not re.fullmatch(r'[A-Z]{3}', moneda):
        raise ExtraccionDHLInvalida('Moneda documental inválida.')
    subtotal = _decimal_exacto(extraccion.get('subtotal'), 'subtotal')
    impuestos = _decimal_exacto(extraccion.get('impuestos'), 'impuestos')
    total = _decimal_exacto(extraccion.get('total'), 'total')
    if total <= 0 or abs(subtotal + impuestos - total) > CENTAVO_CONTROL:
        raise ExtraccionDHLInvalida('Subtotal más impuestos no coincide con el total.')
    crudos = extraccion.get('items')
    if not isinstance(crudos, list) or not crudos:
        raise ExtraccionDHLInvalida('La factura necesita renglones extraídos.')
    permitidos_item = {
        'linea_numero', 'tracking', 'concepto_tipo', 'concepto_codigo',
        'descripcion', 'importe', 'signo', 'tipo_cambio_ars',
        'peso_real_kg', 'peso_volumetrico_kg', 'peso_facturado_kg', 'peso_base',
    }
    items = []
    observaciones = []
    lineas = set()
    for crudo in crudos:
        if not isinstance(crudo, Mapping) or set(crudo) - permitidos_item:
            raise ExtraccionDHLInvalida('Un renglón contiene campos no permitidos.')
        linea = crudo.get('linea_numero')
        if type(linea) is not int or linea <= 0 or linea in lineas:
            raise ExtraccionDHLInvalida('Número de línea inválido o duplicado.')
        lineas.add(linea)
        signo = crudo.get('signo', 1)
        if type(signo) is not int or signo not in (-1, 1):
            raise ExtraccionDHLInvalida('Signo de línea inválido.')
        item = dict(crudo)
        item['importe'] = _decimal_exacto(item.get('importe'), 'importe')
        for campo in ('peso_real_kg', 'peso_volumetrico_kg', 'peso_facturado_kg'):
            if item.get(campo) is not None:
                item[campo] = _decimal_exacto(item[campo], campo)
        fx = item.get('tipo_cambio_ars')
        if moneda != 'ARS' and fx is None:
            raise ExtraccionDHLInvalida('Falta el tipo de cambio documentado; no se usa el dólar del día.')
        item['tipo_cambio_ars'] = _decimal_exacto(fx if fx is not None else 1, 'tipo_cambio_ars')
        if moneda == 'ARS' and item['tipo_cambio_ars'] != 1:
            raise ExtraccionDHLInvalida('Una factura en ARS debe conservar tipo de cambio 1.')
        if not isinstance(item.get('concepto_tipo'), str) or not item['concepto_tipo'].strip():
            raise ExtraccionDHLInvalida('Falta clasificar el concepto de un renglón.')
        if item.get('tracking') is not None and not isinstance(item['tracking'], str):
            raise ExtraccionDHLInvalida('Tracking inválido; debe conservarse como texto.')
        if not str(item.get('tracking') or '').strip():
            observaciones.append(f'Línea {linea}: sin tracking; requiere asignación manual.')
        try:
            preparado = _preparar_item(item, moneda_documento=moneda)
        except ConciliacionCourierError as exc:
            raise ExtraccionDHLInvalida(str(exc)) from exc
        # El escritor canónico espera tracking, no su representación interna.
        preparado['tracking'] = preparado.pop('tracking_raw')
        items.append(preparado)
    suma = sum((i['importe'] * i['signo'] for i in items), Decimal('0'))
    if abs(suma - total) > CENTAVO_CONTROL:
        raise ExtraccionDHLInvalida('Los renglones no suman el total; no se completa la diferencia por inferencia.')
    if tipo in ('NC', 'ND'):
        observaciones.append('Vincular y revisar el documento original antes de conciliar esta NC/ND.')
    cuenta_hash = hashlib.sha256(cuenta_correo.strip().lower().encode()).hexdigest()
    origen_hash = hashlib.sha256(
        (cuenta_hash + '\0' + mensaje_id.strip() + '\0' + adjunto_id.strip()).encode()
    ).hexdigest()
    return PreparacionFacturaDHL(
        datos_registro={
            'courier': 'DHL', 'tipo_documento': tipo, 'numero': numero,
            'moneda': moneda, 'subtotal': subtotal, 'impuestos': impuestos,
            'total': total, 'fecha_emision': emision,
            'fecha_vencimiento': vencimiento,
            'items': sorted(items, key=lambda i: i['linea_numero']),
            'archivo_nombre': archivo_nombre,
            'archivo_contenido': archivo_pdf,
            'archivo_sha256': hashlib.sha256(archivo_pdf).hexdigest(),
            'mensaje_origen_id': origen_hash,
            'metadatos_origen': {
                'canal': 'correo_dhl', 'contrato_version': 1,
                'cuenta_sha256': cuenta_hash,
                'mensaje_id': mensaje_id.strip(), 'adjunto_id': adjunto_id.strip(),
                'revision_extraccion_requerida': True,
            },
        },
        observaciones=tuple(observaciones),
    )
