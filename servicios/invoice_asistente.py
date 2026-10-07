"""Asistente para cargar la invoice comercial desde un archivo o texto.

Reparto de responsabilidades:

- La EXTRACCION del contenido es deterministica (openpyxl, csv, pdfplumber).
- El modelo solo LEE lo que esta escrito y lo devuelve con un JSON schema
  estricto. Lo que no figura vuelve como null: nunca inventa cantidades,
  valores, codigos HS ni paises.
- El codigo decide el resto: totales, redondeo, HS sugerido por el buscador
  local, origen asumido y los avisos para que el cliente revise.

Nada se guarda: el archivo vive en memoria durante el request y no se loguea.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

MAX_BYTES = 10 * 1024 * 1024
MAX_TEXTO = 60_000
MAX_ITEMS = 100
MAX_DESC_EN = 75
MAX_PAGINAS_PDF = 30
MODELO_DEFAULT = "gpt-5.6-luna"

EXTENSIONES = {
    "xlsx": "xlsx", "xls": "xlsx", "csv": "csv", "pdf": "pdf",
    "jpg": "imagen", "jpeg": "imagen", "png": "imagen",
}
MIME_IMAGEN = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}

MENSAJE_NO_DISPONIBLE = "El asistente no está disponible; cargá los artículos a mano."


class InvoiceAsistenteError(ValueError):
    """Error del pedido con un mensaje apto para mostrar al cliente."""


class AsistenteNoDisponible(RuntimeError):
    """No hay modelo configurado y el contenido no admite lectura sin IA."""


def _flag(nombre: str, default: str) -> bool:
    return os.getenv(nombre, default).strip().lower() in {"1", "true", "si", "sí", "yes", "on"}


def habilitado() -> bool:
    """INVOICE_ASISTENTE_ENABLED: apagado por defecto."""
    return _flag("INVOICE_ASISTENTE_ENABLED", "false")


def modelo() -> str:
    return os.getenv("INVOICE_ASISTENTE_MODEL", "").strip() or MODELO_DEFAULT


def ia_disponible() -> bool:
    """IA solo con OPENAI_API_KEY e INVOICE_ASISTENTE_IA (encendido por defecto)."""
    return _flag("INVOICE_ASISTENTE_IA", "true") and bool(os.getenv("OPENAI_API_KEY", "").strip())


# ── Extracción determinística ───────────────────────────────

@dataclass
class Contenido:
    tipo: str
    texto: str = ""
    tablas: list[list[list[str]]] = field(default_factory=list)
    imagen: bytes | None = None
    mime: str = ""


def _celda(valor: Any) -> str:
    if valor is None:
        return ""
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    return str(valor).strip()


def _tabla_a_texto(nombre: str, filas: list[list[str]]) -> str:
    lineas = [f"## {nombre}"] if nombre else []
    lineas += [" | ".join(f for f in fila) for fila in filas if any(fila)]
    return "\n".join(lineas)


def _extraer_xlsx(contenido: bytes) -> Contenido:
    import openpyxl

    try:
        libro = openpyxl.load_workbook(io.BytesIO(contenido), read_only=True, data_only=True)
    except Exception as exc:
        raise InvoiceAsistenteError(
            "No pudimos abrir la planilla. Si es un .xls antiguo, guardala como .xlsx o CSV y volvé a intentar."
        ) from exc
    tablas, textos = [], []
    try:
        for hoja in libro.worksheets:
            filas = []
            for fila in hoja.iter_rows(values_only=True):
                celdas = [_celda(v) for v in fila]
                if any(celdas):
                    filas.append(celdas)
            if filas:
                tablas.append(filas)
                textos.append(_tabla_a_texto(f"Hoja {hoja.title}", filas))
    finally:
        libro.close()
    return Contenido("xlsx", texto="\n\n".join(textos), tablas=tablas)


def _decodificar(contenido: bytes) -> str:
    for codificacion in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return contenido.decode(codificacion)
        except UnicodeDecodeError:
            continue
    return contenido.decode("latin-1", errors="replace")


def _extraer_csv(contenido: bytes) -> Contenido:
    texto = _decodificar(contenido)
    try:
        dialecto = csv.Sniffer().sniff(texto[:4096], delimiters=";,\t|")
        delimitador = dialecto.delimiter
    except csv.Error:
        delimitador = ";" if texto.count(";") > texto.count(",") else ","
    filas = [[c.strip() for c in fila] for fila in csv.reader(io.StringIO(texto), delimiter=delimitador)]
    filas = [f for f in filas if any(f)]
    return Contenido("csv", texto=_tabla_a_texto("", filas), tablas=[filas] if filas else [])


def _extraer_pdf(contenido: bytes) -> Contenido:
    import pdfplumber

    textos, tablas = [], []
    try:
        with pdfplumber.open(io.BytesIO(contenido)) as pdf:
            for numero, pagina in enumerate(pdf.pages[:MAX_PAGINAS_PDF], start=1):
                texto = pagina.extract_text() or ""
                if texto.strip():
                    textos.append(f"## Página {numero}\n{texto.strip()}")
                for tabla in pagina.extract_tables() or []:
                    filas = [[_celda(c) for c in fila] for fila in tabla if fila]
                    filas = [f for f in filas if any(f)]
                    if filas:
                        tablas.append(filas)
                        textos.append(_tabla_a_texto(f"Tabla página {numero}", filas))
    except Exception as exc:
        raise InvoiceAsistenteError("No pudimos leer el PDF. Probá con otra copia o pegá el texto.") from exc
    if not textos:
        raise InvoiceAsistenteError(
            "El PDF no tiene texto seleccionable (parece escaneado). Subí una foto JPG/PNG o pegá el texto."
        )
    return Contenido("pdf", texto="\n\n".join(textos), tablas=tablas)


def extraer_contenido(nombre_archivo: str | None, contenido: bytes | None, texto: str | None) -> Contenido:
    """Valida tipo y tamaño y devuelve el contenido listo para leer."""
    if contenido:
        nombre = (nombre_archivo or "").strip().lower()
        extension = nombre.rsplit(".", 1)[-1] if "." in nombre else ""
        if extension not in EXTENSIONES:
            raise InvoiceAsistenteError(
                "Tipo de archivo no permitido. Subí Excel (.xlsx/.xls), CSV, PDF o una foto JPG/PNG."
            )
        if len(contenido) > MAX_BYTES:
            raise InvoiceAsistenteError("El archivo supera los 10 MB. Subí uno más liviano o pegá el texto.")
        tipo = EXTENSIONES[extension]
        if tipo == "xlsx":
            resultado = _extraer_xlsx(contenido)
        elif tipo == "csv":
            resultado = _extraer_csv(contenido)
        elif tipo == "pdf":
            resultado = _extraer_pdf(contenido)
        else:
            resultado = Contenido("imagen", imagen=contenido, mime=MIME_IMAGEN[extension])
        extra = (texto or "").strip()
        if extra:
            resultado.texto = (resultado.texto + "\n\n## Texto pegado\n" + extra).strip()
        if resultado.tipo != "imagen" and not resultado.texto.strip():
            raise InvoiceAsistenteError("El archivo está vacío.")
        return resultado
    pegado = (texto or "").strip()
    if not pegado:
        raise InvoiceAsistenteError("Subí un archivo o pegá el texto de la invoice.")
    return Contenido("texto", texto=pegado)


# ── Lectura con el modelo ───────────────────────────────────

_NUM_O_NULL = {"type": ["number", "null"]}
_STR_O_NULL = {"type": ["string", "null"]}

SCHEMA_INVOICE: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items", "total_declarado_documento"],
    "properties": {
        "total_declarado_documento": _NUM_O_NULL,
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "descripcion_original", "descripcion_en", "hs_code", "cantidad",
                    "valor_unitario", "moneda", "pais_origen", "peso_neto_kg", "fuente_linea",
                ],
                "properties": {
                    "descripcion_original": {"type": "string"},
                    "descripcion_en": {"type": "string"},
                    "hs_code": _STR_O_NULL,
                    "cantidad": _NUM_O_NULL,
                    "valor_unitario": _NUM_O_NULL,
                    "moneda": _STR_O_NULL,
                    "pais_origen": _STR_O_NULL,
                    "peso_neto_kg": _NUM_O_NULL,
                    "fuente_linea": {"type": "string"},
                },
            },
        },
    },
}

INSTRUCCIONES = """Leés invoices, facturas, packing lists y pedidos para armar la factura comercial de un envío internacional.
Devolvé un ítem por cada artículo de mercadería (no incluyas fletes, seguros, impuestos, subtotales ni totales).
REGLA DURA: NUNCA inventes cantidades, valores, códigos HS ni países. Si un dato no está ESCRITO en la fuente, devolvé null.
- descripcion_original: el texto del artículo tal como figura.
- descripcion_en: descripción aduanera en inglés, máximo 75 caracteres, clara (qué es, material si figura). No agregues material ni composición que no figuren.
- hs_code: solo si el código está escrito en la fuente, copiado tal cual; si no, null.
- cantidad y valor_unitario: números tal como figuran (respetá los decimales; en Argentina la coma es decimal). Si solo figura el total de la línea, valor_unitario es null.
- moneda: código ISO 4217 si figura (USD, EUR, ARS...); si no, null.
- pais_origen: ISO-3166 alfa-2 solo si figura el país de origen o fabricación; si no, null.
- peso_neto_kg: solo si figura; si no, null.
- fuente_linea: el texto literal de la línea o fila de donde salió el ítem.
- total_declarado_documento: el total de mercadería que declara el documento, si existe; si no, null."""


def _cliente_openai() -> Any:
    # Mismo cliente y misma inicialización que los agentes comerciales.
    from servicios.agentes_comerciales import AgenteNoConfigurado, AgentesComercialesOpenAI

    try:
        cliente = AgentesComercialesOpenAI().client
    except AgenteNoConfigurado as exc:
        raise AsistenteNoDisponible(MENSAJE_NO_DISPONIBLE) from exc
    if cliente is None:
        raise AsistenteNoDisponible(MENSAJE_NO_DISPONIBLE)
    return cliente


def leer_con_modelo(contenido: Contenido, client: Any | None = None) -> dict[str, Any]:
    """Un solo llamado con salida estructurada. Devuelve el JSON del modelo."""
    from servicios.agentes_comerciales import SalidaAgenteInvalida

    client = client or _cliente_openai()
    partes: list[dict[str, Any]] = [{
        "type": "input_text",
        "text": ("Contenido de la invoice (" + contenido.tipo + "):\n\n" + contenido.texto[:MAX_TEXTO]).strip(),
    }]
    if contenido.imagen is not None:
        datos = base64.b64encode(contenido.imagen).decode("ascii")
        partes.append({"type": "input_image", "image_url": f"data:{contenido.mime};base64,{datos}"})
    response = client.responses.create(
        model=modelo(),
        instructions=INSTRUCCIONES,
        input=[{"role": "user", "content": partes}],
        text={"format": {"type": "json_schema", "name": "invoice_items", "schema": SCHEMA_INVOICE, "strict": True}},
        store=False,
        metadata={"tauro_task_type": "invoice_reader", "tauro_policy_version": "1"},
    )
    try:
        data = json.loads(getattr(response, "output_text", "") or "")
    except json.JSONDecodeError as exc:
        raise SalidaAgenteInvalida("El asistente no devolvió JSON válido.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise SalidaAgenteInvalida("La salida del asistente no tiene ítems.")
    return data


# ── Fallback sin IA: mapeo por encabezados ──────────────────

SINONIMOS = {
    "descripcion": ["descripcion", "description", "descripcion del producto", "producto", "product",
                    "nombre", "name", "articulo", "item", "detalle"],
    "cantidad": ["cantidad", "qty", "quantity", "units", "unidades", "cant", "pcs"],
    "valor_unitario": ["precio unitario", "unit price", "unit value", "valor unitario", "fob",
                       "fob unitario", "valor", "precio", "price", "unit cost"],
    "hs_code": ["hs", "hs code", "hscode", "ncm", "codigo hs", "posicion arancelaria", "tariff code"],
    "pais_origen": ["origen", "origin", "country of origin", "pais de origen", "pais origen", "made in"],
    "peso_neto_kg": ["peso neto", "net weight", "peso neto kg", "net weight kg"],
    "moneda": ["moneda", "currency"],
}


def _normalizar(texto: Any) -> str:
    texto = unicodedata.normalize("NFKD", str(texto or "")).lower()
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = re.sub(r"\(.*?\)", " ", texto)
    return re.sub(r"[^a-z0-9]+", " ", texto).strip()


def _columnas(encabezado: list[str]) -> dict[str, int]:
    normalizados = [_normalizar(c) for c in encabezado]
    asignadas: dict[str, int] = {}
    usadas: set[int] = set()
    for campo, sinonimos in SINONIMOS.items():
        elegido = None
        for sinonimo in sinonimos:  # el orden de sinónimos es la prioridad
            for i, h in enumerate(normalizados):
                if i in usadas or not h:
                    continue
                if campo == "valor_unitario" and "total" in h.split():
                    continue
                if h == sinonimo or re.fullmatch(rf"{re.escape(sinonimo)}( [a-z0-9]+)?", h):
                    elegido = i
                    break
            if elegido is not None:
                break
        if elegido is not None:
            asignadas[campo] = elegido
            usadas.add(elegido)
    return asignadas


def parsear_numero(valor: Any) -> Decimal | None:
    """Número de planilla o texto: '1.234,56', '1,234.56', '70,000000', 'USD 12'."""
    if valor is None:
        return None
    if isinstance(valor, (int, float, Decimal)) and not isinstance(valor, bool):
        return Decimal(str(valor))
    texto = re.sub(r"[^0-9,.\-]", "", str(valor))
    if not re.search(r"\d", texto):
        return None
    if "," in texto and "." in texto:
        if texto.rfind(",") > texto.rfind("."):
            texto = texto.replace(".", "").replace(",", ".")
        else:
            texto = texto.replace(",", "")
    elif "," in texto:
        texto = texto.replace(",", "") if re.fullmatch(r"-?\d{1,3}(,\d{3}){2,}", texto) else texto.replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3}){2,}", texto):
        texto = texto.replace(".", "")
    try:
        return Decimal(texto)
    except InvalidOperation:
        return None


def leer_por_encabezados(contenido: Contenido) -> dict[str, Any]:
    """Planillas sin IA: busca la fila de encabezados y lee las columnas conocidas."""
    for tabla in contenido.tablas:
        for indice, fila in enumerate(tabla[:20]):
            columnas = _columnas(fila)
            if "descripcion" not in columnas or not ({"cantidad", "valor_unitario"} & columnas.keys()):
                continue
            items = []
            for datos in tabla[indice + 1:]:
                def celda(campo: str) -> str:
                    i = columnas.get(campo)
                    return datos[i].strip() if i is not None and i < len(datos) and datos[i] else ""

                descripcion = celda("descripcion")
                cantidad = parsear_numero(celda("cantidad"))
                unitario = parsear_numero(celda("valor_unitario"))
                if not descripcion and cantidad is None and unitario is None:
                    continue
                if re.match(r"(sub)?total\b", _normalizar(descripcion)) and cantidad is None:
                    continue
                items.append({
                    "descripcion_original": descripcion,
                    "descripcion_en": descripcion,
                    "hs_code": celda("hs_code") or None,
                    "cantidad": cantidad,
                    "valor_unitario": unitario,
                    "moneda": celda("moneda") or None,
                    "pais_origen": celda("pais_origen") or None,
                    "peso_neto_kg": parsear_numero(celda("peso_neto_kg")),
                    "fuente_linea": " | ".join(c for c in datos if c),
                })
            if items:
                return {"items": items, "total_declarado_documento": None}
    raise InvoiceAsistenteError(
        "No encontramos columnas de descripción, cantidad y precio en la planilla. Cargá los artículos a mano."
    )


# ── Post-proceso determinístico ─────────────────────────────

def _dec(valor: Any) -> Decimal | None:
    if valor is None or valor == "":
        return None
    try:
        numero = valor if isinstance(valor, Decimal) else Decimal(str(valor))
    except (InvalidOperation, ValueError):
        return parsear_numero(valor)
    return numero if numero.is_finite() else None


def _fmt(numero: Decimal) -> str:
    """Formato es-AR sin perder decimales significativos: 5825.836 -> 5.825,836."""
    numero = numero.normalize()
    signo = "-" if numero < 0 else ""
    entero, _, decimales = f"{abs(numero):f}".partition(".")
    entero = f"{int(entero):,}".replace(",", ".")
    if decimales:
        decimales = decimales.ljust(2, "0")
        return f"{signo}{entero},{decimales}"
    return f"{signo}{entero},00"


def _json_num(numero: Decimal | None) -> float | None:
    return float(numero) if numero is not None else None


def _sugerir_hs(descripcion: str) -> str | None:
    from servicios.hs_code import sugerir_hs

    try:
        resultado = sugerir_hs(descripcion)
    except (ValueError, OSError, KeyError):
        return None
    candidatos = resultado.get("candidates") or []
    return candidatos[0]["formatted"] if candidatos else None


def procesar(data: dict[str, Any], pais_origen_envio: str | None, fuente: str) -> dict[str, Any]:
    from servicios.paises import normalizar_iso2

    origen_envio = normalizar_iso2(pais_origen_envio or "")
    avisos: list[str] = []
    items: list[dict[str, Any]] = []
    crudos = [i for i in (data.get("items") or []) if isinstance(i, dict)]
    if len(crudos) > MAX_ITEMS:
        avisos.append(f"El documento tiene {len(crudos)} artículos; cargamos los primeros {MAX_ITEMS}.")
        crudos = crudos[:MAX_ITEMS]
    suma_exacta = Decimal(0)
    suma_completa = True
    monedas: set[str] = set()

    for n, crudo in enumerate(crudos, start=1):
        original = str(crudo.get("descripcion_original") or "").strip()
        descripcion = re.sub(r"\s+", " ", str(crudo.get("descripcion_en") or "")).strip()
        nombre = f"Artículo {n}" + (f" ({(original or descripcion)[:40]})" if original or descripcion else "")
        if not descripcion and not original:
            avisos.append(f"{nombre}: no tiene descripción. Completala a mano.")
        if len(descripcion) > MAX_DESC_EN:
            descripcion = descripcion[:MAX_DESC_EN].rstrip()
            avisos.append(f"{nombre}: recortamos la descripción en inglés a {MAX_DESC_EN} caracteres.")

        cantidad = _dec(crudo.get("cantidad"))
        unitario = _dec(crudo.get("valor_unitario"))
        if cantidad is None:
            avisos.append(f"{nombre}: no figura la cantidad.")
        elif cantidad <= 0:
            avisos.append(f"{nombre}: la cantidad es {_fmt(cantidad)}; tiene que ser mayor a 0.")
        elif cantidad != cantidad.to_integral_value():
            avisos.append(f"{nombre}: la cantidad {_fmt(cantidad)} no es un número entero de unidades.")
        if unitario is None:
            avisos.append(f"{nombre}: no figura el valor unitario.")
        elif unitario <= 0:
            avisos.append(f"{nombre}: el valor unitario es {_fmt(unitario)}; tiene que ser mayor a 0.")

        total = None
        if cantidad is not None and unitario is not None:
            exacto = cantidad * unitario
            suma_exacta += exacto
            total = exacto.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            suma_completa = False

        moneda = str(crudo.get("moneda") or "").strip().upper() or None
        if moneda:
            monedas.add(moneda)
        if moneda and moneda != "USD":
            avisos.append(f"{nombre}: el valor está en {moneda}. No lo convertimos; ingresá el total en USD.")

        hs = str(crudo.get("hs_code") or "").strip() or None
        hs_origen = "documento" if hs else None
        if not hs and len(descripcion) >= 3:
            hs = _sugerir_hs(descripcion)
            hs_origen = "sugerido" if hs else None
        if not hs:
            avisos.append(f"{nombre}: no encontramos HS code. Buscalo con el asistente del artículo.")

        pais_crudo = str(crudo.get("pais_origen") or "").strip()
        pais = normalizar_iso2(pais_crudo) if pais_crudo else ""
        pais_origen_tipo = "documento" if pais else None
        if pais_crudo and not pais:
            avisos.append(f"{nombre}: no reconocimos el país de origen «{pais_crudo[:30]}».")
        if not pais and origen_envio:
            pais, pais_origen_tipo = origen_envio, "asumido"

        items.append({
            "descripcion_original": original,
            "descripcion_en": descripcion,
            "hs_code": hs,
            "hs_origen": hs_origen,
            "cantidad": _json_num(cantidad),
            "valor_unitario": _json_num(unitario),
            "valor_total": _json_num(total),
            "moneda": moneda,
            "pais_origen": pais or None,
            "pais_origen_tipo": pais_origen_tipo,
            "peso_neto_kg": _json_num(_dec(crudo.get("peso_neto_kg"))),
            "fuente_linea": str(crudo.get("fuente_linea") or "")[:300],
        })

    if not items:
        avisos.append("No encontramos artículos en lo que cargaste. Revisá el archivo o cargalos a mano.")
    if fuente == "encabezados" and items:
        avisos.append("Leímos la planilla sin IA: revisá que las descripciones estén en inglés.")
    if any(i["pais_origen_tipo"] == "asumido" for i in items):
        avisos.append("Donde no figuraba el país de fabricación, usamos el origen del envío (marcado «asumido»).")
    if any(i["hs_origen"] == "sugerido" for i in items):
        avisos.append("Los HS marcados «sugerido» salen del buscador local: confirmá que correspondan.")

    total_documento = _dec(data.get("total_declarado_documento"))
    if total_documento is not None and items:
        if not suma_completa:
            avisos.append(f"El documento declara un total de {_fmt(total_documento)}; no pudimos compararlo porque faltan valores.")
        elif suma_exacta != total_documento:
            diferencia = abs(total_documento - suma_exacta)
            motivo = " (por redondeo)" if diferencia < Decimal("0.01") * len(items) else ""
            avisos.append(
                f"La suma de los artículos da {_fmt(suma_exacta)} y el documento declara "
                f"{_fmt(total_documento)}: diferencia {_fmt(diferencia)}{motivo}."
            )
    return {
        "items": items,
        "avisos": avisos,
        "total_documento": _json_num(total_documento),
        "suma_items": _json_num(suma_exacta.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if items else None,
        "monedas": sorted(monedas),
        "fuente": fuente,
    }


def leer_invoice(
    nombre_archivo: str | None,
    contenido: bytes | None,
    texto: str | None,
    pais_origen_envio: str | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Punto de entrada: extrae, lee (IA o encabezados) y post-procesa."""
    extraido = extraer_contenido(nombre_archivo, contenido, texto)
    if client is not None or ia_disponible():
        try:
            data = leer_con_modelo(extraido, client=client)
            fuente = "ia"
        except AsistenteNoDisponible:
            data, fuente = None, ""
    else:
        data, fuente = None, ""
    if data is None:
        if extraido.tipo not in {"xlsx", "csv"}:
            raise AsistenteNoDisponible(MENSAJE_NO_DISPONIBLE)
        data, fuente = leer_por_encabezados(extraido), "encabezados"
    resultado = procesar(data, pais_origen_envio, fuente)
    resultado["tipo"] = extraido.tipo
    return resultado
