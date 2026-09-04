"""Límite de concurrencia, recursos y tiempo del parser PDF administrativo."""
import base64
import json
from pathlib import Path
import subprocess
import sys
import threading

from servicios.entrada_facturas_dhl import ExtraccionDHLInvalida

_LECTORES = threading.BoundedSemaphore(2)


def ejecutar_lector_dhl(pdf: bytes, *, numero: str, cuit: str) -> dict:
    if not isinstance(pdf, bytes) or not pdf.startswith(b'%PDF') or len(pdf) > 8 * 1024 * 1024:
        raise ExtraccionDHLInvalida('Se requiere un PDF de hasta 8 MB.')
    # No se permite continuar sin el límite de memoria. Las pruebas del
    # worker desde macOS se ejecutan en un contenedor Linux local.
    if sys.platform != 'linux':
        raise ExtraccionDHLInvalida('La lectura protegida requiere el worker Linux. En este equipo usá la carga manual.')
    if not _LECTORES.acquire(blocking=False):
        raise ExtraccionDHLInvalida('Hay otras lecturas en curso. Reintentá en unos momentos.')
    try:
        entrada = json.dumps({'pdf': base64.b64encode(pdf).decode('ascii'),
                              'numero': numero, 'cuit': cuit}).encode()
        try:
            proceso = subprocess.run(
                [sys.executable, '-I', '-B', str(Path(__file__).with_name('worker_pdf_dhl.py'))],
                input=entrada, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=20, check=False, cwd=str(Path(__file__).resolve().parent),
                env={'LANG': 'C.UTF-8', 'PYTHONUTF8': '1'},
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise ExtraccionDHLInvalida('No se pudo completar la lectura dentro del límite. Revisión manual requerida.') from exc
        if proceso.returncode or len(proceso.stdout) > 512 * 1024:
            raise ExtraccionDHLInvalida('El lector no pudo validar este PDF. Revisión manual requerida.')
        try:
            resultado = json.loads(proceso.stdout)
            if not isinstance(resultado, dict):
                raise ValueError()
            if 'error' in resultado:
                raise ExtraccionDHLInvalida(str(resultado['error'])[:500])
            if set(resultado) != {'extraccion', 'observaciones'}:
                raise ValueError()
            if not isinstance(resultado['observaciones'], list) or not all(
                isinstance(x, str) for x in resultado['observaciones']
            ):
                raise ValueError()
            return resultado
        except (ValueError, TypeError) as exc:
            if isinstance(exc, ExtraccionDHLInvalida):
                raise
            raise ExtraccionDHLInvalida('Respuesta del lector inválida; no se importó nada.') from exc
    finally:
        _LECTORES.release()
