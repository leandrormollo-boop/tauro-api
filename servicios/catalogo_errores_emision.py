"""Catálogo local de errores conocidos durante la emisión de guías.

La clasificación es deliberadamente conservadora. Los códigos y flags del
resultado tienen prioridad; el texto sólo se usa para frases que hoy producen
los servicios de emisión. Un mensaje desconocido nunca se envía a un modelo ni
se convierte en una causa inventada.
"""

from __future__ import annotations

import unicodedata


VERSION_CATALOGO = 1


_DEFINICIONES = (
    {
        "codigo": "TARIFA_GUARDADA_INCONSISTENTE",
        "titulo": "Tarifa guardada sin validar",
        "explicacion": (
            "El sistema no pudo confirmar que la tarifa interna guardada "
            "coincida con el precio aceptado."
        ),
        "accion": (
            "Tauro debe revisar la tarifa y su registro antes de habilitar la emisión."
        ),
        "responsable": "tauro",
        "severidad": "critica",
        "confirmado": True,
        "version": VERSION_CATALOGO,
    },
    {
        "codigo": "RESULTADO_NO_CONFIRMADO",
        "titulo": "Emisión pendiente de verificación",
        "explicacion": "No hay confirmación suficiente del resultado.",
        "accion": (
            "Verificá si el courier llegó a crear la guía antes de autorizar otro intento."
        ),
        "responsable": "operador",
        "severidad": "critica",
        "confirmado": False,
        "version": VERSION_CATALOGO,
    },
    {
        "codigo": "GUIA_EMITIDA_SIN_GUARDAR",
        "titulo": "Guía emitida sin guardar",
        "explicacion": (
            "El courier confirmó la guía, pero el sistema no pudo guardar sus datos."
        ),
        "accion": (
            "Un operador debe recuperar y registrar la guía existente. No autorices otra emisión."
        ),
        "responsable": "operador",
        "severidad": "critica",
        "confirmado": True,
        "version": VERSION_CATALOGO,
    },
    {
        "codigo": "ETIQUETA_PENDIENTE",
        "titulo": "Etiqueta pendiente",
        "explicacion": (
            "La guía existe, pero todavía no hay un PDF válido disponible."
        ),
        "accion": (
            "Un operador debe recuperar o verificar la etiqueta de la guía existente. "
            "No autorices otra emisión."
        ),
        "responsable": "operador",
        "severidad": "critica",
        "confirmado": True,
        "version": VERSION_CATALOGO,
    },
    {
        "codigo": "CARGO_PENDIENTE",
        "titulo": "Cargo pendiente de conciliación",
        "explicacion": (
            "La guía existe, pero el cargo o el reemplazo no quedó cerrado en la cuenta corriente."
        ),
        "accion": (
            "Tauro debe conciliar la guía y el cargo existente antes de continuar."
        ),
        "responsable": "tauro",
        "severidad": "critica",
        "confirmado": True,
        "version": VERSION_CATALOGO,
    },
    {
        "codigo": "DATOS_ENVIO_INVALIDOS",
        "titulo": "Datos del envío inválidos",
        "explicacion": (
            "Un dato requerido del envío no tiene un valor o formato válido."
        ),
        "accion": "Corregí el dato indicado y volvé a validar la solicitud.",
        "responsable": "cliente",
        "severidad": "atencion",
        "confirmado": True,
        "version": VERSION_CATALOGO,
    },
    {
        "codigo": "EMISION_SEGURA_NO_PREPARADA",
        "titulo": "Emisión segura sin preparar",
        "explicacion": (
            "El sistema no pudo guardar los controles previos necesarios para emitir."
        ),
        "accion": (
            "Tauro debe revisar la reserva y la referencia del intento antes de continuar."
        ),
        "responsable": "tauro",
        "severidad": "critica",
        "confirmado": True,
        "version": VERSION_CATALOGO,
    },
    {
        "codigo": "VALORES_INVOICE_INCONSISTENTES",
        "titulo": "Valores de factura inconsistentes",
        "explicacion": (
            "El valor declarado de las cajas no coincide con la mercadería "
            "informada en la factura comercial."
        ),
        "accion": (
            "Revisá el valor por caja, la cantidad de cajas y los totales de los artículos."
        ),
        "responsable": "cliente",
        "severidad": "atencion",
        "confirmado": True,
        "version": VERSION_CATALOGO,
    },
    {
        "codigo": "CIUDAD_CODIGO_POSTAL_INVALIDOS",
        "titulo": "Ciudad o código postal inválido",
        "explicacion": (
            "El courier rechazó la ciudad, el código postal o la combinación de ambos."
        ),
        "accion": "Revisá ciudad, código postal, provincia o estado y país.",
        "responsable": "cliente",
        "severidad": "atencion",
        "confirmado": True,
        "version": VERSION_CATALOGO,
    },
    {
        "codigo": "PRECIO_CAMBIO_ANTES_DE_EMITIR",
        "titulo": "La tarifa cambió",
        "explicacion": (
            "La tarifa vigente es distinta del importe que se había aceptado."
        ),
        "accion": "Revisá el nuevo importe antes de confirmar la emisión.",
        "responsable": "cliente",
        "severidad": "atencion",
        "confirmado": True,
        "version": VERSION_CATALOGO,
    },
    {
        "codigo": "LIMITE_CUENTA_SUPERADO",
        "titulo": "Límite de cuenta superado",
        "explicacion": (
            "La nueva guía llevaría el saldo comprometido por encima del límite de la cuenta."
        ),
        "accion": "Registrá un pago o pedí a Tauro que revise el límite de la cuenta.",
        "responsable": "cliente",
        "severidad": "atencion",
        "confirmado": True,
        "version": VERSION_CATALOGO,
    },
    {
        "codigo": "LIMITE_COURIER_EXCEDIDO",
        "titulo": "Límite del envío excedido",
        "explicacion": (
            "Una cantidad, un peso o la cantidad de artículos supera un límite del courier."
        ),
        "accion": "Revisá el dato indicado y ajustalo al límite informado.",
        "responsable": "cliente",
        "severidad": "atencion",
        "confirmado": True,
        "version": VERSION_CATALOGO,
    },
    {
        "codigo": "AUTORIZACION_EMISION_REQUERIDA",
        "titulo": "Emisión sin autorización",
        "explicacion": (
            "La cuenta o la conexión con el courier no tiene habilitación para emitir."
        ),
        "accion": "Tauro debe revisar la habilitación y los permisos de la cuenta.",
        "responsable": "tauro",
        "severidad": "critica",
        "confirmado": True,
        "version": VERSION_CATALOGO,
    },
    {
        "codigo": "CARRIER_NO_DISPONIBLE",
        "titulo": "Courier no disponible",
        "explicacion": (
            "La conexión del courier no está disponible para completar esta operación."
        ),
        "accion": "Tauro debe revisar la conexión y confirmar cuándo se puede continuar.",
        "responsable": "tauro",
        "severidad": "critica",
        "confirmado": True,
        "version": VERSION_CATALOGO,
    },
    {
        "codigo": "GUIA_YA_EMITIDA",
        "titulo": "La guía ya fue emitida",
        "explicacion": "La solicitud ya tiene una guía asociada.",
        "accion": "Abrí la guía existente y revisá su estado.",
        "responsable": "operador",
        "severidad": "atencion",
        "confirmado": True,
        "version": VERSION_CATALOGO,
    },
    {
        "codigo": "EMISION_EN_CURSO",
        "titulo": "Emisión en curso",
        "explicacion": "Otro intento ya está procesando esta solicitud.",
        "accion": "Actualizá el estado de la solicitud y esperá el resultado del intento actual.",
        "responsable": "operador",
        "severidad": "atencion",
        "confirmado": True,
        "version": VERSION_CATALOGO,
    },
    {
        "codigo": "EMISION_NO_CLASIFICADA",
        "titulo": "Error pendiente de diagnóstico",
        "explicacion": (
            "La evidencia disponible no alcanza para identificar una causa conocida."
        ),
        "accion": (
            "Un operador debe revisar el intento, el estado local y el panel del courier "
            "antes de autorizar otro."
        ),
        "responsable": "operador",
        "severidad": "critica",
        "confirmado": False,
        "version": VERSION_CATALOGO,
    },
)

_POR_CODIGO = {item["codigo"]: item for item in _DEFINICIONES}


def catalogo_errores() -> list[dict]:
    """Devuelve copias del catálogo para que el consumidor no pueda alterarlo."""
    return [dict(item) for item in _DEFINICIONES]


def _ficha(codigo: str) -> dict:
    return dict(_POR_CODIGO[codigo])


def _normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(caracter for caracter in texto if not unicodedata.combining(caracter))
    return " ".join(texto.casefold().split())


def _mensaje(resultado: dict) -> str:
    """Lee sólo campos públicos de texto; no serializa payloads ni objetos."""
    partes = []
    for campo in ("error", "mensaje", "detalle"):
        valor = resultado.get(campo)
        if isinstance(valor, str) and valor.strip():
            partes.append(valor[:4000])
    return _normalizar("\n".join(partes))


def _contiene_todos(texto: str, *fragmentos: str) -> bool:
    return all(_normalizar(fragmento) in texto for fragmento in fragmentos)


def clasificar_error(resultado: dict) -> dict:
    """Clasifica un resultado de emisión sin modificarlo ni inferir causas nuevas."""
    if not isinstance(resultado, dict):
        return _ficha("EMISION_NO_CLASIFICADA")

    # Un resultado marcado exitoso no se convierte en error por conservar un
    # código, flag o mensaje viejo y contradictorio.
    if resultado.get("ok") is True:
        return _ficha("EMISION_NO_CLASIFICADA")

    estado = str(resultado.get("estado") or "").strip().upper()
    # La incertidumbre domina causas que normalmente permitirían corregir y
    # continuar: primero hay que descartar que ya exista una guía real.
    if resultado.get("incierto") is True or estado == "VERIFICAR_COURIER":
        return _ficha("RESULTADO_NO_CONFIRMADO")

    codigo_explicito = resultado.get("codigo_error")
    if isinstance(codigo_explicito, str) and codigo_explicito.strip():
        codigo_explicito = codigo_explicito.strip().upper()
        if codigo_explicito in _POR_CODIGO:
            return _ficha(codigo_explicito)
        # Un código estructurado nuevo prevalece sobre una coincidencia textual:
        # debe incorporarse al catálogo antes de asignarle una causa.
        return _ficha("EMISION_NO_CLASIFICADA")

    if resultado.get("precio_cambio") is True:
        return _ficha("PRECIO_CAMBIO_ANTES_DE_EMITIR")
    if estado == "EMITIENDO":
        return _ficha("EMISION_EN_CURSO")
    if estado == "GUIA_LISTA" and str(resultado.get("tracking") or "").strip():
        return _ficha("GUIA_YA_EMITIDA")

    texto = _mensaje(resultado)
    if not texto:
        return _ficha("EMISION_NO_CLASIFICADA")

    if (
        _contiene_todos(
            texto,
            "el valor declarado de las cajas",
            "no coincide con la mercadería de la factura comercial",
        )
        or _contiene_todos(
            texto,
            "valor declarado: dhl indicó una diferencia con la factura comercial",
            "debe coincidir con la suma",
        )
    ):
        return _ficha("VALORES_INVOICE_INCONSISTENTES")

    if (
        _contiene_todos(
            texto,
            "dhl no encontro la ciudad indicada",
            "revisa ciudad, codigo postal y pais",
        )
        or _contiene_todos(
            texto,
            "dhl no pudo validar la combinacion de ciudad y codigo postal",
            "revisa ciudad, codigo postal y pais",
        )
        or _contiene_todos(
            texto,
            "codigo postal",
            "dhl rechazo el valor o formato enviado",
            "revisa el codigo postal",
        )
        or _contiene_todos(
            texto,
            "codigo postal",
            "dhl no encontro el codigo postal indicado",
            "revisa el codigo postal",
        )
    ):
        return _ficha("CIUDAD_CODIGO_POSTAL_INVALIDOS")

    if (
        texto.startswith("la tarifa dhl cambio de $")
        and "revisa el nuevo importe" in texto
    ):
        return _ficha("PRECIO_CAMBIO_ANTES_DE_EMITIR")

    if _contiene_todos(
        texto,
        "esta guia llevaria el saldo comprometido",
        "por encima del limite de tu cuenta",
    ):
        return _ficha("LIMITE_CUENTA_SUPERADO")

    if (
        (texto.startswith("dhl admite como maximo ") and "bultos por envio" in texto)
        or texto.startswith("las unidades aduaneras deben estar entre ")
        or (texto.startswith("cada bulto dhl debe pesar hasta ") and texto.endswith(" kg."))
        or (texto.startswith("maximo ") and texto.endswith(" items por envio."))
    ):
        return _ficha("LIMITE_COURIER_EXCEDIDO")

    if (
        texto.startswith("tu cuenta no tiene habilitada la emision directa")
        or _contiene_todos(texto, "cuenta dhl:", "rechazo los datos o permisos de la cuenta")
        or texto.startswith("dhl rechazo las credenciales o el acceso productivo")
        or texto == "credenciales ups no configuradas"
        or texto == "falta ups_account_number."
    ):
        return _ficha("AUTORIZACION_EMISION_REQUERIDA")

    if (
        _contiene_todos(texto, "la conexion de dhl no esta disponible", "revisar su configuracion")
        or texto.startswith("oca no esta disponible para crear el envio")
        or texto.startswith("dhl no esta habilitado en produccion")
    ):
        return _ficha("CARRIER_NO_DISPONIBLE")

    if (
        texto.startswith("esa solicitud ya tiene guia emitida")
        or texto.startswith("esta solicitud ya tiene una guia generada")
    ):
        return _ficha("GUIA_YA_EMITIDA")

    if (
        texto.startswith("ya hay una emision en curso para esta solicitud")
        or texto.startswith("la emision anterior se esta verificando con el courier")
    ):
        return _ficha(
            "RESULTADO_NO_CONFIRMADO"
            if "verificando con el courier" in texto
            else "EMISION_EN_CURSO"
        )

    if (
        _contiene_todos(
            texto,
            "la guia se emitio en",
            "(tracking ",
            "pero no pudimos guardarla",
        )
        or _contiene_todos(
            texto,
            "la guia existe (tracking ",
            "pero no pudimos guardarla",
        )
    ):
        return _ficha("GUIA_EMITIDA_SIN_GUARDAR")

    if _contiene_todos(
        texto,
        "la nueva guia existe en",
        "pero la cuenta corriente no pudo cerrar el reemplazo",
    ):
        return _ficha("CARGO_PENDIENTE")

    return _ficha("EMISION_NO_CLASIFICADA")
