# ────────────────────────────────────────────────────────────
# Parser de pedidos por email — Tauro Solutions
# ────────────────────────────────────────────────────────────
# Los clientes B2B mandan pedidos por email en texto semi-estructurado
# en español. Este módulo los convierte en un dict JSON-friendly para
# precargar el formulario de solicitud de envío.
#
# Determinista, SOLO stdlib (re + unicodedata). Sin APIs externas.
# Compatible Python 3.9 (nada de match/case ni sintaxis 3.10+).
#
# API pública:
#   parsear_pedido(texto: str) -> dict
#
# Nunca lanza excepción con input basura: devuelve lo que pudo
# (campos no encontrados = None, paquetes = [], confianza = 0.0).

import re
import unicodedata


# ────────────────────────────────────────────────────────────
# Regex compiladas (todas case-insensitive donde corresponde)
# ────────────────────────────────────────────────────────────

# Headers de sección remitente / destinatario (con o sin tilde)
RE_HEADER_REMITENTE = re.compile(
    r"^\s*(?:qui[eé]n\s+env[ií]a|remitente)\b[:\s]*$", re.IGNORECASE
)
RE_HEADER_DESTINATARIO = re.compile(
    r"^\s*(?:qui[eé]n\s+recibe|destinatario)\b[:\s]*$", re.IGNORECASE
)

# Cantidad de bultos: "1 cajas", "2 caja", "3 bultos", "1 box"
RE_CANTIDAD = re.compile(
    r"^\s*(\d+)\s*(?:cajas?|bultos?|box(?:es)?|paquetes?|cajones?|pallets?)\b",
    re.IGNORECASE,
)

# Dimensiones: "40x40x15cm", "40 x 40 x 15 cm", "40x40x15" (asume cm)
RE_DIMS = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*[xX×]\s*(\d+(?:[.,]\d+)?)\s*[xX×]\s*(\d+(?:[.,]\d+)?)\s*(?:cm)?",
    re.IGNORECASE,
)

# Peso: "Peso 5kg c/u", "peso: 5,5 kg" (etiquetado) / "5 kg" (suelto)
RE_PESO_ETIQUETADO = re.compile(
    r"\bpeso\b[:\s]*(\d+(?:[.,]\d+)?)\s*kg", re.IGNORECASE
)
RE_PESO_SUELTO = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*kg\b", re.IGNORECASE)

# HS code: "HS 5105290000", "HS: 5105.29", "hs code 5105290000"
RE_HS = re.compile(r"\bhs\b\s*(?:code)?\s*:?\s*([\d][\d.]*)", re.IGNORECASE)

# Valor unitario: "Precio USD 20c/u", "USD 20", "u$s 20", "valor 20 usd"
RE_VALOR_MONEDA_PRIMERO = re.compile(
    r"(?:usd|u\$s|us\$|u\$d)\s*:?\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE
)
RE_VALOR_MONEDA_DESPUES = re.compile(
    r"(?:precio|valor)\s*:?\s*(\d+(?:[.,]\d+)?)\s*(?:usd|u\$s|us\$|d[oó]lares)",
    re.IGNORECASE,
)

# Documento: CUIT "20-23780019-3" / "20237800193" o DNI de 7-8 dígitos
RE_CUIT = re.compile(r"^\s*(\d{2}-\d{8}-\d)\s*$")
RE_DOC_SOLO_DIGITOS = re.compile(r"^\s*(\d{7,11})\s*$")

# Código postal argentino: "Cp 1428", "CP: C1043AAZ"
RE_CP_LINEA = re.compile(
    r"^\s*c\.?\s*p\.?\b[:\s.]*([A-Za-z]?\d{4}[A-Za-z]{0,3})\s*$", re.IGNORECASE
)

# Línea de ciudad US: "AMSTERDAM, NY 12010" (zip 5 dígitos, opcional -4)
RE_CIUDAD_ESTADO_CP = re.compile(
    r"^\s*(.+?),\s*([A-Za-z]{2})\.?\s+(\d{4,6}(?:-\d{4})?)\s*$"
)

# Email estándar
RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Teléfono: línea que (sacando etiqueta) es solo dígitos/+/espacios/()/-.
RE_TEL_ETIQUETA = re.compile(
    r"^\s*(?:tel[eé]fono|tel|cel|celular|phone|m[oó]vil|contacto)\b[:.\s]*",
    re.IGNORECASE,
)
RE_TEL_CUERPO = re.compile(r"^\+?[\d\s().\-]{8,}$")


# ────────────────────────────────────────────────────────────
# Tabla chica de países → ISO-2 (claves normalizadas sin tildes)
# ────────────────────────────────────────────────────────────

PAISES_ISO = {
    "united states": "US", "united states of america": "US", "usa": "US",
    "us": "US", "estados unidos": "US", "eeuu": "US", "ee uu": "US",
    "espana": "ES", "spain": "ES",
    "brasil": "BR", "brazil": "BR",
    "chile": "CL",
    "mexico": "MX",
    "uruguay": "UY",
    "canada": "CA",
    "italia": "IT", "italy": "IT",
    "argentina": "AR",
    "paraguay": "PY",
    "bolivia": "BO",
    "peru": "PE",
    "colombia": "CO",
    "ecuador": "EC",
    "venezuela": "VE",
    "alemania": "DE", "germany": "DE",
    "francia": "FR", "france": "FR",
    "reino unido": "GB", "united kingdom": "GB", "uk": "GB",
    "inglaterra": "GB", "england": "GB",
    "portugal": "PT",
    "paises bajos": "NL", "holanda": "NL", "netherlands": "NL",
    "suiza": "CH", "switzerland": "CH",
    "israel": "IL",
    "australia": "AU",
    "china": "CN",
    "japon": "JP", "japan": "JP",
}


# ────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────

def _a_float(s):
    """Convierte string numérico a float tolerando coma decimal argentina."""
    if s is None:
        return None
    try:
        return float(str(s).strip().replace(",", "."))
    except (ValueError, TypeError):
        return None


def _normalizar(s):
    """Minúsculas, sin tildes, sin puntos — para comparar contra tablas."""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower().replace(".", " ").strip()
    # (los puntos se vuelven espacio para que "EE.UU." matchee "ee uu")


def _buscar_pais(linea):
    """Si la línea es un país conocido devuelve (nombre_original, iso). Si no, None."""
    clave = " ".join(_normalizar(linea).split())
    iso = PAISES_ISO.get(clave)
    if iso:
        return linea.strip(), iso
    return None


def _es_telefono(linea):
    """True si la línea parece un teléfono (8-15 dígitos, no formato CUIT)."""
    if RE_CUIT.match(linea):
        return False
    cuerpo = RE_TEL_ETIQUETA.sub("", linea).strip()
    if not RE_TEL_CUERPO.match(cuerpo):
        return False
    digitos = re.sub(r"\D", "", cuerpo)
    return 8 <= len(digitos) <= 15


def _extraer_telefono(linea):
    """Devuelve el teléfono limpio (etiqueta afuera, resto tal cual)."""
    return RE_TEL_ETIQUETA.sub("", linea).strip()


def _es_inicio_paquete(linea):
    """True si la línea abre un bloque de paquete (cantidad o dimensiones)."""
    return bool(RE_CANTIDAD.match(linea) or RE_DIMS.search(linea))


# ────────────────────────────────────────────────────────────
# Estructuras vacías (siempre se devuelven completas)
# ────────────────────────────────────────────────────────────

def _paquete_vacio():
    return {
        "cantidad": None, "largo_cm": None, "ancho_cm": None, "alto_cm": None,
        "peso_kg": None, "descripcion": None, "hs_code": None,
        "descripcion_en": None, "valor_unitario_usd": None,
    }


def _remitente_vacio():
    return {
        "nombre": None, "documento": None, "direccion": None,
        "ciudad": None, "cp": None,
    }


def _destinatario_vacio():
    return {
        "nombre": None, "direccion": None, "ciudad": None, "estado": None,
        "cp": None, "pais": None, "pais_iso": None, "email": None,
        "telefono": None,
    }


# ────────────────────────────────────────────────────────────
# Segmentación: separar bloques de paquetes / remitente / destinatario
# ────────────────────────────────────────────────────────────

def _segmentar(texto):
    """
    Recorre el mail línea por línea con una máquina de estados chica.
    Devuelve (lista de bloques de paquete, líneas remitente, líneas destinatario).
    Cada bloque de paquete es una lista de líneas no vacías.
    """
    bloques_paquete = []
    lineas_remitente = []
    lineas_destinatario = []
    modo = "libre"  # libre | paquete | remitente | destinatario

    for cruda in texto.splitlines():
        linea = cruda.strip()

        # Headers de sección: cambian de modo y no se acumulan
        if RE_HEADER_REMITENTE.match(linea):
            modo = "remitente"
            continue
        if RE_HEADER_DESTINATARIO.match(linea):
            modo = "destinatario"
            continue
        if not linea:
            continue

        # ¿Arranca un paquete nuevo?
        if _es_inicio_paquete(linea):
            if modo != "paquete":
                # Veníamos de otra sección: bloque nuevo
                bloques_paquete.append([linea])
            elif RE_CANTIDAD.match(linea):
                # Línea con cantidad siempre abre bloque nuevo
                bloques_paquete.append([linea])
            elif any(RE_DIMS.search(l) for l in bloques_paquete[-1]):
                # Dimensiones sueltas y el bloque actual ya tenía: bloque nuevo
                bloques_paquete.append([linea])
            else:
                # Dimensiones sueltas completan el bloque actual
                bloques_paquete[-1].append(linea)
            modo = "paquete"
            continue

        # Línea común: va al buffer del modo activo
        if modo == "paquete":
            bloques_paquete[-1].append(linea)
        elif modo == "remitente":
            lineas_remitente.append(linea)
        elif modo == "destinatario":
            lineas_destinatario.append(linea)
        # modo "libre": texto suelto antes del primer bloque, se ignora

    return bloques_paquete, lineas_remitente, lineas_destinatario


# ────────────────────────────────────────────────────────────
# Parseo de un bloque de paquete
# ────────────────────────────────────────────────────────────

def _parsear_paquete(lineas):
    paq = _paquete_vacio()
    libres = []  # líneas sin campo detectado → candidatas a descripción

    for linea in lineas:
        consumida = False

        m = RE_CANTIDAD.match(linea)
        if m and paq["cantidad"] is None:
            paq["cantidad"] = int(m.group(1))
            consumida = True

        m = RE_DIMS.search(linea)
        if m and paq["largo_cm"] is None:
            paq["largo_cm"] = _a_float(m.group(1))
            paq["ancho_cm"] = _a_float(m.group(2))
            paq["alto_cm"] = _a_float(m.group(3))
            consumida = True

        m = RE_HS.search(linea)
        if m and paq["hs_code"] is None:
            paq["hs_code"] = m.group(1).rstrip(".")
            # Lo que sigue al código en la misma línea suele ser la
            # descripción en inglés para la aduana
            resto = linea[m.end():].strip(" \t-—:;,")
            if resto:
                paq["descripcion_en"] = resto
            consumida = True

        if paq["peso_kg"] is None:
            m = RE_PESO_ETIQUETADO.search(linea)
            if not m and not consumida:
                # "5 kg" suelto solo si la línea no matcheó otra cosa
                m = RE_PESO_SUELTO.search(linea)
            if m:
                paq["peso_kg"] = _a_float(m.group(1))
                consumida = True

        if paq["valor_unitario_usd"] is None:
            m = RE_VALOR_MONEDA_PRIMERO.search(linea)
            if not m:
                m = RE_VALOR_MONEDA_DESPUES.search(linea)
            if m:
                paq["valor_unitario_usd"] = _a_float(m.group(1))
                consumida = True

        if not consumida:
            libres.append(linea)

    # Primera línea libre = descripción del producto ("Wool top")
    if libres:
        paq["descripcion"] = libres[0]

    return paq


# ────────────────────────────────────────────────────────────
# Parseo del bloque remitente
# ────────────────────────────────────────────────────────────

def _parsear_remitente(lineas):
    rem = _remitente_vacio()
    restantes = []

    for linea in lineas:
        # Documento: CUIT con guiones o 7-11 dígitos pelados
        if rem["documento"] is None:
            m = RE_CUIT.match(linea) or RE_DOC_SOLO_DIGITOS.match(linea)
            if m:
                rem["documento"] = m.group(1)
                continue
        # Código postal en línea propia ("Cp 1428", "CP: C1043AAZ")
        if rem["cp"] is None:
            m = RE_CP_LINEA.match(linea)
            if m:
                rem["cp"] = m.group(1).upper()
                continue
        restantes.append(linea)

    # Orden esperado: nombre, dirección (la línea con números), resto = ciudad
    if restantes:
        rem["nombre"] = restantes[0]
        resto = restantes[1:]
        ciudad_partes = []
        for linea in resto:
            if rem["direccion"] is None and re.search(r"\d", linea):
                rem["direccion"] = linea
            else:
                ciudad_partes.append(linea)
        if ciudad_partes:
            rem["ciudad"] = ", ".join(ciudad_partes)

    return rem


# ────────────────────────────────────────────────────────────
# Parseo del bloque destinatario
# ────────────────────────────────────────────────────────────

def _parsear_destinatario(lineas):
    dest = _destinatario_vacio()
    restantes = []

    for linea in lineas:
        # Email en cualquier parte de la línea
        m = RE_EMAIL.search(linea)
        if m and dest["email"] is None:
            dest["email"] = m.group(0)
            continue
        # "CIUDAD, XX 12345" → ciudad / estado / cp (formato US)
        m = RE_CIUDAD_ESTADO_CP.match(linea)
        if m and dest["ciudad"] is None:
            dest["ciudad"] = m.group(1).strip()
            dest["estado"] = m.group(2).upper()
            dest["cp"] = m.group(3)
            continue
        # País conocido de la tabla
        if dest["pais_iso"] is None:
            pais = _buscar_pais(linea)
            if pais:
                dest["pais"], dest["pais_iso"] = pais
                continue
        # Teléfono (8+ dígitos, no CUIT ni CP)
        if dest["telefono"] is None and _es_telefono(linea):
            dest["telefono"] = _extraer_telefono(linea)
            continue
        # CP en línea propia: etiquetado ("CP 28001") o suelto ("28001",
        # típico de destinos no-US donde el código va en su propia línea)
        if dest["cp"] is None:
            m = RE_CP_LINEA.match(linea) or re.match(
                r"^\s*([A-Za-z]?\d{4,5}[A-Za-z]{0,3})\s*$", linea
            )
            if m:
                dest["cp"] = m.group(1).upper()
                continue
        restantes.append(linea)

    # Orden esperado: nombre, dirección (línea con números), resto = ciudad
    if restantes:
        dest["nombre"] = restantes[0]
        resto = restantes[1:]
        sobras = []
        for linea in resto:
            if dest["direccion"] is None and re.search(r"\d", linea):
                dest["direccion"] = linea
            else:
                sobras.append(linea)
        # Si no salió ciudad de la línea US, usar la primera sobra
        if dest["ciudad"] is None and sobras:
            dest["ciudad"] = sobras[0]
            sobras = sobras[1:]
        if dest["estado"] is None and sobras:
            dest["estado"] = sobras[0]

    return dest


# ────────────────────────────────────────────────────────────
# Confianza: proporción de campos clave encontrados
# ────────────────────────────────────────────────────────────

def _calcular_confianza(resultado):
    """
    Campos clave (4): destinatario.nombre, destinatario.pais_iso,
    >=1 paquete con dimensiones y peso, remitente.nombre.
    """
    encontrados = 0
    if resultado["destinatario"]["nombre"]:
        encontrados += 1
    if resultado["destinatario"]["pais_iso"]:
        encontrados += 1
    if any(
        p["largo_cm"] is not None and p["ancho_cm"] is not None
        and p["alto_cm"] is not None and p["peso_kg"] is not None
        for p in resultado["paquetes"]
    ):
        encontrados += 1
    if resultado["remitente"]["nombre"]:
        encontrados += 1
    return round(encontrados / 4.0, 2)


# ────────────────────────────────────────────────────────────
# API pública
# ────────────────────────────────────────────────────────────

def parsear_pedido(texto):
    """
    Parsea el texto de un pedido recibido por email y devuelve un dict
    con paquetes, remitente, destinatario y un score de confianza 0-1.
    Nunca lanza excepción: con input basura devuelve la estructura vacía.
    """
    resultado = {
        "paquetes": [],
        "remitente": _remitente_vacio(),
        "destinatario": _destinatario_vacio(),
        "confianza": 0.0,
    }
    try:
        if not isinstance(texto, str) or not texto.strip():
            return resultado

        bloques, lin_rem, lin_dest = _segmentar(texto)

        resultado["paquetes"] = [_parsear_paquete(b) for b in bloques]
        if lin_rem:
            resultado["remitente"] = _parsear_remitente(lin_rem)
        if lin_dest:
            resultado["destinatario"] = _parsear_destinatario(lin_dest)
        resultado["confianza"] = _calcular_confianza(resultado)
    except Exception:
        # Contrato: jamás romper por input basura. Devolvemos lo que haya.
        pass
    return resultado
