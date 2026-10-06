import asyncio
from types import SimpleNamespace

from starlette.requests import Request

from endpoints import admin


def _request(path="/admin/precios-web", method="GET"):
    request = Request({
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
    })
    request.state.csp_nonce = "test"
    return request


class _FormRequest:
    def __init__(self, data):
        self._data = data
        self.method = "POST"
        self.url = SimpleNamespace(path="/admin/precios-web")
        self.headers = {}
        self.client = SimpleNamespace(host="127.0.0.1")
        self.state = SimpleNamespace(csp_nonce="test")

    async def form(self):
        return self._data


def _config(**cambios):
    base = {
        "habilitada": False,
        "markup_pct": None,
        "markup_texto": "",
        "configuracion_completa": False,
        "publicable": False,
        "estado": "Falta cargar el markup de OCA.",
        "error": None,
    }
    return {**base, **cambios}


def test_get_requiere_admin_y_renderiza_precio_separado(monkeypatch):
    monkeypatch.setattr(admin, "_is_auth", lambda token: token == "valido")
    assert admin.admin_precios_web(_request(), admin_token="otro").status_code == 303
    monkeypatch.setattr(
        "servicios.precios_web_nacional.leer_configuracion_oca",
        lambda: _config(markup_pct=20, markup_texto="20", configuracion_completa=True),
    )
    respuesta = admin.admin_precios_web(_request(), admin_token="valido")
    html = respuesta.body.decode()
    assert respuesta.status_code == 200
    assert "Precios de la web" in html
    assert "Ganancia sobre el costo OCA (%)" in html
    assert "precios por cliente" in html


def test_post_exige_csrf(monkeypatch):
    monkeypatch.setattr(admin, "_is_auth", lambda _: True)
    request = _FormRequest({"csrf_precio_web": "malo"})
    respuesta = asyncio.run(admin.admin_precios_web_guardar(request, admin_token="valido"))
    assert respuesta.status_code == 403


def test_post_guarda_y_redirige(monkeypatch):
    monkeypatch.setattr(admin, "_is_auth", lambda _: True)
    token = admin._csrf_dhl("precios-web:oca")
    llamadas = []
    monkeypatch.setattr(
        "servicios.precios_web_nacional.guardar_configuracion_oca",
        lambda **kwargs: llamadas.append(kwargs) or _config(
            habilitada=True, markup_pct=20, markup_texto="20",
            configuracion_completa=True, publicable=True,
            estado="La configuración comercial de OCA está lista.",
        ),
    )
    request = _FormRequest({
        "csrf_precio_web": token,
        "oca_habilitada": "1",
        "oca_markup_pct": "20",
    })
    respuesta = asyncio.run(admin.admin_precios_web_guardar(request, admin_token="valido"))
    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/admin/precios-web?ok=1"
    assert llamadas[0]["habilitada"] is True
    assert llamadas[0]["markup_pct"] == "20"


def test_config_generica_reserva_claves_oca(monkeypatch):
    monkeypatch.setattr(admin, "_is_auth", lambda _: True)
    monkeypatch.setattr(admin, "_get_config", lambda: [])
    monkeypatch.setattr("servicios.leads.estado_entregas_email", lambda: SimpleNamespace())
    request = _FormRequest({
        "_nuevo_parametro": "WEB_MARKUP_PCT_OCA",
        "_nuevo_valor": "15",
    })
    respuesta = asyncio.run(admin.admin_config_save(request, admin_token="valido"))
    assert respuesta.status_code == 422
    assert "Precios de la web" in respuesta.body.decode()
