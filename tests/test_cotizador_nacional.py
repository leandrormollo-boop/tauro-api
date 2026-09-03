"""Contrato del cotizador nacional provincia a provincia.

Estas pruebas no llaman a OCA, Andreani ni a ningún courier internacional.
La primera etapa sólo normaliza la ruta que consumirán los adapters directos
cuando existan credenciales y contratos reales.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from endpoints import portal_cliente as portal
from servicios.cotizador_nacional import preparar_cotizacion_nacional
from servicios.provincias import (
    PROVINCIAS,
    descomponer_codigo_postal,
    normalizar_codigo_postal,
    normalizar_localidad,
    normalizar_provincia,
    opciones,
)


RAIZ = Path(__file__).resolve().parent.parent


def test_catalogo_incluye_las_24_jurisdicciones_sin_codigos_duplicados():
    assert len(PROVINCIAS) == 24
    assert len(set(PROVINCIAS)) == 24
    assert len(set(PROVINCIAS.values())) == 24
    assert opciones() == sorted(PROVINCIAS.items(), key=lambda item: item[1])


@pytest.mark.parametrize("codigo,nombre", PROVINCIAS.items())
def test_cada_provincia_acepta_codigo_iso_subdivision_y_nombre(codigo, nombre):
    assert normalizar_provincia(codigo) == codigo
    assert normalizar_provincia(f"AR-{codigo}") == codigo
    assert normalizar_provincia(nombre) == codigo
    assert normalizar_provincia(nombre.lower()) == codigo


@pytest.mark.parametrize(
    "valor,esperado",
    [
        ("CABA", "C"),
        ("Capital Federal", "C"),
        ("Ciudad de Buenos Aires", "C"),
        ("Provincia de Buenos Aires", "B"),
        ("Tierra del Fuego, Antártida e Islas del Atlántico Sur", "V"),
    ],
)
def test_aliases_habituales_no_dependen_de_tildes_o_puntuacion(valor, esperado):
    assert normalizar_provincia(valor) == esperado


def test_cualquier_provincia_puede_ser_origen_y_destino():
    for origen in PROVINCIAS:
        for destino in PROVINCIAS:
            resultado = preparar_cotizacion_nacional(
                origen_provincia=origen,
                origen_localidad="Localidad origen",
                origen_cp="1000",
                modalidad_origen="domicilio",
                destino_provincia=destino,
                destino_localidad="Localidad destino",
                destino_cp="2000",
                modalidad_destino="domicilio",
                cantidad_bultos=1,
                peso_kg=5.5,
                largo_cm=30,
                ancho_cm=20,
                alto_cm=10,
                valor_declarado_ars=10000,
            )
            assert resultado["origen"]["provincia_codigo"] == origen
            assert resultado["destino"]["provincia_codigo"] == destino


def test_ruta_dentro_de_la_misma_provincia_tambien_es_valida():
    resultado = preparar_cotizacion_nacional(
        origen_provincia="Santa Fe",
        origen_localidad="Rosario",
        origen_cp="2000",
        modalidad_origen="domicilio",
        destino_provincia="S",
        destino_localidad="Rafaela",
        destino_cp="2300",
        modalidad_destino="sucursal",
        cantidad_bultos=1,
        peso_kg=1.2,
        largo_cm=12,
        ancho_cm=33,
        alto_cm=36,
        valor_declarado_ars=40000,
    )

    assert resultado["ruta"] == "Santa Fe → Santa Fe"
    assert resultado["origen"]["localidad_input"] == "Rosario"
    assert resultado["destino"]["localidad_input"] == "Rafaela"


@pytest.mark.parametrize(
    "valor,provincia,esperado",
    [
        ("1425", "C", "1425"),
        ("c 1425 abc", "Capital Federal", "C1425ABC"),
        ("S2000ABC", "Santa Fe", "S2000ABC"),
    ],
)
def test_cp_tradicional_y_cpa_se_normalizan(valor, provincia, esperado):
    assert normalizar_codigo_postal(valor, provincia) == esperado


@pytest.mark.parametrize(
    "valor,provincia",
    [
        ("C1425", "Mendoza"),
        ("C1425", "CABA"),
        ("M-5500", "Mendoza"),
        ("M5500ABC", "CABA"),
        ("123", "Buenos Aires"),
        ("12345", "Buenos Aires"),
        ("B1234AB", "Buenos Aires"),
        ("<script>", "Buenos Aires"),
    ],
)
def test_cpa_incompatible_o_invalido_se_rechaza(valor, provincia):
    assert normalizar_codigo_postal(valor, provincia) == ""


def test_cpa_completo_deriva_cp4_sin_perder_el_dato_original():
    assert descomponer_codigo_postal("c 1425 abc", "CABA") == {
        "cp_input": "C1425ABC",
        "cp4": "1425",
        "cpa8": "C1425ABC",
    }
    assert descomponer_codigo_postal("5500", "Mendoza") == {
        "cp_input": "5500",
        "cp4": "5500",
        "cpa8": None,
    }


def test_localidad_limpia_espacios_y_rechaza_texto_no_valido():
    assert normalizar_localidad("  San   Miguel de Tucumán ") == "San Miguel de Tucumán"
    assert normalizar_localidad("A") == ""
    assert normalizar_localidad("<b>Rosario</b>") == ""


def test_resultado_es_preparacion_sin_precio_ni_accion_falsa():
    resultado = preparar_cotizacion_nacional(
        origen_provincia="Córdoba",
        origen_localidad="Córdoba",
        origen_cp="X5000ABC",
        modalidad_origen="sucursal",
        destino_provincia="Neuquén",
        destino_localidad="Neuquén",
        destino_cp="Q8300ABC",
        modalidad_destino="domicilio",
        cantidad_bultos="2",
        peso_kg="5,5",
        largo_cm="30,5",
        ancho_cm=20,
        alto_cm=10,
        valor_declarado_ars="100.000",
    )

    assert resultado["listo"] is True
    assert resultado["encontrado"] is False
    assert resultado["motivo"] == "integraciones_pendientes"
    assert {c["id"] for c in resultado["carriers"]} == {"oca", "andreani"}
    assert all(c["estado"] == "integracion_pendiente" for c in resultado["carriers"])
    assert not any("precio" in c or "tarifa" in c for c in resultado["carriers"])
    assert resultado["origen"]["cp4"] == "5000"
    assert resultado["destino"]["cp4"] == "8300"
    assert resultado["modalidad"] == {"origen": "sucursal", "destino": "domicilio"}
    assert resultado["bultos"][0]["peso_unitario_kg"] == "5.5"
    assert resultado["totales"] == {
        "cantidad_bultos": 2,
        "peso_kg": "11",
        "volumen_cm3": "12200",
        "volumen_m3": "0.0122",
        "valor_declarado_ars": "100000",
        "peso_real_kg": "11",
        "peso_volumetrico_kg": "3.05",
        "peso_facturable_kg": "11",
        "cobra_por_volumen": False,
    }


@pytest.mark.parametrize("peso", ["5,5", "5.5"])
@pytest.mark.parametrize("valor", ["100.000", "100,000"])
def test_nacional_acepta_formatos_humanos_equivalentes(peso, valor):
    resultado = preparar_cotizacion_nacional(
        origen_provincia="B",
        origen_localidad="La Plata",
        origen_cp="1900",
        modalidad_origen="domicilio",
        destino_provincia="T",
        destino_localidad="San Miguel de Tucumán",
        destino_cp="4000",
        modalidad_destino="domicilio",
        cantidad_bultos="1",
        peso_kg=peso,
        largo_cm="30",
        ancho_cm="20",
        alto_cm="10",
        valor_declarado_ars=valor,
    )

    assert resultado["bultos"][0]["peso_unitario_kg"] == "5.5"
    assert resultado["totales"]["valor_declarado_ars"] == "100000"


@pytest.mark.parametrize(
    "campo,valor,mensaje",
    [
        ("peso_kg", 0, "peso"),
        ("peso_kg", float("nan"), "peso"),
        ("largo_cm", 301, "largo"),
        ("ancho_cm", -1, "ancho"),
        ("alto_cm", float("inf"), "alto"),
    ],
)
def test_paquete_invalido_falla_antes_de_cualquier_adapter(campo, valor, mensaje):
    datos = {
        "origen_provincia": "C",
        "origen_localidad": "Buenos Aires",
        "origen_cp": "1000",
        "modalidad_origen": "domicilio",
        "destino_provincia": "M",
        "destino_localidad": "Mendoza",
        "destino_cp": "5500",
        "modalidad_destino": "domicilio",
        "cantidad_bultos": 1,
        "peso_kg": 1,
        "largo_cm": 10,
        "ancho_cm": 10,
        "alto_cm": 10,
        "valor_declarado_ars": 10000,
    }
    datos[campo] = valor

    with pytest.raises(ValueError, match=mensaje):
        preparar_cotizacion_nacional(**datos)


def test_valor_declarado_no_se_redondea_para_hacerlo_entrar_en_oca():
    datos = {
        "origen_provincia": "C",
        "origen_localidad": "Buenos Aires",
        "origen_cp": "1000",
        "modalidad_origen": "domicilio",
        "destino_provincia": "M",
        "destino_localidad": "Mendoza",
        "destino_cp": "5500",
        "modalidad_destino": "domicilio",
        "cantidad_bultos": 1,
        "peso_kg": "1,2",
        "largo_cm": 12,
        "ancho_cm": 33,
        "alto_cm": 36,
        "valor_declarado_ars": "40.000,50",
    }

    with pytest.raises(ValueError, match="pesos enteros"):
        preparar_cotizacion_nacional(**datos)


def test_post_nacional_acepta_coma_y_no_llama_carriers_internacionales(monkeypatch):
    def no_llamar(**_datos):
        raise AssertionError("una ruta nacional no debe tocar DHL/FedEx/UPS")

    def respuesta_falsa(*, request, name, context, status_code=200, **_kwargs):
        return SimpleNamespace(status_code=status_code, template=name, context=context)

    monkeypatch.setattr(portal, "cotizar_referencia_couriers", no_llamar)
    monkeypatch.setattr(portal.templates, "TemplateResponse", respuesta_falsa)

    respuesta = portal.cotizar_post(
        request=SimpleNamespace(),
        ambito="nacional",
        origen_pais="AR",
        destino_pais="AR",
        origen_provincia="Santa Fe",
        origen_localidad="Rosario",
        origen_cp="2000",
        modalidad_origen="domicilio",
        destino_provincia="Mendoza",
        destino_localidad="Mendoza",
        destino_cp="5500",
        modalidad_destino="sucursal",
        cantidad_bultos="2",
        peso_kg="5,5",
        largo_cm="30,5",
        ancho_cm="20",
        alto_cm="10",
        valor_declarado_ars="100.000",
        cliente="CLIENTE_TEST",
    )

    assert respuesta.status_code == 200
    assert respuesta.template == "portal/cotizar.html"
    assert respuesta.context["error"] is None
    assert respuesta.context["resultado_nacional"]["bultos"][0]["peso_unitario_kg"] == "5.5"
    assert respuesta.context["resultado_nacional"]["totales"]["peso_kg"] == "11"
    assert respuesta.context["resultado_nacional"]["ruta"] == "Santa Fe → Mendoza"


def test_post_nacional_deriva_argentina_si_un_form_viejo_no_manda_paises(monkeypatch):
    monkeypatch.setattr(
        portal.templates,
        "TemplateResponse",
        lambda *, context, status_code=200, **_kw: SimpleNamespace(
            status_code=status_code, context=context,
        ),
    )

    respuesta = portal.cotizar_post(
        request=SimpleNamespace(),
        ambito="nacional",
        origen_pais="",
        destino_pais="",
        origen_provincia="B",
        origen_localidad="La Plata",
        origen_cp="1900",
        modalidad_origen="domicilio",
        destino_provincia="T",
        destino_localidad="San Miguel de Tucumán",
        destino_cp="4000",
        modalidad_destino="domicilio",
        cantidad_bultos="1",
        peso_kg="1",
        largo_cm="10",
        ancho_cm="10",
        alto_cm="10",
        valor_declarado_ars="10000",
        cliente="CLIENTE_TEST",
    )

    assert respuesta.context["error"] is None
    assert respuesta.context["resultado_nacional"]["origen"]["pais"] == "AR"
    assert respuesta.context["resultado_nacional"]["destino"]["pais"] == "AR"


def test_post_nacional_rechaza_un_pais_forzado(monkeypatch):
    monkeypatch.setattr(
        portal.templates,
        "TemplateResponse",
        lambda *, context, status_code=200, **_kw: SimpleNamespace(
            status_code=status_code, context=context,
        ),
    )

    respuesta = portal.cotizar_post(
        request=SimpleNamespace(),
        ambito="nacional",
        origen_pais="US",
        destino_pais="AR",
        origen_provincia="C",
        origen_localidad="Buenos Aires",
        origen_cp="1000",
        modalidad_origen="domicilio",
        destino_provincia="M",
        destino_localidad="Mendoza",
        destino_cp="5500",
        modalidad_destino="domicilio",
        cantidad_bultos="1",
        peso_kg="1",
        largo_cm="10",
        ancho_cm="10",
        alto_cm="10",
        valor_declarado_ars="10000",
        cliente="CLIENTE_TEST",
    )

    assert "dentro de Argentina" in respuesta.context["error"]
    assert respuesta.context["resultado_nacional"] is None


def test_post_nacional_con_error_preserva_todos_los_campos(monkeypatch):
    monkeypatch.setattr(
        portal.templates,
        "TemplateResponse",
        lambda *, context, status_code=200, **_kw: SimpleNamespace(
            status_code=status_code, context=context,
        ),
    )

    respuesta = portal.cotizar_post(
        request=SimpleNamespace(),
        ambito="nacional",
        origen_pais="AR",
        destino_pais="AR",
        origen_provincia="C",
        origen_localidad="Buenos Aires",
        origen_cp="C1425ABC",
        modalidad_origen="domicilio",
        destino_provincia="M",
        destino_localidad="Mendoza",
        destino_cp="C1425ABC",
        modalidad_destino="domicilio",
        cantidad_bultos="1",
        peso_kg="5,5",
        largo_cm="30",
        ancho_cm="20",
        alto_cm="10",
        valor_declarado_ars="10000",
        cliente="CLIENTE_TEST",
    )

    assert "esa provincia" in respuesta.context["error"]
    assert respuesta.context["form"]["destino_provincia"] == "M"
    assert respuesta.context["form"]["destino_cp"] == "C1425ABC"
    assert respuesta.context["form"]["peso_kg"] == "5,5"


def test_template_nacional_envia_ruta_argentina_completa_y_no_simula_tarifas():
    html = (RAIZ / "templates" / "portal" / "_cotizador_nacional.html").read_text(
        encoding="utf-8",
    )

    assert 'name="ambito" value="nacional"' in html
    assert 'name="origen_pais" value="AR"' in html
    assert 'name="destino_pais" value="AR"' in html
    for campo in (
        "origen_provincia", "origen_localidad", "origen_cp",
        "modalidad_origen", "destino_provincia", "destino_localidad",
        "destino_cp", "modalidad_destino", "cantidad_bultos", "peso_kg",
        "largo_cm", "ancho_cm", "alto_cm", "valor_declarado_ars",
    ):
        assert f'name="{campo}"' in html
    assert html.count("data-searchable") == 2
    assert html.count('data-search-placeholder="Buscar provincia o código"') == 2
    assert "Preparar cotización" in html
    assert "APIs pendientes" in html
    assert "No cotizamos ni cobramos nada todavía" in html
    assert "Elegir OCA" not in html
    assert "Elegir Andreani" not in html
    assert "$" not in html
