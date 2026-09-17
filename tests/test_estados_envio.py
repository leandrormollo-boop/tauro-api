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


def test_recolectado_es_violeta_y_entregado_permanece_verde():
    recolectado = presentar_estados_envio({"estado": "DESPACHADO"})
    entregado = presentar_estados_envio({"estado": "ENTREGADO"})

    assert recolectado["estado_operacion_ui"] == {
        "codigo": "DESPACHADO",
        "label": "Recolectado",
        "clase": "accent",
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
    ):
        contenido = (ROOT / relativo).read_text()
        assert "set estados =" not in contenido
        assert "estado_cliente_ui" in contenido


def test_detalle_nombra_recolectado_sin_exponer_despachado():
    contenido = (ROOT / "templates/portal/envio_detalle.html").read_text()

    assert '"DESPACHADO": "Recolectado"' in contenido
    assert '"DESPACHADO": "Despachado"' not in contenido


def test_schema_restringe_estados_operativos():
    schema = (ROOT / "sql/schema.sql").read_text()
    assert "ck_solicitudes_guia_estado" in schema
    assert "'ENTREGADO', 'REEMPLAZADO', 'CANCELADO'" in schema
