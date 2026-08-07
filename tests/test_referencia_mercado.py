"""
La referencia de Boxfly: datos reales, anónimos, y sólo para el admin.

Leandro (07/08): "hoy mismo podríamos armar un cotizador con las tarifas de
boxfly, para tener referencia de los precios de fedex". La fuente es el
historial real de TAURO en el portal de Boxfly — 420 envíos pagados.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from servicios.referencia_mercado import comparar, escalones, resumen  # noqa: E402

RUTA_DATOS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "datos", "referencia_boxfly.json")


def test_el_dataset_esta_y_tiene_volumen():
    d = json.load(open(RUTA_DATOS, encoding="utf-8"))
    assert len(d["envios"]) >= 400, "el dataset perdió filas"
    assert "Estados Unidos" in d["escalones_ars"]


def test_sin_datos_personales_en_el_repo():
    """
    Los nombres de los destinatarios NO viajan al repo: son datos personales
    de los clientes de los clientes. Sólo fecha, país y precio.
    """
    d = json.load(open(RUTA_DATOS, encoding="utf-8"))
    claves = set()
    for e in d["envios"]:
        claves |= set(e.keys())
    assert claves <= {"fecha", "pais", "ars"}, f"claves de más: {claves}"


def test_el_escalon_tipico_de_eeuu_es_el_conocido():
    """
    La banda principal a EE.UU. (224 envíos) ronda los ARS 69.000 — coincide
    con la referencia histórica de ~$71.000 por envío liviano.
    """
    bandas = escalones("Estados Unidos")["Estados Unidos"]
    principal = max(bandas, key=lambda e: e["envios"])
    assert 60000 < principal["mediana"] < 75000
    assert principal["envios"] > 150


def test_comparar_ubica_nuestro_precio():
    r = comparar("Estados Unidos", 80000)
    assert r["hay_referencia"]
    assert not r["ganamos"], "80k no le gana a la banda de 63-72k"
    # 60k: su banda más cercana es la principal (63-72k) y queda DEBAJO.
    # (55k habría caído contra el escalón liviano de 40-45k, donde pierde.)
    r2 = comparar("Estados Unidos", 60000)
    assert r2["ganamos"], "60k está debajo de la banda principal de 63-72k"


def test_resumen_convierte_a_usd():
    filas = resumen(dolar=1500.0)
    assert filas and filas[0]["mediana_usd"] is not None


def test_solo_el_admin_la_ve():
    """La pantalla exige auth de admin: es inteligencia competitiva."""
    import inspect

    import endpoints.admin as adm
    fuente = inspect.getsource(adm.admin_referencia)
    assert "_is_auth" in fuente
