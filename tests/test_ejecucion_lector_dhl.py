import json
import subprocess
from types import SimpleNamespace

import pytest

from servicios import ejecucion_lector_dhl as worker
from servicios.entrada_facturas_dhl import ExtraccionDHLInvalida


@pytest.fixture(autouse=True)
def linux_simulado(monkeypatch):
    monkeypatch.setattr(worker.sys, 'platform', 'linux')


def ejecutar():
    return worker.ejecutar_lector_dhl(b'%PDF-sintetico', numero='1700A00000001', cuit='20123456786')


def test_worker_no_hereda_secretos_ni_recibe_rutas_del_usuario(monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'no-debe-heredarse')
    monkeypatch.setenv('DHL_API_SECRET', 'no-debe-heredarse')
    def run(cmd, **kw):
        assert kw['timeout'] == 20 and kw['stderr'] == subprocess.DEVNULL
        assert set(kw['env']) == {'LANG', 'PYTHONUTF8'}
        assert cmd[1:3] == ['-I', '-B']
        assert len(cmd) == 4 and cmd[-1].endswith('/servicios/worker_pdf_dhl.py')
        assert set(json.loads(kw['input'])) == {'pdf', 'numero', 'cuit'}
        return SimpleNamespace(returncode=0, stdout=b'{"extraccion":{},"observaciones":[]}')
    monkeypatch.setattr(worker.subprocess, 'run', run)
    assert ejecutar() == {'extraccion': {}, 'observaciones': []}


@pytest.mark.parametrize('salida,code', [(b'no-json', 0), (b'[]', 0), (b'{}', 0),
    (b'{"extraccion":{},"observaciones":"no lista"}', 0), (b'', -9), (b'x' * (512*1024+1), 0),
    (b'{"error":"Formato desconocido"}', 0)])
def test_fallos_nunca_se_interpretan_como_extraccion(monkeypatch, salida, code):
    monkeypatch.setattr(worker.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=code, stdout=salida))
    with pytest.raises(ExtraccionDHLInvalida):
        ejecutar()


def test_timeout_se_reporta_sin_filtrar_stderr(monkeypatch):
    def agotar(*a, **kw):
        raise subprocess.TimeoutExpired('worker', 20, stderr=b'dato-privado')
    monkeypatch.setattr(worker.subprocess, 'run', agotar)
    with pytest.raises(ExtraccionDHLInvalida, match='límite') as error:
        ejecutar()
    assert 'dato-privado' not in str(error.value)


def test_no_mas_de_dos_lecturas_por_proceso():
    worker._LECTORES.acquire()
    worker._LECTORES.acquire()
    try:
        with pytest.raises(ExtraccionDHLInvalida, match='otras lecturas'):
            ejecutar()
    finally:
        worker._LECTORES.release()
        worker._LECTORES.release()


def test_web_falla_cerrada_si_no_hay_limite_memoria(monkeypatch):
    monkeypatch.setattr(worker.sys, 'platform', 'darwin')
    with pytest.raises(ExtraccionDHLInvalida, match='Linux'):
        ejecutar()
