from copy import deepcopy

import pytest

from servicios.catalogo_errores_emision import catalogo_errores, clasificar_error


def test_catalogo_tiene_contrato_versionado_y_codigos_unicos():
    catalogo = catalogo_errores()

    assert catalogo
    assert len({item["codigo"] for item in catalogo}) == len(catalogo)
    for item in catalogo:
        assert set(item) == {
            "codigo", "titulo", "explicacion", "accion", "responsable",
            "severidad", "confirmado", "version",
        }
        assert item["responsable"] in {"cliente", "tauro", "operador"}
        assert item["severidad"] in {"atencion", "critica"}
        assert type(item["confirmado"]) is bool
        assert item["version"] == 1


def test_catalogo_devuelve_copias_independientes():
    primera = catalogo_errores()
    primera[0]["titulo"] = "alterado"

    assert catalogo_errores()[0]["titulo"] != "alterado"


@pytest.mark.parametrize(
    ("resultado", "codigo"),
    [
        (
            {
                "codigo_error": "TARIFA_GUARDADA_INCONSISTENTE",
                "precio_cambio": True,
                "error": "La tarifa DHL cambió de $ 10 a $ 20.",
            },
            "TARIFA_GUARDADA_INCONSISTENTE",
        ),
        (
            {
                "codigo_error": "RESULTADO_NO_CONFIRMADO",
                "precio_cambio": True,
                "error": "Esa solicitud ya tiene guía emitida.",
            },
            "RESULTADO_NO_CONFIRMADO",
        ),
    ],
)
def test_codigo_estructurado_tiene_prioridad(resultado, codigo):
    assert clasificar_error(resultado)["codigo"] == codigo


@pytest.mark.parametrize("item", catalogo_errores(), ids=lambda item: item["codigo"])
def test_todo_codigo_del_catalogo_se_acepta_como_estructurado(item):
    resultado = {"ok": False, "codigo_error": item["codigo"]}

    assert clasificar_error(resultado)["codigo"] == item["codigo"]


def test_codigo_estructurado_desconocido_no_se_adivina_por_el_mensaje():
    resultado = {
        "codigo_error": "NUEVO_ERROR_COURIER",
        "error": "Esa solicitud ya tiene guía emitida.",
    }

    clasificacion = clasificar_error(resultado)

    assert clasificacion["codigo"] == "EMISION_NO_CLASIFICADA"
    assert clasificacion["confirmado"] is False


def test_resultado_incierto_exige_verificacion_operativa_antes_de_otro_intento():
    clasificacion = clasificar_error({
        "incierto": True,
        "codigo_error": "DATOS_ENVIO_INVALIDOS",
        "precio_cambio": True,
    })

    assert clasificacion["codigo"] == "RESULTADO_NO_CONFIRMADO"
    assert clasificacion["responsable"] == "operador"
    assert clasificacion["severidad"] == "critica"
    assert clasificacion["confirmado"] is False
    assert "antes de autorizar otro intento" in clasificacion["accion"]


def test_flag_de_cambio_de_precio_es_evidencia_suficiente():
    clasificacion = clasificar_error({"ok": False, "precio_cambio": True})

    assert clasificacion["codigo"] == "PRECIO_CAMBIO_ANTES_DE_EMITIR"
    assert clasificacion["responsable"] == "cliente"


@pytest.mark.parametrize(
    ("mensaje", "codigo"),
    [
        (
            "Caja 1 (2 bultos): el valor declarado de las cajas (USD 20.00) "
            "no coincide con la mercadería de la factura comercial (invoice: USD 18.00).",
            "VALORES_INVOICE_INCONSISTENTES",
        ),
        (
            "DHL rechazó la solicitud (HTTP 422).\n"
            "Ciudad del destinatario: DHL no encontró la ciudad indicada. "
            "Revisá ciudad, código postal y país.",
            "CIUDAD_CODIGO_POSTAL_INVALIDOS",
        ),
        (
            "La tarifa DHL cambió de $ 15.000 a $ 17.500. "
            "Revisá el nuevo importe y volvé a emitir; todavía no generamos ni cobramos nada.",
            "PRECIO_CAMBIO_ANTES_DE_EMITIR",
        ),
        (
            "Esta guía llevaría el saldo comprometido a ARS 95.000, por encima "
            "del límite de tu cuenta (ARS 90.000).",
            "LIMITE_CUENTA_SUPERADO",
        ),
        ("DHL admite como máximo 99 bultos por envío.", "LIMITE_COURIER_EXCEDIDO"),
        (
            "Tu cuenta no tiene habilitada la emisión directa. Reenviá la solicitud a Tauro.",
            "AUTORIZACION_EMISION_REQUERIDA",
        ),
        (
            "La conexión de DHL no está disponible. Tauro debe revisar su configuración.",
            "CARRIER_NO_DISPONIBLE",
        ),
        ("Esa solicitud ya tiene guía emitida.", "GUIA_YA_EMITIDA"),
        ("Ya hay una emisión en curso para esta solicitud.", "EMISION_EN_CURSO"),
        (
            "La guía se emitió en DHL (tracking 123456) pero no pudimos guardarla. "
            "Anotá ese número y avisá a soporte: NO vuelvas a generar la guía.",
            "GUIA_EMITIDA_SIN_GUARDAR",
        ),
        (
            "La guía existe (tracking 123456) pero no pudimos guardarla. "
            "No la vuelvas a emitir; corregí el problema y repetí la conciliación.",
            "GUIA_EMITIDA_SIN_GUARDAR",
        ),
        (
            "La nueva guía existe en DHL (tracking 123456), pero la cuenta corriente "
            "no pudo cerrar el reemplazo.",
            "CARGO_PENDIENTE",
        ),
    ],
)
def test_clasifica_mensajes_reales_estrechamente(mensaje, codigo):
    assert clasificar_error({"ok": False, "error": mensaje})["codigo"] == codigo


@pytest.mark.parametrize(
    "resultado",
    [
        {"ok": False, "error": "La factura y la ciudad parecen raras."},
        {"ok": False, "error": "price limit authorization unavailable"},
        {"ok": True, "error": "Esa solicitud ya tiene guía emitida."},
        None,
    ],
)
def test_ambiguo_contradictorio_o_invalido_queda_pendiente(resultado):
    clasificacion = clasificar_error(resultado)

    assert clasificacion["codigo"] == "EMISION_NO_CLASIFICADA"
    assert clasificacion["confirmado"] is False
    assert clasificacion["responsable"] == "operador"
    assert clasificacion["severidad"] == "critica"


def test_estado_y_tracking_confirman_que_la_guia_ya_existe():
    clasificacion = clasificar_error({"estado": "GUIA_LISTA", "tracking": "123456"})

    assert clasificacion["codigo"] == "GUIA_YA_EMITIDA"
    assert clasificacion["confirmado"] is True


@pytest.mark.parametrize(
    "codigo",
    [
        "GUIA_EMITIDA_SIN_GUARDAR",
        "CARGO_PENDIENTE",
        "DATOS_ENVIO_INVALIDOS",
        "EMISION_SEGURA_NO_PREPARADA",
    ],
)
def test_resultado_exitoso_con_codigo_de_error_contradictorio_no_se_clasifica(codigo):
    clasificacion = clasificar_error({"ok": True, "codigo_error": codigo})

    assert clasificacion["codigo"] == "EMISION_NO_CLASIFICADA"
    assert clasificacion["confirmado"] is False


@pytest.mark.parametrize(
    "codigo",
    ["GUIA_EMITIDA_SIN_GUARDAR", "ETIQUETA_PENDIENTE", "CARGO_PENDIENTE"],
)
def test_falla_posterior_a_emision_no_afirma_que_no_se_emitio(codigo):
    clasificacion = clasificar_error({"ok": False, "codigo_error": codigo})
    texto = " ".join((
        clasificacion["titulo"],
        clasificacion["explicacion"],
        clasificacion["accion"],
    )).lower()

    assert clasificacion["confirmado"] is True
    assert "no se emitió" not in texto
    assert "no emitimos" not in texto
    assert (
        "otra emisión" in texto
        or "guía existente" in texto
        or "la guía existe" in texto
    )


def test_etiqueta_pendiente_exige_recuperar_pdf_sin_reemitir():
    clasificacion = clasificar_error({
        "ok": False,
        "codigo_error": "ETIQUETA_PENDIENTE",
    })

    assert clasificacion["confirmado"] is True
    assert clasificacion["responsable"] == "operador"
    assert clasificacion["severidad"] == "critica"
    assert "PDF válido" in clasificacion["explicacion"]
    assert "No autorices otra emisión" in clasificacion["accion"]


def test_no_muta_el_resultado_ni_expone_el_mensaje_crudo():
    resultado = {
        "ok": False,
        "error": "Falla privada: token=secreto",
        "metadata": {"intentos": [1, 2]},
    }
    original = deepcopy(resultado)

    clasificacion = clasificar_error(resultado)

    assert resultado == original
    assert "Falla privada" not in str(clasificacion)
    assert set(clasificacion) == {
        "codigo", "titulo", "explicacion", "accion", "responsable",
        "severidad", "confirmado", "version",
    }
