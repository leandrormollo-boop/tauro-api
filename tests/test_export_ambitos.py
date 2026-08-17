"""El backup no vuelve a juntar operaciones nacionales e internacionales."""
from datetime import datetime
from io import BytesIO

from openpyxl import load_workbook

from servicios import catalogo, cuenta_corriente, export_cliente, solicitudes_guia


def _envio(numero, courier, origen, destino):
    return {
        "id": numero, "created_at": datetime(2026, 8, numero),
        "estado": "GUIA_LISTA", "courier": courier,
        "remitente_pais": origen, "destino_pais": destino,
        "tracking": str(numero), "producto_alias": "CARGA", "cantidad": 1,
        "dest_nombre": "Persona", "dest_ciudad": "Ciudad",
        "peso_kg": 1, "valor_declarado_usd": 10,
        "precio_tauro_ars": 1000, "precio_tauro_usd": 1,
        "observaciones": "",
    }


def test_excel_crea_hojas_de_envios_separadas(monkeypatch):
    monkeypatch.setattr(
        solicitudes_guia, "listar_solicitudes_cliente",
        lambda *_args, **_kwargs: [
            _envio(1, "DHL", "AR", "AR"),
            _envio(2, "OCA", "US", "AR"),
        ],
    )
    monkeypatch.setattr(catalogo, "get_productos", lambda *_a, **_k: [])
    monkeypatch.setattr(cuenta_corriente, "get_facturado_real", lambda *_: 0)
    monkeypatch.setattr(cuenta_corriente, "get_facturas_recientes", lambda *_a, **_k: [])
    monkeypatch.setattr(cuenta_corriente, "get_pagos", lambda *_: [])
    monkeypatch.setattr(cuenta_corriente, "movimientos", lambda *_: [])
    monkeypatch.setattr(
        cuenta_corriente, "resumir_facturacion",
        lambda *_: {"facturado_ars": 0, "pendiente_ars": 0, "total_cargos_ars": 0},
    )
    monkeypatch.setattr(
        cuenta_corriente, "saldo",
        lambda *_a, **_k: {"pagado_ars": 0, "saldo_pendiente_ars": 0},
    )

    libro = load_workbook(BytesIO(export_cliente.generar_excel_cliente("TEST")))

    assert "Envíos" not in libro.sheetnames
    assert "Envios_Nacionales" in libro.sheetnames
    assert "Envios_Internacionales" in libro.sheetnames
    assert libro["Envios_Nacionales"].max_row == 2
    assert libro["Envios_Internacionales"].max_row == 2
    assert any(
        celda.value == "TOTAL FACTURADO"
        for celda in libro["Cuenta_consolidada"]["C"]
    )
