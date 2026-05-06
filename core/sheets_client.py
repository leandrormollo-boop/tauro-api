import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import os
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────

SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

SHEET_URL = os.getenv("GOOGLE_SHEET_URL")

# ─────────────────────────────────────────────
# CONEXIÓN LAZY
# ─────────────────────────────────────────────

_cliente_sheets = None

def get_cliente_sheets():
    global _cliente_sheets
    if _cliente_sheets is None:
        try:
            credenciales = ServiceAccountCredentials.from_json_keyfile_name(
                "credenciales.json", SCOPE
            )
            _cliente_sheets = gspread.authorize(credenciales)
        except FileNotFoundError:
            raise RuntimeError(
                "credenciales.json no encontrado. "
                "Descargalo desde Google Cloud Console y ponelo en la raíz del proyecto."
            )
        except Exception as e:
            raise RuntimeError(f"Error al conectar con Google Sheets: {e}")
    return _cliente_sheets


def _abrir_sheet():
    if not SHEET_URL:
        raise RuntimeError("GOOGLE_SHEET_URL no configurada en el .env")
    cliente = get_cliente_sheets()
    return cliente.open_by_url(SHEET_URL)


def _leer_hoja(nombre_hoja: str) -> list[dict]:
    doc = _abrir_sheet()
    try:
        hoja = doc.worksheet(nombre_hoja)
    except gspread.exceptions.WorksheetNotFound:
        raise RuntimeError(
            f"Hoja '{nombre_hoja}' no encontrada. "
            f"Verificá que exista con ese nombre exacto."
        )
    return hoja.get_all_records()


# ─────────────────────────────────────────────
# AUTH — API KEY
# ─────────────────────────────────────────────

def obtener_cliente_por_api_key(api_key: str) -> dict:
    """
    Busca la API Key en la hoja PERFILES.
    Retorna el perfil completo del cliente si la key es válida.
    """
    try:
        filas = _leer_hoja("PERFILES")
        for fila in filas:
            key_tabla = str(fila.get("API_KEY", "")).strip()
            if key_tabla.lower() == api_key.strip().lower():
                return {
                    "encontrado": True,
                    "cliente_id": str(fila.get("CLIENTE_ID", "")),
                    "nombre": str(fila.get("NOMBRE_COMPLETO", fila.get("NOMBRE", ""))),
                    "cuit": str(fila.get("CUIT", "")),
                    "direccion": str(fila.get("DIRECCION_RETIRO", fila.get("DIRECCION", ""))),
                    "cp": str(fila.get("CODIGO_POSTAL", fila.get("CP", ""))),
                    "ciudad": str(fila.get("CIUDAD", "BUENOS AIRES")),
                    "pais": str(fila.get("PAIS", "AR")),
                    "telefono": str(fila.get("TELEFONO", "")),
                    "email": str(fila.get("EMAIL", "")),
                }
        return {"encontrado": False}
    except Exception as e:
        print(f"[sheets] Error al verificar API Key: {e}")
        return {"encontrado": False}


# ─────────────────────────────────────────────
# CONFIG — TIPO DE CAMBIO Y MARGEN
# ─────────────────────────────────────────────

def obtener_tipo_cambio() -> float:
    """
    Lee el tipo de cambio oficial desde la hoja CONFIG.
    Formato: fila con CAMPO=TIPO_CAMBIO_OFICIAL, VALOR=xxxx
    Fallback: 1400 si no se puede leer.
    """
    try:
        filas = _leer_hoja("CONFIG")
        for fila in filas:
            campo = str(fila.get("CAMPO", "")).strip().upper()
            if campo == "TIPO_CAMBIO_OFICIAL":
                try:
                    return float(str(fila.get("VALOR", 1400)).replace(",", "."))
                except (ValueError, TypeError):
                    return 1400.0
        print("[sheets] TIPO_CAMBIO_OFICIAL no encontrado en CONFIG, usando 1400.")
        return 1400.0
    except Exception as e:
        print(f"[sheets] Error al leer tipo de cambio: {e}. Usando 1400.")
        return 1400.0


def obtener_margen_minimo_ars() -> float:
    """Lee el margen mínimo de alerta desde CONFIG."""
    try:
        filas = _leer_hoja("CONFIG")
        for fila in filas:
            campo = str(fila.get("CAMPO", "")).strip().upper()
            if campo == "MARGEN_MINIMO_ARS":
                try:
                    return float(str(fila.get("VALOR", 5000)).replace(",", "."))
                except (ValueError, TypeError):
                    return 5000.0
        return 5000.0
    except Exception as e:
        print(f"[sheets] Error al leer margen mínimo: {e}. Usando 5000.")
        return 5000.0


# ─────────────────────────────────────────────
# COTI — PRECIOS DE ENVÍO
# ─────────────────────────────────────────────

def _parse_float(val, default=0.0) -> float:
    """Convierte cualquier formato de número a float. Maneja $, puntos y comas."""
    try:
        s = str(val).replace("$", "").replace(" ", "").strip()
        if not s:
            return default
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            partes = s.split(",")
            if len(partes) == 2 and len(partes[1]) == 3:
                s = s.replace(",", "")
            else:
                s = s.replace(",", ".")
        return float(s)
    except (ValueError, TypeError):
        return default


def obtener_precio_envio(cliente_id: str, producto_id: str, destino_pais: str) -> dict:
    """
    Busca el precio de envío para una combinación exacta:
    cliente + producto + destino_pais.

    Retorna precio_ars, precio_usd y tipo_cambio_usado.
    precio_usd = precio_ars / tipo_cambio (NO multiplicado)
    """
    try:
        filas = _leer_hoja("COTI")
        tc = obtener_tipo_cambio()

        cliente_buscado = cliente_id.strip().upper()
        producto_buscado = producto_id.strip().upper()
        destino_buscado = destino_pais.strip().upper()

        for fila in filas:
            cliente_tabla = str(fila.get("CLIENTE_ID", "")).strip().upper()
            producto_tabla = str(fila.get("PRODUCTO_ID", "")).strip().upper()
            destino_tabla = str(fila.get("DESTINO_PAIS", "")).strip().upper()

            if (cliente_tabla == cliente_buscado and
                    producto_tabla == producto_buscado and
                    destino_tabla == destino_buscado):

                precio_ars = _parse_float(fila.get("PRECIO_ARS", 0))

                precio_usd = round(precio_ars / tc, 2) if tc > 0 else 0.0

                return {
                    "encontrado": True,
                    "precio_ars": precio_ars,
                    "precio_usd": precio_usd,
                    "tipo_cambio_usado": tc,
                    "costo_fedex_ars": _parse_float(fila.get("COSTO_FEDEX_ARS", fila.get("COSTO_FEDEX", 0))),
                    "margen_ars": _parse_float(fila.get("MARGEN_ARS", fila.get("MARGEN", 0))),
                }

        return {"encontrado": False}
    except Exception as e:
        print(f"[sheets] Error al buscar precio: {e}")
        return {"encontrado": False}


def obtener_datos_producto(cliente_id: str, producto_id: str) -> dict:
    """
    Busca datos aduanales del producto en PRODUCTOS_CATALOGO.
    """
    try:
        filas = _leer_hoja("PRODUCTOS_CATALOGO")
        cliente_buscado = cliente_id.strip().upper()
        producto_buscado = producto_id.strip().upper()

        for fila in filas:
            c = str(fila.get("CLIENTE_ID", fila.get("CLIENTE", ""))).strip().upper()
            p = str(fila.get("PRODUCTO_ID", fila.get("PRODUCTO", ""))).strip().upper()
            if c == cliente_buscado and p == producto_buscado:
                nombre = str(fila.get("NOMBRE_ES", fila.get("PRODUCTO", fila.get("PRODUCTO_ID", ""))))
                return {
                    "encontrado": True,
                    "nombre_es": nombre,
                    "nombre_en": str(fila.get("NOMBRE_EN", nombre)),
                    "hs_code": str(fila.get("HS_CODE", "")),
                    "valor_usd": _parse_float(fila.get("VALOR_USD", fila.get("VALOR DECLARADO", 0))),
                    "unidades": int(fila.get("UNIDADES", 1)),
                    "peso_kg": _parse_float(fila.get("PESO_KG", fila.get("PESO", 0))),
                    "largo": _parse_float(fila.get("LARGO", 0)),
                    "ancho": _parse_float(fila.get("ANCHO", 0)),
                    "alto": _parse_float(fila.get("ALTO", 0)),
                }
        return {"encontrado": False}
    except Exception as e:
        print(f"[sheets] Error al buscar producto: {e}")
        return {"encontrado": False}


# ─────────────────────────────────────────────
# JOB SEMANAL — ACTUALIZACIÓN DE COSTOS FEDEX
# ─────────────────────────────────────────────

def actualizar_costo_fedex_en_coti(cliente_id: str, producto_id: str, destino_pais: str, nuevo_costo_ars: float) -> bool:
    """
    Actualiza la columna COSTO_FEDEX_ARS en la hoja COTI.
    Llamado por el job semanal de APScheduler.
    """
    try:
        doc = _abrir_sheet()
        hoja = doc.worksheet("COTI")
        registros = hoja.get_all_records()
        encabezados = hoja.row_values(1)

        col_costo = encabezados.index("COSTO_FEDEX_ARS") + 1 if "COSTO_FEDEX_ARS" in encabezados else None
        col_margen = encabezados.index("MARGEN_ARS") + 1 if "MARGEN_ARS" in encabezados else None
        col_precio = encabezados.index("PRECIO_ARS") + 1 if "PRECIO_ARS" in encabezados else None

        if not col_costo:
            print("[sheets] Columna COSTO_FEDEX_ARS no encontrada en COTI")
            return False

        for idx, fila in enumerate(registros, start=2):
            c = str(fila.get("CLIENTE_ID", "")).strip().upper()
            p = str(fila.get("PRODUCTO_ID", "")).strip().upper()
            d = str(fila.get("DESTINO_PAIS", "")).strip().upper()

            if c == cliente_id.upper() and p == producto_id.upper() and d == destino_pais.upper():
                hoja.update_cell(idx, col_costo, nuevo_costo_ars)

                if col_margen and col_precio:
                    precio_ars = float(str(fila.get("PRECIO_ARS", 0)).replace(",", ".") or 0)
                    margen = precio_ars - nuevo_costo_ars
                    hoja.update_cell(idx, col_margen, round(margen, 2))

                print(f"[sheets] Actualizado: {cliente_id}/{producto_id}/{destino_pais} → ARS {nuevo_costo_ars}")
                return True

        print(f"[sheets] No se encontró fila para actualizar: {cliente_id}/{producto_id}/{destino_pais}")
        return False
    except Exception as e:
        print(f"[sheets] Error al actualizar costo FedEx: {e}")
        return False


def obtener_todas_las_combinaciones_coti() -> list[dict]:
    """
    Retorna todas las filas de COTI para el job semanal.
    """
    try:
        return _leer_hoja("COTI")
    except Exception as e:
        print(f"[sheets] Error al leer COTI: {e}")
        return []
