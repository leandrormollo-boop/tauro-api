"""Copia de presentación de la invoice DHL; el PDF original nunca se escribe.

La ubicación y el logo provienen del ejemplo aprobado por TAURO (02/10/2026).
Sólo se agrega una imagen en el espacio libre de COMMERCIAL_INVOICE_P_10.
Formatos distintos o zonas ocupadas conservan íntegramente el documento original.
"""
from collections import OrderedDict
import hashlib
import io
import logging
from pathlib import Path
import subprocess
import sys
import threading


_LOGO = Path(__file__).resolve().parents[1] / "static/img/tauro-invoice-logo.png"
_VERSION = "1"
# Coordenadas PDF (origen inferior izquierdo), proporciones de la referencia.
_MARCA = (490.0, 770.0, 541.2, 822.3)
_MAX_PDF = 32 * 1024**2
_MAX_CACHE = 8 * 1024**2
_cache = OrderedDict()
_cache_bytes = 0
_cache_lock = threading.Lock()
_slots = threading.BoundedSemaphore(2)
_logger = logging.getLogger(__name__)


def factura_para_cliente(contenido: bytes, courier: str) -> bytes:
    """Personaliza sólo después de que el llamador haya comprobado el dueño.

    Caché de bytes por contenido, sin respuestas HTTP ni datos de sesión. El
    renderer se ejecuta aislado y acotado igual que las miniaturas del portal.
    Un fallo del logotipo no impide descargar la documentación del courier.
    """
    global _cache_bytes
    if not contenido or str(courier or "").strip().upper() != "DHL":
        return contenido
    contenido = bytes(contenido)
    if len(contenido) > _MAX_PDF or not contenido.startswith(b"%PDF-"):
        return contenido
    clave = hashlib.sha256(contenido).digest()
    with _cache_lock:
        if clave in _cache:
            _cache.move_to_end(clave)
            return _cache[clave]
    if not _slots.acquire(timeout=0.25):
        _logger.warning("Invoice TAURO: renderer ocupado; se conserva el original")
        return contenido
    try:
        resultado = subprocess.run(
            [sys.executable, str(Path(__file__).resolve())],
            input=contenido, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=10, check=True,
        ).stdout
        if not resultado.startswith(b"%PDF-") or len(resultado) > _MAX_PDF:
            raise ValueError("Resultado PDF inválido")
        with _cache_lock:
            if len(resultado) <= _MAX_CACHE:
                previo = _cache.pop(clave, b"")
                _cache_bytes -= len(previo)
                _cache[clave] = resultado
                _cache_bytes += len(resultado)
                while _cache_bytes > _MAX_CACHE or len(_cache) > 64:
                    _, descartado = _cache.popitem(last=False)
                    _cache_bytes -= len(descartado)
        return resultado
    except (subprocess.SubprocessError, OSError, ValueError):
        _logger.warning("Invoice TAURO: marca no disponible; se conserva el original")
        return contenido
    finally:
        _slots.release()


def _personalizar_pdf(contenido: bytes) -> bytes:
    """Worker: valida el documento completo antes de agregar la marca."""
    import math
    from PIL import ImageChops
    from pypdf import PdfReader, PdfWriter
    import pypdfium2 as pdfium
    from reportlab.pdfgen import canvas

    reader = PdfReader(io.BytesIO(contenido), strict=False)
    if (reader.is_encrypted or not 1 <= len(reader.pages) <= 30
            or reader.trailer["/Root"].get("/AcroForm")
            or (reader.metadata or {}).get("/TauroInvoiceBrand") == _VERSION):
        return contenido
    textos = []
    # Se comprueban también imágenes y trazos mediante el render; texto vacío
    # en una región no basta para saber si está libre (puede haber otro logo).
    with pdfium.PdfDocument(contenido) as rendered:
        for indice, pagina in enumerate(reader.pages):
            width, height = float(pagina.mediabox.width), float(pagina.mediabox.height)
            if (pagina.rotation != 0 or pagina.get("/UserUnit", 1) != 1
                    or list(pagina.cropbox) != list(pagina.mediabox)
                    or list(pagina.mediabox.lower_left) != [0, 0]
                    or not (594 <= width <= 596 and 840 <= height <= 843)
                    or pagina.get("/Annots")):
                return contenido
            texto = pagina.extract_text() or ""
            header = "".join(texto.upper().split())
            if not all(token in header for token in (
                "COMMERCIALINVOICE", "AWBNO", "INVOICEDATE", "INVOICENO",
            )):
                return contenido
            textos.append(texto.split())
            vista = rendered[indice]
            try:
                bitmap = vista.render(scale=2)
                try:
                    imagen = bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()
            finally:
                vista.close()
            x0, y0, x1, y1 = _MARCA
            # Margen de dos puntos para evitar tocar caracteres/antialiasing.
            region = imagen.crop((
                math.floor((x0 - 2) * 2), math.floor((height - y1 - 2) * 2),
                math.ceil((x1 + 2) * 2), math.ceil((height - y0 + 2) * 2),
            ))
            blanco = region.copy()
            blanco.paste((255, 255, 255), (0, 0, *region.size))
            if ImageChops.difference(region, blanco).getbbox() is not None:
                return contenido

    sello = io.BytesIO()
    c = canvas.Canvas(sello, pagesize=(595, 841), pageCompression=1)
    x0, y0, x1, y1 = _MARCA
    c.drawImage(str(_LOGO), x0, y0, width=x1-x0, height=y1-y0,
                preserveAspectRatio=True, anchor="c", mask="auto")
    c.save()
    overlay = PdfReader(io.BytesIO(sello.getvalue())).pages[0]
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    for pagina in writer.pages:
        pagina.merge_page(overlay, expand=False)
    writer.add_metadata({"/TauroInvoiceBrand": _VERSION})
    salida = io.BytesIO()
    writer.write(salida)
    resultado = salida.getvalue()
    comprobacion = PdfReader(io.BytesIO(resultado))
    if (len(comprobacion.pages) != len(reader.pages)
            or [p.extract_text().split() for p in comprobacion.pages] != textos
            or [list(p.mediabox) for p in comprobacion.pages]
            != [list(p.mediabox) for p in reader.pages]):
        raise ValueError("La copia alteró el contenido de la invoice")
    return resultado


if __name__ == "__main__":
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024**2, 768 * 1024**2))
    data = sys.stdin.buffer.read(_MAX_PDF + 1)
    if len(data) > _MAX_PDF:
        raise ValueError("Documento demasiado grande")
    sys.stdout.buffer.write(_personalizar_pdf(data))
