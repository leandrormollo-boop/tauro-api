"""Borde HTTP del Admin de incidencias, sin base de datos ni llamadas IA."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import ANY, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from endpoints import admin
from servicios import agente_incidencias_ia as agente_ia
from servicios import control_incidencias_emision as control


@pytest.fixture(autouse=True)
def _admin_aislado(monkeypatch):
    monkeypatch.setattr(admin, "_is_auth", lambda token: token == "valido")
    monkeypatch.setattr(admin, "check_rate", lambda *args, **kwargs: True)
    monkeypatch.setattr(admin, "check_auth_rate", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        agente_ia,
        "estado_agente_ia",
        lambda: {"configurado": False, "model": "gpt-5.6-sol"},
    )


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(admin.router)
    with TestClient(app) as cliente:
        cliente.cookies.set("admin_token", "valido")
        yield cliente


def _diagnostico(*, confirmado: bool, titulo: str = "Error pendiente de diagnóstico"):
    return {
        "codigo": "EMISION_NO_CLASIFICADA",
        "titulo": titulo,
        "explicacion": "La evidencia disponible no alcanza para identificar una causa conocida.",
        "accion": "Revisá el intento, el estado local y el panel del courier.",
        "responsable": "operador",
        "severidad": "critica",
        "confirmado": confirmado,
        "version": 1,
    }


def _incidencia(
    incidencia_id: int = 41,
    *,
    confirmado: bool = True,
    estado: str = "PENDIENTE",
    analisis=None,
):
    return {
        "id": incidencia_id,
        "solicitud_id": 730,
        "cliente_id": "CLIENTE <script>cliente()</script>",
        "courier": "DHL",
        "tracking": None,
        "codigo": "EMISION_NO_CLASIFICADA",
        "estado": estado,
        "cantidad": 2,
        "fecha": datetime(2026, 10, 8, 11, 45),
        "resolucion": None,
        "ultimo_intento_id": 912,
        "referencia": "EMI-<script>referencia()</script>",
        "diagnostico": _diagnostico(confirmado=confirmado),
        "evidencia": {
            "tiene_tracking": False,
            "tiene_documento": False,
            "cargo_registrado": False,
            "cargo_pendiente": True,
            "tarifa_coincide": False,
        },
        "analisis": analisis,
        "intentos": [{
            "estado": "FALLIDO",
            "fecha": datetime(2026, 10, 8, 11, 44, 30),
            "metadata": {
                "motivo": '<script id="meta">alert(1)</script>',
                "referencia": '<img src=x onerror="meta()">',
                "etapa": "emision<script>etapa()</script>",
                "error_tipo": "RuntimeError<svg onload=tipo()>",
            },
        }],
    }


def _listado():
    item = _incidencia()
    item["diagnostico"] = _diagnostico(
        confirmado=True,
        titulo='<script id="titulo-lista">titulo()</script>',
    )
    return {
        "items": [item],
        "siguiente": 17,
        "inconclusos": [{
            "id": 99,
            "solicitud_id": 731,
            "cliente_id": 'CLIENTE <img src=x onerror="lista()">',
            "referencia": '<script id="referencia-lista">ref()</script>',
            "fecha": datetime(2026, 10, 8, 10, 0),
        }],
    }


def test_get_y_post_sin_auth_fallan_cerrados_antes_del_servicio(client, monkeypatch):
    listar = Mock(side_effect=AssertionError("no debe listar"))
    resumen = Mock(side_effect=AssertionError("no debe resumir"))
    obtener = Mock(side_effect=AssertionError("no debe obtener"))
    analizar = Mock(side_effect=AssertionError("no debe analizar"))
    revision = Mock(side_effect=AssertionError("no debe tomar revisión"))
    verificar = Mock(side_effect=AssertionError("no debe verificar"))
    monkeypatch.setattr(control, "listar_incidencias", listar)
    monkeypatch.setattr(control, "resumen_incidencias", resumen)
    monkeypatch.setattr(control, "obtener_incidencia", obtener)
    monkeypatch.setattr(control, "analizar_incidencia", analizar)
    monkeypatch.setattr(control, "tomar_revision", revision)
    monkeypatch.setattr(control, "verificar_inconcluso", verificar)
    client.cookies.clear()

    for ruta in ("/admin/incidencias", "/admin/incidencias/41"):
        respuesta = client.get(ruta, follow_redirects=False)
        assert respuesta.status_code == 303
        assert respuesta.headers["location"] == "/admin/login"
    resumen_http = client.get("/admin/incidencias/resumen", follow_redirects=False)
    assert resumen_http.status_code == 401
    assert resumen_http.json() == {"error": "Sesión cerrada."}

    posts = (
        ("/admin/incidencias/41/analizar", {"intento_id": "912", "csrf": "x"}),
        ("/admin/incidencias/41/revision", {"intento_id": "912", "csrf": "x"}),
        ("/admin/incidencias/intentos/912/verificar", {"csrf": "x"}),
    )
    for ruta, data in posts:
        respuesta = client.post(ruta, data=data, follow_redirects=False)
        assert respuesta.status_code == 303
        assert respuesta.headers["location"] == "/admin/login"

    for servicio in (listar, resumen, obtener, analizar, revision, verificar):
        servicio.assert_not_called()


def test_resumen_disponible_no_cachea_y_conserva_conteos(client, monkeypatch):
    resumen = Mock(return_value={
        "pendientes": 3,
        "criticas": 2,
        "inconclusos": 1,
        "total": 4,
        "disponible": True,
    })
    monkeypatch.setattr(control, "resumen_incidencias", resumen)
    respuesta = client.get("/admin/incidencias/resumen")
    assert respuesta.status_code == 200
    assert respuesta.headers["cache-control"] == "no-store"
    assert respuesta.json() == {
        "pendientes": 3,
        "criticas": 2,
        "inconclusos": 1,
        "total": 4,
        "disponible": True,
    }
    resumen.assert_called_once_with()


def test_resumen_indisponible_es_503_y_no_inventa_ceros(client, monkeypatch):
    monkeypatch.setattr(
        control,
        "resumen_incidencias",
        Mock(side_effect=RuntimeError("base fuera de servicio")),
    )
    respuesta = client.get("/admin/incidencias/resumen")
    assert respuesta.status_code == 503
    assert respuesta.headers["cache-control"] == "no-store"
    assert respuesta.json() == {"disponible": False}
    assert not any(campo in respuesta.json() for campo in ("pendientes", "criticas", "total"))


def test_listado_realista_renderiza_y_autoescapa_datos_no_confiables(client, monkeypatch):
    listar = Mock(return_value=_listado())
    monkeypatch.setattr(control, "listar_incidencias", listar)
    respuesta = client.get("/admin/incidencias?estado=pendientes&antes=25")
    assert respuesta.status_code == 200
    html = respuesta.text
    assert "Intentos sin resultado" in html
    assert "Solicitud #731" in html
    assert "Ver anteriores" in html
    assert '<script id="titulo-lista">' not in html
    assert '&lt;script id=&#34;titulo-lista&#34;&gt;titulo()&lt;/script&gt;' in html
    assert '<script id="referencia-lista">' not in html
    assert '&lt;script id=&#34;referencia-lista&#34;&gt;ref()&lt;/script&gt;' in html
    assert '<img src=x onerror="lista()">' not in html
    listar.assert_called_once_with("pendientes", 25)


def test_detalle_con_causa_conocida_no_ofrece_ia(client, monkeypatch):
    obtener = Mock(return_value=_incidencia(confirmado=True))
    monkeypatch.setattr(control, "obtener_incidencia", obtener)
    respuesta = client.get("/admin/incidencias/41")
    assert respuesta.status_code == 200
    assert "Causa pendiente de confirmar." not in respuesta.text
    assert "Analizar con IA" not in respuesta.text
    assert "IA pendiente de conectar" not in respuesta.text
    assert "Tomar en revisión" in respuesta.text
    obtener.assert_called_once_with(41)


def test_detalle_desconocido_muestra_ia_desconectada_y_autoescapa_historial(
    client, monkeypatch,
):
    analisis = {
        "ia": {
            "hipotesis": '<img src=x onerror="hipotesis()">',
            "comprobacion": '<script id="ia">comprobar()</script>',
            "accion": "soporte_tecnico",
            "confirmada": False,
        }
    }
    item = _incidencia(42, confirmado=False, analisis=analisis)
    monkeypatch.setattr(control, "obtener_incidencia", Mock(return_value=item))
    respuesta = client.get("/admin/incidencias/42")
    assert respuesta.status_code == 200
    assert respuesta.headers["cache-control"] == "private, no-store"
    html = respuesta.text
    assert "Causa pendiente de confirmar." in html
    assert "IA pendiente de conectar. La revisión de registros está disponible." in html
    assert "Analizar con IA" not in html
    for texto_crudo in (
        '<img src=x onerror="hipotesis()">',
        '<script id="ia">comprobar()</script>',
        '<script id="meta">alert(1)</script>',
        '<img src=x onerror="meta()">',
        "<svg onload=tipo()>",
    ):
        assert texto_crudo not in html
    assert '&lt;img src=x onerror=&#34;hipotesis()&#34;&gt;' in html
    assert '&lt;script id=&#34;ia&#34;&gt;comprobar()&lt;/script&gt;' in html
    assert '&lt;script id=&#34;meta&#34;&gt;alert(1)&lt;/script&gt;' in html


@pytest.mark.parametrize("resultado", ["sin_cambios", "desactualizado"])
def test_detalle_comunica_resultado_obsoleto(client, monkeypatch, resultado):
    monkeypatch.setattr(
        control,
        "obtener_incidencia",
        Mock(return_value=_incidencia(42, confirmado=False)),
    )
    respuesta = client.get(f"/admin/incidencias/42?revision={resultado}")
    assert respuesta.status_code == 200
    assert "El caso cambió o ya se está revisando" in respuesta.text
    assert "Revisá el estado actualizado antes de continuar" in respuesta.text


def test_detalle_inexistente_es_404(client, monkeypatch):
    obtener = Mock(return_value=None)
    monkeypatch.setattr(control, "obtener_incidencia", obtener)
    respuesta = client.get("/admin/incidencias/404")
    assert respuesta.status_code == 404
    assert "No encontramos esa incidencia" in respuesta.text
    obtener.assert_called_once_with(404)


def test_csrf_ata_incidencia_e_intento_y_no_llama_servicios(client, monkeypatch):
    analizar = Mock()
    revision = Mock()
    verificar = Mock()
    monkeypatch.setattr(control, "analizar_incidencia", analizar)
    monkeypatch.setattr(control, "tomar_revision", revision)
    monkeypatch.setattr(control, "verificar_inconcluso", verificar)

    token_otro_intento = admin._csrf_dhl("incidencia:41:911")
    token_otra_incidencia = admin._csrf_dhl("incidencia:40:912")
    token_otro_inconcluso = admin._csrf_dhl("intento:911")
    respuestas = (
        client.post(
            "/admin/incidencias/41/analizar",
            data={"intento_id": "912", "csrf": token_otro_intento},
            follow_redirects=False,
        ),
        client.post(
            "/admin/incidencias/41/revision",
            data={"intento_id": "912", "csrf": token_otra_incidencia},
            follow_redirects=False,
        ),
        client.post(
            "/admin/incidencias/intentos/912/verificar",
            data={"csrf": token_otro_inconcluso},
            follow_redirects=False,
        ),
    )
    assert [respuesta.status_code for respuesta in respuestas] == [403, 403, 403]
    analizar.assert_not_called()
    revision.assert_not_called()
    verificar.assert_not_called()


def test_posts_validos_invocan_servicios_con_ids_y_modo_ia(client, monkeypatch):
    analizar = Mock(return_value="analizado")
    revision = Mock(return_value=True)
    verificar = Mock(return_value=True)
    monkeypatch.setattr(control, "analizar_incidencia", analizar)
    monkeypatch.setattr(control, "tomar_revision", revision)
    monkeypatch.setattr(control, "verificar_inconcluso", verificar)

    csrf_incidencia = admin._csrf_dhl("incidencia:41:912")
    respuesta_analizar = client.post(
        "/admin/incidencias/41/analizar",
        data={"intento_id": "912", "csrf": csrf_incidencia, "usar_ia": "true"},
        follow_redirects=False,
    )
    respuesta_revision = client.post(
        "/admin/incidencias/41/revision",
        data={"intento_id": "912", "csrf": csrf_incidencia},
        follow_redirects=False,
    )
    respuesta_verificar = client.post(
        "/admin/incidencias/intentos/912/verificar",
        data={"csrf": admin._csrf_dhl("intento:912")},
        follow_redirects=False,
    )

    assert respuesta_analizar.status_code == respuesta_revision.status_code == 303
    assert respuesta_analizar.headers["location"] == "/admin/incidencias/41?revision=analizado"
    assert respuesta_revision.headers["location"] == "/admin/incidencias/41"
    assert respuesta_verificar.status_code == 303
    assert respuesta_verificar.headers["location"].endswith("verificacion=resuelto")
    analizar.assert_called_once_with(41, 912, usar_ia=True, request=ANY)
    revision.assert_called_once_with(41, 912, ANY)
    verificar.assert_called_once_with(912, ANY)


def test_presupuesto_durable_ia_agotado_devuelve_429_sin_analizar(client, monkeypatch):
    presupuesto = Mock(return_value=False)
    analizar = Mock(side_effect=AssertionError("no debe analizar"))
    monkeypatch.setattr(admin, "check_auth_rate", presupuesto)
    monkeypatch.setattr(control, "analizar_incidencia", analizar)
    respuesta = client.post(
        "/admin/incidencias/41/analizar",
        data={
            "intento_id": "912",
            "csrf": admin._csrf_dhl("incidencia:41:912"),
            "usar_ia": "true",
        },
        follow_redirects=False,
    )
    assert respuesta.status_code == 429
    assert "límite de análisis de IA" in respuesta.text
    presupuesto.assert_called_once_with(
        "admin:incidencias:ia", max_attempts=12, window_seconds=600
    )
    analizar.assert_not_called()


def test_analisis_sin_ia_no_consume_presupuesto_durable(client, monkeypatch):
    presupuesto = Mock(side_effect=AssertionError("no debe consumir presupuesto IA"))
    analizar = Mock(return_value="analizado")
    monkeypatch.setattr(admin, "check_auth_rate", presupuesto)
    monkeypatch.setattr(control, "analizar_incidencia", analizar)
    respuesta = client.post(
        "/admin/incidencias/41/analizar",
        data={
            "intento_id": "912",
            "csrf": admin._csrf_dhl("incidencia:41:912"),
        },
        follow_redirects=False,
    )
    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/admin/incidencias/41?revision=analizado"
    presupuesto.assert_not_called()
    analizar.assert_called_once_with(41, 912, usar_ia=False, request=ANY)


@pytest.mark.parametrize(
    "ruta,servicio,resultado",
    [
        ("/admin/incidencias/41/analizar", "analizar_incidencia", "sin_cambios"),
        ("/admin/incidencias/41/analizar", "analizar_incidencia", "desactualizado"),
        ("/admin/incidencias/41/revision", "tomar_revision", False),
    ],
)
def test_formulario_obsoleto_comunica_que_no_se_aplico(
    client, monkeypatch, ruta, servicio, resultado,
):
    llamada = Mock(return_value=resultado)
    monkeypatch.setattr(control, servicio, llamada)
    respuesta = client.post(
        ruta,
        data={
            "intento_id": "912",
            "csrf": admin._csrf_dhl("incidencia:41:912"),
        },
        follow_redirects=False,
    )
    comunica_en_cuerpo = (
        respuesta.status_code in {409, 422}
        and any(palabra in respuesta.text.casefold() for palabra in ("cambió", "venció", "actualiz"))
    )
    comunica_en_redirect = (
        respuesta.status_code == 303
        and respuesta.headers.get("location", "") != "/admin/incidencias/41"
        and any(
            palabra in respuesta.headers.get("location", "").casefold()
            for palabra in ("sin_cambios", "desactualizado", "obsoleto", "vencio")
        )
    )
    assert comunica_en_cuerpo or comunica_en_redirect
