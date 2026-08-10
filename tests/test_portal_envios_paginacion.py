"""Filtros y páginas del historial de envíos del cliente."""

from pathlib import Path

from servicios.panel_cliente import preparar_historial_envios


RAIZ = Path(__file__).resolve().parent.parent


def _envio(numero: int, courier: str, estado: str) -> dict:
    return {"id": numero, "courier": courier, "estado": estado}


def _historial() -> list[dict]:
    return [
        _envio(1, "FEDEX", "SOLICITADO"),
        _envio(2, "ENVIA", "SOLICITADO"),
        _envio(3, "DHL", "GUIA_LISTA"),
        _envio(4, "ANDREANI", "GUIA_LISTA"),
        _envio(5, "UPS", "DESPACHADO"),
        _envio(6, "ENVIA", "ENTREGADO"),
        _envio(7, "FEDEX", "CANCELADO"),
    ]


def test_historial_se_divide_en_paginas_sin_perder_filas():
    primera = preparar_historial_envios(_historial(), pagina=1)
    segunda = preparar_historial_envios(_historial(), pagina=2)
    tercera = preparar_historial_envios(_historial(), pagina=3)

    assert [s["id"] for s in primera["solicitudes"]] == [1, 2, 3]
    assert [s["id"] for s in segunda["solicitudes"]] == [4, 5, 6]
    assert [s["id"] for s in tercera["solicitudes"]] == [7]
    assert primera["total_resultados"] == 7
    assert primera["total_paginas"] == 3
    assert (segunda["pagina_desde"], segunda["pagina_hasta"]) == (4, 6)


def test_filtros_todos_internacionales_y_nacionales_son_distintos():
    todos = preparar_historial_envios(_historial(), por_pagina=20)
    internacionales = preparar_historial_envios(
        _historial(), tipo="internacional", por_pagina=20
    )
    nacionales = preparar_historial_envios(
        _historial(), tipo="nacional", por_pagina=20
    )

    assert [s["id"] for s in todos["solicitudes"]] == [1, 2, 3, 4, 5, 6, 7]
    assert [s["id"] for s in internacionales["solicitudes"]] == [1, 3, 5, 7]
    assert [s["id"] for s in nacionales["solicitudes"]] == [2, 4, 6]


def test_tipo_y_estado_se_combinan_antes_de_paginar():
    vista = preparar_historial_envios(
        _historial(), tipo="nacional", paso="guia_lista", pagina=1
    )

    assert [s["id"] for s in vista["solicitudes"]] == [4]
    assert vista["total_sin_filtrar"] == 3
    assert vista["total_resultados"] == 1
    assert next(c for c in vista["chips"] if c["clave"] == "guia_lista")["cantidad"] == 1


def test_pagina_invalida_o_fuera_de_rango_se_normaliza():
    invalida = preparar_historial_envios(_historial(), pagina="texto")
    negativa = preparar_historial_envios(_historial(), pagina=-5)
    excesiva = preparar_historial_envios(_historial(), pagina=99)

    assert invalida["pagina_actual"] == 1
    assert negativa["pagina_actual"] == 1
    assert excesiva["pagina_actual"] == 3
    assert [s["id"] for s in excesiva["solicitudes"]] == [7]


def test_filtro_sin_resultados_no_se_confunde_con_cliente_sin_historial():
    vista = preparar_historial_envios(
        [_envio(1, "FEDEX", "GUIA_LISTA")], tipo="nacional"
    )

    assert vista["tiene_historial"] is True
    assert vista["solicitudes"] == []
    assert vista["total_resultados"] == 0
    assert vista["pagina_actual"] == 1


def test_template_preserva_filtros_en_paginacion_y_reinicia_al_filtrar():
    html = (RAIZ / "templates" / "portal" / "envios.html").read_text(encoding="utf-8")

    assert "pagina={{ numero }}" in html
    assert "&tipo={{ tipo_filtro }}" in html
    assert "&paso={{ paso_filtro }}" in html
    assert "Mostrando {{ pagina_desde }}–{{ pagina_hasta }}" in html
    assert "cotizado en esta página" in html
    assert "{% if tiene_historial %}" in html
    assert "No hay envíos que coincidan con estos filtros" in html
    # Los links de tipo y de paso no incluyen `pagina`: cambiar un filtro
    # siempre vuelve a la primera hoja.
    assert '/portal/envios?tipo=internacional{{ con_paso }}' in html
    assert '/portal/envios?paso={{ c.clave }}{{ con_tipo }}' in html


def test_endpoint_real_usa_el_contrato_paginado_de_historial():
    endpoint = (RAIZ / "endpoints" / "portal_cliente.py").read_text(encoding="utf-8")

    assert "preparar_historial_envios(historial, tipo, paso, pagina)" in endpoint
    assert 'pagina: str = "1"' in endpoint
