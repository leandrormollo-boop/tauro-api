"""Adjuntar evidencia no es informar ni acreditar un segundo pago."""
from decimal import Decimal
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from endpoints import portal_cliente as pc
from servicios import cuenta_corriente as cc


URL = "/portal/pagos/23/adjuntar-comprobante"


@pytest.fixture
def web(monkeypatch):
    pago = dict(id=23, fecha="06/10/2026", monto_ars=Decimal("1000000.00"),
                referencia="TRANSFERENCIA-DEMO", estado="APROBADO", estado_label="Acreditado",
                imputacion_label="Pago imputado a 2 envíos", tiene_comprobante=False)
    app = FastAPI()
    app.include_router(pc.router)
    app.dependency_overrides[pc.cliente_actual] = lambda: "DEMO"

    @app.middleware("http")
    async def nonce(request, call_next):
        request.state.csp_nonce = "test"
        return await call_next(request)

    monkeypatch.setattr(pc, "obtener_pago_cliente", lambda cliente, ident: pago if cliente == "DEMO" and ident == 23 else None)
    monkeypatch.setattr(pc, "check_rate", lambda *a, **k: True)
    for key, fn in dict(saldo_menu=lambda *_: None, pendientes_menu=lambda *_: {}, ayuda=lambda: {}).items():
        monkeypatch.setitem(pc.templates.env.globals, key, fn)
    guardar = Mock(return_value=True)
    monkeypatch.setattr(cc, "adjuntar_comprobante_pago", guardar)
    return TestClient(app, follow_redirects=False), pago, guardar


def test_formulario_identifica_el_pago_sin_campos_contables_editables(web):
    client, _, _ = web
    r = client.get(URL)
    assert r.status_code == 200
    assert "private, no-store" == r.headers["cache-control"]
    assert "Pago #23" in r.text and "$ 1.000.000,00" in r.text
    assert "Acreditado" in r.text and "Pago imputado a 2 envíos" in r.text
    assert 'name="comprobante"' in r.text and 'enctype="multipart/form-data"' in r.text
    assert 'name="monto"' not in r.text and 'name="estado"' not in r.text
    assert 'name="cliente_id"' not in r.text


def test_post_usa_identidad_de_sesion_e_ignora_monto_y_cliente_inyectados(web):
    client, _, guardar = web
    r = client.post(URL, files={"comprobante": ("pago.pdf", b"%PDF-1.4\nprueba", "application/pdf")},
                    data={"cliente_id": "OTRO", "monto": "999999999"})
    assert r.status_code == 303 and r.headers["location"] == URL + "?adjuntado=1"
    guardar.assert_called_once_with("DEMO", 23, b"%PDF-1.4\nprueba", "pago.pdf")


@pytest.mark.parametrize("method", ["get", "post"])
def test_ajeno_o_inexistente_no_revela_pago_ni_adjunta(web, method):
    client, _, guardar = web
    assert getattr(client, method)(URL.replace("23", "24")).status_code == 404
    guardar.assert_not_called()


@pytest.mark.parametrize("method", ["get", "post"])
def test_sin_sesion_no_puede_ver_ni_adjuntar(web, method):
    client, _, guardar = web
    client.app.dependency_overrides.clear()
    r = getattr(client, method)(URL)
    assert r.status_code == 303 and r.headers["location"] == "/portal/login"
    guardar.assert_not_called()


@pytest.mark.parametrize("contenido", [None, b"", b"%PDF" + b"x" * cc.COMPROBANTE_MAX_BYTES])
def test_vacio_y_limite_se_rechazan_antes_del_servicio(web, contenido):
    client, _, guardar = web
    r = client.post(URL, files={"comprobante": ("pago.pdf", contenido, "application/pdf")} if contenido is not None else {})
    assert r.status_code == 303 and "error=" in r.headers["location"]
    guardar.assert_not_called()


@pytest.mark.parametrize("error", [ValueError("El pago ya tiene un comprobante."), LookupError(), RuntimeError("detalle interno privado")])
def test_conflictos_y_fallos_no_se_presentan_como_exito(web, error):
    client, _, guardar = web
    guardar.side_effect = error
    r = client.post(URL, files={"comprobante": ("pago.pdf", b"%PDF-1.4", "application/pdf")})
    if isinstance(error, LookupError):
        assert r.status_code == 404
    else:
        assert r.status_code == 303
        message = parse_qs(urlsplit(r.headers["location"]).query)["error"][0]
        assert "detalle interno" not in message
        assert "adjuntado=" not in r.headers["location"]


def test_reintento_confirmado_y_comprobante_existente_se_pueden_ver_sin_reemplazo(web):
    client, pago, guardar = web
    guardar.return_value = False
    r = client.post(URL, files={"comprobante": ("pago.pdf", b"%PDF-1.4", "application/pdf")})
    assert r.status_code == 303 and "adjuntado=1" in r.headers["location"]
    pago["tiene_comprobante"] = True
    r = client.get(r.headers["location"])
    assert "Comprobante adjuntado al pago #23" in r.text
    assert 'data-document-url="/portal/documentos/pago/23/contenido"' in r.text
    assert 'name="comprobante"' not in r.text


def test_limita_intentos_sin_escribir(web, monkeypatch):
    client, _, guardar = web
    monkeypatch.setattr(pc, "check_rate", lambda *a, **k: False)
    r = client.post(URL, files={"comprobante": ("pago.pdf", b"%PDF-1.4", "application/pdf")})
    assert r.status_code == 303 and "error=" in r.headers["location"]
    guardar.assert_not_called()


def test_historial_tambien_ofrece_adjuntar_a_pagos_antiguos_sin_archivo(web):
    from starlette.requests import Request
    client, _, _ = web
    movimientos = dict(items=[dict(tipo="PAGO", pago_id=41, concepto="Transferencia", archivo_url=None,
                                  debe_ars=0, haber_ars=1000000, valor_envio_ars=None)],
                       pagina_actual=2, total_paginas=2, total_resultados=9, pagina_desde=9, pagina_hasta=9)
    context = dict(request=Request(dict(type="http", path="/portal/cuenta", headers=[], query_string=b"")),
                   movimientos=movimientos, cuenta_url=lambda **k: "/portal/cuenta", ambito_filtro="consolidado", tipo_filtro="pagos")
    template = pc.templates.env.get_template("portal/cuenta_movimientos.html")
    assert '/portal/pagos/41/adjuntar-comprobante' in template.render(**context)
    movimientos["items"][0]["archivo_url"] = "/portal/pagos/41/comprobante"
    assert '/portal/pagos/41/adjuntar-comprobante' not in template.render(**context)


@pytest.mark.parametrize("headers", [{"Origin": "https://ajeno.example"}, {"Sec-Fetch-Site": "cross-site"}])
def test_csrf_global_rechaza_post_antes_del_servicio(web, monkeypatch, headers):
    import main
    from servicios import auditoria
    client, _, guardar = web
    monkeypatch.setattr(main, "_host_permitido", lambda host: host == "testserver")
    monkeypatch.setattr(auditoria, "registrar_desde_request", Mock())
    client.app.middleware("http")(main.headers_de_seguridad)
    r = client.post(URL, files={"comprobante": ("pago.pdf", b"%PDF-1.4", "application/pdf")}, headers=headers)
    assert r.status_code == 403
    guardar.assert_not_called()
