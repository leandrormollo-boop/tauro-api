"""Intake y controles transaccionales con datos sintéticos y PostgreSQL aislado."""
import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import psycopg2
import pytest

from test_conciliacion_couriers_postgres import (
    conciliacion_db, _crear_solicitud, DATABASE_URL,
)
from test_entrada_facturas_dhl import ejemplo
from servicios import bandeja_facturas_dhl as bandeja
from servicios import correo_facturas_dhl as correo
from servicios.entrada_facturas_dhl import ExtraccionDHLInvalida
from servicios.conciliacion_couriers import DocumentoCourierDuplicadoError

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason='requiere PostgreSQL aislado')
NUMERO = '1700A00000001'
CUIT = '20123456786'
PDF = b'%PDF-evidencia-sintetica'


@pytest.fixture
def db(conciliacion_db, monkeypatch):
    monkeypatch.setattr(bandeja, 'get_conn', conciliacion_db)
    datos = ejemplo() | {'numero': NUMERO}
    # Impuesto general sin atribuir cliente.
    datos['items'][1]['tracking'] = None
    monkeypatch.setattr(bandeja, 'ejecutar_lector_dhl',
                        lambda *a, **kw: {'extraccion': deepcopy(datos), 'observaciones': []})
    return conciliacion_db


def recibir(**kw):
    return bandeja.recibir_pdf_dhl(**{'pdf': PDF, 'nombre': 'original.pdf',
        'numero': NUMERO, 'cuit': CUIT, 'actor': 'test', **kw})


def recibir_correo(**kw):
    return bandeja.recibir_pdf_dhl_correo(**{'pdf': PDF + b'-correo', 'nombre': 'original-correo.pdf',
        'numero': NUMERO, 'cuit': CUIT, 'actor': 'sistema:correo-dhl',
        'correo_mensaje_id': 'gmail-message-1', 'correo_adjunto_id': 'gmail-attachment-1', **kw})


def leer(entrada_id, **kw):
    return bandeja.leer_entrada_dhl(entrada_id, **{'numero': NUMERO, 'cuit': CUIT, 'actor': 'test', **kw})


def importar(entrada_id, **kw):
    entrada = bandeja.obtener_entrada_dhl(entrada_id)
    return bandeja.importar_entrada_dhl(entrada_id, **{
        'revision_sha256': entrada['revision_sha256'], 'revision_confirmada': True, 'actor': 'test', **kw})


def contar(db, tabla):
    with db() as conn, conn.cursor() as cur:
        cur.execute(f'SELECT COUNT(*) AS n FROM {tabla}')
        return cur.fetchone()['n']


def configurar_correo(monkeypatch, db, *, auto_import=False, max_attempts='5'):
    for nombre, valor in {
        'BASE_URL': 'https://taurosolutions.ar',
        'DHL_GMAIL_CLIENT_ID': 'client-id',
        'DHL_GMAIL_CLIENT_SECRET': 'client-secret',
        'DHL_GMAIL_ACCOUNT': 'taurosolutionsar@gmail.com',
        'DHL_GMAIL_CUIT': CUIT,
        'DHL_GMAIL_TOKEN_ENCRYPTION_KEY': 'clave-exclusiva-dhl-gmail-segura-2026',
        'DHL_GMAIL_AUTO_IMPORT': 'true' if auto_import else 'false',
        'DHL_GMAIL_MAX_ATTEMPTS': max_attempts,
    }.items():
        monkeypatch.setenv(nombre, valor)
    monkeypatch.setattr(correo, 'get_conn', db)
    with db() as conn, conn.cursor() as cur:
        cur.execute('''INSERT INTO integracion_correo_dhl
            (id, cuenta_email, access_token_cifrado, refresh_token_cifrado,
             token_expira_at, scopes, estado, conectado_por)
            VALUES (1,%s,'enc:v1:access','enc:v1:refresh',NOW() + interval '1 hour',
                    %s,'CONECTADA','test')''',
            ('taurosolutionsar@gmail.com', correo.GMAIL_READONLY))


def mensaje_gmail(mensaje_id, pdf, *, adjunto_id=None):
    adjunto_id = adjunto_id or 'att-' + mensaje_id
    return {
        'id': mensaje_id, 'threadId': 'thread-' + mensaje_id,
        'historyId': 'history-' + mensaje_id, 'labelIds': ['INBOX'],
        'payload': {
            'mimeType': 'multipart/mixed', 'headers': [
                {'name': 'From', 'value': 'DHL Argentina <AR.E-Billing@dhl.com>'},
                {'name': 'Subject', 'value':
                    'DHL Invoice services 20123456786 – 1700A00000001'},
                {'name': 'Authentication-Results', 'value':
                    'mx.google.com; dkim=pass header.i=@dhl.com; '
                    'spf=pass smtp.mailfrom=dhl.com; dmarc=pass header.from=dhl.com'},
            ], 'parts': [{
                'mimeType': 'application/pdf',
                'filename': 'DHL-1700A00000001_02092026.pdf',
                'partId': '1', 'body': {'attachmentId': adjunto_id, 'size': len(pdf)},
            }],
        },
    }


def cliente_gmail_sintetico(mensajes, pdfs, *, paginas=None, consultas=None):
    class GmailSintetico:
        cuenta = 'taurosolutionsar@gmail.com'

        def get(self, ruta, *, params=None):
            if ruta == '/users/me/messages':
                if consultas is not None:
                    consultas.append(dict(params or {}))
                pagina = (params or {}).get('pageToken')
                if paginas is not None:
                    return deepcopy(paginas[pagina])
                return {'messages': [
                    {'id': m['id'], 'threadId': m['threadId']} for m in mensajes.values()
                ]}
            for mensaje_id, mensaje in mensajes.items():
                if ruta == f'/users/me/messages/{mensaje_id}':
                    return deepcopy(mensaje)
                adjunto = mensaje['payload']['parts'][0]['body']['attachmentId']
                if ruta.endswith('/attachments/' + adjunto):
                    return {'data': base64.urlsafe_b64encode(pdfs[mensaje_id]).decode().rstrip('=')}
            raise AssertionError(f'consulta Gmail inesperada: {ruta}')

    return GmailSintetico


def test_flujo_solo_evidencia_matches_propuestos_y_sin_saldos(db):
    solicitud = _crear_solicitud(db, sufijo='DHL_INTAKE', tracking='0123456789')
    entrada_id = recibir()['id']
    assert bandeja.obtener_entrada_dhl(entrada_id)['estado'] == 'RECIBIDA'
    assert contar(db, 'facturas_courier') == 0
    assert leer(entrada_id) == 'PARA_REVISION'
    assert contar(db, 'facturas_courier') == 0
    factura = importar(entrada_id)
    assert not factura['duplicado']
    assert bandeja.obtener_entrada_dhl(entrada_id)['estado'] == 'IMPORTADA'
    assert importar(entrada_id) == {'id': factura['id'], 'duplicado': True}
    assert contar(db, 'facturas_courier') == 1
    assert contar(db, 'facturas_courier_items') == 2
    for tabla in ('envios', 'pagos', 'ajustes_cliente', 'conciliaciones_envio'):
        assert contar(db, tabla) == 0
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT solicitud_id, estado FROM factura_courier_item_matches')
        assert [dict(r) for r in cur.fetchall()] == [{'solicitud_id': solicitud, 'estado': 'PROPUESTO'}]
        cur.execute('SELECT metadatos_origen, archivo_pdf FROM facturas_courier')
        f = cur.fetchone()
        assert bytes(f['archivo_pdf']) == PDF
        assert f['metadatos_origen']['canal'] == 'admin_pdf_dhl'
        assert 'mensaje_id' not in f['metadatos_origen']
        assert f['metadatos_origen']['revision_financiera_pendiente'] is True
        cur.execute('SELECT precio_tauro_ars FROM solicitudes_guia WHERE id=%s', (solicitud,))
        assert cur.fetchone()['precio_tauro_ars'] == 10000


def test_correo_autenticado_importa_y_matchea_sin_confirmar_ni_cobrar(db):
    solicitud = _crear_solicitud(db, sufijo='DHL_CORREO', tracking='0123456789')
    entrada_id = recibir_correo()['id']
    assert leer(entrada_id) == 'PARA_REVISION'
    factura = bandeja.importar_entrada_dhl_correo(
        entrada_id, cuenta_correo='taurosolutionsar@gmail.com', actor='sistema:correo-dhl')
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT metadatos_origen FROM facturas_courier WHERE id=%s', (factura['id'],))
        metadata = cur.fetchone()['metadatos_origen']
        assert metadata['canal'] == 'correo_dhl'
        assert metadata['revision_extraccion_requerida'] is True
        assert metadata['revision_financiera_pendiente'] is True
        cur.execute('SELECT revisado_por, revisado_at FROM entradas_pdf_dhl WHERE id=%s',
                    (entrada_id,))
        assert dict(cur.fetchone()) == {'revisado_por': None, 'revisado_at': None}
        cur.execute('SELECT solicitud_id, estado FROM factura_courier_item_matches')
        assert [dict(r) for r in cur.fetchall()] == [
            {'solicitud_id': solicitud, 'estado': 'PROPUESTO'},
        ]
    for tabla in ('envios', 'pagos', 'ajustes_cliente', 'conciliaciones_envio'):
        assert contar(db, tabla) == 0


def test_sincronizacion_gmail_completa_es_idempotente_y_no_genera_deuda(db, monkeypatch):
    pdf = PDF + b'-sincronizacion-gmail'
    mensaje = {
        'id': 'gmail-sync-1', 'threadId': 'thread-1', 'historyId': 'history-1',
        'labelIds': ['INBOX'], 'payload': {
            'mimeType': 'multipart/mixed', 'headers': [
                {'name': 'From', 'value': 'DHL Argentina <AR.E-Billing@dhl.com>'},
                {'name': 'Subject', 'value':
                    'DHL Invoice services 20123456786 – 1700A00000001'},
                {'name': 'Authentication-Results', 'value':
                    'mx.google.com; dkim=pass header.i=@dhl.com; '
                    'spf=pass smtp.mailfrom=dhl.com; dmarc=pass header.from=dhl.com'},
            ], 'parts': [{
                'mimeType': 'application/pdf',
                'filename': 'DHL-1700A00000001_02092026.pdf',
                'partId': '1', 'body': {'attachmentId': 'att-sync-1', 'size': len(pdf)},
            }],
        },
    }
    mensaje_repetido = deepcopy(mensaje)
    mensaje_repetido.update(id='gmail-sync-2', threadId='thread-2', historyId='history-2')
    mensaje_repetido['payload']['parts'][0]['body']['attachmentId'] = 'att-sync-2'

    class GmailSintetico:
        cuenta = 'taurosolutionsar@gmail.com'

        def get(self, ruta, *, params=None):
            if ruta == '/users/me/messages':
                return {'messages': [
                    {'id': mensaje['id'], 'threadId': mensaje['threadId']},
                    {'id': mensaje_repetido['id'], 'threadId': mensaje_repetido['threadId']},
                ]}
            if ruta.endswith('/attachments/att-sync-1') or ruta.endswith('/attachments/att-sync-2'):
                return {'data': base64.urlsafe_b64encode(pdf).decode().rstrip('=')}
            if ruta == '/users/me/messages/gmail-sync-1':
                return deepcopy(mensaje)
            if ruta == '/users/me/messages/gmail-sync-2':
                return deepcopy(mensaje_repetido)
            raise AssertionError(f'consulta Gmail inesperada: {ruta}')

    for nombre, valor in {
        'BASE_URL': 'https://taurosolutions.ar',
        'DHL_GMAIL_CLIENT_ID': 'client-id',
        'DHL_GMAIL_CLIENT_SECRET': 'client-secret',
        'DHL_GMAIL_ACCOUNT': 'taurosolutionsar@gmail.com',
        'DHL_GMAIL_CUIT': CUIT,
        'DHL_GMAIL_TOKEN_ENCRYPTION_KEY': 'clave-exclusiva-dhl-gmail-segura-2026',
        'DHL_GMAIL_AUTO_IMPORT': 'true',
    }.items():
        monkeypatch.setenv(nombre, valor)
    monkeypatch.setattr(correo, 'get_conn', db)
    monkeypatch.setattr(correo, '_ClienteGmail', GmailSintetico)
    _crear_solicitud(db, sufijo='DHL_SYNC', tracking='0123456789')
    with db() as conn, conn.cursor() as cur:
        cur.execute('''INSERT INTO integracion_correo_dhl
            (id, cuenta_email, access_token_cifrado, refresh_token_cifrado,
             token_expira_at, scopes, estado, conectado_por)
            VALUES (1,%s,'enc:v1:access','enc:v1:refresh',NOW() + interval '1 hour',
                    %s,'CONECTADA','test')''',
            ('taurosolutionsar@gmail.com', correo.GMAIL_READONLY))

    primero = correo.sincronizar_facturas_dhl()
    segundo = correo.sincronizar_facturas_dhl()
    assert primero == {'estado': 'OK', 'procesados': 2, 'resultados': {'IMPORTADO': 2}}
    assert segundo == {'estado': 'OK', 'procesados': 2, 'resultados': {'YA_PROCESADO': 2}}
    assert contar(db, 'facturas_courier') == 1
    assert contar(db, 'entradas_pdf_dhl') == 1
    assert contar(db, 'correos_dhl_procesados') == 2
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT estado, intentos, entrada_id, autenticacion '
                    'FROM correos_dhl_procesados WHERE gmail_mensaje_id=%s',
                    (mensaje['id'],))
        guardado = cur.fetchone()
        assert guardado['estado'] == 'IMPORTADO'
        assert guardado['intentos'] == 1
        assert guardado['entrada_id'] is not None
        assert guardado['autenticacion'] == {'dkim': True, 'dmarc': True, 'spf': True}
        cur.execute('SELECT intentos, entrada_id FROM correos_dhl_procesados ORDER BY gmail_mensaje_id')
        checkpoints = cur.fetchall()
        assert [r['intentos'] for r in checkpoints] == [1, 1]
        assert len({r['entrada_id'] for r in checkpoints}) == 1
    with pytest.raises(psycopg2.Error, match='origen autenticado'), db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE correos_dhl_procesados SET gmail_adjunto_id='otro-adjunto'")
    for tabla in ('envios', 'pagos', 'ajustes_cliente', 'conciliaciones_envio'):
        assert contar(db, tabla) == 0


def test_refresh_token_invalido_deja_integracion_en_reautorizar(db, monkeypatch):
    for nombre, valor in {
        'BASE_URL': 'https://taurosolutions.ar',
        'DHL_GMAIL_CLIENT_ID': 'client-id',
        'DHL_GMAIL_CLIENT_SECRET': 'client-secret',
        'DHL_GMAIL_ACCOUNT': 'taurosolutionsar@gmail.com',
        'DHL_GMAIL_CUIT': CUIT,
        'DHL_GMAIL_TOKEN_ENCRYPTION_KEY': 'clave-exclusiva-dhl-gmail-segura-2026',
    }.items():
        monkeypatch.setenv(nombre, valor)
    monkeypatch.setattr(correo, 'get_conn', db)
    monkeypatch.setattr(correo, '_post_token', lambda *a, **kw: (_ for _ in ()).throw(
        correo.ConfiguracionCorreoDHL('refresh rechazado')))
    with db() as conn, conn.cursor() as cur:
        cur.execute('''INSERT INTO integracion_correo_dhl
            (id, cuenta_email, access_token_cifrado, refresh_token_cifrado,
             token_expira_at, scopes, estado, conectado_por)
            VALUES (1,%s,%s,%s,NOW() - interval '1 hour',%s,'CONECTADA','test')''', (
                'taurosolutionsar@gmail.com', correo._cifrar('access'),
                correo._cifrar('refresh'), correo.GMAIL_READONLY,
            ))
    assert correo.sincronizar_facturas_dhl() == {'estado': 'REAUTORIZAR', 'procesados': 0}
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT estado, ultimo_error_codigo FROM integracion_correo_dhl WHERE id=1')
        assert dict(cur.fetchone()) == {
            'estado': 'REAUTORIZAR', 'ultimo_error_codigo': 'REAUTORIZAR',
        }


def test_refresh_recifra_ambos_tokens_al_rotar_la_clave(db, monkeypatch):
    clave_anterior = 'clave-anterior-exclusiva-dhl-gmail-2026'
    clave_nueva = 'clave-nueva-exclusiva-dhl-gmail-2026'
    for nombre, valor in {
        'BASE_URL': 'https://taurosolutions.ar',
        'DHL_GMAIL_CLIENT_ID': 'client-id',
        'DHL_GMAIL_CLIENT_SECRET': 'client-secret',
        'DHL_GMAIL_ACCOUNT': 'taurosolutionsar@gmail.com',
        'DHL_GMAIL_CUIT': CUIT,
        'DHL_GMAIL_TOKEN_ENCRYPTION_KEY': clave_anterior,
    }.items():
        monkeypatch.setenv(nombre, valor)
    monkeypatch.setattr(correo, 'get_conn', db)
    access_anterior = correo._cifrar('access-anterior')
    refresh_anterior = correo._cifrar('refresh-conservado')
    with db() as conn, conn.cursor() as cur:
        cur.execute('''INSERT INTO integracion_correo_dhl
            (id, cuenta_email, access_token_cifrado, refresh_token_cifrado,
             token_expira_at, scopes, estado, conectado_por)
            VALUES (1,%s,%s,%s,NOW() - interval '1 hour',%s,'CONECTADA','test')''', (
                'taurosolutionsar@gmail.com', access_anterior, refresh_anterior,
                correo.GMAIL_READONLY,
            ))
    monkeypatch.setenv('DHL_GMAIL_TOKEN_ENCRYPTION_KEY_PREVIOUS', clave_anterior)
    monkeypatch.setenv('DHL_GMAIL_TOKEN_ENCRYPTION_KEY', clave_nueva)
    monkeypatch.setattr(correo, '_post_token', lambda *a, **kw: {
        'access_token': 'access-nuevo', 'expires_in': 3600,
    })

    assert correo._token_acceso() == 'access-nuevo'
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT access_token_cifrado, refresh_token_cifrado '
                    'FROM integracion_correo_dhl WHERE id=1')
        tokens = dict(cur.fetchone())
    monkeypatch.delenv('DHL_GMAIL_TOKEN_ENCRYPTION_KEY_PREVIOUS')
    assert correo._descifrar(tokens['access_token_cifrado']) == 'access-nuevo'
    assert correo._descifrar(tokens['refresh_token_cifrado']) == 'refresh-conservado'


def test_reintento_gmail_con_entrada_conservada_no_viola_check(db, monkeypatch):
    pdf = PDF + b'-retry-gmail'
    mensaje = mensaje_gmail('gmail-retry-1', pdf)
    configurar_correo(monkeypatch, db, auto_import=False)
    monkeypatch.setattr(
        correo, '_ClienteGmail',
        cliente_gmail_sintetico({mensaje['id']: mensaje}, {mensaje['id']: pdf}),
    )
    lector_ok = bandeja.ejecutar_lector_dhl
    monkeypatch.setattr(
        bandeja, 'ejecutar_lector_dhl',
        lambda *a, **kw: (_ for _ in ()).throw(
            bandeja.LectorDHLNoDisponible('Lector temporalmente ocupado')
        ),
    )

    primero = correo.sincronizar_facturas_dhl()
    assert primero == {
        'estado': 'OK', 'procesados': 1, 'resultados': {'REINTENTAR': 1},
    }
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE correos_dhl_procesados SET updated_at=NOW()-interval '1 day'")
    monkeypatch.setattr(bandeja, 'ejecutar_lector_dhl', lector_ok)
    # Ya no aparece en la ventana normal de Gmail: debe recuperarse por el
    # checkpoint REINTENTAR, no quedar varado al avanzar el cursor.
    monkeypatch.setattr(
        correo, '_ClienteGmail',
        cliente_gmail_sintetico(
            {mensaje['id']: mensaje}, {mensaje['id']: pdf},
            paginas={None: {'messages': []}},
        ),
    )

    segundo = correo.sincronizar_facturas_dhl()
    assert segundo == {
        'estado': 'OK', 'procesados': 1, 'resultados': {'PARA_REVISION': 1},
    }
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT estado, intentos, entrada_id FROM correos_dhl_procesados')
        checkpoint = dict(cur.fetchone())
        assert checkpoint['estado'] == 'PARA_REVISION'
        assert checkpoint['intentos'] == 2 and checkpoint['entrada_id'] is not None
    assert contar(db, 'entradas_pdf_dhl') == 1
    assert contar(db, 'facturas_courier') == 0


def test_reintentos_agotados_pasan_correo_y_pdf_a_revision_manual(db, monkeypatch):
    pdf = PDF + b'-retry-max'
    mensaje = mensaje_gmail('gmail-retry-max', pdf)
    configurar_correo(monkeypatch, db, auto_import=False, max_attempts='1')
    monkeypatch.setattr(
        correo, '_ClienteGmail',
        cliente_gmail_sintetico({mensaje['id']: mensaje}, {mensaje['id']: pdf}),
    )
    monkeypatch.setattr(
        bandeja, 'ejecutar_lector_dhl',
        lambda *a, **kw: (_ for _ in ()).throw(
            bandeja.LectorDHLNoDisponible('Lector temporalmente ocupado')
        ),
    )
    assert correo.sincronizar_facturas_dhl()['resultados'] == {'REINTENTAR': 1}
    assert correo.sincronizar_facturas_dhl()['resultados'] == {'REVISION_MANUAL': 1}
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT estado, error_codigo FROM correos_dhl_procesados')
        assert dict(cur.fetchone()) == {
            'estado': 'REVISION_MANUAL', 'error_codigo': 'REINTENTOS_AGOTADOS',
        }
        cur.execute('SELECT estado, error_lectura FROM entradas_pdf_dhl')
        entrada = dict(cur.fetchone())
        assert entrada['estado'] == 'REVISION_MANUAL'
        assert 'agotaron' in entrada['error_lectura']


def test_paginacion_gmail_continua_sin_adelantar_checkpoint(db, monkeypatch):
    pdf = PDF + b'-paginacion'
    uno = mensaje_gmail('gmail-page-1', pdf)
    dos = mensaje_gmail('gmail-page-2', pdf)
    paginas = {
        None: {'messages': [{'id': uno['id'], 'threadId': uno['threadId']}],
               'nextPageToken': 'pagina-2'},
        'pagina-2': {'messages': [{'id': dos['id'], 'threadId': dos['threadId']}]},
    }
    consultas = []
    configurar_correo(monkeypatch, db, auto_import=False)
    monkeypatch.setenv('DHL_GMAIL_SYNC_LIMIT', '1')
    monkeypatch.setattr(
        correo, '_ClienteGmail',
        cliente_gmail_sintetico(
            {uno['id']: uno, dos['id']: dos}, {uno['id']: pdf, dos['id']: pdf},
            paginas=paginas, consultas=consultas,
        ),
    )

    primero = correo.sincronizar_facturas_dhl()
    assert primero['estado'] == 'PAGINACION_PENDIENTE'
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT sync_page_token, sync_query_after, ultimo_sync_at '
                    'FROM integracion_correo_dhl WHERE id=1')
        parcial = cur.fetchone()
        assert parcial['sync_page_token'] == 'pagina-2'
        assert parcial['sync_query_after'] is not None and parcial['ultimo_sync_at'] is None

    segundo = correo.sincronizar_facturas_dhl()
    assert segundo['estado'] == 'OK'
    assert consultas[0].get('pageToken') is None
    assert consultas[1]['pageToken'] == 'pagina-2'
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT sync_page_token, sync_query_after, ultimo_sync_at '
                    'FROM integracion_correo_dhl WHERE id=1')
        completo = cur.fetchone()
        assert completo['sync_page_token'] is None
        assert completo['sync_query_after'] is None
        assert completo['ultimo_sync_at'] is not None
    assert contar(db, 'correos_dhl_procesados') == 2


def test_error_de_configuracion_del_conector_se_persiste(db, monkeypatch):
    configurar_correo(monkeypatch, db)

    class GmailMalConfigurado:
        def __init__(self):
            raise correo.ConfiguracionCorreoDHL('cuenta o clave inválida')

    monkeypatch.setattr(correo, '_ClienteGmail', GmailMalConfigurado)
    assert correo.sincronizar_facturas_dhl() == {
        'estado': 'ERROR_CONFIGURACION', 'procesados': 0,
    }
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT estado, ultimo_error_codigo FROM integracion_correo_dhl WHERE id=1')
        assert dict(cur.fetchone()) == {
            'estado': 'ERROR', 'ultimo_error_codigo': 'CONFIGURACION',
        }


def test_pdf_repetido_y_referencia_incompatible(db):
    primero = recibir()
    assert recibir(nombre='otro-nombre.pdf') == {'id': primero['id'], 'duplicado': True}
    with pytest.raises(ExtraccionDHLInvalida, match='otra referencia'):
        recibir(numero='1700A00000002')
    assert contar(db, 'entradas_pdf_dhl') == 1


def test_doble_upload_concurrente(db):
    with ThreadPoolExecutor(max_workers=2) as workers:
        resultados = list(workers.map(lambda _: recibir(), range(2)))
    assert resultados[0]['id'] == resultados[1]['id']
    assert sorted(r['duplicado'] for r in resultados) == [False, True]


def test_doble_importacion_concurrente(db):
    entrada_id = recibir()['id']
    leer(entrada_id)
    with ThreadPoolExecutor(max_workers=2) as workers:
        resultados = list(workers.map(lambda _: importar(entrada_id), range(2)))
    assert resultados[0]['id'] == resultados[1]['id']
    assert contar(db, 'facturas_courier') == 1


def test_error_lector_persiste_evidencia_y_permite_reintento(db, monkeypatch):
    entrada_id = recibir()['id']
    real = bandeja.ejecutar_lector_dhl
    def fallar(*a, **kw):
        raise ExtraccionDHLInvalida('Formato desconocido')
    monkeypatch.setattr(bandeja, 'ejecutar_lector_dhl', fallar)
    assert leer(entrada_id) == 'REVISION_MANUAL'
    e = bandeja.obtener_entrada_dhl(entrada_id, con_pdf=True)
    assert bytes(e['archivo_pdf']) == PDF and e['extraccion'] is None
    assert e['intentos'] == 1 and e['error_lectura'] == 'Formato desconocido'
    with pytest.raises(ExtraccionDHLInvalida):
        importar(entrada_id)
    monkeypatch.setattr(bandeja, 'ejecutar_lector_dhl', real)
    assert leer(entrada_id) == 'PARA_REVISION'
    assert bandeja.obtener_entrada_dhl(entrada_id)['intentos'] == 2


def test_referencia_admin_se_puede_corregir_antes_de_extraer(db):
    entrada_id = recibir(numero='1700A00000002')['id']
    assert leer(entrada_id) == 'PARA_REVISION'
    assert bandeja.obtener_entrada_dhl(entrada_id)['numero_esperado'] == NUMERO


@pytest.mark.parametrize('cambio', [{'revision_confirmada': False}, {'revision_confirmada': 'si'},
                                 {'revision_sha256': '0'*64}, {'revision_sha256': ''}])
def test_no_importa_sin_revision_explicita_y_exacta(db, cambio):
    entrada_id = recibir()['id']
    leer(entrada_id)
    with pytest.raises(ExtraccionDHLInvalida):
        importar(entrada_id, **cambio)
    assert contar(db, 'facturas_courier') == 0


def test_conflicto_de_fc_no_duplica_ni_marca_importada(db):
    uno = recibir()['id']
    leer(uno)
    importar(uno)
    dos = recibir(pdf=PDF + b'otro-contenido')['id']
    leer(dos)
    with pytest.raises(DocumentoCourierDuplicadoError):
        importar(dos)
    assert bandeja.obtener_entrada_dhl(dos)['estado'] == 'PARA_REVISION'
    assert contar(db, 'facturas_courier') == 1


def test_fallo_despues_de_registrar_hace_rollback_total(db, monkeypatch):
    entrada_id = recibir()['id']
    leer(entrada_id)
    def fallar(*a, **kw):
        raise RuntimeError('falla simulada después del INSERT de factura')
    monkeypatch.setattr(bandeja, 'matchear_items_exactos', fallar)
    with pytest.raises(RuntimeError):
        importar(entrada_id)
    assert contar(db, 'facturas_courier') == 0
    assert contar(db, 'facturas_courier_items') == 0
    assert bandeja.obtener_entrada_dhl(entrada_id)['estado'] == 'PARA_REVISION'


@pytest.mark.parametrize('sql', ["UPDATE entradas_pdf_dhl SET archivo_pdf='otro'",
                               "UPDATE entradas_pdf_dhl SET extraccion='{}'",
                               "DELETE FROM entradas_pdf_dhl"])
def test_evidencia_y_revision_no_se_alteran(db, sql):
    entrada_id = recibir()['id']
    leer(entrada_id)
    with pytest.raises(psycopg2.Error), db() as conn, conn.cursor() as cur:
        cur.execute(sql)
    assert bandeja.obtener_entrada_dhl(entrada_id)['estado'] == 'PARA_REVISION'


def test_listado_no_carga_pdfs_y_pagina_acotada(db):
    recibir()
    filas = bandeja.listar_entradas_dhl(pagina=-1)
    assert filas['pagina'] == 1 and not filas['hay_mas']
    assert 'archivo_pdf' not in filas['items'][0]
    assert 'archivo_pdf' not in bandeja.obtener_entrada_dhl(filas['items'][0]['id'])


def test_migracion_idempotente_preserva_entrada(db):
    from pathlib import Path
    entrada_id = recibir()['id']
    leer(entrada_id)
    with db() as conn, conn.cursor() as cur:
        cur.execute((Path(__file__).resolve().parents[1] / 'sql/schema.sql').read_text())
    assert bandeja.obtener_entrada_dhl(entrada_id)['estado'] == 'PARA_REVISION'


def test_version_obsoleta_se_bloquea_relee_y_conserva_historial(db, monkeypatch):
    entrada_id = recibir()['id']
    leer(entrada_id)
    original = bandeja.obtener_entrada_dhl(entrada_id)
    monkeypatch.setattr(bandeja, 'LECTOR_VERSION', bandeja.LECTOR_VERSION + 1)
    with pytest.raises(ExtraccionDHLInvalida, match='versión anterior'):
        importar(entrada_id)
    assert contar(db, 'facturas_courier') == 0
    leer(entrada_id)
    vigente = bandeja.obtener_entrada_dhl(entrada_id)
    assert original['revision_sha256'] != vigente['revision_sha256']
    assert vigente['lector_version'] == bandeja.LECTOR_VERSION
    with pytest.raises(ExtraccionDHLInvalida, match='revisión no coincide'):
        importar(entrada_id, revision_sha256=original['revision_sha256'])
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT * FROM historial_extracciones_dhl ORDER BY id')
        historial = cur.fetchall()
        assert len(historial) == 2
        assert historial[0]['extraccion'] == original['extraccion']
        assert historial[0]['revision_sha256'] == original['revision_sha256']
    assert importar(entrada_id)['id']
    assert contar(db, 'historial_extracciones_dhl') == 2


@pytest.mark.parametrize('previa', [False, True])
def test_saturacion_transitoria_no_rechaza_documento_ni_incrementa_intentos(db, monkeypatch, previa):
    entrada_id = recibir()['id']
    if previa:
        leer(entrada_id)
        monkeypatch.setattr(bandeja, 'LECTOR_VERSION', bandeja.LECTOR_VERSION + 1)
    antes = bandeja.obtener_entrada_dhl(entrada_id)
    real = bandeja.ejecutar_lector_dhl
    def ocupado(*a, **kw):
        raise bandeja.LectorDHLNoDisponible('Hay otras lecturas en curso.')
    monkeypatch.setattr(bandeja, 'ejecutar_lector_dhl', ocupado)
    assert leer(entrada_id) == 'REINTENTAR'
    despues = bandeja.obtener_entrada_dhl(entrada_id)
    assert despues['intentos'] == antes['intentos']
    assert despues['extraccion'] == antes['extraccion']
    assert despues['revision_sha256'] == antes['revision_sha256']
    with pytest.raises(ExtraccionDHLInvalida):
        importar(entrada_id)
    monkeypatch.setattr(bandeja, 'ejecutar_lector_dhl', real)
    assert leer(entrada_id) == 'PARA_REVISION'
    assert bandeja.obtener_entrada_dhl(entrada_id)['intentos'] == antes['intentos'] + 1


def test_fallo_documental_al_actualizar_conserva_revision_anterior(db, monkeypatch):
    entrada_id = recibir()['id']
    leer(entrada_id)
    original = bandeja.obtener_entrada_dhl(entrada_id)
    monkeypatch.setattr(bandeja, 'LECTOR_VERSION', bandeja.LECTOR_VERSION + 1)
    def invalido(*a, **kw):
        raise ExtraccionDHLInvalida('No validado con la versión nueva')
    monkeypatch.setattr(bandeja, 'ejecutar_lector_dhl', invalido)
    assert leer(entrada_id) == 'REVISION_MANUAL'
    assert bandeja.obtener_entrada_dhl(entrada_id)['extraccion'] == original['extraccion']
    assert contar(db, 'historial_extracciones_dhl') == 1


def test_no_degrada_ni_modifica_entradas_ya_importadas(db, monkeypatch):
    entrada_id = recibir()['id']
    leer(entrada_id)
    version = bandeja.LECTOR_VERSION
    monkeypatch.setattr(bandeja, 'LECTOR_VERSION', version-1)
    with pytest.raises(ExtraccionDHLInvalida, match='no se puede degradar'):
        leer(entrada_id)
    monkeypatch.setattr(bandeja, 'LECTOR_VERSION', version)
    factura = importar(entrada_id)
    monkeypatch.setattr(bandeja, 'LECTOR_VERSION', version+1)
    assert leer(entrada_id) == 'IMPORTADA'
    assert importar(entrada_id) == {'id': factura['id'], 'duplicado': True}


@pytest.mark.parametrize('sql', ['UPDATE historial_extracciones_dhl SET lector_version=100',
                               'DELETE FROM historial_extracciones_dhl'])
def test_historial_no_se_puede_sobrescribir(db, sql):
    leer(recibir()['id'])
    with pytest.raises(psycopg2.Error), db() as conn, conn.cursor() as cur:
        cur.execute(sql)
