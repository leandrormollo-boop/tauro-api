"""El backup descargable nunca debe transportar credenciales vivas."""

from servicios.backup import COLUMNAS_SENSIBLES


def test_backup_excluye_par_completo_de_tokens_shopify():
    sensibles = COLUMNAS_SENSIBLES["shopify_instalaciones"]

    assert {"access_token", "refresh_token"} <= sensibles


def test_backup_excluye_config_generica_completa():
    assert "config" not in backup.TABLAS
    assert backup.TABLAS_EXCLUIDAS == ["config"]

from contextlib import contextmanager
from decimal import Decimal
import pytest
from servicios import backup


def test_dinero_no_pierde_precision_y_binarios_no_salen():
    assert backup._serializar(Decimal('123456789012.34')) == '123456789012.34'
    assert backup._serializar(b'privado') == '<binario 7 bytes>'


def test_falla_de_tabla_aborta_exportacion(monkeypatch):
    class Cursor:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def execute(self,sql,*args):
            if sql.startswith('SELECT *'):raise RuntimeError('fallo privado')
    @contextmanager
    def conexion():yield type('Conn',(),{'cursor':lambda _:Cursor()})()
    monkeypatch.setattr(backup,'get_conn',conexion)
    monkeypatch.setattr(backup,'_existe_tabla',lambda *_:True)
    with pytest.raises(RuntimeError):backup.generar_backup_json()


def test_exportacion_declara_alcance_y_omite_secretos(monkeypatch):
    consultas=[]
    class Cursor:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def execute(self,sql,*args):consultas.append(sql)
        def fetchall(self):return [dict(cliente_id='DEMO',api_key='secreto',password_hash='hash',saldo=Decimal('1.01'))]
    @contextmanager
    def conexion():yield type('Conn',(),{'cursor':lambda _:Cursor()})()
    monkeypatch.setattr(backup,'get_conn',conexion)
    monkeypatch.setattr(backup,'_existe_tabla',lambda cur,t:t=='clientes')
    datos=backup.generar_backup()
    assert datos['restaurable'] is False and datos['tipo']=='exportacion_parcial'
    assert datos['tablas_incluidas']==['clientes']
    assert datos['tablas_excluidas']==['config']
    assert datos['tablas']['clientes']==[dict(cliente_id='DEMO',saldo='1.01')]
    assert 'REPEATABLE READ' in consultas[0]
    assert 'secreto' not in str(datos)
