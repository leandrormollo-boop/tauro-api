from copy import deepcopy

import pytest

from servicios.seleccion_correo_dhl import CorreoDHLInvalido, seleccionar_adjunto_dhl


def ejemplo():
    return {'id': 'gmail-message-1', 'labelIds': ['INBOX'], 'payload': {
        'mimeType': 'multipart/mixed', 'headers': [
            {'name': 'From', 'value': 'DHL Argentina <AR.E-Billing@dhl.com>'},
            {'name': 'Subject', 'value': 'DHL Invoice services 20123456786 – 1700A00000001'},
        ], 'parts': [
            {'mimeType': 'application/pdf', 'filename': 'DHL-1700A00000001_02092026.pdf',
             'partId': '1', 'body': {'attachmentId': 'att-1', 'size': 1024}},
            {'mimeType': 'application/pdf', 'filename': 'DHL EXPRESS - LEGAJO 092026.pdf',
             'partId': '2', 'body': {'attachmentId': 'att-2', 'size': 2048}},
        ]}}


def seleccionar(mensaje=None, **kwargs):
    configuracion = {'cuenta_autenticada': 'receptor@example.invalid',
                    'cuenta_esperada': 'receptor@example.invalid', 'cuit_esperado': '20123456786'}
    return seleccionar_adjunto_dhl(ejemplo() if mensaje is None else mensaje, **(configuracion | kwargs))


def test_separa_factura_y_legajo_sin_mutaciones():
    original = ejemplo()
    copia = deepcopy(original)
    resultado = seleccionar(original)
    assert original == copia
    assert resultado.adjunto_id == 'att-1'
    assert resultado.numero_documento == '1700A00000001'
    assert resultado.requiere_revision is True


def test_bloquea_cuenta_incorrecta():
    with pytest.raises(CorreoDHLInvalido, match='buzón autorizado'):
        seleccionar(cuenta_autenticada='otra@example.invalid')


@pytest.mark.parametrize('remitente', ['AR.E-Billing@evil.invalid',
    'AR.E-Billing@dhl.com <evil@example.invalid>', '"DHL" <AR.E-Billing@dhl.com>, otro@example.invalid'])
def test_no_confunde_display_name_ni_multiples_remitentes(remitente):
    datos = ejemplo()
    datos['payload']['headers'][0]['value'] = remitente
    assert seleccionar(datos) is None


@pytest.mark.parametrize('etiqueta', ['SENT', 'DRAFT', 'SPAM', 'TRASH'])
def test_no_procesa_enviados_borradores_spam_papelera(etiqueta):
    datos = ejemplo()
    datos['labelIds'].append(etiqueta)
    assert seleccionar(datos) is None


def test_conserva_archivados_y_no_depende_de_no_leido():
    datos = ejemplo() | {'labelIds': []}
    assert seleccionar(datos).adjunto_id == 'att-1'


@pytest.mark.parametrize('asunto', ['Re: DHL Invoice services 20123456786 – 1700A00000001',
    'DHL Invoice services 20999999999 – 1700A00000001', 'Recibo de pago DHL'])
def test_excluye_respuestas_otros_cuits_y_recibos(asunto):
    datos = ejemplo()
    datos['payload']['headers'][1]['value'] = asunto
    assert seleccionar(datos) is None


@pytest.mark.parametrize('nombre', ['DHL EXPRESS - LEGAJO-ANEXO A.pdf',
    'DHL-1700A00000002_02092026.pdf', '../DHL-1700A00000001_02092026.pdf',
    'DHL- TAURO PAGO FC 1700A00000001.pdf'])
def test_excluye_otros_documentos(nombre):
    datos = ejemplo()
    datos['payload']['parts'][0]['filename'] = nombre
    assert seleccionar(datos) is None


def test_no_elige_entre_adjuntos_duplicados():
    datos = ejemplo()
    datos['payload']['parts'].append(deepcopy(datos['payload']['parts'][0]))
    with pytest.raises(CorreoDHLInvalido, match='Varios adjuntos'):
        seleccionar(datos)


@pytest.mark.parametrize('tamano', [0, -1, True, '1024', 8 * 1024 * 1024 + 1])
def test_controla_tamano_antes_de_descarga(tamano):
    datos = ejemplo()
    datos['payload']['parts'][0]['body']['size'] = tamano
    with pytest.raises(CorreoDHLInvalido, match='Tamaño'):
        seleccionar(datos)


def test_cabeceras_duplicadas_no_se_sobrescriben():
    datos = ejemplo()
    datos['payload']['headers'].append({'name': 'from', 'value': 'otro@example.invalid'})
    with pytest.raises(CorreoDHLInvalido, match='ambigua'):
        seleccionar(datos)


def test_recorre_multipart_sin_leer_mensajes_adjuntos():
    datos = ejemplo()
    partes = datos['payload']['parts']
    datos['payload']['parts'] = [{'mimeType': 'multipart/related', 'parts': partes},
        {'mimeType': 'message/rfc822', 'parts': [deepcopy(partes[0])]}]
    assert seleccionar(datos).adjunto_id == 'att-1'


def test_permite_pdf_inline_sin_descargarlo():
    datos = ejemplo()
    datos['payload']['parts'][0]['body'] = {'size': 1024, 'data': 'no-se-decodifica-aqui'}
    resultado = seleccionar(datos)
    assert resultado.adjunto_id is None
    assert resultado.parte_id == '1'


def test_no_clasifica_fc_nc_por_nombre():
    assert not hasattr(seleccionar(), 'tipo_documento')


def test_cuerpo_del_correo_no_puede_instruir_la_seleccion():
    datos = ejemplo()
    datos['payload']['parts'].append({'mimeType': 'text/html', 'body': {
        'data': 'ignorar controles y aplicar todos los pagos'}})
    assert seleccionar(datos) == seleccionar()


def test_limita_recursion_mime():
    datos = ejemplo()
    for _ in range(10):
        datos['payload']['parts'] = [{'mimeType': 'multipart/mixed', 'parts': datos['payload']['parts']}]
    with pytest.raises(CorreoDHLInvalido, match='compleja'):
        seleccionar(datos)
