from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from contextlib import contextmanager
import pytest
from servicios import control_automatizaciones as control

@pytest.mark.parametrize('resultado,esperado', [
    ({'ok':True,'consultados':5},'OK'),
    ({'ok':True,'errores':1},'ERROR'),
    ({'ok':False,'error':'payload sensible'},'ERROR'),
    ({'ok':True,'reemplazadas':{'ok':False}},'ERROR'),
    ({'ok':True,'reemplazadas':{'ok':True,'errores':1}},'ERROR'),
    ({'estado':'ERROR_CONFIGURACION'},'ERROR'),
    ({'estado':'SIN_CONECTAR'},'OMITIDA'),
    ({'estado':'DESHABILITADA'},'OMITIDA'),
    ({'estado':'PAGINACION_PENDIENTE'},'PARCIAL'),
    ({'estado':'OK','resultados':{'REVISION_MANUAL':1}},'ERROR'),
    ({'estado':'OK','resultados':{'ORIGEN_RECHAZADO':1}},'ERROR'),
    ({'estado':'OK','resultados':{'REINTENTO_DIFERIDO':1}},'PARCIAL'),
    ({'estado':'OK','resultados':{'PARA_REVISION':2,'IMPORTADO':1}},'OK'),
    ({'estado':'OK','resultados':{'ESTADO_FUTURO':1}},'SIN_CONFIRMAR'),
    ({'guardadas':0,'fallidas':3},'ERROR'),
    ({'guardadas':5,'fallidas':0},'OK'),
    (None,'SIN_CONFIRMAR'),
])
def test_clasificacion_no_confunde_retorno_con_exito(resultado,esperado):
    assert control.clasificar_resultado(resultado)==esperado


def test_preserva_excepcion_y_no_guarda_payload(monkeypatch):
    llamadas=[]
    monkeypatch.setattr(control,'registrar',lambda *a:llamadas.append(a))
    def fallar(): raise ValueError('datos privados')
    with pytest.raises(ValueError):control.observar('tracking_dhl_diario',fallar)()
    assert llamadas==[('tracking_dhl_diario','EN_CURSO'),('tracking_dhl_diario','ERROR')]
    assert 'privados' not in str(llamadas)


def test_fallo_de_observabilidad_no_repite_operacion(monkeypatch):
    def sin_db(*_):raise ConnectionError('secreto')
    monkeypatch.setattr(control,'registrar',sin_db)
    llamadas=[]
    def operacion():llamadas.append(1);return {'ok':True}
    assert control.observar('tracking_dhl_diario',operacion)()=={'ok':True}
    assert llamadas==[1]


def test_resultado_anidado_se_reduce_a_estado_sin_persistir_payload(monkeypatch):
    llamadas=[]
    monkeypatch.setattr(control,'registrar',lambda *a:llamadas.append(a))
    resultado={'estado':'OK','resultados':{'REVISION_MANUAL':1},
               'detalle_privado':'documento y destinatario'}
    assert control.observar('facturas_dhl_gmail',lambda:resultado)()==resultado
    assert llamadas==[('facturas_dhl_gmail','EN_CURSO'),
                      ('facturas_dhl_gmail','ERROR')]
    assert 'documento' not in str(llamadas)

from test_conciliacion_couriers_postgres import conciliacion_db, DATABASE_URL

@pytest.mark.skipif(not DATABASE_URL, reason='requiere PostgreSQL local aislado')
def test_historial_durable_conserva_exito_y_marca_atraso(conciliacion_db,monkeypatch):
    monkeypatch.setattr(control,'get_conn',conciliacion_db)
    ahora=datetime.now(timezone.utc)
    control.registrar('tracking_dhl_diario','OK',ahora-timedelta(hours=4))
    control.registrar('tracking_dhl_diario','ERROR',ahora-timedelta(hours=1))
    control.registrar('tracking_dhl_vigilancia','OK',ahora-timedelta(hours=32))
    resumen=control.resumen(ahora=ahora)
    diario,vigilancia,correo,tarifas=resumen['items']
    assert diario['estado']=='ERROR'
    assert diario['ultimo_exito']==ahora-timedelta(hours=4)
    assert vigilancia['estado']=='ATRASADA'
    assert correo['estado']=='SIN_EVIDENCIA'
    assert len(resumen['historial'])==3

@pytest.mark.skipif(not DATABASE_URL, reason='requiere PostgreSQL local aislado')
def test_historial_acotado_y_fallo_db_visible(conciliacion_db,monkeypatch):
    monkeypatch.setattr(control,'get_conn',conciliacion_db)
    control.registrar('tracking_dhl_diario','OK')
    with conciliacion_db() as conn,conn.cursor() as cur:
        cur.execute("""INSERT INTO automatizaciones_historial(clave,estado,fecha)
            SELECT 'tracking_dhl_diario','OK',NOW() FROM generate_series(1,110)""")
    control.registrar('tracking_dhl_diario','ERROR')
    with conciliacion_db() as conn,conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM automatizaciones_historial")
        assert cur.fetchone()['n']==100
    def falla():raise ConnectionError('secreto')
    monkeypatch.setattr(control,'get_conn',falla)
    assert control.resumen()==dict(disponible=False,items=[],historial=[],atencion=1)


def test_admin_control_exige_sesion(monkeypatch):
    from endpoints import admin
    monkeypatch.setattr(admin,'_is_auth',lambda _:False)
    monkeypatch.setattr(control,'resumen',lambda:pytest.fail('lectura privada sin sesión'))
    respuesta=admin.admin_automatizaciones(SimpleNamespace(),None)
    assert respuesta.status_code==303
    assert respuesta.headers['location'].startswith('/admin/login')
