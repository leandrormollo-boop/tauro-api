from __future__ import annotations

import copy
import json

import pytest

from servicios import importacion_historica_melcior as imp


def _manifiesto(monkeypatch) -> dict:
    envios = [{
        "source_key": "MELCIOR-2026:ENERO:6",
        "mes": "ENERO",
        "fila_cliente": 6,
        "fila_maestra": 100,
        "fecha": "2026-01-02",
        "remitente": "JUAN PABLO MELCIOR",
        "destinatario": "DESTINATARIO PRUEBA",
        "pais_fuente": "USA",
        "peso_fuente": "2 KG",
        "medidas_fuente": "20X20X20",
        "tracking": "123456789012",
        "tracking_fuente": "123456789012",
        "tracking_pendiente": False,
        "tipo_fuente": "FLETE",
        "estado_portal": "DESPACHADO",
        "genera_deuda": True,
        "requiere_revision": False,
        "importe_inicial_ars": "1000.00",
        "diferencia_ars": "100.00",
        "costo_estimado_ars": "500.00",
        "costo_real_ars": "600.00",
        "costo_real_derivado": False,
        "resolucion": "TRACKING_UNICO_CLIENTE",
    }]
    pagos = [{
        "source_key": f"MELCIOR-2026:PAGO:{fila}",
        "fila_cliente": fila,
        "fecha": "2026-09-02",
        "monto_ars": "1.00",
        "detalle_fuente": "",
        "fecha_original_informada": False,
    } for fila in range(8, 14)]
    manifiesto = {
        "schema_version": 1,
        "cliente_id": "MELCIOR",
        "periodo": 2026,
        "source_sha256": "b" * 64,
        "source_files_sha256": {
            "melcior_2026": "c" * 64,
            "tauro_2026": "d" * 64,
        },
        "duplicados_descartados": [{"tracking": str(i)} for i in range(5)],
        "envios": envios,
        "saldo_pendiente_2025": {
            "source_key": "MELCIOR-2026:SALDO-PENDIENTE-2025",
            "fecha": "2025-12-31",
            "monto_ars": "200.00",
            "concepto": "SALDO PENDIENTE 2025",
        },
        "pagos": pagos,
        "resumen_mensual": {
            "ENERO": {
                "envios": 1,
                "cargos": 1,
                "cancelados": 0,
                "requieren_revision": 0,
                "tracking_pendiente": 0,
                "con_diferencia": 1,
                "importe_inicial_ars": "1000.00",
                "diferencias_ars": "100.00",
            }
        },
        "resumen": {
            "envios": 1,
            "cargos": 1,
            "cancelados": 0,
            "requieren_revision": 0,
            "tracking_pendiente": 0,
            "con_diferencia": 1,
            "importe_inicial_ars": "1000.00",
            "diferencias_ars": "100.00",
            "saldo_pendiente_2025_ars": "200.00",
            "pagos_ars": "6.00",
            "saldo_resultante_ars": "1294.00",
        },
    }
    manifiesto["manifest_sha256"] = imp._manifest_hash(manifiesto)
    monkeypatch.setattr(imp, "EXPECTED_SOURCE_SHA256", "b" * 64)
    monkeypatch.setattr(imp, "EXPECTED_MANIFEST_SHA256", manifiesto["manifest_sha256"])
    return manifiesto


def test_manifiesto_valido_cierra_y_expone_resumen(monkeypatch):
    manifiesto = _manifiesto(monkeypatch)
    contenido = json.dumps(manifiesto).encode()

    validado = imp.leer_manifiesto(contenido)
    resumen = imp.resumen_periodo(validado, "enero")

    assert resumen["envios"] == 1
    assert str(resumen["importe_inicial_ars"]) == "1000.00"
    assert str(resumen["diferencias_ars"]) == "100.00"


def test_manifiesto_modificado_falla_antes_de_escribir(monkeypatch):
    manifiesto = _manifiesto(monkeypatch)
    manifiesto["envios"][0]["importe_inicial_ars"] = "1001.00"

    with pytest.raises(imp.ImportacionHistoricaError, match="modificado"):
        imp.validar_manifiesto(manifiesto)


def test_tracking_duplicado_falla_aunque_los_importes_sean_distintos(monkeypatch):
    manifiesto = _manifiesto(monkeypatch)
    duplicado = copy.deepcopy(manifiesto["envios"][0])
    duplicado["source_key"] = "MELCIOR-2026:ENERO:7"
    duplicado["fila_cliente"] = 7
    manifiesto["envios"].append(duplicado)
    manifiesto["resumen_mensual"]["ENERO"].update({
        "envios": 2, "cargos": 2, "con_diferencia": 2,
        "importe_inicial_ars": "2000.00", "diferencias_ars": "200.00",
    })
    manifiesto["resumen"].update({
        "envios": 2, "cargos": 2, "con_diferencia": 2,
        "importe_inicial_ars": "2000.00", "diferencias_ars": "200.00",
        "saldo_resultante_ars": "2394.00",
    })
    manifiesto["manifest_sha256"] = imp._manifest_hash(manifiesto)
    monkeypatch.setattr(imp, "EXPECTED_MANIFEST_SHA256", manifiesto["manifest_sha256"])

    with pytest.raises(imp.ImportacionHistoricaError, match="Tracking repetido"):
        imp.validar_manifiesto(manifiesto)


def test_cancelado_no_puede_generar_deuda(monkeypatch):
    manifiesto = _manifiesto(monkeypatch)
    fila = manifiesto["envios"][0]
    fila["estado_portal"] = "CANCELADO"
    fila["genera_deuda"] = False
    # Mantener un importe en una fila cancelada debe bloquear el lote.
    manifiesto["resumen_mensual"]["ENERO"].update({
        "cargos": 0, "cancelados": 1, "importe_inicial_ars": "0.00",
    })
    manifiesto["resumen"].update({
        "cargos": 0, "cancelados": 1, "importe_inicial_ars": "0.00",
        "saldo_resultante_ars": "294.00",
    })
    manifiesto["manifest_sha256"] = imp._manifest_hash(manifiesto)
    monkeypatch.setattr(imp, "EXPECTED_MANIFEST_SHA256", manifiesto["manifest_sha256"])

    with pytest.raises(imp.ImportacionHistoricaError, match="cancelado no puede generar saldo"):
        imp.validar_manifiesto(manifiesto)


def test_bultos_historicos_conservan_medidas_y_repeticiones():
    bultos = imp._bultos_historicos(
        "40X40X15 X2 30X20X20", "17 KG"
    )

    assert bultos == [
        {
            "producto_alias": "Mercadería", "cantidad": 2,
            "largo_cm": 40.0, "ancho_cm": 40.0, "alto_cm": 15.0,
            "peso_kg": None,
        },
        {
            "producto_alias": "Mercadería", "cantidad": 1,
            "largo_cm": 30.0, "ancho_cm": 20.0, "alto_cm": 20.0,
            "peso_kg": None,
        },
    ]


def test_bulto_unico_recibe_el_peso_total_informado():
    assert imp._bultos_historicos("20X20X20", "2 KG") == [{
        "producto_alias": "Mercadería", "cantidad": 1,
        "largo_cm": 20.0, "ancho_cm": 20.0, "alto_cm": 20.0,
        "peso_kg": 2.0,
    }]
