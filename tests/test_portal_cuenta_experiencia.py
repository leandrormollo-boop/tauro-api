"""Cuenta del portal: filtros, degradación legible y exportación propia segura."""

from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from starlette.datastructures import Headers

import endpoints.portal_cliente as portal
import servicios.cuenta_corriente as cuenta
import servicios.export_cuenta as exportacion
from servicios import periodo_visual_cuenta as periodo_visual


CLIENTE = "CLIENTE_SESION"
INICIO_WAIMAO = date(2026, 9, 1)
HOY = date(2026, 10, 6)


def _resumen():
    scope = {
        "debe_ars": Decimal("1500.25"), "haber_ars": Decimal("800"),
        "saldo_ars": Decimal("700.25"), "facturado_ars": Decimal("1200"),
        "pendiente_facturacion_ars": Decimal("300.25"),
    }
    return {
        "consolidado": dict(scope), "nacional": dict(scope),
        "internacional": dict(scope), "credito_sin_imputar_ars": Decimal("0"),
        "cargos_sin_clasificar_ars": Decimal("0"),
    }


def _movimientos(items=None, total=None):
    items = items or []
    return {
        "items": items, "total_resultados": len(items) if total is None else total,
        "pagina_actual": 1, "total_paginas": 1,
        "pagina_desde": 1 if items else 0, "pagina_hasta": len(items),
        "paginas_visibles": [1],
    }


def _periodo(inicio=INICIO_WAIMAO):
    return {
        "saldo_anterior_ars": Decimal("400"),
        "cargos_desde_ars": Decimal("600.50"),
        "creditos_desde_ars": Decimal("50.25"),
        "pagos_desde_ars": Decimal("250"),
        "neto_desde_ars": Decimal("300.25"),
        "saldo_total_ars": Decimal("700.25"),
        "redondeo_ars": Decimal("0"),
        "fecha_inicio": inicio.isoformat(),
        "fecha_inicio_visible": inicio.strftime("%d/%m/%Y"),
        "hay_movimientos_anteriores": True,
        "sin_fecha_cantidad": 0,
        "criterio": "Según estados actuales; no es un cierre auditado.",
        "advertencia_sin_fecha": "",
    }


def _experiencia():
    return {
        "pagos": [{"id": 41, "referencia": "PAGO-DEMO"}],
        "pagos_paginacion": {
            "pagina_actual": 1, "total_paginas": 1, "total": 1,
            "pagina_desde": 1, "pagina_hasta": 1,
        },
    }


@pytest.fixture(autouse=True)
def hoy_argentina_fijo(monkeypatch):
    monkeypatch.setattr(periodo_visual, "_hoy_argentina", lambda: HOY)
    monkeypatch.setattr(
        periodo_visual, "inicio_cuenta_cliente",
        lambda cliente: INICIO_WAIMAO if str(cliente).strip().upper() == "WAIMAO" else None,
    )


@pytest.fixture
def cuenta_servicios(monkeypatch):
    """No conexión DB ni servicios reales incluso cuando cambia un handler."""
    llamadas = []
    resumen = _resumen()
    monkeypatch.setattr(portal, "resumen_cuenta_por_ambito", lambda cliente: (
        llamadas.append(("resumen", cliente)) or resumen
    ))
    monkeypatch.setattr(portal, "listar_destinos_pago", lambda cliente: (
        llamadas.append(("destinos", cliente)) or []
    ))
    monkeypatch.setattr(portal, "obtener_experiencia_cuenta", lambda cliente, resumen, **kwargs: (
        llamadas.append(("experiencia", cliente, resumen, kwargs)) or _experiencia()
    ))
    monkeypatch.setattr(portal, "movimientos_cuenta_paginados", lambda *args, **kwargs: (
        llamadas.append(("movimientos", args, kwargs)) or _movimientos()
    ))
    monkeypatch.setattr(portal, "obtener_periodo_cuenta", lambda cliente, inicio: (
        llamadas.append(("periodo", cliente, inicio)) or _periodo(inicio)
    ))
    return llamadas


@pytest.fixture
def capturar_template(monkeypatch):
    monkeypatch.setattr(portal.templates, "TemplateResponse", lambda **kwargs: kwargs)


def test_filtros_llegan_al_servicio_y_conservan_cliente_autenticado(
    cuenta_servicios, capturar_template,
):
    respuesta = portal.cuenta_corriente(
        SimpleNamespace(), ambito="INTERNACIONAL", tipo="CARGOS", pagina="2",
        cliente=CLIENTE, q="DEMO-2409", desde="2026-09-01", hasta="2026-09-15",
    )
    contexto = respuesta["context"]
    consulta = next(c for c in cuenta_servicios if c[0] == "movimientos")
    assert consulta[1][:4] == (CLIENTE, "internacional", "cargos", 2)
    assert consulta[2] == {
        "q": "DEMO-2409", "desde": "2026-09-01", "hasta": "2026-09-15",
    }
    assert contexto["q_filtro"] == "DEMO-2409"
    assert contexto["desde_filtro"] == "2026-09-01"
    assert contexto["hasta_filtro"] == "2026-09-15"
    assert contexto["saldo"]["saldo_pendiente_ars"] == Decimal("700.25")
    assert all(c[1] == CLIENTE for c in cuenta_servicios if c[0] != "movimientos")


def test_sin_filtros_conserva_contrato_posicional_del_servicio(
    cuenta_servicios, capturar_template,
):
    portal.cuenta_corriente(SimpleNamespace(), cliente=CLIENTE)
    consulta = next(c for c in cuenta_servicios if c[0] == "movimientos")
    assert consulta[1] == (CLIENTE, "consolidado", "todos", 1, 6)
    assert consulta[2] == {
        "q": "", "desde": "2026-01-01", "hasta": "2026-12-31",
    }


def test_filtro_parcial_actualiza_movimientos_pagos_y_apertura_sin_panel_global(
    cuenta_servicios, capturar_template, monkeypatch,
):
    def no_necesario(*_args, **_kwargs):
        cuenta_servicios.append(("consulta innecesaria",))
        raise AssertionError("El filtro parcial no debe recalcular toda la cuenta")

    for nombre in ("resumen_cuenta_por_ambito", "listar_destinos_pago"):
        monkeypatch.setattr(portal, nombre, no_necesario)
    respuesta = portal.cuenta_corriente(
        SimpleNamespace(headers=Headers({"X-Tauro-Partial": "cuenta"})),
        ambito="nacional", tipo="pagos", pagina="2", pagina_pagos="3", cliente=CLIENTE,
        q="OP-123", desde="2026-09-01", hasta="2026-09-15",
    )
    assert respuesta["name"] == "portal/cuenta_parcial.html"
    assert respuesta["headers"] == {
        "X-Tauro-Partial": "cuenta", "Cache-Control": "private, no-store",
    }
    assert respuesta["context"]["movimientos"]["total_resultados"] == 0
    assert respuesta["context"]["experiencia"]["pagos"][0]["id"] == 41
    assert respuesta["context"]["periodo_cuenta"]["fecha_inicio"] == "2026-09-01"
    assert not [c for c in cuenta_servicios if c[0] == "consulta innecesaria"]
    consulta = next(c for c in cuenta_servicios if c[0] == "movimientos")
    assert consulta[0] == "movimientos"
    assert consulta[1][:4] == (CLIENTE, "nacional", "pagos", 2)
    assert consulta[2] == {
        "q": "OP-123", "desde": "2026-09-01", "hasta": "2026-09-15",
    }
    experiencia = next(c for c in cuenta_servicios if c[0] == "experiencia")
    assert experiencia[1] == CLIENTE
    assert experiencia[2] == {}
    assert experiencia[3] == {
        "desde": "2026-09-01", "hasta": "2026-09-15", "pagina_pagos": 3,
    }
    assert ("periodo", CLIENTE, date(2026, 9, 1)) in cuenta_servicios
    assert not [c for c in cuenta_servicios if c[0] in {"resumen", "destinos"}]


def test_navegacion_preserva_periodo_y_escapa_busqueda(
    cuenta_servicios, capturar_template,
):
    respuesta = portal.cuenta_corriente(
        SimpleNamespace(), ambito="nacional", cliente=CLIENTE,
        q="A&B + ropa", desde="2026-09-01", hasta="2026-09-15", pagina_pagos="4",
    )
    url = respuesta["context"]["cuenta_url"](pagina=3, tipo="pagos")
    partes = urlsplit(url)
    assert partes.path == "/portal/cuenta"
    assert parse_qs(partes.query) == {
        "ambito": ["nacional"], "tipo": ["pagos"], "pagina": ["3"],
        "q": ["A&B + ropa"], "desde": ["2026-09-01"], "hasta": ["2026-09-15"],
        "ventana": ["rango"], "pagina_pagos": ["1"],
    }


def test_filtro_mensual_resume_el_mes_y_exporta_el_mismo_rango(
    cuenta_servicios, capturar_template, monkeypatch,
):
    resumen_mes = {
        "clave": "2026-05", "desde": "2026-05-01", "hasta": "2026-05-31",
        "label": "Mayo 2026", "ambito": "internacional",
        "envios_realizados": 47, "fletes_ars": Decimal("1000"),
        "tax_cantidad": 8, "tax_ars": Decimal("100"),
        "retornos": 0, "retornos_ars": Decimal("0"),
        "diferencias": 2, "diferencias_ars": Decimal("50"),
        "otros": 0, "otros_ars": Decimal("0"),
        "total_mes_ars": Decimal("1150"),
    }
    llamadas = []
    monkeypatch.setattr(portal, "resumen_mensual_cuenta", lambda *args: (
        llamadas.append(args) or resumen_mes
    ))

    respuesta = portal.cuenta_corriente(
        SimpleNamespace(), cliente=CLIENTE, ambito="internacional", periodo="2026-05",
    )
    contexto = respuesta["context"]
    consulta = next(c for c in cuenta_servicios if c[0] == "movimientos")

    assert consulta[2] == {"q": "", "desde": "2026-05-01", "hasta": "2026-05-31"}
    assert llamadas == [(CLIENTE, "2026-05", "internacional")]
    assert contexto["periodo_filtro"] == "2026-05"
    assert contexto["periodo_filtro_label"] == "Mayo 2026"
    assert contexto["resumen_mensual"] == resumen_mes
    assert parse_qs(urlsplit(contexto["cuenta_url"](pagina=2)).query) == {
        "ambito": ["internacional"], "tipo": ["todos"],
        "periodo": ["2026-05"], "ventana": ["mes"],
        "pagina": ["2"], "pagina_pagos": ["1"],
    }
    assert parse_qs(urlsplit(contexto["cuenta_url"](exportar=True)).query) == {
        "ambito": ["internacional"], "tipo": ["todos"],
        "periodo": ["2026-05"], "ventana": ["mes"],
    }


def test_exportacion_acepta_el_mismo_periodo_mensual_del_portal(
    cuenta_servicios, monkeypatch,
):
    recibidos = []
    monkeypatch.setattr(portal, "generar_excel_cuenta", lambda *args, **kwargs: (
        recibidos.append((args, kwargs)) or b"xlsx"
    ))
    respuesta = portal.exportar_cuenta(
        cliente=CLIENTE, ambito="internacional", periodo="2026-05",
    )

    assert respuesta.body == b"xlsx"
    assert recibidos == [((CLIENTE, "internacional", "todos"), {
        "q": "", "desde": "2026-05-01", "hasta": "2026-05-31",
    })]


def test_panel_no_disponible_no_simula_saldo_cero(
    cuenta_servicios, capturar_template, monkeypatch,
):
    def falla(*_args, **_kwargs):
        raise RuntimeError("detalle interno que no debe llegar al cliente")

    monkeypatch.setattr(portal, "obtener_experiencia_cuenta", falla)
    respuesta = portal.cuenta_corriente(SimpleNamespace(), cliente=CLIENTE)
    contexto = respuesta["context"]
    assert contexto["experiencia"] is None
    assert contexto["experiencia_error"]
    assert "detalle interno" not in str(contexto["experiencia_error"])
    assert contexto["saldo"]["saldo_pendiente_ars"] == Decimal("700.25")


@pytest.mark.parametrize("seleccion", ["F:99", "E:999", "https://example.invalid", "F:20"])
def test_preseleccion_solo_documento_propio_con_importe_disponible(
    cuenta_servicios, capturar_template, monkeypatch, seleccion,
):
    monkeypatch.setattr(portal, "listar_destinos_pago", lambda _cliente: [
        {"clave": "F:99", "disponible": Decimal("125.50")},
        {"clave": "F:20", "disponible": Decimal("0")},
    ])
    respuesta = portal.cuenta_corriente(SimpleNamespace(), cliente=CLIENTE, pagar=seleccion)
    contexto = respuesta["context"]
    assert contexto["pago_preseleccionado"] == ("F:99" if seleccion == "F:99" else "")
    assert contexto["pago_monto_preseleccionado"] == ("125.50" if seleccion == "F:99" else "")


def _cliente_http(cliente_sesion=CLIENTE):
    app = FastAPI()
    app.include_router(portal.router)
    app.dependency_overrides[portal.cliente_actual] = lambda: cliente_sesion
    return TestClient(app)


def test_excel_respeta_sesion_y_filtros_y_no_ejecuta_texto_como_formula(
    cuenta_servicios, monkeypatch,
):
    recibidos = []
    item = {
        "fecha": "15/09/2026", "tipo": "FC", "ambito": "INTERNACIONAL",
        "concepto": '=HYPERLINK("https://example.invalid","cargo")',
        "referencia": "+SUM(1,2)", "numero_guia": "@tracking",
        "numero_factura": "-FACTURA", "destinatario": "Cliente de ejemplo",
        "debe_ars": Decimal("1200.50"), "haber_ars": Decimal("0"),
        "monto_ars": Decimal("1200.50"), "estado": "ACTIVO",
        "valor_envio_ars": Decimal("1200.50"),
    }

    def movimientos(*args, **kwargs):
        recibidos.append((args, kwargs))
        return _movimientos([item])

    monkeypatch.setattr(exportacion, "movimientos_cuenta_paginados", movimientos)
    with _cliente_http() as cliente:
        respuesta = cliente.get(
            "/portal/cuenta/exportar.xlsx",
            params={
                "cliente": "CLIENTE_AJENO", "cliente_id": "CLIENTE_AJENO",
                "ambito": "internacional", "tipo": "cargos", "q": "ropa",
                "desde": "2026-09-01", "hasta": "2026-09-15",
            },
        )
    assert respuesta.status_code == 200
    assert "spreadsheetml" in respuesta.headers["content-type"]
    assert "no-store" in respuesta.headers.get("cache-control", "")
    assert recibidos
    assert all(args[0] == CLIENTE for args, _ in recibidos)
    assert all(args[1:3] == ("internacional", "cargos") for args, _ in recibidos)
    assert all(kwargs == {
        "q": "ropa", "desde": "2026-09-01", "hasta": "2026-09-15", "exportar": True,
    } for _, kwargs in recibidos)
    libro = load_workbook(BytesIO(respuesta.content), data_only=False)
    celdas = [celda for hoja in libro for fila in hoja for celda in fila]
    assert all(celda.data_type != "f" for celda in celdas)
    texto = {celda.value for celda in celdas if isinstance(celda.value, str)}
    assert item["concepto"] in texto
    assert item["numero_guia"] in texto
    assert any(celda.value == 1200.5 and celda.data_type == "n" for celda in celdas)


@pytest.mark.parametrize("contados,recibidos", [(10001, 0), (10000, 10001)])
def test_excel_no_trunca_silenciosamente_mas_de_diez_mil(
    cuenta_servicios, monkeypatch, contados, recibidos,
):
    consultas = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, query, params):
            consultas.append((query, params))

        def fetchone(self):
            return {"total": contados}

        def fetchall(self):
            return [{} for _ in range(recibidos)]

    @contextmanager
    def conexion():
        yield SimpleNamespace(cursor=Cursor)

    monkeypatch.setattr(cuenta, "get_conn", conexion)
    with _cliente_http() as cliente:
        respuesta = cliente.get("/portal/cuenta/exportar.xlsx", follow_redirects=False)
    assert respuesta.status_code in {303, 400, 413, 422}
    assert "spreadsheetml" not in respuesta.headers.get("content-type", "")
    assert respuesta.content or "error" in respuesta.headers.get("location", "")
    assert "10.000" in respuesta.text
    assert len(consultas) == (1 if contados > 10000 else 2)
    if recibidos:
        assert consultas[-1][1][-2:] == (10001, 0)


def test_excel_periodo_invertido_no_devuelve_toda_la_cuenta(
    cuenta_servicios, monkeypatch,
):
    recibidos = []
    monkeypatch.setattr(exportacion, "movimientos_cuenta_paginados", lambda *args, **kwargs: (
        recibidos.append((args, kwargs)) or _movimientos()
    ))
    with _cliente_http() as cliente:
        respuesta = cliente.get(
            "/portal/cuenta/exportar.xlsx?desde=2026-09-15&hasta=2026-09-01",
            follow_redirects=False,
        )
    assert respuesta.status_code in {303, 400, 422}
    assert recibidos == []


@pytest.mark.parametrize("cliente_sesion", [CLIENTE, "OTRO_CLIENTE", "WAIMAO"])
def test_sin_ventana_todos_los_clientes_usan_el_anio_calendario(
    cuenta_servicios, capturar_template, cliente_sesion,
):
    respuesta = portal.cuenta_corriente(SimpleNamespace(), cliente=cliente_sesion)
    contexto = respuesta["context"]
    consulta = next(c for c in cuenta_servicios if c[0] == "movimientos")

    assert consulta[1][0] == cliente_sesion
    assert consulta[2] == {
        "q": "", "desde": "2026-01-01", "hasta": "2026-12-31",
    }
    assert contexto["ventana_filtro"] == "anio"
    assert contexto["ventana_label"] == "Este año · 2026"
    assert contexto["desde_filtro"] == "2026-01-01"
    assert contexto["hasta_filtro"] == "2026-12-31"
    assert ("periodo", cliente_sesion, date(2026, 1, 1)) in cuenta_servicios


def test_ventana_septiembre_es_explicita_y_conserva_saldo_anterior(
    cuenta_servicios, capturar_template,
):
    respuesta = portal.cuenta_corriente(
        SimpleNamespace(), cliente="WAIMAO", ventana="septiembre",
    )
    contexto = respuesta["context"]
    consulta = next(c for c in cuenta_servicios if c[0] == "movimientos")

    assert consulta[2] == {
        "q": "", "desde": "2026-09-01", "hasta": "2026-12-31",
    }
    assert contexto["ventana_filtro"] == "septiembre"
    assert contexto["periodo_cuenta"] == _periodo(INICIO_WAIMAO)
    assert contexto["periodo_error"] == ""
    assert contexto["saldo"]["saldo_pendiente_ars"] == Decimal("700.25")
    assert parse_qs(urlsplit(contexto["cuenta_url"](pagina=2)).query) == {
        "ambito": ["consolidado"], "tipo": ["todos"],
        "ventana": ["septiembre"], "pagina": ["2"], "pagina_pagos": ["1"],
    }


def test_mes_anterior_a_septiembre_es_valido_y_no_se_recorta(
    cuenta_servicios, capturar_template, monkeypatch,
):
    resumen_mes = {"clave": "2026-08", "label": "Agosto 2026"}
    resumenes = []
    monkeypatch.setattr(portal, "resumen_mensual_cuenta", lambda *args: (
        resumenes.append(args) or resumen_mes
    ))

    respuesta = portal.cuenta_corriente(
        SimpleNamespace(), cliente="WAIMAO", ventana="mes", periodo="2026-08",
    )
    contexto = respuesta["context"]
    consulta = next(c for c in cuenta_servicios if c[0] == "movimientos")

    assert consulta[2] == {
        "q": "", "desde": "2026-08-01", "hasta": "2026-08-31",
    }
    assert contexto["periodo_filtro"] == "2026-08"
    assert contexto["periodo_filtro_label"] == "Agosto 2026"
    assert contexto["filtros_error"] == ""
    assert contexto["resumen_mensual"] == resumen_mes
    assert resumenes == [("WAIMAO", "2026-08", "consolidado")]


@pytest.mark.parametrize(
    "ventana,periodo,desde,hasta,esperado",
    [
        ("anio", "2025-02", "fecha-invalida", "fecha-invalida",
         ("2026-01-01", "2026-12-31")),
        ("mes", "2025-02", "fecha-invalida", "fecha-invalida",
         ("2025-02-01", "2025-02-28")),
        ("rango", "2025-02", "2025-03-04", "2025-03-09",
         ("2025-03-04", "2025-03-09")),
        ("septiembre", "2025-02", "fecha-invalida", "fecha-invalida",
         ("2026-09-01", "2026-12-31")),
    ],
)
def test_ventana_explicita_manda_sobre_parametros_inactivos(
    cuenta_servicios, capturar_template, monkeypatch,
    ventana, periodo, desde, hasta, esperado,
):
    monkeypatch.setattr(portal, "resumen_mensual_cuenta", lambda *_args: {})
    portal.cuenta_corriente(
        SimpleNamespace(), cliente="WAIMAO", ventana=ventana,
        periodo=periodo, desde=desde, hasta=hasta,
    )
    consulta = next(c for c in cuenta_servicios if c[0] == "movimientos")
    assert (consulta[2]["desde"], consulta[2]["hasta"]) == esperado


@pytest.mark.parametrize("faltante", [
    {"desde": "2026-08-01"}, {"hasta": "2026-08-31"},
])
def test_rango_incompleto_recupera_la_pantalla_con_anio_y_error(
    cuenta_servicios, capturar_template, faltante,
):
    respuesta = portal.cuenta_corriente(
        SimpleNamespace(), cliente="WAIMAO", ventana="rango", pagina="8",
        pagina_pagos="9", q="NO-DEBE-PASAR", **faltante,
    )
    contexto = respuesta["context"]
    consulta = next(c for c in cuenta_servicios if c[0] == "movimientos")

    assert consulta[1][3] == 1
    assert consulta[2] == {
        "q": "", "desde": "2026-01-01", "hasta": "2026-12-31",
    }
    assert contexto["ventana_filtro"] == "anio"
    assert contexto["filtros_error"]
    assert "Desde y Hasta" in contexto["filtros_error"]
    assert "pagina_pagos=1" in contexto["cuenta_url"]()


def test_rango_anterior_a_septiembre_se_conserva_en_pantalla_y_url(
    cuenta_servicios, capturar_template,
):
    respuesta = portal.cuenta_corriente(
        SimpleNamespace(), cliente="WAIMAO", ventana="rango",
        q="DEMO-2408", desde="2026-08-01", hasta="2026-08-31",
    )
    contexto = respuesta["context"]
    consulta = next(c for c in cuenta_servicios if c[0] == "movimientos")

    assert consulta[2] == {
        "q": "DEMO-2408", "desde": "2026-08-01", "hasta": "2026-08-31",
    }
    assert parse_qs(urlsplit(contexto["cuenta_url"](pagina=2)).query) == {
        "ambito": ["consolidado"], "tipo": ["todos"], "q": ["DEMO-2408"],
        "ventana": ["rango"], "desde": ["2026-08-01"],
        "hasta": ["2026-08-31"], "pagina": ["2"], "pagina_pagos": ["1"],
    }
    assert parse_qs(urlsplit(contexto["cuenta_url"](exportar=True)).query) == {
        "ambito": ["consolidado"], "tipo": ["todos"], "q": ["DEMO-2408"],
        "ventana": ["rango"], "desde": ["2026-08-01"], "hasta": ["2026-08-31"],
    }


def test_filtro_invalido_recupera_el_anio_sin_perder_seguridad(
    cuenta_servicios, capturar_template,
):
    respuesta = portal.cuenta_corriente(
        SimpleNamespace(), cliente="WAIMAO", pagina="8", pagina_pagos="7",
        q="x" * 121,
    )
    contexto = respuesta["context"]
    consulta = next(c for c in cuenta_servicios if c[0] == "movimientos")

    assert consulta[1][3] == 1
    assert consulta[2] == {
        "q": "", "desde": "2026-01-01", "hasta": "2026-12-31",
    }
    assert contexto["filtros_error"]
    assert contexto["q_filtro"] == ""
    assert "pagina_pagos=1" in contexto["cuenta_url"]()


@pytest.mark.parametrize(
    "params,esperado",
    [
        ({}, ("2026-01-01", "2026-12-31")),
        ({"ventana": "septiembre"}, ("2026-09-01", "2026-12-31")),
        ({"ventana": "mes", "periodo": "2026-08"}, ("2026-08-01", "2026-08-31")),
        ({"ventana": "rango", "desde": "2026-02-03", "hasta": "2026-02-07"},
         ("2026-02-03", "2026-02-07")),
    ],
)
def test_excel_usa_el_mismo_normalizador_sin_recortar_fechas(
    cuenta_servicios, monkeypatch, params, esperado,
):
    recibidos = []
    monkeypatch.setattr(portal, "generar_excel_cuenta", lambda *args, **kwargs: (
        recibidos.append((args, kwargs)) or b"xlsx"
    ))

    respuesta = portal.exportar_cuenta(cliente="WAIMAO", **params)

    assert respuesta.body == b"xlsx"
    assert recibidos == [(("WAIMAO", "consolidado", "todos"), {
        "q": "", "desde": esperado[0], "hasta": esperado[1],
    })]


@pytest.mark.parametrize("faltante", [
    {"desde": "2026-08-01"}, {"hasta": "2026-08-31"},
])
def test_excel_rechaza_rango_incompleto_sin_generar_archivo(
    cuenta_servicios, monkeypatch, faltante,
):
    recibidos = []
    monkeypatch.setattr(portal, "generar_excel_cuenta", lambda *args, **kwargs: (
        recibidos.append((args, kwargs)) or b"xlsx"
    ))

    with pytest.raises(portal.HTTPException) as error:
        portal.exportar_cuenta(cliente="WAIMAO", ventana="rango", **faltante)

    assert error.value.status_code == 400
    assert "Desde y Hasta" in error.value.detail
    assert recibidos == []


@pytest.mark.parametrize("problema", ["lectura", "saldo_distinto"])
def test_periodo_no_disponible_no_oculta_saldo_ni_simula_cero(
    cuenta_servicios, capturar_template, monkeypatch, problema,
):
    def periodo_fallido(_cliente, inicio):
        if problema == "lectura":
            raise RuntimeError("detalle interno de base")
        return {**_periodo(inicio), "saldo_total_ars": Decimal("999999")}

    monkeypatch.setattr(portal, "obtener_periodo_cuenta", periodo_fallido)
    respuesta = portal.cuenta_corriente(SimpleNamespace(), cliente="WAIMAO")
    contexto = respuesta["context"]

    assert contexto["periodo_cuenta"] is None
    assert contexto["periodo_error"]
    assert "detalle interno" not in contexto["periodo_error"]
    assert contexto["saldo"]["saldo_pendiente_ars"] == Decimal("700.25")
    assert contexto["desde_filtro"] == "2026-01-01"
