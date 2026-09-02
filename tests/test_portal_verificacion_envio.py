from pathlib import Path

from starlette.requests import Request

import endpoints.portal_cliente as portal
import servicios.recolecciones as recolecciones


ROOT = Path(__file__).resolve().parents[1]


def _request(path="/portal/envios/81/verificacion"):
    return Request({
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": b"",
        "headers": [],
        "scheme": "https",
        "server": ("taurosolutions.ar", 443),
        "client": ("127.0.0.1", 1234),
    })


def test_listado_abre_verificacion_en_dialog_sin_cambiar_de_pagina():
    html = (ROOT / "templates" / "portal" / "envios.html").read_text(encoding="utf-8")
    assert "data-verificar-envio" in html
    assert 'id="shipment-verification-dialog"' in html
    assert "dialog.showModal()" in html
    assert "fetch(button.dataset.verificarUrl" in html
    assert "data-check-next" in html
    assert "window.location.href" in html  # fallback sólo para navegadores sin dialog


def test_boton_verificar_se_muestra_unicamente_despues_de_emitir():
    html = (ROOT / "templates" / "portal" / "envios.html").read_text(encoding="utf-8")
    inicio = html.index("data-verificar-envio")
    condicion = html[html.rfind("{% if", 0, inicio):inicio]
    assert "s.tracking or s.tiene_label or s.guia_url" in condicion


def test_fragmento_tiene_las_cinco_paginas_y_no_expone_costos_internos():
    html = (ROOT / "templates" / "portal" / "_envio_verificacion.html").read_text(
        encoding="utf-8"
    )
    for pagina in range(5):
        assert f'data-check-page="{pagina}"' in html
    for titulo in ("Datos del remitente", "Datos del destinatario", "Datos de las cajas",
                   "Contenido declarado", "Recolección programada"):
        assert titulo in html
    assert "precio_tauro_ars" not in html
    assert "precio_tauro_usd" not in html
    assert "costo_courier" not in html
    assert "margen" not in html.lower()


def test_endpoint_verifica_dueno_antes_de_consultar_recoleccion(monkeypatch):
    consulto_retiro = False

    def retiro(*_args):
        nonlocal consulto_retiro
        consulto_retiro = True

    monkeypatch.setattr(portal, "obtener_solicitud_de_cliente", lambda *_: None)
    monkeypatch.setattr(recolecciones, "obtener_de_solicitud", retiro)

    respuesta = portal.envio_verificacion(_request(), 81, cliente="WAIMAO")

    assert respuesta.status_code == 404
    assert not consulto_retiro
    assert respuesta.headers["cache-control"] == "private, no-store"


def test_endpoint_bloquea_verificacion_antes_de_emitir(monkeypatch):
    monkeypatch.setattr(
        portal,
        "obtener_solicitud_de_cliente",
        lambda *_: {"id": 81, "tracking": "", "tiene_label": False, "guia_url": ""},
    )
    respuesta = portal.envio_verificacion(_request(), 81, cliente="WAIMAO")
    assert respuesta.status_code == 409
    assert "cuando se emita" in respuesta.body.decode("utf-8")


def test_endpoint_emitido_entrega_fragmento_y_retiro_del_mismo_cliente(monkeypatch):
    solicitud = {"id": 81, "tracking": "1234567890", "courier": "DHL"}
    retiro = {"fecha": "2026-09-03", "estado": "AGENDADA"}
    monkeypatch.setattr(portal, "obtener_solicitud_de_cliente", lambda sid, cliente: solicitud)
    monkeypatch.setattr(recolecciones, "obtener_de_solicitud", lambda cliente, sid: retiro)
    monkeypatch.setattr(portal.templates, "TemplateResponse", lambda **kwargs: kwargs)

    respuesta = portal.envio_verificacion(_request(), 81, cliente="WAIMAO")

    assert respuesta["name"] == "portal/_envio_verificacion.html"
    assert respuesta["context"]["s"] is solicitud
    assert respuesta["context"]["recoleccion"] is retiro
    assert respuesta["context"]["recoleccion_error"] is False
    assert respuesta["headers"]["Cache-Control"] == "private, no-store"


def test_endpoint_distingue_error_de_consulta_de_retiro(monkeypatch):
    solicitud = {"id": 81, "tracking": "1234567890", "courier": "DHL"}
    monkeypatch.setattr(portal, "obtener_solicitud_de_cliente", lambda *_: solicitud)
    monkeypatch.setattr(
        recolecciones,
        "obtener_de_solicitud",
        lambda *_: (_ for _ in ()).throw(RuntimeError("db no disponible")),
    )
    monkeypatch.setattr(portal.templates, "TemplateResponse", lambda **kwargs: kwargs)

    respuesta = portal.envio_verificacion(_request(), 81, cliente="WAIMAO")

    assert respuesta["context"]["recoleccion"] is None
    assert respuesta["context"]["recoleccion_error"] is True


def test_consulta_de_recoleccion_filtra_cliente_y_solicitud(monkeypatch):
    ejecutado = {}

    class Cursor:
        def execute(self, sql, params):
            ejecutado["sql"] = sql
            ejecutado["params"] = params

        def fetchone(self):
            return {"id": 9, "estado": "AGENDADA"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Conn:
        def cursor(self):
            return Cursor()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(recolecciones, "_ensure_tabla", lambda: None)
    monkeypatch.setattr(recolecciones, "get_conn", lambda: Conn())

    resultado = recolecciones.obtener_de_solicitud("waimao", 81)

    assert resultado["id"] == 9
    assert "cliente_id = %s AND solicitud_id = %s" in ejecutado["sql"]
    assert ejecutado["params"] == ("WAIMAO", 81)
