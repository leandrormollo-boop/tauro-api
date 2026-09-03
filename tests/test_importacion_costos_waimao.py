from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

from servicios.importacion_costos_waimao import (
    ImportacionCostoWaimaoError,
    leer_costos_iniciales_waimao,
)


HEADERS = [
    "EMPRESA", "COBRO CLIENTE", "MES", "FECHA", "REMITENTE",
    "DESTINATARIO", "PAIS", "PESO", "MEDIDAS", "TRACKING",
    "FLETE O TAX", "NRO FC", "FACTURADO", "DIF inicial vs fc",
    "SALDO ARS", "COSTOINICIAL", "RENTABFINAL", "RENTABinicial",
    "ESTADO", "COSTO ESTIMADO",
]


def _libro(tmp_path, filas):
    ruta = tmp_path / "tauro.xlsx"
    libro = openpyxl.Workbook()
    hoja = libro.active
    hoja.title = "ENVIOS 2026"
    hoja.append(HEADERS)
    for fila in filas:
        hoja.append([fila.get(h) for h in HEADERS])
    libro.save(ruta)
    return ruta


def test_lee_solo_flete_con_costoinicial_sin_confundir_saldo_ars(tmp_path):
    ruta = _libro(tmp_path, [
        {
            "EMPRESA": "DHL", "REMITENTE": "WAIMAO", "TRACKING": 123456,
            "FLETE O TAX": "FLETE", "FACTURADO": 100,
            "SALDO ARS": 95, "COSTOINICIAL": 80,
        },
        {
            "EMPRESA": "DHL", "REMITENTE": "WAIMAO", "TRACKING": "999",
            "FLETE O TAX": "FLETE", "FACTURADO": 100,
            "SALDO ARS": 70, "COSTOINICIAL": None,
        },
        {
            "EMPRESA": "DHL", "REMITENTE": "WAIMAO", "TRACKING": "123 456",
            "FLETE O TAX": "TAX", "FACTURADO": 10,
            "SALDO ARS": 10, "COSTOINICIAL": 10,
        },
        {
            "EMPRESA": "DHL", "REMITENTE": "OTRO", "TRACKING": "777",
            "FLETE O TAX": "FLETE", "FACTURADO": 100,
            "SALDO ARS": 70, "COSTOINICIAL": 60,
        },
    ])

    resultado = leer_costos_iniciales_waimao(ruta)

    assert len(resultado["evidencias"]) == 1
    assert resultado["evidencias"][0]["tracking"] == "123456"
    assert resultado["evidencias"][0]["costo_inicial_ars"] == Decimal("80.0000")
    assert {x["motivo"] for x in resultado["omitidas"]} == {
        "SIN_COSTO_INICIAL", "NO_ES_FLETE"
    }


def test_rechaza_dos_costos_iniciales_distintos_para_un_tracking(tmp_path):
    ruta = _libro(tmp_path, [
        {
            "EMPRESA": "DHL", "REMITENTE": "WAIMAO", "TRACKING": "123",
            "FLETE O TAX": "FLETE", "FACTURADO": 100,
            "SALDO ARS": 90, "COSTOINICIAL": 80,
        },
        {
            "EMPRESA": "DHL", "REMITENTE": "WAIMAO", "TRACKING": "123",
            "FLETE O TAX": "FLETE", "FACTURADO": 100,
            "SALDO ARS": 91, "COSTOINICIAL": 81,
        },
    ])

    with pytest.raises(ImportacionCostoWaimaoError, match="incompatibles"):
        leer_costos_iniciales_waimao(ruta)


def test_script_exige_flag_explicito_para_escribir():
    texto = (Path(__file__).resolve().parents[1] / "scripts" / "importar_costos_waimao_2026.py").read_text()
    assert '"--aplicar"' in texto
    assert "planificar_importacion_costos_waimao" in texto
    assert "IMPORT_SHEET_2026" not in texto
