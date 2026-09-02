"""Año, mes y semanas contables de Mis envíos y ADMIN."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from servicios.panel_cliente import preparar_historial_envios
from servicios.periodos_envios import normalizar_periodo, rango_periodo


RAIZ = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "semana,desde,hasta",
    [
        (0, date(2026, 8, 1), date(2026, 9, 1)),
        (1, date(2026, 8, 1), date(2026, 8, 8)),
        (2, date(2026, 8, 8), date(2026, 8, 15)),
        (3, date(2026, 8, 15), date(2026, 8, 22)),
        (4, date(2026, 8, 22), date(2026, 9, 1)),
    ],
)
def test_semanas_son_tramos_fijos_que_no_cruzan_el_mes(semana, desde, hasta):
    assert rango_periodo(2026, 8, semana) == (desde, hasta)


def test_semana_cuatro_cierra_correctamente_febrero_bisiesto():
    assert rango_periodo(2028, 2, 4) == (
        date(2028, 2, 22), date(2028, 3, 1)
    )


def test_default_es_todo_el_historial_y_query_invalida_es_segura():
    completo = normalizar_periodo(
        "", "", "", [(2026, 8), (2026, 5)], hoy=date(2026, 9, 1)
    )
    manipulada = normalizar_periodo(
        "SQL", "99", "8", [(2026, 8)], hoy=date(2026, 9, 1)
    )

    assert (completo["anio"], completo["mes"], completo["semana"]) == (0, 0, 0)
    assert completo["desde"] is None and completo["hasta"] is None
    assert completo["etiqueta"] == "Todo el historial"
    assert (manipulada["anio"], manipulada["mes"], manipulada["semana"]) == (
        0, 0, 0
    )


def test_anio_mes_y_semana_se_aplican_en_jerarquia():
    anual = normalizar_periodo("2026", "", "", [(2026, 8)])
    mensual = normalizar_periodo("2026", "8", "", [(2026, 8)])

    assert (anual["desde"], anual["hasta"], anual["etiqueta"]) == (
        date(2026, 1, 1), date(2027, 1, 1), "Año 2026"
    )
    assert (mensual["desde"], mensual["hasta"], mensual["etiqueta"]) == (
        date(2026, 8, 1), date(2026, 9, 1), "Agosto 2026"
    )
    assert normalizar_periodo("", "8", "2", [(2026, 8)])["activo"] is False


def test_resumen_del_periodo_no_cobra_canceladas_ni_reemplazadas():
    base = {
        "courier": "DHL",
        "remitente_pais": "AR",
        "destino_pais": "US",
    }
    vista = preparar_historial_envios([
        {**base, "id": 1, "estado": "GUIA_LISTA", "tracking": "A",
         "precio_tauro_ars": Decimal("100")},
        {**base, "id": 2, "estado": "SOLICITADO", "tracking": None,
         "precio_tauro_ars": Decimal("50")},
        {**base, "id": 3, "estado": "CANCELADO", "tracking": "C",
         "precio_tauro_ars": Decimal("999")},
        {**base, "id": 4, "estado": "REEMPLAZADO", "tracking": "D",
         "precio_tauro_ars": Decimal("999")},
    ], tipo="internacional")

    assert vista["resumen_periodo"] == {
        "operaciones_vigentes": 2,
        "guias_emitidas": 1,
        "canceladas_reemplazadas": 2,
        "total_ars": Decimal("150"),
    }


def test_endpoint_portal_aplica_el_rango_al_cliente_autenticado(monkeypatch):
    from endpoints import portal_cliente as portal
    from servicios import configuracion_couriers_cliente as permisos

    llamadas = []
    monkeypatch.setattr(
        portal, "periodos_solicitudes_cliente", lambda cliente: [(2026, 8)]
    )
    monkeypatch.setattr(
        portal,
        "listar_solicitudes_cliente",
        lambda cliente, **kw: llamadas.append((cliente, kw)) or [],
    )
    monkeypatch.setattr(
        portal,
        "preparar_historial_envios",
        lambda *_args, **_kwargs: {
            "solicitudes": [], "tipo_filtro": "internacional",
            "paso_filtro": "", "chips": [], "total_sin_filtrar": 0,
            "total_resultados": 0, "pagina_actual": 1, "total_paginas": 1,
            "paginas_visibles": [1], "pagina_desde": 0, "pagina_hasta": 0,
            "tiene_historial": False, "total_nacionales": 0,
            "total_internacionales": 0, "total_sin_clasificar": 0,
            "resumen_periodo": {},
        },
    )
    monkeypatch.setattr(permisos, "mapa_permisos", lambda *_args: {})
    monkeypatch.setattr(
        portal.templates,
        "TemplateResponse",
        lambda *, context, **_kw: SimpleNamespace(status_code=200, context=context),
    )

    respuesta = portal.envios_view(
        SimpleNamespace(), tipo="internacional", anio="2026", mes="8",
        semana="2", cliente="WAIMAO",
    )

    assert respuesta.status_code == 200
    assert llamadas == [("WAIMAO", {
        "limite": None,
        "desde": date(2026, 8, 8),
        "hasta": date(2026, 8, 15),
    })]
    assert respuesta.context["periodo"]["etiqueta"] == (
        "8–14 de agosto de 2026"
    )


def test_buscador_portal_descarta_todos_los_filtros_activos(monkeypatch):
    from endpoints import portal_cliente as portal
    from servicios import configuracion_couriers_cliente as permisos

    llamadas = []
    argumentos_vista = []
    monkeypatch.setattr(
        portal, "periodos_solicitudes_cliente", lambda _cliente: [(2026, 8)]
    )
    monkeypatch.setattr(
        portal, "listar_solicitudes_cliente",
        lambda cliente, **kw: llamadas.append((cliente, kw)) or [],
    )
    monkeypatch.setattr(
        portal, "preparar_historial_envios",
        lambda _historial, **kw: argumentos_vista.append(kw) or {
            "solicitudes": [], "tipo_filtro": "", "paso_filtro": "",
            "chips": [], "total_sin_filtrar": 0, "total_busqueda": 0,
            "total_resultados": 0, "pagina_actual": 1, "total_paginas": 1,
            "paginas_visibles": [1], "pagina_desde": 0, "pagina_hasta": 0,
            "tiene_historial": False, "total_nacionales": 0,
            "total_internacionales": 0, "total_sin_clasificar": 0,
            "resumen_periodo": {}, "busqueda_filtro": "TRACK-123",
        },
    )
    monkeypatch.setattr(permisos, "mapa_permisos", lambda *_args: {})
    monkeypatch.setattr(
        portal.templates, "TemplateResponse",
        lambda *, context, **_kw: SimpleNamespace(status_code=200, context=context),
    )

    respuesta = portal.envios_view(
        SimpleNamespace(), tipo="nacional", paso="despachados",
        buscar=" TRACK-123 ", anio="2026", mes="8", semana="2",
        cliente="WAIMAO",
    )

    assert llamadas == [("WAIMAO", {
        "limite": None, "desde": None, "hasta": None,
    })]
    assert argumentos_vista == [{
        "tipo": "", "paso": "", "pagina": "1", "buscar": "TRACK-123",
    }]
    assert respuesta.context["periodo"]["etiqueta"] == "Todo el historial"
    assert respuesta.context["periodo_query"] == ""


def test_portal_y_admin_comparten_los_tres_filtros_y_resumen():
    portal_html = (RAIZ / "templates/portal/envios.html").read_text()
    admin_html = (RAIZ / "templates/admin/cliente_detail.html").read_text()
    admin_py = (RAIZ / "endpoints/admin.py").read_text()
    solicitudes = (RAIZ / "servicios/solicitudes_guia.py").read_text()
    schema = (RAIZ / "sql/schema.sql").read_text()

    for nombre in ('name="anio"', 'name="mes"', 'name="semana"'):
        assert nombre in portal_html
        assert nombre in admin_html
    assert "requestSubmit()" in portal_html and "requestSubmit()" in admin_html
    assert "Todos los años" in portal_html and "Todos los meses" in portal_html
    assert "Guías emitidas" in portal_html
    assert "Envíos vigentes" in admin_html
    assert "fecha >= %s AND fecha < %s" in admin_py
    assert "idx_envios_cliente_fecha" in schema
    assert "fecha_operacion" in solicitudes
    inicio = solicitudes.index("def periodos_solicitudes_cliente")
    fin = solicitudes.index("def listar_envios_api", inicio)
    assert "label_pdf" not in solicitudes[inicio:fin]
    # El período describe el historial completo; cancelar quita el cargo pero
    # no borra el mes en el que el cliente realizó la operación.
    assert "s.estado <> 'CANCELADO'" not in solicitudes[inicio:fin]
    assert "cargo_cancelado.estado='CANCELADO'" not in solicitudes[inicio:fin]
