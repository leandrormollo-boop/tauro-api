# ============================================================
# Backup de los datos del negocio
# ============================================================
# Railway tiene sus propios backups de Postgres, pero dependen de la
# consola y del plan. Esto es una red extra bajo control de Tauro: un
# exportación JSON PARCIAL para consulta, descargable desde el admin.
# No sirve para restaurar la aplicación: omite tablas, secretos y documentos.
#
# No incluye sesiones ni tokens: son efímeros y restaurarlos sería un
# problema de seguridad, no una ayuda.
# ============================================================
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

from core.database import get_conn

# Lista explícita de la exportación; no es un manifiesto de restauración.
TABLAS = [
    "clientes",
    "rutas",
    "productos",
    "direcciones",
    "cotizaciones",
    "envios",
    "pagos",
    "solicitudes_guia",
    "tiendas_conectadas",
    "pedidos_tienda",
    "config_envio_tienda",
    "shopify_instalaciones",
]

# Config admite parámetros libres desde el admin. Aunque hoy contenga precios
# y tipo de cambio, una clave futura podría ser una credencial; se excluye la
# tabla completa en lugar de mantener una lista negra que envejezca mal.
TABLAS_EXCLUIDAS = ["config"]

# Columnas que NO viajan en el backup: secretos que, si el archivo se
# filtra, dan acceso a cuentas de terceros.
COLUMNAS_SENSIBLES = {
    "tiendas_conectadas": {"secreto"},
    "shopify_instalaciones": {"access_token", "refresh_token"},
    # api_key es una CREDENCIAL VIVA de la API B2B: con ella se cotiza y se
    # pide guías en nombre del cliente. Sale del backup por lo mismo que el
    # password_hash — el archivo se descarga y termina en un Drive cualquiera.
    "clientes": {"password_hash", "api_key", "api_key_hash"},
}


def _serializar(valor):
    if isinstance(valor, (datetime, date)):
        return valor.isoformat()
    if isinstance(valor, Decimal):
        return str(valor)  # conservar exactamente los centavos registrados
    if isinstance(valor, (bytes, memoryview)):
        return f"<binario {len(bytes(valor))} bytes>"
    return valor


def _existe_tabla(cur, tabla: str) -> bool:
    cur.execute("""
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = CURRENT_SCHEMA() AND table_name = %s
    """, (tabla,))
    return cur.fetchone() is not None


def generar_backup() -> dict:
    """
    Exportación parcial y consistente. Un error aborta la descarga completa;
    nunca entregar un archivo que parece válido con tablas fallidas adentro.
    """
    datos: dict = {
        "generado": datetime.now(timezone.utc).isoformat(),
        "version": 2,
        "tipo": "exportacion_parcial",
        "restaurable": False,
        "limitaciones": ["Sólo incluye las tablas declaradas en tablas_incluidas.",
                         "No incluye archivos adjuntos ni credenciales.",
                         "No incluye la configuración genérica de la aplicación.",
                         "No reemplaza una copia PostgreSQL comprobada mediante restauración."],
        "tablas_incluidas": [],
        "tablas_no_presentes": [],
        "tablas_excluidas": list(TABLAS_EXCLUIDAS),
        "tablas": {},
    }
    total = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            for tabla in TABLAS:
                if not _existe_tabla(cur, tabla):
                    datos["tablas_no_presentes"].append(tabla)
                    continue
                cur.execute(f"SELECT * FROM {tabla}")
                sensibles = COLUMNAS_SENSIBLES.get(tabla, set())
                filas = [{k: _serializar(v) for k, v in dict(r).items()
                          if k not in sensibles} for r in cur.fetchall()]
                datos["tablas"][tabla] = filas
                datos["tablas_incluidas"].append(tabla)
                total += len(filas)
    datos["total_filas"] = total
    return datos


def generar_backup_json() -> bytes:
    return json.dumps(generar_backup(), ensure_ascii=False, indent=2).encode("utf-8")


def resumen_backup() -> dict:
    """Cuántos registros hay para respaldar, para mostrarlo en el admin."""
    resumen = {}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                for tabla in TABLAS:
                    if not _existe_tabla(cur, tabla):
                        continue
                    cur.execute(f"SELECT COUNT(*) AS n FROM {tabla}")
                    resumen[tabla] = int(cur.fetchone()["n"])
    except Exception as e:
        print(f"[backup] resumen falló: {e}")
    return resumen
