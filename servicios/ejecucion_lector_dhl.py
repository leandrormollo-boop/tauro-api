"""Límite de concurrencia, recursos y tiempo del parser PDF administrativo."""
import base64
import json
from pathlib import Path
import subprocess
import sys
import threading

from servicios.entrada_facturas_dhl import ExtraccionDHLInvalida

_LECTORES = threading.BoundedSemaphore(2)


class LectorDHLNoDisponible(RuntimeError):
    """Falla operativa transitoria; no es un rechazo del documento."""


def ejecutar_lector_dhl(pdf: bytes, *, numero: str, cuit: str) -> dict:
    if not isinstance(pdf, bytes) or not pdf.startswith(b'%PDF') or len(pdf) > 8 * 1024 * 1024:
        raise ExtraccionDHLInvalida('Se requiere un PDF de hasta 8 MB.')
    # No se permite continuar sin el límite de memoria. Las pruebas del
    # worker desde macOS se ejecutan en un contenedor Linux local.
    if sys.platform != 'linux':
        raise LectorDHLNoDisponible('La lectura protegida requiere el worker Linux. Contactá al administrador.')
    if not _LECTORES.acquire(blocking=False):
        raise LectorDHLNoDisponible('Hay otras lecturas en curso. Reintentá en unos momentos.')
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
        except OSError as exc:
            raise LectorDHLNoDisponible('El lector no está disponible. Reintentá; si persiste, contactá al administrador.') from exc
        except subprocess.TimeoutExpired as exc:
            raise ExtraccionDHLInvalida('No se pudo completar la lectura dentro del límite. Revisión manual requerida.') from exc
        if proceso.returncode:
            # Los rechazos documentales normales son JSON {error} con exit 0.
            # Un crash no prueba un defecto del PDF. No filtramos stderr.
            raise LectorDHLNoDisponible('El lector se interrumpió. Reintentá; si persiste, contactá al administrador.')
        if len(proceso.stdout) > 512 * 1024:
            raise LectorDHLNoDisponible('El lector devolvió una respuesta excesiva. Contactá al administrador.')
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
            raise LectorDHLNoDisponible('Respuesta del lector inválida; no se importó nada. Reintentá o contactá al administrador.') from exc
    finally:
        _LECTORES.release()
