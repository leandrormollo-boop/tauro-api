"""El cliente ve el tracking en castellano, en hora argentina y con alerta
cuando el courier lleva días sin informar un movimiento nuevo."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from servicios.estados_envio import presentar_estados_envio
from servicios.tracking_presentacion import (
    mensaje_cliente,
    presentar_tracking,
)

RAIZ = Path(__file__).resolve().parents[1]
AHORA = datetime(2026, 10, 6, 11, 20, tzinfo=timezone.utc)


def _envio(**extra):
    base = {
        "courier": "DHL",
        "tracking": "2413493235",
        "estado": "DESPACHADO",
        "tracking_estado": "PROCESO_ENTREGA",
        "tracking_estado_courier": "PU",
        "tracking_descripcion": "Shipment picked up",
        "tracking_evento_at": datetime(2026, 10, 1, 10, 40, tzinfo=timezone(timedelta(hours=8))),
        "tracking_actualizado_at": datetime(2026, 10, 6, 8, 20, tzinfo=timezone.utc),
    }
    base.update(extra)
    return presentar_estados_envio(base)


def test_traduce_el_codigo_dhl_y_conserva_el_original():
    ui = presentar_tracking(_envio(), ahora=AHORA)["tracking_ui"]

    assert ui["mensaje"] == "DHL retiró el envío"
    assert ui["original"] == "Shipment picked up"


def test_codigo_desconocido_muestra_el_mensaje_original_sin_repetirlo():
    ui = presentar_tracking(
        _envio(tracking_estado_courier="ZZ", tracking_descripcion="Something new"),
        ahora=AHORA,
    )["tracking_ui"]

    assert ui["mensaje"] == "Something new"
    assert ui["original"] == ""
    assert mensaje_cliente("FEDEX", "PU", "Picked up") == "Picked up"


def test_fechas_en_hora_argentina():
    ui = presentar_tracking(_envio(), ahora=AHORA)["tracking_ui"]

    # 01/10 10:40 en China (+08) = 30/09 23:40 en Argentina (-03).
    assert ui["evento_label"] == "30/09/2026 23:40"
    assert ui["consulta_label"] == "06/10/2026 05:20"


def test_alerta_cuando_no_hay_movimientos_hace_varios_dias():
    ui = presentar_tracking(_envio(), ahora=AHORA)["tracking_ui"]

    assert ui["dias_sin_movimiento"] == 6
    assert ui["alerta_sin_avance"] is True


def test_sin_alerta_si_el_movimiento_es_reciente_o_el_envio_termino():
    reciente = _envio(tracking_evento_at=AHORA - timedelta(days=1))
    entregado = _envio(estado="ENTREGADO", tracking_estado="ENTREGADO",
                       tracking_estado_courier="OK")
    retenido = _envio(tracking_estado="RETENIDO", tracking_estado_courier="OH")

    assert presentar_tracking(reciente, ahora=AHORA)["tracking_ui"]["alerta_sin_avance"] is False
    assert presentar_tracking(entregado, ahora=AHORA)["tracking_ui"]["alerta_sin_avance"] is False
    # La retención ya tiene su propio aviso de incidencia.
    assert presentar_tracking(retenido, ahora=AHORA)["tracking_ui"]["alerta_sin_avance"] is False


def test_sin_fecha_de_evento_usa_el_ultimo_cambio_visto_y_si_no_hay_no_alerta():
    con_cambio = _envio(tracking_evento_at=None,
                        tracking_cambio_at=AHORA - timedelta(days=5))
    sin_datos = _envio(tracking_evento_at=None)

    assert presentar_tracking(con_cambio, ahora=AHORA)["tracking_ui"]["alerta_sin_avance"] is True
    ui = presentar_tracking(sin_datos, ahora=AHORA)["tracking_ui"]
    assert ui["alerta_sin_avance"] is False and ui["dias_sin_movimiento"] is None


def test_el_detalle_muestra_movimiento_hora_argentina_y_alerta():
    html = (RAIZ / "templates/portal/envio_detalle.html").read_text(encoding="utf-8")

    assert "Último movimiento" in html
    assert "hora de Argentina" in html
    assert "tu.alerta_sin_avance" in html
    assert "%Z" not in html.split("Tracking{%")[1].split("Composición del envío")[0]


def test_schema_agrega_columnas_de_evento_y_cambio():
    schema = (RAIZ / "sql/schema.sql").read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS tracking_evento_at TIMESTAMPTZ" in schema
    assert "ADD COLUMN IF NOT EXISTS tracking_cambio_at TIMESTAMPTZ" in schema
