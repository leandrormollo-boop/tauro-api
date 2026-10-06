"""Contrato HTTP del precio DHL público en el panel administrador."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from endpoints import admin
from servicios import precios_web_dhl as precios_dhl


def _request(path: str = "/admin/precios-web", method: str = "GET") -> Request:
    request = Request({
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 1234),
    })
    request.state.csp_nonce = "test"
    return request


class _FormRequest:
    def __init__(self, data: dict, path: str = "/admin/precios-web/dhl"):
        self._data = data
        self.form_calls = 0
        self.method = "POST"
        self.url = SimpleNamespace(path=path)
        self.headers = {}
        self.client = SimpleNamespace(host="127.0.0.1")
        self.state = SimpleNamespace(csp_nonce="test")

    async def form(self):
        self.form_calls += 1
        return self._data


def _oca_config() -> dict:
    return {
        "habilitada": False,
        "markup_pct": None,
        "markup_texto": "",
        "configuracion_completa": False,
        "publicable": False,
        "estado": "La cotización pública de OCA está desactivada.",
        "error": None,
    }


def _dhl_config(**cambios) -> dict:
    base = {
        "modo": "FIJO_ARS",
        "modo_explicito": False,
        "markup_pct": None,
        "markup_texto": "100000",
        "margen_fijo_ars": Decimal("135000"),
        "fijo_texto": "135000",
        "configuracion_completa": True,
        "publicable": True,
        "estado": "DHL conserva la configuración histórica hasta guardar un modo.",
        "error": None,
    }
    return {**base, **cambios}


def _mock_lecturas(monkeypatch, *, dhl=None):
    monkeypatch.setattr(
        "servicios.precios_web_nacional.leer_configuracion_oca",
        _oca_config,
    )
    monkeypatch.setattr(
        "servicios.precios_web_dhl.leer_configuracion_dhl",
        lambda: dhl if dhl is not None else _dhl_config(),
    )


def test_get_exige_admin_y_muestra_la_regla_dhl_separada(monkeypatch):
    monkeypatch.setattr(admin, "_is_auth", lambda token: token == "valido")
    _mock_lecturas(monkeypatch)

    rechazada = admin.admin_precios_web(_request(), admin_token="otro")
    assert rechazada.status_code == 303
    assert rechazada.headers["location"] == "/admin/login"

    respuesta = admin.admin_precios_web(_request(), admin_token="valido")
    html = respuesta.body.decode()

    assert respuesta.status_code == 200
    assert respuesta.headers["cache-control"] == "private, no-store"
    assert 'action="/admin/precios-web/dhl"' in html
    assert 'name="csrf_precio_dhl"' in html
    assert '<option value="FIJO_ARS" selected>' in html
    assert 'name="dhl_margen_fijo_ars"' in html
    assert 'value="135.000,00"' in html
    assert "no modifica el portal ni el checkout de tiendas" in html


def test_post_no_autorizado_no_lee_el_formulario(monkeypatch):
    monkeypatch.setattr(admin, "_is_auth", lambda _token: False)
    request = _FormRequest({"dhl_modo": "PCT"})

    respuesta = asyncio.run(
        admin.admin_precio_web_dhl_guardar(request, admin_token="invalido")
    )

    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/admin/login"
    assert request.form_calls == 0


def test_csrf_dhl_rechaza_token_oca_y_no_intenta_guardar(monkeypatch):
    monkeypatch.setattr(admin, "_is_auth", lambda _token: True)
    llamadas = []
    monkeypatch.setattr(
        "servicios.precios_web_dhl.guardar_configuracion_dhl",
        lambda **kwargs: llamadas.append(kwargs),
    )
    request = _FormRequest({
        "csrf_precio_dhl": admin._csrf_dhl("precios-web:oca"),
        "dhl_modo": "PCT",
        "dhl_markup_pct": "25",
    })

    respuesta = asyncio.run(
        admin.admin_precio_web_dhl_guardar(request, admin_token="valido")
    )

    assert respuesta.status_code == 403
    assert llamadas == []


def test_csrf_oca_rechaza_token_dhl_y_no_intenta_guardar(monkeypatch):
    monkeypatch.setattr(admin, "_is_auth", lambda _token: True)
    llamadas = []
    monkeypatch.setattr(
        "servicios.precios_web_nacional.guardar_configuracion_oca",
        lambda **kwargs: llamadas.append(kwargs),
    )
    request = _FormRequest(
        {
            "csrf_precio_web": admin._csrf_dhl("precios-web:dhl"),
            "oca_habilitada": "1",
            "oca_markup_pct": "20",
        },
        path="/admin/precios-web",
    )

    respuesta = asyncio.run(
        admin.admin_precios_web_guardar(request, admin_token="valido")
    )

    assert respuesta.status_code == 403
    assert llamadas == []


@pytest.mark.parametrize(
    ("formulario", "esperado"),
    [
        (
            {"dhl_modo": "pct", "dhl_markup_pct": "25,5"},
            {"modo": "PCT", "markup_pct": "25,5", "margen_fijo_ars": ""},
        ),
        (
            {"dhl_modo": "FIJO_ARS", "dhl_margen_fijo_ars": "145.000"},
            {"modo": "FIJO_ARS", "markup_pct": "", "margen_fijo_ars": "145.000"},
        ),
    ],
)
def test_post_envia_modo_y_solo_el_valor_activo_del_formulario(
    monkeypatch, formulario, esperado
):
    monkeypatch.setattr(admin, "_is_auth", lambda _token: True)
    llamadas = []
    monkeypatch.setattr(
        "servicios.precios_web_dhl.guardar_configuracion_dhl",
        lambda **kwargs: llamadas.append(kwargs),
    )
    request = _FormRequest({
        "csrf_precio_dhl": admin._csrf_dhl("precios-web:dhl"),
        **formulario,
    })

    respuesta = asyncio.run(
        admin.admin_precio_web_dhl_guardar(request, admin_token="valido")
    )

    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/admin/precios-web?ok=dhl"
    assert llamadas == [{"request": request, **esperado}]


@pytest.mark.parametrize(
    ("modo", "campo", "valor"),
    [
        ("PCT", "dhl_markup_pct", "25,5"),
        ("FIJO_ARS", "dhl_margen_fijo_ars", "145.000"),
    ],
)
def test_error_422_es_visible_y_preserva_el_input_activo(
    monkeypatch, modo, campo, valor
):
    monkeypatch.setattr(admin, "_is_auth", lambda _token: True)
    _mock_lecturas(monkeypatch)

    def rechazar(**_kwargs):
        raise ValueError("Regla DHL inválida para la prueba.")

    monkeypatch.setattr(
        "servicios.precios_web_dhl.guardar_configuracion_dhl",
        rechazar,
    )
    request = _FormRequest({
        "csrf_precio_dhl": admin._csrf_dhl("precios-web:dhl"),
        "dhl_modo": modo,
        campo: valor,
    })

    respuesta = asyncio.run(
        admin.admin_precio_web_dhl_guardar(request, admin_token="valido")
    )
    html = respuesta.body.decode()

    assert respuesta.status_code == 422
    assert respuesta.headers["cache-control"] == "private, no-store"
    assert "Regla DHL inválida para la prueba." in html
    assert f'<option value="{modo}" selected>' in html
    assert f'value="{valor}"' in html


def test_error_503_no_expone_la_excepcion_y_preserva_la_regla(monkeypatch):
    monkeypatch.setattr(admin, "_is_auth", lambda _token: True)
    _mock_lecturas(monkeypatch)

    def fallar(**_kwargs):
        raise RuntimeError("postgres://usuario:secreto@produccion")

    monkeypatch.setattr(
        "servicios.precios_web_dhl.guardar_configuracion_dhl",
        fallar,
    )
    request = _FormRequest({
        "csrf_precio_dhl": admin._csrf_dhl("precios-web:dhl"),
        "dhl_modo": "FIJO_ARS",
        "dhl_margen_fijo_ars": "150.000",
    })

    respuesta = asyncio.run(
        admin.admin_precio_web_dhl_guardar(request, admin_token="valido")
    )
    html = respuesta.body.decode()

    assert respuesta.status_code == 503
    assert respuesta.headers["cache-control"] == "private, no-store"
    assert "No pudimos guardar el precio de DHL. Probá de nuevo." in html
    assert 'value="150.000"' in html
    assert "postgres://" not in html
    assert "secreto" not in html
    assert "RuntimeError" not in html


@pytest.mark.parametrize("parametro", sorted(precios_dhl.PARAMETROS_RESERVADOS))
@pytest.mark.parametrize("como_nuevo", [False, True])
def test_config_generica_rechaza_claves_dhl_nuevas_y_legacy(
    monkeypatch, parametro, como_nuevo
):
    monkeypatch.setattr(admin, "_is_auth", lambda _token: True)
    monkeypatch.setattr(admin, "_get_config", lambda: [])
    monkeypatch.setattr(
        "servicios.leads.estado_entregas_email",
        lambda: SimpleNamespace(),
    )
    datos = (
        {"_nuevo_parametro": parametro.lower(), "_nuevo_valor": "25"}
        if como_nuevo
        else {parametro.lower(): "25"}
    )
    request = _FormRequest(datos, path="/admin/config")

    respuesta = asyncio.run(admin.admin_config_save(request, admin_token="valido"))

    assert respuesta.status_code == 422
    assert "Precios de la web" in respuesta.body.decode()
