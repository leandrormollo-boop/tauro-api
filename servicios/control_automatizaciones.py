"""Estado durable de tareas críticas; sin datos de clientes ni respuestas del operador."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import wraps
import logging

from core.database import get_conn

log = logging.getLogger(__name__)
TAREAS = {
    "tracking_dhl_diario": ("Rastreo DHL y guías descartadas", 30, "/admin/home#vigilancia"),
    "tracking_dhl_vigilancia": ("Segunda ronda DHL retenidos", 30, "/admin/home#vigilancia"),
    "facturas_dhl_gmail": ("Recepción de facturas DHL", 108, "/admin/facturas-internacionales"),
    "tarifas_checkout": ("Actualización de tarifas de tienda", 30, "/admin/bandeja"),
}
_ESTADOS_OMITIDOS = {"DESHABILITADA", "SIN_CONECTAR", "EN_CURSO"}
_RESULTADOS_ERROR = {"ORIGEN_RECHAZADO", "REVISION_MANUAL"}
_RESULTADOS_PARCIAL = {"REINTENTO_DIFERIDO"}
_RESULTADOS_OK = {"IMPORTADO", "PARA_REVISION", "YA_PROCESADO"}


def _clasificar_resultados_anidados(resultados):
    """Reduce sólo conteos conocidos; un estado nuevo falla sin inventar OK."""
    if not isinstance(resultados, dict):
        return "SIN_CONFIRMAR"
    activos = set()
    for estado, cantidad in resultados.items():
        if isinstance(cantidad, bool) or not isinstance(cantidad, int) or cantidad < 0:
            return "SIN_CONFIRMAR"
        if cantidad:
            activos.add(str(estado).upper())
    if activos & _RESULTADOS_ERROR:
        return "ERROR"
    if activos & _RESULTADOS_PARCIAL:
        return "PARCIAL"
    if activos - _RESULTADOS_ERROR - _RESULTADOS_PARCIAL - _RESULTADOS_OK:
        return "SIN_CONFIRMAR"
    return None


def clasificar_resultado(resultado):
    """Un wrapper que atrapa errores no se confunde con una ejecución sana."""
    if not isinstance(resultado, dict):
        return "SIN_CONFIRMAR"
    estado = str(resultado.get("estado", "")).upper()
    if (resultado.get("ok") is False or resultado.get("error")
            or resultado.get("errores") or resultado.get("fallidas")
            or estado.startswith("ERROR") or estado == "REAUTORIZAR"):
        return "ERROR"
    if isinstance(resultado.get("reemplazadas"), dict):
        anidado = clasificar_resultado(resultado["reemplazadas"])
        if anidado in {"ERROR", "SIN_CONFIRMAR"}:
            return anidado
        if anidado in {"OMITIDA", "PARCIAL"}:
            return "PARCIAL"
    if "resultados" in resultado:
        anidado = _clasificar_resultados_anidados(resultado["resultados"])
        if anidado is not None:
            return anidado
    if estado in _ESTADOS_OMITIDOS or resultado.get("omitido") or resultado.get("motivo"):
        return "OMITIDA"
    if estado == "PAGINACION_PENDIENTE":
        return "PARCIAL"
    if resultado.get("ok") is True or estado == "OK" or resultado.get("guardadas", 0) > 0:
        return "OK"
    return "SIN_CONFIRMAR"


def registrar(clave, estado, instante=None):
    if clave not in TAREAS:
        raise ValueError("Tarea no registrada")
    instante = instante or datetime.now(timezone.utc)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO automatizaciones_estado
            (clave, estado, ultima_ejecucion, ultimo_exito)
            VALUES (%s,%s,%s,CASE WHEN %s='OK' THEN %s END)
            ON CONFLICT(clave) DO UPDATE SET estado=EXCLUDED.estado,
              ultima_ejecucion=EXCLUDED.ultima_ejecucion,
              ultimo_exito=COALESCE(EXCLUDED.ultimo_exito,automatizaciones_estado.ultimo_exito)
            """, (clave, estado, instante, estado, instante))
        cur.execute("""INSERT INTO automatizaciones_historial(clave,estado,fecha)
                       VALUES (%s,%s,%s)""", (clave, estado, instante))
        cur.execute("""DELETE FROM automatizaciones_historial WHERE clave=%s AND id NOT IN
            (SELECT id FROM automatizaciones_historial WHERE clave=%s ORDER BY id DESC LIMIT 100)""",
                    (clave, clave))


def _registrar_seguro(clave, estado):
    try:
        registrar(clave, estado)
    except Exception as exc:
        # No filtrar DSN, payloads ni PII; un registro ausente aparecerá sin verificar/atrasado.
        log.error("No se registró control de %s: %s", clave, type(exc).__name__)


def observar(clave, funcion):
    if clave not in TAREAS:
        raise ValueError("Tarea no registrada")

    @wraps(funcion)
    def ejecutar(*args, **kwargs):
        _registrar_seguro(clave, "EN_CURSO")
        try:
            resultado = funcion(*args, **kwargs)
        except Exception:
            _registrar_seguro(clave, "ERROR")
            raise
        _registrar_seguro(clave, clasificar_resultado(resultado))
        return resultado
    return ejecutar


def resumen(*, ahora=None):
    ahora = ahora or datetime.now(timezone.utc)
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT clave,estado,ultima_ejecucion,ultimo_exito FROM automatizaciones_estado")
            filas = {r["clave"]: dict(r) for r in cur.fetchall()}
            cur.execute("""SELECT clave,estado,fecha FROM automatizaciones_historial
                           ORDER BY id DESC LIMIT 20""")
            historial = list(cur.fetchall())
    except Exception as exc:
        log.warning("Control de automatizaciones no disponible: %s", type(exc).__name__)
        return {"disponible": False, "items": [], "historial": [], "atencion": 1}
    items = []
    for clave, (nombre, horas, url) in TAREAS.items():
        f = filas.get(clave, {})
        ultima = f.get("ultima_ejecucion")
        atrasada = ultima is not None and ahora - ultima > timedelta(hours=horas)
        estado = "ATRASADA" if atrasada else f.get("estado", "SIN_EVIDENCIA")
        items.append({**f, "clave": clave, "nombre": nombre, "url": url,
                      "estado": estado, "atencion": estado != "OK", "max_horas": horas})
    return {"disponible": True, "items": items, "historial": historial,
            "atencion": sum(i["atencion"] for i in items)}
