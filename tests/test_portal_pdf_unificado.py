"""Legajo descargable del cliente: guía primero, invoice al final."""

from io import BytesIO
from pathlib import Path

import pytest
from fastapi import HTTPException
from pypdf import PdfReader, PdfWriter

from endpoints import portal_cliente
from servicios import solicitudes_guia


RAIZ = Path(__file__).resolve().parents[1]


def _pdf_con_paginas(*tamanios: tuple[float, float]) -> bytes:
    writer = PdfWriter()
    for ancho, alto in tamanios:
        writer.add_blank_page(width=ancho, height=alto)
    salida = BytesIO()
    writer.write(salida)
    return salida.getvalue()


def test_unifica_todas_las_etiquetas_antes_de_la_invoice():
    guia = _pdf_con_paginas((100, 200), (110, 210))
    invoice = _pdf_con_paginas((300, 400))

    unificado = solicitudes_guia.unir_guia_e_invoice_pdf(guia, invoice)
    paginas = PdfReader(BytesIO(unificado)).pages

    assert len(paginas) == 3
    assert [(float(p.mediabox.width), float(p.mediabox.height)) for p in paginas] == [
        (100, 200),
        (110, 210),
        (300, 400),
    ]


def test_guia_historica_sin_invoice_conserva_su_pdf_original():
    guia = _pdf_con_paginas((100, 200))

    assert solicitudes_guia.unir_guia_e_invoice_pdf(guia) == guia


def test_invoice_invalida_no_se_oculta_devolviendo_solo_la_guia():
    with pytest.raises(ValueError, match="unificar"):
        solicitudes_guia.unir_guia_e_invoice_pdf(
            _pdf_con_paginas((100, 200)), b"esto no es un PDF",
        )


def test_nombre_del_archivo_sigue_formato_y_es_seguro_para_http():
    assert solicitudes_guia.nombre_archivo_documentos_envio(
        cliente_id="WAIMAO",
        dest_nombre="MARSANTEX",
        remitente_pais="CN", numero_guia_tauro=50300,
    ) == "TAURO - WAIMAO - MARSANTEX - CN - 50300.pdf"

    nombre = solicitudes_guia.nombre_archivo_documentos_envio(
        cliente_id='Waimao\r\n"malicioso',
        dest_nombre="José/Álvarez; SA",
        remitente_pais="cn", numero_guia_tauro=50301,
    )
    assert nombre == "TAURO - WAIMAO MALICIOSO - JOSE ALVAREZ SA - CN - 50301.pdf"
    assert not any(caracter in nombre for caracter in ('\r', '\n', '"', '/', ';'))


def test_descarga_portal_entrega_un_solo_pdf_con_nombre_comercial(monkeypatch):
    pdf = _pdf_con_paginas((100, 200), (300, 400))
    llamadas = []

    def preparar(solicitud_id, cliente_id):
        llamadas.append((solicitud_id, cliente_id))
        return {
            "pdf": pdf,
            "incluye_invoice": True,
            "filename": "TAURO - WAIMAO - MARSANTEX - CN - 50300.pdf",
        }

    monkeypatch.setattr(portal_cliente, "preparar_documentos_envio_portal", preparar)
    marcadas = []
    monkeypatch.setattr(
        portal_cliente,
        "marcar_guia_descargada_cliente",
        lambda solicitud_id, cliente_id: marcadas.append(
            (solicitud_id, cliente_id)
        ) or True,
    )

    respuesta = portal_cliente.descargar_guia(91, cliente="WAIMAO")

    assert llamadas == [(91, "WAIMAO")]
    assert marcadas == [(91, "WAIMAO")]
    assert bytes(respuesta.body) == pdf
    assert respuesta.media_type == "application/pdf"
    assert respuesta.headers["content-disposition"] == (
        'attachment; filename="TAURO - WAIMAO - MARSANTEX - CN - 50300.pdf"'
    )
    assert respuesta.headers["cache-control"] == "private, no-store"


def test_preparacion_filtra_por_duenio_y_usa_los_datos_del_mismo_envio(monkeypatch):
    guia = _pdf_con_paginas((100, 200))
    invoice = _pdf_con_paginas((300, 400))

    class Cursor:
        consulta = ""
        parametros = ()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, consulta, parametros):
            self.consulta = consulta
            self.parametros = parametros

        def fetchone(self):
            return {
                "label_pdf": guia,
                "commercial_invoice_pdf": invoice,
                "cliente_id": "WAIMAO",
                "dest_nombre": "MARSANTEX",
                "destino_pais": "UY",
                "remitente_pais": "CN",
                "numero_guia_tauro": 50300,
            }

    cursor = Cursor()

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return cursor

    monkeypatch.setattr(solicitudes_guia, "get_conn", lambda: Conn())

    documentos = solicitudes_guia.preparar_documentos_envio_portal(91, "waimao")

    assert documentos is not None
    assert documentos["incluye_invoice"] is True
    assert documentos["filename"] == "TAURO - WAIMAO - MARSANTEX - CN - 50300.pdf"
    assert len(PdfReader(BytesIO(documentos["pdf"])).pages) == 2
    assert cursor.parametros == (91, "WAIMAO")
    assert "s.cliente_id=%s" in cursor.consulta
    assert "s.estado <> 'CANCELADO'" in cursor.consulta
    assert "e.estado='CANCELADO'" in cursor.consulta


def test_descarga_portal_falla_cerrada_si_no_puede_unificar(monkeypatch):
    def fallar(*_args):
        raise ValueError("No se pudieron unificar la guía y la invoice.")

    monkeypatch.setattr(portal_cliente, "preparar_documentos_envio_portal", fallar)
    marcadas = []
    monkeypatch.setattr(
        portal_cliente,
        "marcar_guia_descargada_cliente",
        lambda *_args: marcadas.append(True),
    )

    with pytest.raises(HTTPException) as exc:
        portal_cliente.descargar_guia(91, cliente="WAIMAO")

    assert exc.value.status_code == 500
    assert "invoice" in exc.value.detail
    assert marcadas == []


def test_tarjetas_conservan_descarga_unificada_y_separan_vista_previa_invoice():
    detalle = (RAIZ / "templates" / "portal" / "envio_detalle.html").read_text(
        encoding="utf-8",
    )
    listado = (RAIZ / "templates" / "portal" / "envios.html").read_text(
        encoding="utf-8",
    )

    assert "Descargar guía + invoice" in detalle
    assert "Descargar guía + invoice" in listado
    assert "tarjeta_documento('guia'" in detalle
    assert "tarjeta_documento('invoice'" in detalle
