"""Contrato de marca TAURO para invoices DHL, con PDFs totalmente sintéticos."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import subprocess

import pypdfium2 as pdfium
import pytest
from PIL import ImageChops, ImageDraw
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from servicios import invoice_marca


MARCA = (490.0, 770.0, 541.2, 822.3)


@pytest.fixture(autouse=True)
def _sin_cache_de_otras_pruebas():
    """Cada prueba observa el worker y sus fallbacks, no una respuesta cacheada."""
    with invoice_marca._cache_lock:
        invoice_marca._cache.clear()
        invoice_marca._cache_bytes = 0
    yield
    with invoice_marca._cache_lock:
        invoice_marca._cache.clear()
        invoice_marca._cache_bytes = 0


def _invoice(*, paginas: int = 2, ocupa_marca_en: int | None = None) -> bytes:
    """Invoice portrait compatible, sin datos de una guía ni de un cliente real."""
    salida = BytesIO()
    pdf = canvas.Canvas(salida, pagesize=(595, 841), pageCompression=1)
    for numero in range(1, paginas + 1):
        pdf.setFont("Helvetica-Bold", 18)
        pdf.drawString(55, 795, "Commercial Invoice")
        pdf.setFont("Helvetica", 10)
        pdf.drawString(55, 772, "AWB No: SYNTHETIC-0001")
        pdf.drawString(225, 772, "Invoice Date: 2026-10-02")
        pdf.drawString(390, 772, "Invoice No: SYN-0001")
        # Todo el contenido operativo queda debajo de la franja de la marca.
        pdf.drawString(55, 700, f"SHIP FROM: Synthetic sender / page {numero}")
        pdf.drawString(55, 680, "SHIP TO: Synthetic receiver")
        pdf.drawString(55, 640, "Description: synthetic catalogue sample")
        for x in range(60, 181, 12):
            pdf.rect(x, 590, 4, 64, fill=1, stroke=0)
        pdf.drawString(55, 560, "Reference Type & ID: TEST-ONLY")
        pdf.drawString(55, 90, "I certify this synthetic invoice is correct.")
        if ocupa_marca_en == numero:
            pdf.setFont("Helvetica", 8)
            pdf.drawString(500, 790, "OCCUPIED")
        pdf.showPage()
    pdf.save()
    return salida.getvalue()


def _texto_por_pagina(contenido: bytes) -> list[str]:
    return [pagina.extract_text() for pagina in PdfReader(BytesIO(contenido)).pages]


def _imagenes_de_pagina(pagina) -> list[object]:
    recursos = pagina.get("/Resources") or {}
    xobjects = recursos.get("/XObject") or {}
    return [obj.get_object() for obj in xobjects.values()
            if obj.get_object().get("/Subtype") == "/Image"]


def _pixeles(contenido: bytes, numero: int):
    with pdfium.PdfDocument(contenido) as documento:
        pagina = documento[numero]
        try:
            bitmap = pagina.render(scale=2)
            try:
                return bitmap.to_pil().convert("RGB").copy()
            finally:
                bitmap.close()
        finally:
            pagina.close()


def _afuera_de_marca(imagen):
    """Conserva todos los píxeles salvo la marca y dos píxeles de margen."""
    x0, y0, x1, y1 = MARCA
    resultado = imagen.copy()
    # En el raster, PDF tiene origen inferior izquierdo.
    izquierda = int((x0 - 1) * 2)
    derecha = int((x1 + 1) * 2)
    arriba = int((841 - y1 - 1) * 2)
    abajo = int((841 - y0 + 1) * 2)
    ImageDraw.Draw(resultado).rectangle(
        (izquierda, arriba, derecha - 1, abajo - 1), fill=(255, 255, 255),
    )
    return resultado


def test_worker_marca_todas_las_invoices_compatibles_sin_alterar_texto_ni_geometria():
    original = _invoice()
    resultado = invoice_marca._personalizar_pdf(original)

    antes = PdfReader(BytesIO(original))
    despues = PdfReader(BytesIO(resultado))
    assert resultado != original
    assert len(despues.pages) == 2
    assert _texto_por_pagina(resultado) == _texto_por_pagina(original)
    assert [list(p.mediabox) for p in despues.pages] == [list(p.mediabox) for p in antes.pages]
    assert [p.rotation for p in despues.pages] == [p.rotation for p in antes.pages] == [0, 0]
    assert despues.metadata["/TauroInvoiceBrand"] == "1"
    assert all(_imagenes_de_pagina(pagina) for pagina in despues.pages)

    for indice in range(2):
        diferencia = ImageChops.difference(
            _afuera_de_marca(_pixeles(original, indice)),
            _afuera_de_marca(_pixeles(resultado, indice)),
        )
        assert diferencia.getbbox() is None


def test_worker_es_idempotente_por_metadata_de_marca():
    primera = invoice_marca._personalizar_pdf(_invoice())
    assert invoice_marca._personalizar_pdf(primera) == primera


def test_una_region_ocupada_en_cualquier_pagina_conserva_el_pdf_completo():
    original = _invoice(ocupa_marca_en=2)
    assert invoice_marca._personalizar_pdf(original) == original


def test_pdf_rotado_se_conserva_byte_a_byte():
    original = _invoice(paginas=1)
    reader = PdfReader(BytesIO(original))
    writer = PdfWriter()
    writer.add_page(reader.pages[0].rotate(90))
    rotado = BytesIO()
    writer.write(rotado)
    contenido = rotado.getvalue()

    assert invoice_marca._personalizar_pdf(contenido) == contenido


@pytest.mark.parametrize("contenido,courier", [(b"", "DHL"), (b"%PDF-sin-importancia", "FedEx")])
def test_courier_distinto_o_vacio_no_inicia_el_worker(monkeypatch, contenido, courier):
    def no_deberia_ejecutarse(*_args, **_kwargs):
        raise AssertionError("no debe iniciar el subprocess")

    monkeypatch.setattr(invoice_marca.subprocess, "run", no_deberia_ejecutarse)
    assert invoice_marca.factura_para_cliente(contenido, courier) == contenido


def test_fallo_del_worker_devuelve_el_original(monkeypatch):
    original = _invoice(paginas=1)

    def expirar(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("invoice_marca", 10)

    monkeypatch.setattr(invoice_marca.subprocess, "run", expirar)
    assert invoice_marca.factura_para_cliente(original, "DHL") == original
