"""La guía corregida conserva una base propia antes de llamar a DHL."""

from decimal import Decimal
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import servicios.carriers as carriers
import servicios.solicitudes_guia as solicitudes


class _DHLTarifa:
    MULTIBULTO = True

    def get_rates(self, _origen, _destino, paquete=None, paquetes=None):
        return {
            "encontrado": True,
            "costo": 1016.70,
            "moneda": "USD",
            "servicio": "EXPRESS_WORLDWIDE",
            "dias_estimados": "2",
        }


def _cotizar(*, incluir_base_interna=False):
    piezas = [
        {"peso_kg": 7, "largo_cm": 50, "ancho_cm": 36, "alto_cm": 36},
        {"peso_kg": 3.5, "largo_cm": 36, "ancho_cm": 36, "alto_cm": 28},
    ]
    configuracion = [{
        "id": "dhl", "nombre": "DHL Express", "servicio": "Express",
        "logo": "/dhl.svg", "requisitos": ("FAKE_KEY",), "cliente": _DHLTarifa,
    }]
    pricing = {
        "tipo": "FIJO_ARS",
        "valor": 150_000,
        "tramos_usd": {
            "alto_desde_usd": 190,
            "alto_markup_usd": 100,
        },
    }
    with mock.patch.dict("os.environ", {"FAKE_KEY": "1"}, clear=True), \
         mock.patch.object(carriers, "CARRIERS", configuracion):
        return carriers.cotizar_carriers_cliente(
            origen={"country": "CN"}, destino={"country": "UY"},
            paquete=piezas[0], paquetes=piezas, dolar=1535,
            pricing_cliente=pricing, incluir_base_interna=incluir_base_interna,
        )[0]


def _solicitud_reemplazo():
    return {
        "id": 53, "cliente_id": "WAIMAO", "courier": "DHL",
        "reemplaza_solicitud_id": 52, "precio_tauro_ars": 1_714_134,
        "precio_tauro_usd": 1116.70, "created_at": "2026-09-02T13:53:33Z",
        "remitente_pais": "CN", "remitente_ciudad": "SUXI TOWN",
        "remitente_zip": "322000", "remitente_estado": "ZHEJIANG",
        "destino_pais": "UY", "dest_ciudad": "MONTEVIDEO",
        "dest_zip": "11800", "dest_estado": "MONTEVIDEO",
        "asegurar_carga": False,
        "bultos": [
            {"cantidad": 1, "unidades_aduana": 20, "peso_kg": 7,
             "largo_cm": 50, "ancho_cm": 36, "alto_cm": 36,
             "valor_unitario_usd": 6, "descripcion_en": "TSHIRTS SAMPLE",
             "hs_code": "6109.10", "pais_origen": "CN"},
            {"cantidad": 1, "unidades_aduana": 10, "peso_kg": 3.5,
             "largo_cm": 36, "ancho_cm": 36, "alto_cm": 28,
             "valor_unitario_usd": 6, "descripcion_en": "TSHIRTS SAMPLE",
             "hs_code": "6109.10", "pais_origen": "CN"},
        ],
    }


def test_portal_sigue_sin_base_y_el_opt_in_conserva_importes_exactos():
    publica = _cotizar()
    assert "_base_interna" not in publica
    assert not any(k.startswith(("costo", "margen", "markup")) for k in publica)

    privada = _cotizar(incluir_base_interna=True)
    base = privada["_base_interna"]
    assert Decimal(base["costo_courier_estimado"]) == Decimal("1016.7")
    assert Decimal(base["tipo_cambio_ars"]) == Decimal("1535")
    assert Decimal(base["costo_courier_estimado_ars"]) == Decimal("1560634.5")
    assert Decimal(base["precio_cliente_inicial_ars"]) == Decimal("1714134")
    assert Decimal(base["margen_tauro_protegido_ars"]) == Decimal("153499.5")
    assert Decimal(base["peso_real_cotizado_kg"]) == Decimal("10.5")
    assert Decimal(base["peso_facturable_cotizado_kg"]) == Decimal("20.2176")


def test_recotizacion_de_reemplazo_congela_la_base_antes_de_emitir(monkeypatch):
    sol = _solicitud_reemplazo()
    base = _cotizar(incluir_base_interna=True)["_base_interna"]
    vistos = []

    def cotizar(*_args, **kwargs):
        assert kwargs["incluir_base_interna"] is True
        return {"opciones": [{
            "id": "dhl", "precio_ars": 1_714_134,
            "precio_usd": 1116.70, "servicio": "Express Worldwide",
            "_base_interna": base,
        }]}

    monkeypatch.setattr("servicios.api_b2b.cotizar_couriers_cliente", cotizar)
    monkeypatch.setattr(
        solicitudes, "_congelar_base_recotizada",
        lambda solicitud, privada: vistos.append((solicitud["id"], privada)) or True,
    )

    assert solicitudes._recotizar_dhl_antes_de_emitir(sol) == {"ok": True}
    assert vistos == [(53, base)]


class _CursorSnapshot:
    def __init__(self):
        self.consultas = []

    def execute(self, sql, params=None):
        self.consultas.append((" ".join(sql.split()), params))


def test_snapshot_de_reemplazo_usa_su_solicitud_y_no_la_anterior():
    sol = _solicitud_reemplazo()
    base = _cotizar(incluir_base_interna=True)["_base_interna"]
    cur = _CursorSnapshot()

    assert solicitudes._congelar_cotizacion_aceptada_con_cursor(
        cur, sol, base_interna=base,
    ) is True

    sql, params = cur.consultas[-1]
    assert "INSERT INTO envio_cotizacion_snapshots" in sql
    assert params[0] == 53
    assert params[0] != sol["reemplaza_solicitud_id"]
    assert Decimal(str(params[7])) == Decimal("1560634.5")
    assert Decimal(str(params[8])) == Decimal("1714134")


def test_despachador_no_contacta_dhl_si_reemplazo_queda_sin_snapshot(monkeypatch):
    sol = _solicitud_reemplazo()
    monkeypatch.setattr(solicitudes, "obtener_solicitud", lambda _id: sol)
    monkeypatch.setattr(solicitudes, "_reemision_tiene_snapshot", lambda _sol: False)
    monkeypatch.setattr(
        solicitudes, "_recotizar_dhl_antes_de_emitir", lambda _sol: {"ok": True},
    )
    emitir = mock.Mock()
    monkeypatch.setattr(solicitudes, "generar_guia_internacional", emitir)

    resultado = solicitudes.generar_guia(53)

    assert resultado["ok"] is False
    assert "base interna" in resultado["error"]
    emitir.assert_not_called()


def test_despachador_emite_reemplazo_despues_de_confirmar_snapshot(monkeypatch):
    sol = _solicitud_reemplazo()
    estados = iter([False, True])
    monkeypatch.setattr(solicitudes, "obtener_solicitud", lambda _id: sol)
    monkeypatch.setattr(
        solicitudes, "_reemision_tiene_snapshot", lambda _sol: next(estados),
    )
    monkeypatch.setattr(
        solicitudes, "_recotizar_dhl_antes_de_emitir", lambda _sol: {"ok": True},
    )
    monkeypatch.setattr(
        "servicios.configuracion_couriers_cliente.estado_integracion",
        lambda _courier: {"operativa": True},
    )
    emitir = mock.Mock(return_value={"ok": True, "tracking": "NEW"})
    monkeypatch.setattr(solicitudes, "generar_guia_internacional", emitir)

    resultado = solicitudes.generar_guia(53)

    assert resultado["ok"] is True
    emitir.assert_called_once_with(53, courier="DHL")
