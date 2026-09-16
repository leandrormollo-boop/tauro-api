"""Errores públicos de MyDHL: causa, campo y corrección, sin volcar el body.

El esquema oficial usa detail y additionalDetails (strings). Se toleran
también objetos de error. No se deducen causas a partir de un código solo:
los códigos y HTTP sirven para soporte, el texto/campo determinan la causa.
"""

import re


def _codigo(texto):
    encontrado = re.match(r"\s*(\d{3,6})\s*:", texto)
    return encontrado.group(1) if encontrado else ""


def _campo(texto):
    t = texto.lower()
    roles = [
        (r"shipper|sender|origin", "remitente"),
        (r"receiver|recipient|consignee|destination", "destinatario"),
        (r"seller", "vendedor"), (r"buyer", "comprador"),
        (r"importer", "importador"), (r"exporter", "exportador"),
        (r"payer", "pagador"),
    ]
    rol = next((nombre for patron, nombre in roles if re.search(patron, t)), "")
    sufijo = f" del {rol}" if rol else ""
    for patron, nombre, accion in (
        (r"cityname|\bcity\b", "Ciudad", "Revisá ciudad, código postal y país"),
        (r"postalcode|postal.code|post.code|\bzip\b", "Código postal", "Revisá el código postal y su correspondencia con la ciudad y el país"),
        (r"countrycode|country.code", "País", "Seleccioná el país correcto"),
        (r"province|statecode|state.name|\bstate\b", "Provincia o estado", "Revisá la provincia o el estado y su código"),
        (r"addressline[123]|street", "Dirección", "Revisá la calle, el número y la longitud de la dirección"),
        (r"phonenumber|phone|telephone", "Teléfono", "Completá un teléfono válido con código de país"),
        (r"email", "Email", "Completá un email válido"),
        (r"registrationnumbers|tax.?id|vat.?number", "Identificación fiscal", "Revisá el documento fiscal y el tipo exigido para ese país"),
        (r"fullname|companyname", "Nombre o razón social", "Revisá el nombre o la razón social"),
        (r"commoditycodes|hscode|hs.code|tariffcode", "HS code", "Revisá el código arancelario de la mercadería"),
        (r"declaredvalue|declared.value", "Valor declarado", "Revisá el valor declarado y los importes de la factura comercial"),
        (r"description", "Descripción de la mercadería", "Completá la descripción detallada del artículo"),
        (r"quantity", "Cantidad de unidades", "Revisá las unidades comerciales del artículo"),
        (r"\bprice\b|unitprice", "Valor unitario", "Revisá el valor unitario del artículo en la factura comercial"),
        (r"weight", "Peso", "Revisá el peso declarado en kg"),
        (r"dimensions|\b(?:length|width|height)\b", "Medidas", "Revisá largo, ancho y alto en centímetros"),
        (r"plannedshipping|pickupdate|pick.up.date", "Fecha de envío o retiro", "Revisá la fecha, la hora y el huso del origen"),
    ):
        if re.search(patron, t):
            # Los índices de las rutas JSON de DHL comienzan en cero.
            pieza = re.search(r"packages/(\d{1,3})(?:/|\b)", t)
            item = re.search(r"lineitems/(\d{1,3})(?:/|\b)", t)
            ubicacion = f" · Bulto {int(pieza[1]) + 1}" if pieza else ""
            if item:
                ubicacion += f" · Artículo {int(item[1]) + 1} de la factura comercial"
            return nombre + sufijo + ubicacion, accion + sufijo + "."
    return None


def _traducir(texto):
    t = texto.lower()
    if re.search(r"account(number)?|credential|authentication|authorization", t):
        return ("Cuenta DHL: DHL rechazó los datos o permisos de la cuenta. "
                "Tauro debe revisar la habilitación con DHL; no cambies los datos de la mercadería.")
    if ("declared" in t and re.search(r"sum|total|match|equal", t)
            and re.search(r"invoice|line.?items|customs|exportdeclaration", t)):
        return ("Valor declarado: DHL indicó una diferencia con la factura comercial. "
                "El total de las cajas debe coincidir con la suma de unidades × valor unitario de los artículos.")
    if re.search(r"special service|servicecode|productcode|requested product|no.*products?.*available", t):
        return ("Servicio DHL: DHL rechazó el servicio solicitado para esta operación. "
                "Volvé a cotizar la ruta y los servicios adicionales; si persiste, pedí ayuda a Tauro.")
    campo = _campo(texto)
    if not campo:
        if "7120:" in t and ("documentimages" in t or "typecode=invoice" in t):
            return ("Factura comercial: DHL exige la factura o su generación en la solicitud. "
                    "Tauro debe revisar el envío de ese documento.")
        return None
    nombre, accion = campo
    longitud = re.search(r"(?:expected\s+)?(max|min)length\s*:?\s*(\d{1,4})", t)
    rango = re.search(r"expected\s+(minimum|maximum|exclusiveminimum|exclusivemaximum)\s*:\s*(-?\d{1,7}(?:\.\d{1,4})?)", t)
    if longitud:
        limite = "máximo" if longitud[1] == "max" else "mínimo"
        motivo = f"DHL exige un {limite} de {longitud[2]} caracteres."
    elif rango:
        operador = {"minimum": "mayor o igual a", "maximum": "menor o igual a",
                    "exclusiveminimum": "mayor a", "exclusivemaximum": "menor a"}[rango[1]]
        motivo = f"DHL exige un valor {operador} {rango[2]}."
    elif re.search(r"required|mandatory|missing|must.*present", t):
        motivo = "Falta un dato obligatorio para DHL."
    elif re.search(r"not found|cannot find|could not find|not exist|unknown city", t):
        dato = ("la ciudad indicada" if nombre.startswith("Ciudad") else
                "el código postal indicado" if nombre.startswith("Código postal") else
                "el dato indicado")
        motivo = f"DHL no encontró {dato}."
    elif "city" in t and re.search(r"postal|zip", t):
        motivo = "DHL no pudo validar la combinación de ciudad y código postal."
    else:
        motivo = "DHL rechazó el valor o formato enviado."
    return f"{nombre}: {motivo} {accion}"


def error_dhl_publico(resp):
    """Texto apto para portal, admin y API; nunca devuelve payloads ni secretos."""
    status = getattr(resp, "status_code", None)
    http = str(status) if isinstance(status, int) else "desconocido"
    if status in (401, 403):
        return (f"DHL rechazó las credenciales o el acceso productivo (HTTP {http}). "
                "Tauro debe revisar la conexión y los permisos de su cuenta DHL.")
    if status == 429:
        return ("DHL alcanzó el límite de consultas (HTTP 429). "
                "Esperá unos minutos antes de volver a intentar.")
    try:
        data = resp.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    detalles = data.get("additionalDetails") or []
    if isinstance(detalles, (str, dict)):
        detalles = [detalles]
    if not isinstance(detalles, (list, tuple)):
        detalles = []
    # El detail puede ser otra causa además de los detalles, o sólo un título.
    base = data.get("detail") or data.get("message") or data.get("title")
    detalles = list(detalles)
    titulos_genericos = {
        "multiple problems found, see additional details",
        "multiple problems found, see additionaldetails",
        "validation error", "bad request", "unprocessable entity",
    }
    if isinstance(base, str) and base.strip().rstrip(".").lower() not in titulos_genericos:
        detalles.append(base)
    mensajes, desconocidos = [], []
    codigo_base = data.get("code") or data.get("errorCode")
    if re.fullmatch(r"\d{3,6}", str(codigo_base or "")):
        desconocidos.append(str(codigo_base))
    hay_desconocidos = False
    for detalle in detalles[:30]:
        codigo = ""
        if isinstance(detalle, dict):
            valor = detalle.get("code") or detalle.get("errorCode")
            codigo = str(valor) if re.fullmatch(r"\d{3,6}", str(valor or "")) else ""
            detalle = " ".join(str(detalle[k]) for k in ("path", "field", "message", "detail", "description")
                               if isinstance(detalle.get(k), str))
        if not isinstance(detalle, str):
            continue
        detalle = detalle[:4000]
        codigo = codigo or _codigo(detalle)
        mensaje = _traducir(detalle)
        if mensaje:
            if codigo:
                mensaje += f" (DHL {codigo})"
            if mensaje not in mensajes:
                mensajes.append(mensaje)
        else:
            hay_desconocidos = True
            if codigo and codigo not in desconocidos:
                desconocidos.append(codigo)
    if not mensajes or hay_desconocidos:
        referencia = f" Códigos DHL: {', '.join(desconocidos[:5])}." if desconocidos else ""
        mensajes.append("DHL rechazó la solicitud sin un motivo que el portal pueda interpretar."
                        + referencia + " Pedí a Tauro que revise el rechazo; no cambies datos al azar.")
    if len(mensajes) > 8 or len(detalles) > 30:
        mensajes = mensajes[:8] + ["DHL informó más observaciones. Corregí las indicadas y solicitá una nueva validación."]
    codigo_encabezado = f" · DHL {codigo_base}" if desconocidos and str(codigo_base) in desconocidos else ""
    return f"DHL rechazó la solicitud (HTTP {http}{codigo_encabezado}).\n" + "\n".join(mensajes)
