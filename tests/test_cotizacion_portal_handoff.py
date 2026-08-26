"""La copia enviada por mail debe continuar en el portal sin rehacer datos."""

from pathlib import Path

import endpoints.portal_cliente as pc
from servicios import leads


ROOT = Path(__file__).resolve().parents[1]


def _request():
    from starlette.requests import Request

    request = Request({
        "type": "http", "method": "GET", "path": "/portal/envios/nuevo",
        "raw_path": b"/portal/envios/nuevo", "query_string": b"",
        "headers": [], "scheme": "https", "server": ("testserver", 443),
        "client": ("127.0.0.1", 1234), "root_path": "",
    })
    request.state.csp_nonce = "test"
    return request


def _snapshot():
    return {
        "quote_id": "Q-abcdefghijklmnopqrstuvwxyz123456",
        "referencia": "TW-20260818-ABC123",
        "origen": "CN", "destino": "US",
        "peso_kg": "5.500", "largo_cm": "30.00", "ancho_cm": "20.00",
        "alto_cm": "10.00", "valor_declarado_usd": "100.00",
        "opciones": [{
            "id": "dhl", "recomendada": True, "precio_ars": "95000.00",
        }],
    }


def _preparar_wizard(monkeypatch):
    monkeypatch.setattr(pc.templates, "TemplateResponse", lambda **kw: kw)
    monkeypatch.setattr(pc, "get_productos", lambda _cliente: [])
    monkeypatch.setattr(pc, "_paises_con_nacional", lambda: [("CN", "China"), ("US", "Estados Unidos")])
    monkeypatch.setattr(pc, "obtener_remitente_para_envio", lambda _cliente: None)
    monkeypatch.setattr(pc, "listar_direcciones", lambda *_a: [])
    monkeypatch.setattr(pc, "tax_paga_cliente", lambda _cliente: "DESTINATARIO")
    monkeypatch.setattr(pc, "courier_default_cliente", lambda _cliente: "fedex")


def test_login_solo_redirige_a_snapshot_vigente_y_opaco(monkeypatch):
    qid = "Q-abcdefghijklmnopqrstuvwxyz123456"
    monkeypatch.setattr(leads, "obtener_cotizacion", lambda *a, **k: _snapshot())

    destino = pc._destino_post_login(qid)

    assert destino == f"/portal/envios/nuevo?ambito=internacional&quote_id={qid}"
    assert pc._destino_post_login("https://evil.example") == "/portal/home"


def test_wizard_precarga_snapshot_y_revalida_sin_confiar_en_query(monkeypatch):
    _preparar_wizard(monkeypatch)
    monkeypatch.setattr(leads, "obtener_cotizacion", lambda *a, **k: _snapshot())

    respuesta = pc.envio_nuevo_form(
        _request(), ambito="internacional",
        quote_id="Q-abcdefghijklmnopqrstuvwxyz123456", cliente="MELCIOR",
    )
    contexto = respuesta["context"]
    form = contexto["form"]

    assert contexto["cotizacion_web"]["referencia"] == "TW-20260818-ABC123"
    assert form["rem_pais"] == "CN"
    assert form["destino_pais"] == "US"
    assert form["intl_courier"] == "dhl"
    assert form["precio_cotizado_ars"] == "95000.00"
    assert form["bultos"][0]["peso_kg"] == "5.500"
    assert form["bultos"][0]["valor_unitario_usd"] == "100.00"
    assert "Cotización web TW-20260818-ABC123" == form["observaciones"]


def test_ctas_conservan_quote_id_sin_mandar_precio():
    publica = (ROOT / "templates/public/cotizacion.html").read_text(encoding="utf-8")
    widget = (ROOT / "web/components/02-quote-widget.jsx").read_text(encoding="utf-8")

    assert "/portal/login?quote_id=" in publica
    assert "encodeURIComponent(result.quote_id)" in widget
    assert "precio_ars" not in publica.split("/portal/login?quote_id=", 1)[1].split('"', 1)[0]
