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


CLIENTE = "CLIENTE_SESION"
INICIO_WAIMAO = date(2026, 9, 1)


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


def _periodo():
    return {
        "saldo_anterior_ars": Decimal("400"),
        "cargos_desde_ars": Decimal("600.50"),
        "creditos_desde_ars": Decimal("50.25"),
        "pagos_desde_ars": Decimal("250"),
        "neto_desde_ars": Decimal("300.25"),
        "saldo_total_ars": Decimal("700.25"),
        "redondeo_ars": Decimal("0"),
        "fecha_inicio": "2026-09-01",
        "fecha_inicio_visible": "01/09/2026",
        "sin_fecha_cantidad": 0,
        "criterio": "Según estados actuales; no es un cierre auditado.",
        "advertencia_sin_fecha": "",
    }


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
    monkeypatch.setattr(portal, "obtener_experiencia_cuenta", lambda cliente, resumen: (
        llamadas.append(("experiencia", cliente)) or {"dato": "panel simulado"}
    ))
    monkeypatch.setattr(portal, "movimientos_cuenta_paginados", lambda *args, **kwargs: (
        llamadas.append(("movimientos", args, kwargs)) or _movimientos()
    ))
    monkeypatch.setattr(portal, "obtener_periodo_cuenta", lambda cliente, inicio: (
        llamadas.append(("periodo", cliente, inicio)) or _periodo()
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
    assert consulta[2] == {}


def test_filtro_parcial_solo_consulta_movimientos_de_la_sesion(
    cuenta_servicios, capturar_template, monkeypatch,
):
    def no_necesario(*_args, **_kwargs):
        cuenta_servicios.append(("consulta innecesaria",))
        raise AssertionError("El filtro parcial no debe recalcular toda la cuenta")

    for nombre in ("resumen_cuenta_por_ambito", "listar_destinos_pago", "obtener_experiencia_cuenta"):
        monkeypatch.setattr(portal, nombre, no_necesario)
    respuesta = portal.cuenta_corriente(
        SimpleNamespace(headers=Headers({"X-Tauro-Partial": "cuenta"})),
        ambito="nacional", tipo="pagos", pagina="2", cliente=CLIENTE,
        q="OP-123", desde="2026-09-01", hasta="2026-09-15",
    )
    assert respuesta["context"]["movimientos"]["total_resultados"] == 0
    assert len(cuenta_servicios) == 1
    consulta = cuenta_servicios[0]
    assert consulta[0] == "movimientos"
    assert consulta[1][:4] == (CLIENTE, "nacional", "pagos", 2)
    assert consulta[2] == {
        "q": "OP-123", "desde": "2026-09-01", "hasta": "2026-09-15",
    }


def test_navegacion_preserva_periodo_y_escapa_busqueda(
    cuenta_servicios, capturar_template,
):
    respuesta = portal.cuenta_corriente(
        SimpleNamespace(), ambito="nacional", cliente=CLIENTE,
        q="A&B + ropa", desde="2026-09-01", hasta="2026-09-15",
    )
    url = respuesta["context"]["cuenta_url"](pagina=3, tipo="pagos")
    partes = urlsplit(url)
    assert partes.path == "/portal/cuenta"
    assert parse_qs(partes.query) == {
        "ambito": ["nacional"], "tipo": ["pagos"], "pagina": ["3"],
        "q": ["A&B + ropa"], "desde": ["2026-09-01"], "hasta": ["2026-09-15"],
    }


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


def test_waimao_inicia_en_septiembre_y_conserva_saldo_anterior(
    cuenta_servicios, capturar_template,
):
    respuesta = portal.cuenta_corriente(SimpleNamespace(), cliente="WAIMAO")
    contexto = respuesta["context"]
    consulta = next(c for c in cuenta_servicios if c[0] == "movimientos")
    assert consulta[1][:4] == ("WAIMAO", "consolidado", "todos", 1)
    assert consulta[2] == {"q": "", "desde": "2026-09-01", "hasta": ""}
    assert ("periodo", "WAIMAO", INICIO_WAIMAO) in cuenta_servicios
    assert contexto["inicio_cuenta"] == INICIO_WAIMAO
    assert contexto["desde_filtro"] == "2026-09-01"
    assert contexto["periodo_cuenta"] == _periodo()
    assert contexto["periodo_error"] == ""
    assert contexto["saldo"]["saldo_pendiente_ars"] == Decimal("700.25")
    periodo = contexto["periodo_cuenta"]
    assert periodo["saldo_anterior_ars"] + periodo["neto_desde_ars"] == periodo["saldo_total_ars"]


@pytest.mark.parametrize("cliente_sesion", [CLIENTE, "OTRO_CLIENTE", "WAIMAO_2"])
def test_corte_waimao_no_recorta_otros_clientes(
    cuenta_servicios, capturar_template, cliente_sesion,
):
    respuesta = portal.cuenta_corriente(SimpleNamespace(), cliente=cliente_sesion)
    contexto = respuesta["context"]
    consulta = next(c for c in cuenta_servicios if c[0] == "movimientos")
    assert consulta[1][0] == cliente_sesion
    assert consulta[2] == {}
    assert contexto["inicio_cuenta"] is None
    assert contexto["periodo_cuenta"] is None
    assert contexto["periodo_error"] == ""
    assert contexto["desde_filtro"] == ""
    assert not any(c[0] == "periodo" for c in cuenta_servicios)


@pytest.mark.parametrize("desde,esperado", [
    ("", "2026-09-01"), ("2026-08-01", "2026-09-01"),
    ("2026-09-01", "2026-09-01"), ("2026-09-10", "2026-09-10"),
])
def test_busqueda_waimao_respeta_el_corte_sin_perder_referencia(
    cuenta_servicios, capturar_template, desde, esperado,
):
    respuesta = portal.cuenta_corriente(
        SimpleNamespace(), cliente="WAIMAO", q="DEMO-2409", desde=desde,
    )
    contexto = respuesta["context"]
    consulta = next(c for c in cuenta_servicios if c[0] == "movimientos")
    assert consulta[2] == {"q": "DEMO-2409", "desde": esperado, "hasta": ""}
    assert contexto["q_filtro"] == "DEMO-2409"
    assert contexto["desde_filtro"] == esperado
    assert parse_qs(urlsplit(contexto["cuenta_url"](pagina=2)).query)["desde"] == [esperado]
    assert parse_qs(urlsplit(contexto["cuenta_url"](exportar=True)).query)["desde"] == [esperado]


@pytest.mark.parametrize("filtros", [
    {"q": "x" * 121}, {"desde": "fecha-invalida"}, {"hasta": "2026-08-31"},
])
def test_filtros_invalidos_waimao_no_quitan_corte_al_recuperar_pantalla(
    cuenta_servicios, capturar_template, filtros,
):
    respuesta = portal.cuenta_corriente(
        SimpleNamespace(), cliente="WAIMAO", pagina="8", **filtros,
    )
    contexto = respuesta["context"]
    consulta = next(c for c in cuenta_servicios if c[0] == "movimientos")
    assert consulta[1][3] == 1
    assert consulta[2] == {"q": "", "desde": "2026-09-01", "hasta": ""}
    assert contexto["desde_filtro"] == "2026-09-01"
    assert contexto["filtros_error"]
    assert contexto["inicio_cuenta"] == INICIO_WAIMAO


def test_fragmento_waimao_aplica_corte_sin_leer_periodo_ni_paneles(
    cuenta_servicios, capturar_template, monkeypatch,
):
    def no_necesario(*_args, **_kwargs):
        cuenta_servicios.append(("consulta innecesaria",))
        raise AssertionError("El filtro sólo necesita el historial")

    for nombre in (
        "resumen_cuenta_por_ambito", "listar_destinos_pago",
        "obtener_experiencia_cuenta", "obtener_periodo_cuenta",
    ):
        monkeypatch.setattr(portal, nombre, no_necesario)
    respuesta = portal.cuenta_corriente(
        SimpleNamespace(headers=Headers({"X-Tauro-Partial": "cuenta"})),
        cliente="WAIMAO", ambito="internacional", tipo="pagos",
        desde="2026-08-01", q="FC 0001-00000124",
    )
    assert respuesta["headers"]["X-Tauro-Partial"] == "cuenta"
    assert respuesta["context"]["inicio_cuenta"] == INICIO_WAIMAO
    assert cuenta_servicios == [(
        "movimientos", ("WAIMAO", "internacional", "pagos", 1, 6),
        {"q": "FC 0001-00000124", "desde": "2026-09-01", "hasta": ""},
    )]


@pytest.mark.parametrize("filtros", [
    {}, {"desde": "", "hasta": "", "q": ""},
    {"desde": "2026-01-01", "cliente": CLIENTE, "cliente_id": CLIENTE},
])
def test_excel_waimao_no_elude_inicio_al_limpiar_o_manipular_filtros(
    cuenta_servicios, monkeypatch, filtros,
):
    recibidos = []

    def movimientos(*args, **kwargs):
        recibidos.append((args, kwargs))
        assert args[0] == "WAIMAO"
        assert kwargs["desde"] == "2026-09-01"
        assert kwargs["exportar"] is True
        return _movimientos([{
            "fecha_iso": "2026-09-15", "tipo": "FC", "ambito": "INTERNACIONAL",
            "concepto": "Cargo de septiembre", "debe_ars": Decimal("100"),
            "haber_ars": Decimal("0"), "monto_ars": Decimal("100"), "estado": "ACTIVO",
        }])

    monkeypatch.setattr(exportacion, "movimientos_cuenta_paginados", movimientos)
    with _cliente_http("WAIMAO") as cliente:
        respuesta = cliente.get("/portal/cuenta/exportar.xlsx", params=filtros)
    assert respuesta.status_code == 200
    assert len(recibidos) == 1
    libro = load_workbook(BytesIO(respuesta.content))
    consulta = {fila[0].value: fila[1].value for fila in libro["Consulta"]}
    assert consulta["Cliente"] == "WAIMAO"
    assert consulta["Desde"] == "2026-09-01"
    filas = list(libro["Movimientos"].iter_rows(min_row=2, values_only=True))
    assert len(filas) == 1
    assert filas[0][0].date() >= INICIO_WAIMAO


def test_excel_waimao_rechaza_hasta_anterior_al_inicio_sin_consultar_movimientos(
    cuenta_servicios, monkeypatch,
):
    recibidos = []
    monkeypatch.setattr(exportacion, "movimientos_cuenta_paginados", lambda *args, **kwargs: (
        recibidos.append((args, kwargs)) or _movimientos()
    ))
    with _cliente_http("WAIMAO") as cliente:
        respuesta = cliente.get("/portal/cuenta/exportar.xlsx?hasta=2026-08-31")
    assert respuesta.status_code == 400
    assert "01/09/2026" in respuesta.text
    assert recibidos == []
    assert "spreadsheetml" not in respuesta.headers.get("content-type", "")


@pytest.mark.parametrize("problema", ["lectura", "saldo_distinto"])
def test_periodo_no_disponible_no_oculta_saldo_anterior_ni_simula_cero(
    cuenta_servicios, capturar_template, monkeypatch, problema,
):
    def periodo_fallido(*_args):
        if problema == "lectura":
            raise RuntimeError("detalle interno de base")
        return {**_periodo(), "saldo_total_ars": Decimal("999999")}

    monkeypatch.setattr(portal, "obtener_periodo_cuenta", periodo_fallido)
    respuesta = portal.cuenta_corriente(SimpleNamespace(), cliente="WAIMAO")
    contexto = respuesta["context"]
    assert contexto["inicio_cuenta"] == INICIO_WAIMAO
    assert contexto["periodo_cuenta"] is None
    assert contexto["periodo_error"]
    assert "detalle interno" not in contexto["periodo_error"]
    assert contexto["saldo"]["saldo_pendiente_ars"] == Decimal("700.25")
    assert contexto["desde_filtro"] == "2026-09-01"
