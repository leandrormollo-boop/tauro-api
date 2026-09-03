from __future__ import annotations

import asyncio
from pathlib import Path

from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

import main
from endpoints import admin
from servicios.couriers_urls import nombre_courier
from servicios.presentacion import dinero_ars


ROOT = Path(__file__).resolve().parents[1]


def _request(path="/no-existe"):
    return Request({
        "type": "http", "method": "GET", "path": path,
        "raw_path": path.encode(), "headers": [], "query_string": b"",
        "scheme": "https", "server": ("taurosolutions.ar", 443),
        "client": ("127.0.0.1", 1), "root_path": "", "app": main.app,
    })


def test_404_publico_tiene_diseno_y_no_json_crudo():
    respuesta = asyncio.run(main.error_http_con_diseno(
        _request("/pagina-inexistente"), StarletteHTTPException(404),
    ))

    assert respuesta.status_code == 404
    assert respuesta.media_type == "text/html"
    assert b"ERROR 404" in respuesta.body
    assert b"TAURO Solutions" in respuesta.body
    assert b'"detail"' not in respuesta.body


def test_robots_separa_web_de_superficies_privadas():
    respuesta = main.robots_txt()
    texto = respuesta.body.decode()
    assert "Allow: /web" in texto
    assert "Disallow: /portal" in texto
    assert "Disallow: /admin" in texto


def test_nombres_de_courier_salen_del_mapa_sin_title_case():
    assert nombre_courier("dhl") == "DHL"
    assert nombre_courier("fedex") == "FedEx"
    assert nombre_courier("ups") == "UPS"
    assert nombre_courier("courier Nuevo") == "courier Nuevo"


def test_formato_ars_es_argentino_y_con_centavos():
    assert dinero_ars("27330786") == "$ 27.330.786,00"
    for relativo in (
        "templates/admin/home.html", "templates/admin/pedidos.html",
        "templates/admin/referencia.html", "templates/admin/pedido_editar.html",
        "templates/admin/bandeja_cliente.html",
    ):
        contenido = (ROOT / relativo).read_text()
        assert "{:,.0f}" not in contenido


def test_lista_clientes_muestra_pricing_efectivo_por_courier(monkeypatch):
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def execute(self, *_): pass
        def fetchall(self):
            return [{
                "cliente_id": "WAIMAO", "email": "w@example.invalid",
                "markup_tipo": "PCT", "markup_valor": 25,
                "markup_pct": 25,
            }]

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def cursor(self): return Cursor()

    monkeypatch.setattr(admin, "get_conn", lambda: Conn())
    monkeypatch.setattr(admin, "obtener_matriz", lambda _cliente: {
        "couriers": [
            {"nombre": "DHL", "pricing": {"tipo": "FIJO_ARS", "valor": 95000}},
            {"nombre": "FedEx", "pricing": {"tipo": "PCT", "valor": 25}},
        ]
    })

    cliente = admin._get_clientes_lista()[0]
    assert cliente["pricing_por_courier"] == [
        {"nombre": "DHL", "descripcion": "+ ARS 95.000"},
        {"nombre": "FedEx", "descripcion": "25%"},
    ]


def test_aviso_de_cp_es_por_pais_y_no_bloquea_submit():
    html = (ROOT / "templates/portal/envio_nuevo.html").read_text()
    bloque = html.split("function warnDestinationPostal()", 1)[1].split(
        "// ── Remitente", 1
    )[0]

    assert 'country === "US"' in bloque and r"^\d{5}$" in bloque
    assert 'country === "AR"' in bloque and "CPA completo" in bloque
    assert "setCustomValidity" not in bloque
    assert "preventDefault" not in bloque


def test_nombres_de_personas_no_se_normalizan_en_el_servicio():
    servicio = (ROOT / "servicios/solicitudes_guia.py").read_text()
    assert "dest_nombre.title()" not in servicio
    assert "remitente_nombre.title()" not in servicio
    assert "dest_nombre.upper()" not in servicio
