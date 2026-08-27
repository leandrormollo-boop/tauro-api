# ============================================================
# Backup de los datos del negocio
# ============================================================
# Railway tiene sus propios backups de Postgres, pero dependen de la
# consola y del plan. Esto es una red extra bajo control de Tauro: un
# archivo JSON con TODO lo que no se puede volver a generar (clientes,
# envíos, pagos, catálogo, direcciones, integraciones), descargable
# desde el admin.
#
# No incluye sesiones ni tokens: son efímeros y restaurarlos sería un
# problema de seguridad, no una ayuda.
# ============================================================
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

from core.database import get_conn

# Orden pensado para restaurar: primero lo que otras tablas referencian.
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
    "config",
]

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
        return float(valor)
    if isinstance(valor, (bytes, memoryview)):
        return f"<binario {len(bytes(valor))} bytes>"
    return valor


def _existe_tabla(cur, tabla: str) -> bool:
    cur.execute("""
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = %s
    """, (tabla,))
    return cur.fetchone() is not None


def generar_backup() -> dict:
    """
    Snapshot completo del negocio. Las tablas que todavía no existen se
    saltean (no todas se crean hasta que se usa la feature).
    """
    datos: dict = {
        "generado": datetime.utcnow().isoformat() + "Z",
        "version": 1,
        "tablas": {},
    }
    total = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for tabla in TABLAS:
                try:
                    if not _existe_tabla(cur, tabla):
                        continue
                    cur.execute(f"SELECT * FROM {tabla}")
                    filas = []
                    sensibles = COLUMNAS_SENSIBLES.get(tabla, set())
                    for r in cur.fetchall():
                        fila = {k: _serializar(v) for k, v in dict(r).items()
                                if k not in sensibles}
                        filas.append(fila)
                    datos["tablas"][tabla] = filas
                    total += len(filas)
                except Exception as e:
                    print(f"[backup] no pude exportar {tabla}: {e}")
                    datos["tablas"][tabla] = {"error": str(e)}
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
