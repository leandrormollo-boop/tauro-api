"""Lectura privada de documentos del cliente; nunca registra descargas ni pagos."""

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import subprocess
import sys
import threading


@dataclass(frozen=True)
class Documento:
    contenido: bytes
    tipo: str


_RUTAS = (
    (r"/portal/envios/([1-9][0-9]*)/guia\.pdf", "guia", "Guía"),
    (r"/portal/envios/([1-9][0-9]*)/factura-comercial\.pdf", "invoice", "Factura comercial"),
    (r"/portal/facturas/([1-9][0-9]*)/pdf", "factura", "Factura"),
    (r"/portal/facturas-legacy/([1-9][0-9]*)/pdf", "factura-legacy", "Factura"),
    (r"/portal/pagos/([1-9][0-9]*)/comprobante", "pago", "Comprobante de pago"),
)


def descriptor_documento(url):
    """Sólo rutas propias conocidas. No resuelve URLs ni documentos de operadores."""
    for patron, tipo, titulo in _RUTAS:
        match = re.fullmatch(patron, str(url or ""))
        if match:
            return {"tipo": tipo, "id": int(match[1]), "titulo": titulo, "url": url}
    return None


def tipo_contenido(contenido):
    if contenido.startswith(b"%PDF-"):
        return "application/pdf"
    if contenido.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if contenido.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if contenido[:4] == b"RIFF" and contenido[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("Formato de documento no compatible")


def obtener_documento(tipo, documento_id, cliente_id):
    # Impide activar accidentalmente el modo administrador de los getters.
    if not cliente_id or not cliente_id.strip() or documento_id <= 0:
        return None
    contenido = None
    if tipo == "guia":
        from servicios.solicitudes_guia import obtener_label_pdf
        contenido = obtener_label_pdf(documento_id, cliente_id=cliente_id)
    elif tipo == "invoice":
        from servicios.solicitudes_guia import obtener_factura_comercial_pdf
        contenido = obtener_factura_comercial_pdf(documento_id, cliente_id=cliente_id)
    elif tipo == "factura":
        from servicios.facturacion_clientes import get_factura_cliente_pdf
        dato = get_factura_cliente_pdf(documento_id, cliente_id=cliente_id)
        contenido = dato[0] if dato else None
    elif tipo == "factura-legacy":
        from servicios.cuenta_corriente import get_factura_pdf
        dato = get_factura_pdf(documento_id, cliente_id=cliente_id)
        contenido = dato[0] if dato else None
    elif tipo == "pago":
        from servicios.cuenta_corriente import get_comprobante
        dato = get_comprobante(documento_id, cliente_id=cliente_id)
        contenido = dato[0] if dato else None
    if not contenido:
        return None
    contenido = bytes(contenido)
    if len(contenido) > 32 * 1024 * 1024:
        raise ValueError("Documento demasiado grande para vista previa")
    return Documento(contenido, tipo_contenido(contenido))


# Sólo imágenes pequeñas, máximo 8 MB por proceso. La autorización se comprueba
# ANTES de consultar esta caché; jamás se cachean respuestas HTTP privadas.
_cache = OrderedDict()
_cache_lock = threading.Lock()
_render_slots = threading.BoundedSemaphore(2)
_MAX_THUMB = 128 * 1024
_MAX_ENTRIES = 64


def miniatura_documento(documento):
    if len(documento.contenido) > 32 * 1024 * 1024:
        raise ValueError("Documento demasiado grande para miniatura")
    clave = hashlib.sha256(documento.contenido).digest()
    with _cache_lock:
        if clave in _cache:
            _cache.move_to_end(clave)
            return _cache[clave]
    # PDFium vive en procesos aislados: es no thread-safe y un PDF dañado no
    # debe detener el servidor. Tiempo, memoria y concurrencia acotados.
    if not _render_slots.acquire(timeout=0.25):
        raise ValueError("Miniatura ocupada")
    try:
        resultado = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("documentos_miniatura.py"))],
            input=documento.contenido, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=8, check=True,
        ).stdout
        if not resultado.startswith(b"\xff\xd8\xff") or len(resultado) > _MAX_THUMB:
            raise ValueError("Miniatura no disponible")
        with _cache_lock:
            _cache[clave] = resultado
            _cache.move_to_end(clave)
            while len(_cache) > _MAX_ENTRIES:
                _cache.popitem(last=False)
        return resultado
    except (subprocess.SubprocessError, OSError) as exc:
        raise ValueError("Miniatura no disponible") from exc
    finally:
        _render_slots.release()
