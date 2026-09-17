"""Regresiones de estados contradictorios y respuestas parciales de Mis envíos."""
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.requests import Request

from servicios.couriers_urls import ambito_envio, nombre_courier, url_tracking
from servicios.estados_envio import presentar_estados_envio
from servicios.panel_cliente import preparar_historial_envios
from servicios.periodos_envios import normalizar_periodo


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("operacion,tracking,principal,paso", [
    ("DESPACHADO", "ENTREGADO", "ENTREGADO", "entregados"),
    ("GUIA_LISTA", "RETENIDO", "RETENIDO", "retenidos"),
    ("DESPACHADO", "RETENIDO", "RETENIDO", "retenidos"),
    ("GUIA_LISTA", "PROCESO_ENTREGA", "PROCESO_ENTREGA", "despachados"),
    ("DESPACHADO", None, "DESPACHADO", "despachados"),
    ("GUIA_LISTA", None, "GUIA_LISTA", "guia_lista"),
    ("REEMPLAZADO", "ENTREGADO", "REEMPLAZADO", "modificados"),
    ("CANCELADO", "RETENIDO", "CANCELADO", "canceladas"),
    ("ENTREGADO", "PROCESO_ENTREGA", "ENTREGADO", "entregados"),
])
def test_estado_principal_y_filtro_coinciden_sin_reescribir_historia(operacion, tracking, principal, paso):
    envio = {"id": 1, "estado": operacion, "tracking_estado": tracking,
             "remitente_pais": "CN", "destino_pais": "UY"}
    presentacion = presentar_estados_envio(envio)
    assert presentacion["estado_cliente_ui"]["codigo"] == principal
    assert presentacion["estado"] == operacion
    assert presentacion["tracking_estado"] == tracking
    vista = preparar_historial_envios([envio], paso=paso)
    assert [s["id"] for s in vista["solicitudes"]] == [1]
    assert sum(c["cantidad"] for c in vista["chips"]) == 1


def _render(parcial=False, estado="DESPACHADO", tracking="ENTREGADO", precio=100, template="portal/envios.html",
            error="", puede_emitir=False, tracking_numero="DEMO-0001"):
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape())
    env.globals.update(
        url_tracking=url_tracking, nombre_courier=nombre_courier,
        ambito_envio=ambito_envio, nombre_pais=lambda p: p or "",
        saldo_menu=lambda *_: None, pendientes_menu=lambda *_: {},
        ayuda=lambda: {"mail_url": "mailto:demo@example.invalid"},
    )
    request = Request({"type": "http", "method": "GET", "path": "/portal/envios",
                       "query_string": urlencode({"error": error}).encode() if error else b"",
                       "headers": [], "state": {"csp_nonce": "test"}})
    envio = {"id": 1, "estado": estado, "tracking_estado": tracking,
             "courier": "DHL", "remitente_pais": "CN", "destino_pais": "UY",
             "dest_nombre": "Destinatario DEMO", "tracking": tracking_numero,
             "tracking_descripcion": "<script>no ejecutar</script>",
             "tracking_actualizado_at": datetime(2026, 9, 14, 8, 20, tzinfo=timezone.utc),
             "producto_alias": "Muestras", "precio_tauro_ars": precio,
             "precio_cliente_final_ars": precio * 2, "numero_guia_tauro": 50300,
             "precio_inicial_cliente_ars": precio, "precio_final_cliente_ars": precio,
             "tiene_label": True, "bultos": [], "cantidad": 1,
             "resumen_pesos": {"real_total_kg": 1, "volumetrico_total_kg": 1,
                               "facturable_total_kg": 1, "divisor": 5000, "cobra_por_volumen": False}}
    return env.get_template(template).render(
        **preparar_historial_envios([envio], paso={"CANCELADO":"canceladas", "REEMPLAZADO":"modificados"}.get(estado,"")), parcial=parcial, request=request,
        cliente="CLIENTE_DEMO", periodo=normalizar_periodo("", "", "", [(2026, 9)]),
        periodo_query="", puede_emitir=puede_emitir, s=presentar_estados_envio(envio),
    )


@pytest.mark.parametrize("estado", ["EMITIENDO", "VERIFICAR_COURIER"])
def test_detalle_no_ofrece_reemitir_una_operacion_en_curso_o_incierta(estado):
    html = _render(template="portal/envio_detalle.html", estado=estado,
                   tracking=None, tracking_numero=None, puede_emitir=True)
    assert "Emitir guía ahora" not in html


def test_error_de_emision_se_muestra_una_vez_y_no_promete_solicitud_lista():
    html = _render(template="portal/envio_detalle.html", estado="SOLICITADO",
                   tracking=None, tracking_numero=None, puede_emitir=True,
                   error="Ciudad del destinatario: DHL no encontró la ciudad.")
    assert html.count("Ciudad del destinatario: DHL no encontró la ciudad.") == 1
    assert "Tu solicitud está lista" not in html
    assert "La guía sigue pendiente" in html


def test_respuesta_parcial_conserva_estado_y_acciones_sin_repetir_navegacion():
    full, partial = _render(), _render(parcial=True)
    for html in (full, partial):
        assert 'data-envios-region' in html
        assert html.count('title="Estado actual del envío">Entregado</span>') == 1
        assert 'title="Estado de la operación"' not in html
        assert '/portal/envios/1/guia.pdf' in html
        assert '/portal/envios/nuevo?repetir=1' in html
        assert 'no ejecutar</script>' not in html
        assert '&lt;script&gt;no ejecutar&lt;/script&gt;' in html
        assert 'Sincronizado 14/09/2026 08:20 UTC' in html
    assert '<aside class="sidebar">' in full
    assert '<aside class="sidebar">' not in partial
    assert '<script' not in partial
    assert 'id="shipment-verification-dialog"' not in partial
    assert len(partial.encode()) < len(full.encode())


def test_cancelado_no_ofrece_etiqueta_ni_suma_su_precio_en_la_pagina():
    html = _render(estado="CANCELADO", precio=987654)
    assert '/portal/envios/1/guia.pdf' not in html
    assert '987.654' not in html
    assert 'Sin cargo' in html


def test_importe_sin_ajustes_no_repite_precio_inicial_y_final():
    html = _render(parcial=True, precio=125)
    assert 'Ver desglose' not in html
    assert 'envio-price-initial' not in html
    assert 'Total registrado · ARS' in html


def test_los_retenciones_no_caen_en_el_filtro_de_entregados():
    historial = [
        {"id": i, "estado": "DESPACHADO", "tracking_estado": tracking,
         "remitente_pais": "CN", "destino_pais": "UY"}
        for i, tracking in enumerate(["RETENIDO", "ENTREGADO", None], 1)
    ]
    for paso, ids in (("retenidos", [1]), ("entregados", [2]), ("despachados", [3])):
        vista = preparar_historial_envios(historial, paso=paso)
        assert [s["id"] for s in vista["solicitudes"]] == ids
        assert sum(c["cantidad"] for c in vista["chips"]) == 3


@pytest.mark.parametrize("tracking,esperado", [
    ("ENTREGADO", "El courier confirmó la entrega del envío."),
    ("RETENIDO", "El courier informa una retención."),
    ("PROCESO_ENTREGA", "Tu envío está en tránsito."),
])
def test_detalle_no_contradice_el_seguimiento_con_un_hito_anterior(tracking, esperado):
    html = _render(tracking=tracking, template="portal/envio_detalle.html")
    assert esperado in html
    assert "ya recolectó tu envío. Seguilo" not in html
    assert "no ejecutar</script>" not in html


@pytest.mark.parametrize("template", ["portal/envios.html", "portal/envio_detalle.html"])
def test_retiro_solo_para_guias_sin_movimiento(template):
    assert '/portal/recolecciones?envio=1' in _render(estado="GUIA_LISTA", tracking=None, template=template)
    for tracking in ("ENTREGADO", "RETENIDO", "PROCESO_ENTREGA"):
        assert '/portal/recolecciones?envio=1' not in _render(estado="GUIA_LISTA", tracking=tracking, template=template)


@pytest.mark.parametrize("estado", ["CANCELADO", "REEMPLAZADO"])
@pytest.mark.parametrize("template", ["portal/envios.html", "portal/envio_detalle.html"])
def test_baja_no_muestra_costos_aunque_conserve_importes_historicos(estado, template):
    html = _render(estado=estado, precio=987654, template=template)
    assert '987.654' not in html
    assert '1.975.308' not in html
    assert 'Sin cargo' in html
    assert estado in html
    assert '/portal/envios/1/guia.pdf' not in html


def test_numero_tauro_permite_encontrar_el_mismo_envio():
    vista = preparar_historial_envios([
        dict(id=7, numero_guia_tauro=50300, estado='GUIA_LISTA', remitente_pais='CN', destino_pais='AR'),
        dict(id=8, numero_guia_tauro=50301, estado='GUIA_LISTA', remitente_pais='CN', destino_pais='AR'),
    ], buscar='50300')
    assert [s['id'] for s in vista['solicitudes']] == [7]
