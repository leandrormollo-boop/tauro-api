"""Intake administrativo durable. Revisar extracción != aprobar cargo al cliente.

No conexión a Gmail, no envío a modelos, no cambio de precios/saldos. El PDF
original se conserva; las coincidencias posteriores son sólo PROPUESTAS.
"""
import hashlib
import re

from psycopg2.extras import Json

from core.database import get_conn
from servicios.conciliacion_couriers import (
    ConciliacionCourierError, _hash_json, _json_seguro, _registrar_auditoria,
    registrar_factura_courier, matchear_items_exactos,
)
from servicios.entrada_facturas_dhl import ExtraccionDHLInvalida, preparar_factura_dhl_manual
from servicios.ejecucion_lector_dhl import ejecutar_lector_dhl

LECTOR_VERSION = 1
_CAMPOS = '''id, archivo_nombre, archivo_sha256, numero_esperado, cuit_esperado,
    canal, estado, extraccion, observaciones, revision_sha256, lector_version,
    error_lectura, intentos, factura_id, creado_por, revisado_por,
    created_at, updated_at, revisado_at'''


def _referencia(numero, cuit):
    numero = str(numero or '').strip().upper()
    cuit = str(cuit or '').strip()
    if not re.fullmatch(r'[0-9]{4}A[0-9]{8}', numero):
        raise ExtraccionDHLInvalida('Número esperado: 4 dígitos, A y 8 dígitos (ej. 1700A00000001).')
    if not re.fullmatch(r'[0-9]{11}', cuit):
        raise ExtraccionDHLInvalida('Indicá el CUIT receptor con 11 dígitos, sin guiones.')
    return numero, cuit


def _auditar(cur, entrada, evento, actor, **extra):
    _registrar_auditoria(cur, evento=evento, actor=actor, metadata={
        'entrada_dhl_id': entrada['id'], 'archivo_sha256': entrada['archivo_sha256'],
        **extra,
    })


def recibir_pdf_dhl(*, pdf, nombre, numero, cuit, actor):
    numero, cuit = _referencia(numero, cuit)
    if not isinstance(pdf, bytes) or not pdf.startswith(b'%PDF') or len(pdf) > 8 * 1024 * 1024:
        raise ExtraccionDHLInvalida('Adjuntá el PDF original, de hasta 8 MB.')
    nombre = ''.join(c for c in str(nombre or '') if c.isalnum() or c in '._- ')[:180] or 'factura.pdf'
    sha = hashlib.sha256(pdf).hexdigest()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', ('tauro:entrada-dhl:' + sha,))
        cur.execute('SELECT id, numero_esperado, cuit_esperado FROM entradas_pdf_dhl WHERE archivo_sha256=%s', (sha,))
        existente = cur.fetchone()
        if existente:
            if (existente['numero_esperado'], existente['cuit_esperado']) != (numero, cuit):
                raise ExtraccionDHLInvalida('Este PDF ya está en la bandeja con otra referencia. Abrí su detalle para corregirla antes de leer.')
            return {'id': existente['id'], 'duplicado': True}
        cur.execute('''INSERT INTO entradas_pdf_dhl
            (archivo_nombre, archivo_pdf, archivo_sha256, numero_esperado, cuit_esperado, creado_por)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING id, archivo_sha256''',
            (nombre, pdf, sha, numero, cuit, actor))
        entrada = cur.fetchone()
        _auditar(cur, entrada, 'DHL_PDF_RECIBIDO', actor)
        return {'id': entrada['id'], 'duplicado': False}


def listar_entradas_dhl(*, pagina=1):
    pagina = max(1, min(int(pagina), 10000))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('''SELECT id, numero_esperado, archivo_nombre, estado, created_at,
            factura_id, error_lectura FROM entradas_pdf_dhl ORDER BY id DESC LIMIT 51 OFFSET %s''',
            ((pagina - 1) * 50,))
        filas = [dict(f) for f in cur.fetchall()]
    return {'items': filas[:50], 'pagina': pagina, 'hay_mas': len(filas) > 50}


def obtener_entrada_dhl(entrada_id, *, con_pdf=False):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {_CAMPOS}{', archivo_pdf' if con_pdf else ''} FROM entradas_pdf_dhl WHERE id=%s", (int(entrada_id),))
        fila = cur.fetchone()
        return dict(fila) if fila else None


def _bloquear(cur, entrada_id):
    cur.execute('SELECT * FROM entradas_pdf_dhl WHERE id=%s FOR UPDATE', (int(entrada_id),))
    entrada = cur.fetchone()
    if not entrada:
        raise ExtraccionDHLInvalida('Entrada DHL no encontrada.')
    pdf = bytes(entrada['archivo_pdf'])
    if hashlib.sha256(pdf).hexdigest() != entrada['archivo_sha256']:
        raise ExtraccionDHLInvalida('La evidencia no coincide con su huella. Operación bloqueada.')
    return entrada, pdf


def _huella_revision(entrada, extraccion, observaciones, version):
    return _hash_json({'archivo_sha256': entrada['archivo_sha256'],
                      'numero': entrada['numero_esperado'], 'cuit': entrada['cuit_esperado'],
                      'extraccion': extraccion, 'observaciones': observaciones, 'lector_version': version})


def leer_entrada_dhl(entrada_id, *, numero, cuit, actor):
    numero, cuit = _referencia(numero, cuit)
    # Lock acotado por el timeout del subprocess; un cierre inesperado hace
    # rollback y conserva RECIBIDA/REVISION_MANUAL para reintentar, sin lease huérfano.
    with get_conn() as conn, conn.cursor() as cur:
        entrada, pdf = _bloquear(cur, entrada_id)
        if entrada['estado'] in ('PARA_REVISION', 'IMPORTADA'):
            return entrada['estado']
        if (numero, cuit) != (entrada['numero_esperado'], entrada['cuit_esperado']):
            _auditar(cur, entrada, 'DHL_REFERENCIA_CORREGIDA', actor)
        entrada.update(numero_esperado=numero, cuit_esperado=cuit)
        try:
            lectura = ejecutar_lector_dhl(pdf, numero=numero, cuit=cuit)
            extraccion = _json_seguro(lectura['extraccion'])
            preparado = preparar_factura_dhl_manual(extraccion, archivo_pdf=pdf, archivo_nombre=entrada['archivo_nombre'])
            if preparado.datos_registro['numero'] != numero or preparado.datos_registro['tipo_documento'] != 'FC':
                raise ExtraccionDHLInvalida('El documento no coincide con la referencia o formato admitido.')
            observaciones = list(dict.fromkeys(lectura['observaciones'] + list(preparado.observaciones)))
            huella = _huella_revision(entrada, extraccion, observaciones, LECTOR_VERSION)
            cur.execute('''UPDATE entradas_pdf_dhl SET numero_esperado=%s, cuit_esperado=%s,
                estado='PARA_REVISION', extraccion=%s, observaciones=%s, revision_sha256=%s,
                lector_version=%s, error_lectura=NULL, intentos=intentos+1, updated_at=NOW() WHERE id=%s''',
                (numero, cuit, Json(extraccion), Json(observaciones), huella, LECTOR_VERSION, entrada_id))
            _auditar(cur, entrada, 'DHL_PDF_EXTRAIDO', actor, lector_version=LECTOR_VERSION, revision_sha256=huella)
            return 'PARA_REVISION'
        except (ExtraccionDHLInvalida, ConciliacionCourierError) as exc:
            cur.execute('''UPDATE entradas_pdf_dhl SET numero_esperado=%s, cuit_esperado=%s,
                estado='REVISION_MANUAL', error_lectura=%s, intentos=intentos+1, updated_at=NOW() WHERE id=%s''',
                (numero, cuit, str(exc)[:500], entrada_id))
            _auditar(cur, entrada, 'DHL_PDF_REVISION_MANUAL', actor, lector_version=LECTOR_VERSION)
            return 'REVISION_MANUAL'


def importar_entrada_dhl(entrada_id, *, revision_sha256, revision_confirmada, actor):
    if revision_confirmada is not True:
        raise ExtraccionDHLInvalida('Confirmá la revisión del PDF y los datos extraídos.')
    with get_conn() as conn, conn.cursor() as cur:
        entrada, pdf = _bloquear(cur, entrada_id)
        huella = _huella_revision(entrada, entrada['extraccion'], entrada['observaciones'], entrada['lector_version'])
        if not revision_sha256 or huella != entrada['revision_sha256'] or huella != revision_sha256:
            raise ExtraccionDHLInvalida('La revisión no coincide. Volvé a abrir el detalle.')
        if entrada['estado'] == 'IMPORTADA':
            return {'id': entrada['factura_id'], 'duplicado': True}
        if entrada['estado'] != 'PARA_REVISION':
            raise ExtraccionDHLInvalida('Esta entrada todavía no está lista para importar.')
        preparado = preparar_factura_dhl_manual(entrada['extraccion'], archivo_pdf=pdf, archivo_nombre=entrada['archivo_nombre'])
        datos = preparado.datos_registro
        if datos['numero'] != entrada['numero_esperado'] or datos['tipo_documento'] != 'FC':
            raise ExtraccionDHLInvalida('Identidad documental no admitida.')
        datos['metadatos_origen'].update({
            'entrada_dhl_id': entrada_id, 'revision_extraccion_requerida': False,
            'revision_sha256': huella, 'lector_version': entrada['lector_version'],
            'revision_financiera_pendiente': True,
        })
        factura = registrar_factura_courier(**datos, actor=actor, _conn=conn)
        matchear_items_exactos(factura['id'], actor=actor, _conn=conn)
        cur.execute('''UPDATE entradas_pdf_dhl SET estado='IMPORTADA', factura_id=%s,
            revisado_por=%s, revisado_at=NOW(), updated_at=NOW() WHERE id=%s''',
            (factura['id'], actor, entrada_id))
        _auditar(cur, entrada, 'DHL_PDF_IMPORTADO_REVISADO', actor, factura_id=factura['id'], revision_sha256=huella)
        return factura
