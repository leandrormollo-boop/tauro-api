"""El catálogo es opcional en los envíos internacionales del portal."""
from __future__ import annotations

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import servicios.api_b2b as b2b  # noqa: E402


class _ProductoFalso:
    activo = True
    alias_interno = "CAJA-A"
    nombre_invoice = "Cotton shirts"
    hs_code = "6205.30"
    peso_kg = 4.0
    largo_cm = 40.0
    ancho_cm = 30.0
    alto_cm = 20.0
    valor_usd_default = 100.0


def _piezas(bultos, producto=None):
    with mock.patch.object(b2b, "get_producto", return_value=producto):
        return b2b._piezas_del_catalogo("ACME", bultos)


def test_caja_manual_sin_producto_no_se_rechaza():
    bultos = [{
        "producto": "", "cantidad": 2, "unidades_aduana": 2, "peso_kg": 3.5,
        "largo_cm": 40, "ancho_cm": 30, "alto_cm": 20,
        "valor_unitario_usd": 50, "descripcion_en": "Leather shoes",
    }]
    piezas, detalle, error = _piezas(bultos)

    assert error is None
    assert len(piezas) == 2
    assert detalle[0]["peso_kg"] == 3.5
    assert detalle[0]["descripcion_en"] == "Leather shoes"
    assert detalle[0]["cantidad"] == 2


def test_carga_manual_exige_descripcion_aduanera():
    _, _, error = _piezas([{
        "producto": "", "cantidad": 1, "unidades_aduana": 1, "peso_kg": 1.0,
        "largo_cm": 10, "ancho_cm": 10, "alto_cm": 10,
    }])
    assert "descripción" in error


def test_un_alias_que_no_existe_sigue_fallando():
    _, _, error = _piezas([
        {"producto": "NO-EXISTE", "cantidad": 1, "unidades_aduana": 1,
         "peso_kg": 1.0}
    ])
    assert "producto_no_encontrado" in error


def test_lo_cargado_a_mano_pisa_al_catalogo():
    _, detalle, error = _piezas(
        [{"producto": "CAJA-A", "cantidad": 1, "unidades_aduana": 1,
          "peso_kg": 9.9, "largo_cm": 55}],
        producto=_ProductoFalso(),
    )

    assert error is None
    assert detalle[0]["peso_kg"] == 9.9
    assert detalle[0]["largo_cm"] == 55.0
    assert detalle[0]["ancho_cm"] == 30.0


def test_caja_sin_peso_se_rechaza_con_motivo_claro():
    _, _, error = _piezas([{
        "producto": "", "cantidad": 1, "unidades_aduana": 1, "largo_cm": 10,
        "ancho_cm": 10, "alto_cm": 10, "descripcion_en": "Something",
    }])
    assert "cada caja necesita peso" in error


def test_el_tope_de_peso_se_aplica_igual_sin_catalogo():
    _, _, error = _piezas([{
        "producto": "", "cantidad": 1, "unidades_aduana": 1,
        "peso_kg": b2b.MAX_KG_POR_CAJA + 1,
        "largo_cm": 10, "ancho_cm": 10, "alto_cm": 10,
        "descripcion_en": "Heavy parcel",
    }])
    assert "máximo" in error


def test_caja_sin_cantidad_o_unidades_no_recibe_defaults_silenciosos():
    base = {
        "producto": "", "peso_kg": 1, "largo_cm": 10,
        "ancho_cm": 10, "alto_cm": 10, "valor_unitario_usd": 10,
        "descripcion_en": "Leather shoes",
    }
    for cambio in ({"cantidad": 1}, {"unidades_aduana": 1}):
        _, _, error = _piezas([{**base, **cambio}])
        assert "obligatorias" in error
