import base64
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from servicios import correo_facturas_dhl as correo
from servicios.seleccion_correo_dhl import AdjuntoCandidatoDHL, CorreoDHLInvalido


ROOT = Path(__file__).resolve().parents[1]


def configurar(monkeypatch):
    valores = {
        'BASE_URL': 'https://taurosolutions.ar',
        'DHL_GMAIL_CLIENT_ID': 'client-id',
        'DHL_GMAIL_CLIENT_SECRET': 'client-secret',
        'DHL_GMAIL_ACCOUNT': 'taurosolutionsar@gmail.com',
        'DHL_GMAIL_CUIT': '20123456786',
        'DHL_GMAIL_TOKEN_ENCRYPTION_KEY': 'clave-exclusiva-dhl-gmail-segura-2026',
    }
    for nombre, valor in valores.items():
        monkeypatch.setenv(nombre, valor)


def test_preflight_falla_cerrado_sin_credenciales(monkeypatch):
    for nombre in ('DHL_GMAIL_CLIENT_ID', 'DHL_GMAIL_CLIENT_SECRET', 'DHL_GMAIL_ACCOUNT',
                   'DHL_GMAIL_CUIT', 'DHL_GMAIL_TOKEN_ENCRYPTION_KEY'):
        monkeypatch.delenv(nombre, raising=False)
    control = correo.preflight_correo_dhl()
    assert not control['configurada']
    assert set(control['bloqueos']) == {
        'oauth_google', 'cuenta_gmail', 'cuit_receptor', 'cifrado_tokens',
    }
    assert correo.sincronizar_facturas_dhl()['estado'] == 'DESHABILITADA'


def test_preflight_rechaza_clave_de_cifrado_debil(monkeypatch):
    configurar(monkeypatch)
    monkeypatch.setenv('DHL_GMAIL_TOKEN_ENCRYPTION_KEY', 'corta')
    control = correo.preflight_correo_dhl()
    assert not control['configurada']
    assert control['bloqueos'] == ['cifrado_tokens']


def test_oauth_pide_exclusivamente_gmail_readonly_y_callback_canonico(monkeypatch):
    configurar(monkeypatch)
    url = correo.url_autorizacion('s' * 40, code_challenge='c' * 43)
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == 'https' and parsed.netloc == 'accounts.google.com'
    assert query['scope'] == [correo.GMAIL_READONLY]
    assert query['access_type'] == ['offline']
    assert query['prompt'] == ['consent']
    assert query['code_challenge'] == ['c' * 43]
    assert query['code_challenge_method'] == ['S256']
    assert 'include_granted_scopes' not in query
    assert query['redirect_uri'] == [
        'https://taurosolutions.ar/admin/conciliacion-couriers/entrada-dhl/gmail/callback'
    ]
    assert 'gmail.modify' not in url and 'mail.google.com' not in url


def test_tokens_se_cifran_con_clave_exclusiva_y_admiten_rotacion(monkeypatch):
    configurar(monkeypatch)
    cifrado = correo._cifrar('refresh-super-secreto')
    assert cifrado.startswith('enc:v1:') and 'refresh-super-secreto' not in cifrado
    assert correo._descifrar(cifrado) == 'refresh-super-secreto'
    monkeypatch.setenv('DHL_GMAIL_TOKEN_ENCRYPTION_KEY_PREVIOUS',
                       'clave-exclusiva-dhl-gmail-segura-2026')
    monkeypatch.setenv('DHL_GMAIL_TOKEN_ENCRYPTION_KEY', 'clave-nueva-segura-y-exclusiva-2026')
    assert correo._descifrar(cifrado) == 'refresh-super-secreto'


def test_oauth_rechaza_otra_cuenta_antes_de_guardar_tokens(monkeypatch):
    configurar(monkeypatch)
    pedido_token = {}
    def post_token(*a, **kw):
        pedido_token.update(kw['data'])
        return SimpleNamespace(status_code=200, json=lambda: {
            'access_token': 'access-secreto', 'refresh_token': 'refresh-secreto',
            'token_type': 'Bearer', 'scope': correo.GMAIL_READONLY, 'expires_in': 3600,
        })
    monkeypatch.setattr(correo.requests, 'post', post_token)
    monkeypatch.setattr(correo.requests, 'get', lambda *a, **kw: SimpleNamespace(
        status_code=200, json=lambda: {'emailAddress': 'otra-cuenta@gmail.com'}))
    monkeypatch.setattr(correo, 'get_conn', lambda: (_ for _ in ()).throw(AssertionError('no debe guardar')))
    with pytest.raises(correo.ConfiguracionCorreoDHL, match='no es el buzón'):
        correo.conectar_desde_codigo(
            'codigo-oauth-valido', code_verifier='v' * 64, actor='test',
        )
    assert pedido_token['code_verifier'] == 'v' * 64


def test_oauth_rechaza_permisos_adicionales(monkeypatch):
    configurar(monkeypatch)
    monkeypatch.setattr(correo.requests, 'post', lambda *a, **kw: SimpleNamespace(
        status_code=200, json=lambda: {
            'access_token': 'access-secreto', 'refresh_token': 'refresh-secreto',
            'token_type': 'Bearer',
            'scope': correo.GMAIL_READONLY + ' https://www.googleapis.com/auth/gmail.modify',
            'expires_in': 3600,
        }))
    monkeypatch.setattr(correo.requests, 'get',
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError('no debe consultar')))
    with pytest.raises(correo.ConfiguracionCorreoDHL, match='sólo lectura'):
        correo.conectar_desde_codigo(
            'codigo-oauth-valido', code_verifier='v' * 64, actor='test',
        )


@pytest.mark.parametrize('reason', [
    'rateLimitExceeded', 'userRateLimitExceeded', 'dailyLimitExceeded',
    'quotaExceeded', 'backendError',
])
def test_google_403_de_cuota_es_temporal_y_no_pide_reautorizar(reason):
    respuesta = SimpleNamespace(status_code=403, json=lambda: {
        'error': {'message': 'detalle-secreto', 'errors': [{'reason': reason}]},
    })
    with pytest.raises(correo.CorreoDHLTemporal) as error:
        correo._respuesta_json(respuesta, operacion='Gmail')
    assert 'detalle-secreto' not in str(error.value)


def test_google_403_de_permisos_si_pide_reautorizar():
    respuesta = SimpleNamespace(status_code=403, json=lambda: {
        'error': {'errors': [{'reason': 'insufficientPermissions'}]},
    })
    with pytest.raises(correo.CorreoDHLReautorizar):
        correo._respuesta_json(respuesta, operacion='Gmail')


def test_google_403_desconocido_falla_cerrado_sin_filtrar_body():
    respuesta = SimpleNamespace(status_code=403, json=lambda: {
        'error': {'message': 'detalle-secreto', 'status': 'PERMISSION_DENIED'},
    })
    with pytest.raises(correo.ConfiguracionCorreoDHL) as error:
        correo._respuesta_json(respuesta, operacion='Gmail')
    assert 'detalle-secreto' not in str(error.value)


def test_importacion_automatica_es_opt_in(monkeypatch):
    monkeypatch.delenv('DHL_GMAIL_AUTO_IMPORT', raising=False)
    assert correo._auto_import_habilitado() is False
    monkeypatch.setenv('DHL_GMAIL_AUTO_IMPORT', 'true')
    assert correo._auto_import_habilitado() is True


def test_backoff_y_tope_de_reintentos_son_acotados(monkeypatch):
    assert correo._espera_reintento(1).total_seconds() == 30 * 60
    assert correo._espera_reintento(2).total_seconds() == 60 * 60
    assert correo._espera_reintento(50).total_seconds() == 24 * 60 * 60
    monkeypatch.setenv('DHL_GMAIL_MAX_ATTEMPTS', '999')
    assert correo._max_intentos() == 20


def _mensaje(pdf: bytes, *, inline=False):
    data = base64.urlsafe_b64encode(pdf).decode().rstrip('=')
    body = {'size': len(pdf), 'data': data} if inline else {'size': len(pdf), 'attachmentId': 'att-1'}
    return {'id': 'msg-1', 'payload': {'partId': '', 'mimeType': 'multipart/mixed', 'parts': [
        {'partId': '1', 'mimeType': 'application/pdf', 'filename': 'DHL-1700A00000001_02092026.pdf',
         'body': body}
    ]}}


class Cliente:
    def __init__(self, pdf):
        self.pdf = pdf
        self.rutas = []

    def get(self, ruta, *, params=None):
        self.rutas.append((ruta, params))
        return {'data': base64.urlsafe_b64encode(self.pdf).decode().rstrip('=')}


@pytest.mark.parametrize('inline', [False, True])
def test_descarga_exacta_pdf_sin_aceptar_tamano_distinto(inline):
    pdf = b'%PDF-original-dhl'
    mensaje = _mensaje(pdf, inline=inline)
    candidato = AdjuntoCandidatoDHL(
        'msg-1', '1', None if inline else 'att-1',
        'DHL-1700A00000001_02092026.pdf', '1700A00000001',
    )
    cliente = Cliente(pdf)
    assert correo.descargar_adjunto(cliente, mensaje, candidato) == pdf
    assert bool(cliente.rutas) is (not inline)
    mensaje['payload']['parts'][0]['body']['size'] += 1
    with pytest.raises(CorreoDHLInvalido, match='no coincide'):
        correo.descargar_adjunto(cliente, mensaje, candidato)


def test_respuestas_google_no_filtran_el_body_de_error():
    respuesta = SimpleNamespace(status_code=500, json=lambda: {'token': 'secreto'})
    with pytest.raises(correo.CorreoDHLTemporal) as error:
        correo._respuesta_json(respuesta, operacion='Gmail')
    assert 'secreto' not in str(error.value)


def test_schema_conserva_oauth_dedupe_y_canal_sin_aprobar_cargos():
    schema = (ROOT / 'sql/schema.sql').read_text()
    assert 'CREATE TABLE IF NOT EXISTS integracion_correo_dhl' in schema
    assert 'access_token_cifrado TEXT NOT NULL' in schema
    assert 'refresh_token_cifrado TEXT NOT NULL' in schema
    assert "scopes = 'https://www.googleapis.com/auth/gmail.readonly'" in schema
    assert "canal IN ('ADMIN_PDF','CORREO_DHL')" in schema
    assert 'CREATE TABLE IF NOT EXISTS correos_dhl_procesados' in schema
    assert 'gmail_mensaje_id TEXT PRIMARY KEY' in schema
    assert 'trg_proteger_origen_correo_dhl' in schema
    assert 'trg_no_borrar_correo_dhl' in schema


def test_scheduler_es_idempotente_y_no_promete_aplicar_diferencias():
    main = (ROOT / 'main.py').read_text()
    assert 'id="facturas_dhl_gmail"' in main
    bloque = main[main.index('# Facturas DHL por Gmail:'):main.index(
        '# Job diario: podar el registro de auditoría'
    )]
    assert 'trigger="cron"' in bloque
    assert 'day_of_week="mon,fri"' in bloque
    assert 'DHL_GMAIL_SYNC_MINUTES' not in bloque
    assert 'DHL_GMAIL_CRON_HOUR' in bloque
    assert 'DHL_GMAIL_CRON_MINUTE' in bloque
    assert 'max_instances=1' in main
    assert 'sincronizar_facturas_dhl_seguro' in main
    assert 'nunca' in main[main.index('Facturas DHL por Gmail'):main.index('Facturas DHL por Gmail') + 500]
