"""El Admin explica cuándo el rastreo FedEx no está habilitado."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from endpoints import admin_operadores
from servicios.disponibilidad_tracking import disponibilidad_tracking_fedex


ROOT = Path(__file__).resolve().parents[1]


def test_credenciales_faltantes_informan_la_causa_sin_exponer_valores():
    secreto = "secreto-que-no-debe-salir"
    estado = disponibilidad_tracking_fedex({
        "FEDEX_API_KEY": "",
        "FEDEX_SECRET_KEY": secreto,
        "FEDEX_ENVIRONMENT": "production",
    })

    assert estado["disponible"] is False
    assert estado["motivo"] == "credenciales_faltantes"
    assert estado["titulo"] == "Rastreo FedEx sin activar"
    assert secreto not in repr(estado)


def test_sandbox_reproduce_el_bloqueo_del_job_sin_hacer_http(monkeypatch):
    llamada_http = Mock(side_effect=AssertionError("no debe consultar FedEx"))
    monkeypatch.setattr("httpx.Client.request", llamada_http)

    estado = disponibilidad_tracking_fedex({
        "FEDEX_API_KEY": "key-prueba",
        "FEDEX_SECRET_KEY": "secret-prueba",
        "FEDEX_ENVIRONMENT": "sandbox",
    })

    assert estado["disponible"] is False
    assert estado["motivo"] == "sandbox_bloqueado"
    assert estado["detalle"] == (
        "FedEx está en modo de pruebas. TAURO debe configurar el rastreo real "
        "para actualizar estas guías."
    )
    llamada_http.assert_not_called()


def test_produccion_es_elegible_pero_no_afirma_autenticacion():
    estado = disponibilidad_tracking_fedex({
        "FEDEX_API_KEY": "key-produccion",
        "FEDEX_SECRET_KEY": "secret-produccion",
        "FEDEX_ENVIRONMENT": "production",
    })

    assert estado["disponible"] is True
    assert estado["motivo"] == "produccion_configurada"
    assert estado["autenticacion_verificada"] is False
    assert "El acceso se confirma al consultar FedEx" in str(estado["detalle"])
    assert "key-produccion" not in repr(estado)
    assert "secret-produccion" not in repr(estado)


def test_sandbox_solo_es_elegible_con_flag_explicito():
    estado = disponibilidad_tracking_fedex({
        "FEDEX_API_KEY": "key-prueba",
        "FEDEX_SECRET_KEY": "secret-prueba",
        "FEDEX_ENVIRONMENT": "sandbox",
        "FEDEX_TRACKING_PERMITIR_SANDBOX": "1",
    })

    assert estado["disponible"] is True
    assert estado["motivo"] == "sandbox_habilitado"


def test_control_envios_autentica_antes_de_armar_contexto(monkeypatch):
    admin = SimpleNamespace(
        _is_auth=lambda token: False,
        _redirect_login=lambda: "login",
    )
    estado = Mock(side_effect=AssertionError("no debe leer configuración"))
    listado = Mock(side_effect=AssertionError("no debe consultar envíos"))
    monkeypatch.setattr(admin_operadores, "_admin", lambda: admin)
    monkeypatch.setattr(admin_operadores, "disponibilidad_tracking_fedex", estado)
    monkeypatch.setattr(admin_operadores.negocio, "listar_envios", listado)

    respuesta = admin_operadores.control_envios(
        SimpleNamespace(query_params={}), admin_token=None
    )

    assert respuesta == "login"
    estado.assert_not_called()
    listado.assert_not_called()


def test_control_envios_entrega_el_estado_al_template(monkeypatch):
    admin = SimpleNamespace(_is_auth=lambda token: token == "valido")
    estado = {
        "disponible": False,
        "motivo": "sandbox_bloqueado",
        "titulo": "Rastreo FedEx sin activar",
        "detalle": "FedEx está configurado en sandbox.",
    }
    monkeypatch.setattr(admin_operadores, "_admin", lambda: admin)
    monkeypatch.setattr(
        admin_operadores.negocio,
        "listar_envios",
        lambda **kwargs: {"items": [], "total": 0},
    )
    monkeypatch.setattr(
        admin_operadores, "disponibilidad_tracking_fedex", lambda: estado
    )
    monkeypatch.setattr(
        admin_operadores,
        "_render",
        lambda request, template, **context: (template, context),
    )

    template, contexto = admin_operadores.control_envios(
        SimpleNamespace(query_params={}), admin_token="valido"
    )

    assert template == "control_envios.html"
    assert contexto["tracking_fedex"] == estado


def test_template_muestra_aviso_interno_sin_enlace_a_tracking_historico():
    fuente = (ROOT / "templates/admin/control_envios.html").read_text(
        encoding="utf-8"
    )
    css = (ROOT / "static/css/tauro.css").read_text(encoding="utf-8")

    assert "Rastreo FedEx sin activar" not in fuente
    assert "tracking_fedex.titulo" in fuente
    assert "tracking_fedex.detalle" in fuente
    assert 'class="alert alert-warn"' in fuente
    assert ".alert {" in css
    assert ".alert.alert-warn" in css
    assert "/admin/tracking-fedex" not in fuente
