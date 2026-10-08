"""Bandeja durable de emisión y diagnóstico sin herramientas de envío o cobro."""
from __future__ import annotations

import uuid
from psycopg2.extras import Json

from core.database import get_conn
from servicios.catalogo_errores_emision import catalogo_errores, clasificar_error
from servicios.auditoria import registrar_desde_request_con_cursor


def _catalogo(cur, ficha):
    cur.execute("""INSERT INTO emision_catalogo_errores(codigo,version,contenido)
        VALUES(%s,%s,%s) ON CONFLICT(codigo) DO UPDATE
        SET version=EXCLUDED.version, contenido=EXCLUDED.contenido
        WHERE emision_catalogo_errores.version <= EXCLUDED.version""",
        (ficha['codigo'], ficha['version'], Json(ficha)))


def inicializar_catalogo():
    with get_conn() as conn, conn.cursor() as cur:
        for ficha in catalogo_errores():
            _catalogo(cur, ficha)


def iniciar_intento(solicitud_id, referencia, actor_type, actor_ref):
    """Commit previo al callable: una caída posterior queda como inconclusa."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO emision_intentos(referencia,solicitud_id,actor_type)
            SELECT %s,id,%s FROM solicitudes_guia WHERE id=%s
              AND (%s='admin' OR cliente_id=%s)
            RETURNING id""", (referencia, actor_type, solicitud_id, actor_type, actor_ref))
        fila = cur.fetchone()
        return fila['id'] if fila else None


def _evidencia(cur, solicitud_id):
    # Las comparaciones monetarias se hacen en PostgreSQL; sólo se exponen
    # booleanos al diagnóstico. Esto no comprueba la tarifa vigente del courier.
    cur.execute("""SELECT s.estado, UPPER(s.courier) AS courier,
        NULLIF(BTRIM(s.tracking),'') IS NOT NULL AS tiene_tracking,
        (s.label_pdf IS NOT NULL AND substring(s.label_pdf FROM 1 FOR 4) = '\\x25504446'::bytea)
          AS tiene_documento,
        s.guia_generada_at IS NOT NULL AS fecha_guia_confirmada,
        s.cargo_pendiente,
        NULLIF(s.courier_message_reference,'') IS NOT NULL AS tiene_referencia_courier,
        q.id IS NOT NULL AS tiene_tarifa,
        COALESCE(q.courier=UPPER(s.courier)
          AND NULLIF(BTRIM(q.coti_id),'') IS NOT DISTINCT FROM NULLIF(BTRIM(s.coti_id),'')
          AND ABS(q.precio_cliente_inicial_ars-s.precio_tauro_ars)<=0.02,FALSE)
          AS tarifa_coincide,
        EXISTS(SELECT 1 FROM envios e WHERE e.solicitud_id=s.id
          AND e.estado='ACTIVO' AND e.tracking=s.tracking AND e.cliente_id=s.cliente_id
          AND ABS(e.monto_ars-s.precio_tauro_ars)<=0.02) AS cargo_registrado
        FROM solicitudes_guia s LEFT JOIN envio_cotizacion_snapshots q ON q.solicitud_id=s.id
        WHERE s.id=%s""", (solicitud_id,))
    fila = cur.fetchone()
    return dict(fila) if fila else {}


def _emitida_coherente(e):
    return bool(e.get('tiene_tracking') and e.get('tiene_documento')
        and e.get('cargo_registrado') and not e.get('cargo_pendiente')
        and e.get('tiene_referencia_courier') and e.get('fecha_guia_confirmada')
        and e.get('estado') in {'GUIA_LISTA','DESPACHADO','ENTREGADO'})


def _cerrar_verificados(cur,solicitud_id,hasta_id):
    cur.execute("""UPDATE emision_incidencias SET estado='RESUELTO',resuelta_en=NOW(),
        resolucion='Guía, documento y cargo verificados.',actualizada_en=NOW(),
        analisis_token=NULL,analisis_desde=NULL
        WHERE solicitud_id=%s AND estado<>'RESUELTO'""", (solicitud_id,))
    # No se inventa el resultado del intento perdido: se marca conciliado por
    # la evidencia actual, diferenciándolo de un intento que devolvió éxito.
    cur.execute("""UPDATE emision_intentos SET estado='CONCILIADO',finalizado_en=NOW(),
        metadata=metadata || '{"motivo":"Estado actual verificado: guía, documento y cargo registrados."}'::jsonb
        WHERE solicitud_id=%s AND id<=%s AND estado='INICIADO'""",(solicitud_id,hasta_id))


def verificar_inconcluso(intento_id,request=None):
    with get_conn() as conn,conn.cursor() as cur:
        cur.execute("SELECT solicitud_id FROM emision_intentos WHERE id=%s AND estado='INICIADO' AND iniciado_en<NOW()-INTERVAL '10 minutes'",(intento_id,))
        fila=cur.fetchone()
        if not fila:
            return False
        sid=fila['solicitud_id']
        cur.execute('SELECT id FROM solicitudes_guia WHERE id=%s FOR UPDATE',(sid,))
        if not _emitida_coherente(_evidencia(cur,sid)):
            return False
        _cerrar_verificados(cur,sid,intento_id)
        _auditar(cur,request,'admin.intento_conciliado',None,intento_id=intento_id)
    return True


def finalizar_intento(intento_id, resultado, metadata):
    """Idempotente. Bloquea la solicitud sólo al persistir, nunca durante HTTP."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT solicitud_id FROM emision_intentos WHERE id=%s', (intento_id,))
        previa = cur.fetchone()
        if not previa:
            raise ValueError('Intento inexistente')
        sid = previa['solicitud_id']
        cur.execute('SELECT id FROM solicitudes_guia WHERE id=%s FOR UPDATE', (sid,))
        cur.execute('SELECT * FROM emision_intentos WHERE id=%s FOR UPDATE', (intento_id,))
        intento = cur.fetchone()
        if intento['estado'] != 'INICIADO':
            return
        ficha = clasificar_error(resultado)
        ok = bool(resultado.get('ok'))
        evidencia = _evidencia(cur, sid)
        coherente = _emitida_coherente(evidencia)
        if ok and not coherente:
            codigo = ('RESULTADO_NO_CONFIRMADO' if not evidencia.get('tiene_tracking') else
                      'CARGO_PENDIENTE' if evidencia.get('cargo_pendiente') or not evidencia.get('cargo_registrado') else
                      'ETIQUETA_PENDIENTE' if not evidencia.get('tiene_documento') else 'EMISION_NO_CLASIFICADA')
            ficha = clasificar_error({'ok':False,'codigo_error':codigo})
        estado = 'EXITOSO' if ok else ('INCIERTO' if ficha['codigo']=='RESULTADO_NO_CONFIRMADO' else 'FALLIDO')
        # metadata ya minimizada por el wrapper. Guardar sólo campos conocidos.
        seguro = {k: str(metadata.get(k) or '')[:800 if k=='motivo' else 80]
                  for k in ('codigo','etapa','error_tipo','motivo','referencia')}
        seguro['clasificacion'] = ficha['codigo'] if not ok or not coherente else 'GUIA_EMITIDA'
        cur.execute("""UPDATE emision_intentos SET estado=%s,finalizado_en=NOW(),metadata=%s
            WHERE id=%s""", (estado, Json(seguro), intento_id))
        if not ok or not coherente:
            _catalogo(cur, ficha)
            # Un resultado antiguo no debe pisar un diagnóstico más reciente.
            cur.execute("""INSERT INTO emision_incidencias
                (solicitud_id,codigo,diagnostico,ultimo_intento_id,estado,resuelta_en,resolucion)
                VALUES(%s,%s,%s,%s,%s,CASE WHEN %s THEN NOW() END,%s)
                ON CONFLICT(solicitud_id,codigo) DO UPDATE SET
                  cantidad=emision_incidencias.cantidad+1,
                  ultimo_intento_id=EXCLUDED.ultimo_intento_id,
                  diagnostico=EXCLUDED.diagnostico, actualizada_en=NOW(),
                  estado=EXCLUDED.estado, resuelta_en=EXCLUDED.resuelta_en,
                  resolucion=EXCLUDED.resolucion, analisis=NULL,
                  analisis_token=NULL, analisis_desde=NULL
                WHERE emision_incidencias.ultimo_intento_id < EXCLUDED.ultimo_intento_id""",
                (sid,ficha['codigo'],Json(ficha),intento_id,
                 'RESUELTO' if coherente else 'PENDIENTE',coherente,
                 'Guía, documento y cargo verificados.' if coherente else None))
            if cur.rowcount==0:
                cur.execute('UPDATE emision_incidencias SET cantidad=cantidad+1 WHERE solicitud_id=%s AND codigo=%s',
                            (sid,ficha['codigo']))
        if coherente:
            _cerrar_verificados(cur,sid,intento_id)


def resumen_incidencias():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT COUNT(*) AS pendientes,
            COUNT(*) FILTER(WHERE diagnostico->>'severidad'='critica') AS criticas
            FROM emision_incidencias WHERE estado<>'RESUELTO'""")
        datos = dict(cur.fetchone())
        cur.execute("""SELECT COUNT(*) AS inconclusos FROM emision_intentos
            WHERE estado='INICIADO' AND iniciado_en<NOW()-INTERVAL '10 minutes'""")
        datos.update(cur.fetchone())
    return {**datos, 'total':datos['pendientes']+datos['inconclusos'], 'disponible':True}


def listar_incidencias(estado='pendientes', antes=0):
    condiciones = ["i.estado='RESUELTO'" if estado=='resueltos' else "i.estado<>'RESUELTO'"]
    params = []
    if antes>0:
        condiciones.append('i.id<%s')
        params.append(antes)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"""SELECT i.*,s.cliente_id,s.courier,
            i.actualizada_en AT TIME ZONE 'America/Argentina/Buenos_Aires' AS fecha
            FROM emision_incidencias i JOIN solicitudes_guia s ON s.id=i.solicitud_id
            WHERE {' AND '.join(condiciones)} ORDER BY i.id DESC LIMIT 51""",params)
        filas = [dict(r) for r in cur.fetchall()]
        cur.execute("""SELECT t.id,t.solicitud_id,t.referencia,s.cliente_id,
            t.iniciado_en AT TIME ZONE 'America/Argentina/Buenos_Aires' AS fecha
            FROM emision_intentos t JOIN solicitudes_guia s ON s.id=t.solicitud_id
            WHERE t.estado='INICIADO' AND t.iniciado_en<NOW()-INTERVAL '10 minutes'
            ORDER BY t.id DESC LIMIT 50""")
        inconclusos = [dict(r) for r in cur.fetchall()]
    return {'items':filas[:50], 'siguiente':filas[49]['id'] if len(filas)>50 else 0,
            'inconclusos':inconclusos}


def obtener_incidencia(incidencia_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT i.*,s.cliente_id,s.courier,s.tracking,
            t.referencia,t.metadata,
            i.actualizada_en AT TIME ZONE 'America/Argentina/Buenos_Aires' AS fecha
            FROM emision_incidencias i JOIN solicitudes_guia s ON s.id=i.solicitud_id
            JOIN emision_intentos t ON t.id=i.ultimo_intento_id WHERE i.id=%s""",(incidencia_id,))
        fila=cur.fetchone()
        if not fila:
            return None
        item=dict(fila)
        item['evidencia']=_evidencia(cur,item['solicitud_id'])
        cur.execute("""SELECT estado,metadata,
            iniciado_en AT TIME ZONE 'America/Argentina/Buenos_Aires' AS fecha
            FROM emision_intentos WHERE solicitud_id=%s ORDER BY id DESC LIMIT 20""",
            (item['solicitud_id'],))
        item['intentos']=[dict(r) for r in cur.fetchall()]
    return item


def _auditar(cur,request,event,incidencia_id,**metadata):
    registrar_desde_request_con_cursor(cur,request,event=event,actor_type='admin',
        actor_ref='admin',success=True,status_code=200,
        metadata={'incidencia_id':incidencia_id,**metadata})


def tomar_revision(incidencia_id, intento_id, request=None):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""UPDATE emision_incidencias SET estado='EN_REVISION'
            WHERE id=%s AND ultimo_intento_id=%s AND estado='PENDIENTE' RETURNING id""",
            (incidencia_id,intento_id))
        if not cur.fetchone():
            return False
        _auditar(cur,request,'admin.incidencia_revision',incidencia_id)
    return True


def analizar_incidencia(incidencia_id, intento_id, *, usar_ia=False, request=None):
    """Sólo puede actualizar la incidencia; no importa funciones que emiten/cobran.

    El CAS descarta análisis viejos. El lease evita llamadas IA simultáneas y
    vence si el proceso se interrumpe. La IA jamás decide cerrar una incidencia.
    """
    token=uuid.uuid4().hex
    with get_conn() as conn,conn.cursor() as cur:
        cur.execute("""UPDATE emision_incidencias SET analisis_token=%s,analisis_desde=NOW()
            WHERE id=%s AND ultimo_intento_id=%s AND estado<>'RESUELTO'
              AND (analisis_token IS NULL OR analisis_desde<NOW()-INTERVAL '2 minutes')
              AND (analisis_desde IS NULL OR analisis_desde<NOW()-INTERVAL '30 seconds')
            RETURNING solicitud_id,diagnostico""",(token,incidencia_id,intento_id))
        fila=cur.fetchone()
        if not fila:
            return 'sin_cambios'
        sid=fila['solicitud_id']
        ficha=dict(fila['diagnostico'])
    try:
        with get_conn() as conn,conn.cursor() as cur:
            e=_evidencia(cur,sid)
        analisis={'fuente':'reglas', 'explicacion':ficha['explicacion'],
                  'accion':ficha['accion'], 'evidencia':e}
        if usar_ia and not ficha['confirmado'] and not _emitida_coherente(e):
            from servicios.agente_incidencias_ia import sugerir_diagnostico
            analisis['ia']=sugerir_diagnostico({'codigo':ficha['codigo'],**e})
        with get_conn() as conn,conn.cursor() as cur:
            # Releer bajo lock: el estado puede cambiar mientras la IA responde.
            cur.execute('SELECT id FROM solicitudes_guia WHERE id=%s FOR UPDATE',(sid,))
            vigente=_evidencia(cur,sid)
            resuelto=_emitida_coherente(vigente)
            analisis['evidencia']=vigente
            cur.execute("""UPDATE emision_incidencias SET analisis=%s,
                analisis_token=NULL,estado=CASE WHEN %s THEN 'RESUELTO' ELSE 'EN_REVISION' END,
                resuelta_en=CASE WHEN %s THEN NOW() END,
                resolucion=CASE WHEN %s THEN 'Guía, documento y cargo verificados.' END
                WHERE id=%s AND ultimo_intento_id=%s AND analisis_token=%s RETURNING id""",
                (Json(analisis),resuelto,resuelto,resuelto,incidencia_id,intento_id,token))
            if not cur.fetchone():
                return 'desactualizado'
            _auditar(cur,request,'admin.incidencia_analizada',incidencia_id,
                fuente='ia' if 'ia' in analisis else 'reglas', resuelto=resuelto,
                modelo=analisis.get('ia',{}).get('model'),politica=1,
                input_hash=analisis.get('ia',{}).get('input_hash'),
                outcome=analisis.get('ia',{}).get('outcome'))
        return 'resuelto' if resuelto else 'analizado'
    finally:
        try:
            with get_conn() as conn,conn.cursor() as cur:
                cur.execute('UPDATE emision_incidencias SET analisis_token=NULL WHERE id=%s AND analisis_token=%s',
                            (incidencia_id,token))
        except Exception as exc:
            print(f'[incidencias] limpieza de análisis pendiente: {type(exc).__name__}')
