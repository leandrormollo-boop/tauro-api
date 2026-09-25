"""Cotización nacional de referencia: permisos y pricing del cliente, sin emitir."""
from decimal import Decimal
from urllib.parse import urlencode
import uuid

from servicios.carrier_adapter import OperationState, validate_quote_result
from servicios.carrier_contract import Ambito, carriers_for
from servicios.configuracion_couriers_cliente import configuracion_cotizacion
from servicios.cotizador_nacional import preparar_cotizacion_nacional
from servicios import oca_portal


def cotizar_referencia_nacional(cliente: str, **datos) -> dict:
    normal = preparar_cotizacion_nacional(**datos)
    if normal['modalidad'] != {'origen': 'domicilio', 'destino': 'domicilio'}:
        raise ValueError('Por ahora cotizamos retiro y entrega a domicilio.')
    acceso = configuracion_cotizacion(cliente)
    habilitados = acceso['couriers_habilitados']
    opciones, no_disponibles = [], []
    b = normal['bultos'][0]
    payload = {
        'origin': {'cp': normal['origen']['cp4']},
        'destination': {'cp': normal['destino']['cp4']},
        'declared_value': normal['totales']['valor_declarado_ars'],
        'packages': [{'quantity': b['cantidad'], 'weight_kg': b['peso_unitario_kg'],
                      'length_cm': b['largo_cm'], 'width_cm': b['ancho_cm'],
                      'height_cm': b['alto_cm']}],
    }
    # La cotización sólo necesita CP y bultos. La dirección/contacto completos
    # se validan al continuar con el envío, nunca en esta consulta de tarifas.
    prefill = {k: datos[k] for k in ('origen_provincia', 'origen_localidad',
               'destino_provincia', 'destino_localidad')}
    prefill.update(origen_provincia=normal['origen']['provincia_codigo'],
                   destino_provincia=normal['destino']['provincia_codigo'], origen_cp=payload['origin']['cp'], destino_cp=payload['destination']['cp'],
                   cantidad_bultos=b['cantidad'], peso_kg=b['peso_unitario_kg'],
                   largo_cm=b['largo_cm'], ancho_cm=b['ancho_cm'], alto_cm=b['alto_cm'],
                   valor_declarado_ars=payload['declared_value'])
    for spec in carriers_for(Ambito.NACIONAL):
        if spec.id not in habilitados:
            continue
        try:
            # Cada conexión nueva debe resolver su pricing por cliente antes de
            # incorporarse aquí. Un adapter genérico no autoriza márgenes globales.
            if spec.id != 'oca':
                raise RuntimeError('Adapter de portal pendiente')
            _, adapter = oca_portal.adapter_cliente(cliente)
            resultados = adapter.quote(oca_portal.request_cotizacion(cliente, payload, uuid.uuid4().hex))
            ofertas = []
            for result in resultados:
                validate_quote_result(result, spec.id)
                if result.state != OperationState.COTIZADO:
                    continue
                if result.currency != 'ARS' or result.carrier_currency != 'ARS':
                    continue
                if result.customer_price < result.carrier_cost:
                    continue
                ofertas.append({
                    'carrier_id': spec.id, 'carrier_nombre': spec.nombre, 'carrier_logo': spec.logo,
                    'servicio': result.service_name, 'servicio_nombre': result.service_name,
                    'precio_final_ars': result.customer_price, 'dias_estimados': result.estimated_days,
                    'continuar_url': '/portal/oca/nuevo?' + urlencode(prefill),
                })
            opciones.extend(ofertas)
            if not ofertas:
                no_disponibles.append({'id': spec.id, 'nombre': spec.nombre,
                                       'motivo': 'No devolvió tarifa para esta ruta.'})
        except Exception:
            # Nunca enviar credenciales, respuestas XML o detalles comerciales al HTML.
            no_disponibles.append({'id': spec.id, 'nombre': spec.nombre,
                                   'motivo': 'No pudimos consultar su tarifa. Intentá nuevamente.'})
    opciones.sort(key=lambda op: Decimal(str(op['precio_final_ars'])))
    return {'opciones': opciones, 'no_disponibles': no_disponibles,
            'encontrado': bool(opciones), 'resumen': {'ruta': normal['ruta'],
            'cantidad_bultos': b['cantidad'], 'peso_real_kg': normal['totales']['peso_real_kg']}}
