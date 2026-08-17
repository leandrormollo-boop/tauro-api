"""Retiro operativo del agregador nacional anterior, sin borrar historia."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest import mock

import pytest

import endpoints.portal_cliente as portal
import servicios.cotizador as cotizador
import servicios.solicitudes_guia as solicitudes


RAIZ = Path(__file__).resolve().parents[1]


def _leer(ruta: str) -> str:
    return (RAIZ / ruta).read_text()


def test_cliente_y_servicio_del_agregador_fueron_eliminados():
    assert not (RAIZ / "core/envia_client.py").exists()
    assert not (RAIZ / "servicios/nacional.py").exists()


def test_no_quedan_credenciales_rutas_ni_imports_activos():
    activos = [
        "core", "endpoints", "jobs", "servicios", "templates", "sql",
    ]
    prohibidos = (
        "ENVIA_API_KEY", "EnviaClient", "core.envia_client",
        "servicios.nacional", "/api/precio-nacional",
    )
    for carpeta in activos:
        for ruta in (RAIZ / carpeta).rglob("*"):
            if not ruta.is_file() or ruta.suffix not in {".py", ".html", ".sql", ".js"}:
                continue
            texto = ruta.read_text(errors="ignore")
            for prohibido in prohibidos:
                assert prohibido not in texto, f"{prohibido} sigue activo en {ruta}"
    for ruta in (RAIZ / "main.py", RAIZ / ".env.example"):
        texto = ruta.read_text(errors="ignore")
        for prohibido in prohibidos:
            assert prohibido not in texto, f"{prohibido} sigue activo en {ruta}"


def test_solicitud_historica_envia_falla_antes_de_llamar_un_courier():
    with mock.patch.object(
        solicitudes, "obtener_solicitud",
        return_value={"id": 17, "courier": "ENVIA"},
    ), mock.patch.object(solicitudes, "generar_guia_internacional") as emitir:
        resultado = solicitudes.generar_guia(17)

    assert resultado["ok"] is False
    assert "integración nacional anterior fue retirada" in resultado["error"]
    assert "no se emitió ni generó ningún cargo" in resultado["error"]
    emitir.assert_not_called()


def test_domestico_se_bloquea_pero_importacion_a_argentina_sigue_internacional():
    endpoint = _leer("endpoints/portal_cliente.py")
    wizard = _leer("templates/portal/envio_nuevo.html")
    cotizador = _leer("servicios/cotizador.py")

    assert 'if origen_pais == "AR" and destino_pais == "AR":' in endpoint
    assert 'if (origen === "AR" && destino === "AR")' in wizard
    assert 'if origen_iso == "AR" and destino_iso == "AR":' in cotizador
    assert 'fetch("/portal/api/precio-multi"' in wizard
    assert "Andreani y OCA directamente" in wizard


def test_cotizador_rapido_domestico_falla_antes_de_consultar_carriers():
    with mock.patch("servicios.carriers.cotizar_carriers_cliente") as consultar:
        with pytest.raises(ValueError, match="Andreani y OCA"):
            cotizador.cotizar_referencia_couriers(
                cliente="MELCIOR", origen_pais="AR", destino_pais="AR",
                peso_kg=1, largo_cm=10, ancho_cm=10, alto_cm=10,
                valor_declarado_usd=100,
            )
    consultar.assert_not_called()


def test_preview_domestico_falla_antes_de_consultar_carriers():
    class _Request:
        async def json(self):
            return {
                "origen_pais": "AR", "destino": "AR",
                "bultos": [{"peso_kg": 1, "descripcion_en": "Merchandise"}],
            }

    with mock.patch.object(portal, "cotizar_couriers_cliente") as consultar:
        respuesta = asyncio.run(
            portal.api_precio_envio_multi(_Request(), cliente="MELCIOR")
        )

    assert respuesta.status_code == 200
    assert json.loads(respuesta.body)["motivo"] == "nacional_no_disponible"
    consultar.assert_not_called()


def test_admin_y_cliente_no_ofrecen_emitir_una_solicitud_historica():
    admin = _leer("templates/admin/pedidos.html")
    detalle = _leer("templates/portal/envio_detalle.html")
    listado = _leer("templates/portal/envios.html")

    assert "Integración retirada" in admin
    assert "No se puede emitir ni generar cargos" in admin
    assert "courier_codigo == 'ENVIA'" in detalle
    assert "no puede emitirse desde el portal" in detalle
    assert "s.estado == 'GUIA_LISTA' and (s.courier or '')|upper != 'ENVIA'" in listado
    assert "(s.courier or '')|upper != 'ENVIA' and s.puede_emitir_cliente" in listado
