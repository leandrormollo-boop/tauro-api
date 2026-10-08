from pathlib import Path

from servicios.estados_envio import presentar_estados_envio


def test_ultimo_tracking_disponible_no_equivale_a_entrega():
    aviso = 'This is final status for this shipment tracking number.'
    envio = presentar_estados_envio({'estado': 'DESPACHADO', 'tracking_estado': 'PROCESO_ENTREGA', 'tracking_descripcion': aviso})
    assert envio['seguimiento_sin_actualizaciones'] is True
    assert envio['estado_cliente_ui']['label'] == 'En tránsito'
    assert envio['tracking_descripcion'] == aviso
    assert envio['estado'] == 'DESPACHADO'
    for estado in ('ENTREGADO', 'REEMPLAZADO', 'CANCELADO'):
        terminado = presentar_estados_envio({'estado': estado, 'tracking_descripcion': aviso})
        assert terminado['seguimiento_sin_actualizaciones'] is False
from servicios.panel_cliente import preparar_historial_envios


ROOT = Path(__file__).resolve().parents[1]


def test_presenta_dos_estados_con_vocabulario_canonico():
    envio = presentar_estados_envio({
        "estado": "GUIA_LISTA",
        "tracking_estado": "PROCESO_ENTREGA",
    })

    assert envio["estado_operacion_ui"]["label"] == "Guía lista"
    assert envio["estado_tracking_ui"] == {
        "codigo": "PROCESO_ENTREGA",
        "label": "En tránsito",
        "clase": "warn",
    }


def test_colores_de_tracking_siguen_la_regla_operativa():
    casos = {
        "PROCESO_ENTREGA": ("En tránsito", "warn"),
        "ENTREGADO": ("Entregado", "ok"),
        "RETENIDO": ("Retenido", "error"),
    }

    for codigo, (label, clase) in casos.items():
        presentado = presentar_estados_envio({"tracking_estado": codigo})
        assert presentado["estado_tracking_ui"]["label"] == label
        assert presentado["estado_tracking_ui"]["clase"] == clase


def test_transito_es_ambar_y_entregado_permanece_verde():
    recolectado = presentar_estados_envio({"estado": "DESPACHADO"})
    entregado = presentar_estados_envio({"estado": "ENTREGADO"})

    assert recolectado["estado_operacion_ui"] == {
        "codigo": "DESPACHADO",
        "label": "En tránsito",
        "clase": "warn",
    }
    assert entregado["estado_operacion_ui"] == {
        "codigo": "ENTREGADO",
        "label": "Entregado",
        "clase": "ok",
    }


def test_tracking_sin_snapshot_es_sin_movimientos():
    envio = presentar_estados_envio({"estado": "SOLICITADO"})
    assert envio["estado_operacion_ui"]["label"] == "Solicitado"
    assert envio["estado_tracking_ui"]["label"] == "Sin movimientos"


def test_contadores_incluyen_canceladas_y_suman_total():
    historial = [
        {"estado": "SOLICITADO", "destino_pais": "US", "remitente_pais": "AR"},
        {"estado": "GUIA_LISTA", "destino_pais": "US", "remitente_pais": "AR"},
        {"estado": "CANCELADO", "destino_pais": "US", "remitente_pais": "AR"},
        {"estado": "REEMPLAZADO", "destino_pais": "US", "remitente_pais": "AR"},
    ]

    vista = preparar_historial_envios(historial)
    conteos = {chip["clave"]: chip["cantidad"] for chip in vista["chips"]}

    assert conteos["canceladas"] == 1
    assert conteos["modificados"] == 1
    assert sum(conteos.values()) == 4
    assert vista["total_busqueda"] == 2


def test_plantillas_no_duplican_mapas_de_estados():
    for relativo in (
        "templates/portal/home.html",
        "templates/portal/envios.html",
        "templates/portal/envio_detalle.html",
    ):
        contenido = (ROOT / relativo).read_text()
        assert "set estados =" not in contenido
        assert "estado_cliente_ui" in contenido


def test_stepper_usa_los_hitos_canonicos():
    from servicios.estados_envio import HITOS_ENVIO_UI
    assert [hito['label'] for hito in HITOS_ENVIO_UI] == [
        'Solicitado', 'Guía lista', 'En tránsito', 'Entregado',
    ]
    contenido = (ROOT / 'templates/portal/envio_detalle.html').read_text()
    assert 'for hito in hitos_envio_ui' in contenido
    assert 'estados_labels' not in contenido


def test_schema_restringe_estados_operativos():
    schema = (ROOT / "sql/schema.sql").read_text()
    assert "ck_solicitudes_guia_estado" in schema
    assert "'ENTREGADO', 'REEMPLAZADO', 'CANCELADO'" in schema


def test_envio_realizado_presenta_despacho_declarado_sin_reescribir_historia():
    from decimal import Decimal
    datos = dict(estado='GUIA_LISTA', coti_id='EXT-declarado', tracking='123',
                 precio_tauro_ars=Decimal('1234.5678'), created_at='2026-10-01')
    envio = presentar_estados_envio(dict(datos))
    assert envio['estado_cliente_ui']['label'] == 'Despachado'
    assert envio['seguimiento_pendiente_tauro'] is True
    assert {k: envio[k] for k in datos} == datos
    vista = preparar_historial_envios([envio], paso='despachados')
    assert len(vista['solicitudes']) == 1
    assert vista['total_requieren_accion'] == 0


def test_presentacion_manual_no_infiere_despacho_por_edad_ni_pisa_estados_finales():
    from servicios.estados_envio import estado_operativo_presentado
    for estado in ('CANCELADO', 'REEMPLAZADO', 'ENTREGADO', 'SOLICITADO'):
        assert estado_operativo_presentado(dict(
            estado=estado, coti_id='EXT-declarado', tracking='123')) == estado
    for datos in ({'coti_id': 'EXT-declarado'}, {'tracking': '123'}):
        assert estado_operativo_presentado(dict(
            estado='GUIA_LISTA', created_at='2020-01-01', **datos)) == 'GUIA_LISTA'
    entregado = presentar_estados_envio(dict(estado='GUIA_LISTA', coti_id='EXT-real',
                                            tracking='123', tracking_estado='ENTREGADO'))
    assert entregado['estado_cliente_ui']['label'] == 'Entregado'
    assert entregado['seguimiento_pendiente_tauro'] is False
