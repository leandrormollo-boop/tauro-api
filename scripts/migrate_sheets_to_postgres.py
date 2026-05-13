#!/usr/bin/env python3
"""
migrate_sheets_to_postgres.py
==============================
Migración ONE-TIME de Google Sheets → PostgreSQL.

Corre LOCALMENTE (no en Railway). Necesita:
  - DATABASE_URL en el entorno (apuntando a la DB de Railway)
  - GOOGLE_CREDENTIALS_FILE o GOOGLE_CREDENTIALS_JSON (igual que la API)

Uso:
  export DATABASE_URL="postgresql://..."
  python scripts/migrate_sheets_to_postgres.py

Qué migra:
  PERFILES          → clientes
  SESSIONS          → sessions     (solo las no expiradas)
  RUTAS_DEFAULT     → rutas
  PRODUCTOS_CATALOGO → productos
  PAGOS             → pagos
  CONFIG            → config
  ENVIOS 2026       → envios       (sheet operativo TAURO 2026)
"""

import os
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

# Asegurar que el path raíz del proyecto está en sys.path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import psycopg2
import psycopg2.extras
import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv(ROOT / ".env")

# ── Config ────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL", "")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL no está configurada.")
    print("También podés usar DATABASE_PUBLIC_URL si vas a correr la migración desde tu Mac.")
    sys.exit(1)

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

TAURO_2026_SHEET_ID = "1-c83aUq5LOUM5RkFrcaZaPhPDz3mC3Mf1blecJcrPGg"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _abrir_gc():
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds_path = os.getenv("GOOGLE_CREDENTIALS_FILE", str(ROOT / "credenciales.json"))
        creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    return gspread.authorize(creds)


def _abrir_sheet_api(gc):
    sheet_url = os.getenv("GOOGLE_SHEET_URL")
    if not sheet_url:
        raise RuntimeError("GOOGLE_SHEET_URL no está configurada.")
    return gc.open_by_url(sheet_url)


def _parse_monto(v) -> float:
    if not v or v in ("-", "N/A", "n/a"):
        return 0.0
    s = str(v).replace("$", "").replace(" ", "").replace("\xa0", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_bool(v) -> bool:
    return str(v).strip().upper() in ("TRUE", "SI", "1")


def main():
    print("=== Migración Sheets → PostgreSQL ===\n")

    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    cur = conn.cursor()

    gc = _abrir_gc()
    sh_api = None
    try:
        sh_api = _abrir_sheet_api(gc)
        print("✓ Conectado al Sheet API")
    except Exception as e:
        print(f"✗ No se pudo abrir Sheet API: {e}")
        sys.exit(1)

    try:
        # ── 1. PERFILES → clientes ──────────────────────────────
        print("\n[1/7] Migrando PERFILES → clientes...")
        hoja = sh_api.worksheet("PERFILES")
        rows = hoja.get_all_records()
        migrados = 0
        for r in rows:
            cliente_id = str(r.get("CLIENTE_ID") or r.get("CLIENTE") or "").strip().upper()
            email = str(r.get("EMAIL", "")).strip().lower()
            if not cliente_id or not email:
                continue
            activo = _parse_bool(r.get("ACTIVO", "TRUE"))
            try:
                markup_pct = float(str(r.get("MARKUP_PCT", 25)).replace(",", ".") or 25)
            except (ValueError, TypeError):
                markup_pct = 25.0

            cur.execute(
                """
                INSERT INTO clientes
                    (cliente_id, email, api_key, markup_pct, activo,
                     nombre, cuit, direccion, cp, ciudad, pais, telefono, notas)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (cliente_id) DO UPDATE SET
                    email=EXCLUDED.email, markup_pct=EXCLUDED.markup_pct,
                    activo=EXCLUDED.activo, nombre=EXCLUDED.nombre,
                    cuit=EXCLUDED.cuit, direccion=EXCLUDED.direccion,
                    cp=EXCLUDED.cp, ciudad=EXCLUDED.ciudad,
                    pais=EXCLUDED.pais, telefono=EXCLUDED.telefono
                """,
                (
                    cliente_id, email,
                    str(r.get("API_KEY", "")).strip() or None,
                    markup_pct, activo,
                    str(r.get("NOMBRE", "")).strip() or None,
                    str(r.get("CUIT", "")).strip() or None,
                    str(r.get("DIRECCION", "")).strip() or None,
                    str(r.get("CP", "")).strip() or None,
                    str(r.get("CIUDAD", "")).strip() or None,
                    str(r.get("PAIS", "AR")).strip() or "AR",
                    str(r.get("TELEFONO", "")).strip() or None,
                    str(r.get("NOTAS", "")).strip() or None,
                ),
            )
            migrados += 1
        print(f"  → {migrados} clientes migrados")

        # ── 2. RUTAS_DEFAULT → rutas ─────────────────────────────
        print("\n[2/7] Migrando RUTAS_DEFAULT → rutas...")
        hoja = sh_api.worksheet("RUTAS_DEFAULT")
        rows = hoja.get_all_records()
        migrados = 0
        for r in rows:
            ruta_id = str(r.get("RUTA_ID", "")).strip().upper()
            if not ruta_id:
                continue
            try:
                cur.execute(
                    """
                    INSERT INTO rutas
                        (ruta_id, origen_pais, origen_ciudad, origen_zip,
                         destino_pais, destino_ciudad, destino_zip, dias_estimados, activa)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (ruta_id) DO UPDATE SET
                        origen_pais=EXCLUDED.origen_pais, origen_ciudad=EXCLUDED.origen_ciudad,
                        origen_zip=EXCLUDED.origen_zip, destino_pais=EXCLUDED.destino_pais,
                        destino_ciudad=EXCLUDED.destino_ciudad, destino_zip=EXCLUDED.destino_zip,
                        dias_estimados=EXCLUDED.dias_estimados, activa=EXCLUDED.activa
                    """,
                    (
                        ruta_id,
                        str(r.get("ORIGEN_PAIS", "")).strip().upper(),
                        str(r.get("ORIGEN_CITY", "")).strip().upper(),
                        str(r.get("ORIGEN_ZIP", "")).strip(),
                        str(r.get("DESTINO_PAIS", "")).strip().upper(),
                        str(r.get("DESTINO_CITY", "")).strip().upper(),
                        str(r.get("DESTINO_ZIP", "")).strip(),
                        int(r.get("DIAS_ESTIMADOS", 5) or 5),
                        _parse_bool(r.get("ACTIVA", "TRUE")),
                    ),
                )
                migrados += 1
            except Exception as e:
                print(f"  ! Ruta {ruta_id}: {e}")
        print(f"  → {migrados} rutas migradas")

        # ── 3. PRODUCTOS_CATALOGO → productos ────────────────────
        print("\n[3/7] Migrando PRODUCTOS_CATALOGO → productos...")
        hoja = sh_api.worksheet("PRODUCTOS_CATALOGO")
        rows = hoja.get_all_records()
        migrados = 0
        for r in rows:
            cliente_id = str(r.get("CLIENTE", "")).strip().upper()
            alias = str(r.get("ALIAS_INTERNO", "")).strip()
            if not cliente_id or not alias:
                continue
            try:
                cur.execute(
                    """
                    INSERT INTO productos
                        (cliente_id, alias_interno, nombre_invoice, hs_code,
                         largo_cm, ancho_cm, alto_cm, peso_kg, valor_usd_default, activo)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (cliente_id, alias_interno) DO NOTHING
                    """,
                    (
                        cliente_id, alias,
                        str(r.get("NOMBRE_INVOICE", "")).strip(),
                        str(r.get("HS_CODE", "")).strip(),
                        float(r.get("LARGO_CM", 0) or 0),
                        float(r.get("ANCHO_CM", 0) or 0),
                        float(r.get("ALTO_CM", 0) or 0),
                        float(r.get("PESO_KG", 0) or 0),
                        float(r.get("VALOR_USD_DEFAULT", 0) or 0),
                        _parse_bool(r.get("ACTIVO", "FALSE")),
                    ),
                )
                migrados += 1
            except Exception as e:
                print(f"  ! Producto {cliente_id}/{alias}: {e}")
        print(f"  → {migrados} productos migrados")

        # ── 4. PAGOS → pagos ─────────────────────────────────────
        print("\n[4/7] Migrando PAGOS → pagos...")
        hoja = sh_api.worksheet("PAGOS")
        rows = hoja.get_all_records()
        migrados = 0
        for r in rows:
            cliente_id = str(r.get("CLIENTE", "")).strip().upper()
            fecha_raw = str(r.get("FECHA", "")).strip()
            monto = _parse_monto(r.get("MONTO_ARS", 0))
            if not cliente_id or not fecha_raw or monto <= 0:
                continue
            # Parsear fecha
            fecha = None
            for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
                try:
                    fecha = datetime.strptime(fecha_raw, fmt).date()
                    break
                except ValueError:
                    continue
            if not fecha:
                print(f"  ! Pago con fecha inválida: {fecha_raw}")
                continue
            try:
                cur.execute(
                    """
                    INSERT INTO pagos (cliente_id, fecha, monto_ars, metodo, referencia, nota)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        cliente_id, fecha, monto,
                        str(r.get("METODO", "transferencia")).strip() or "transferencia",
                        str(r.get("REFERENCIA", "")).strip() or None,
                        str(r.get("NOTA", "")).strip() or None,
                    ),
                )
                migrados += 1
            except Exception as e:
                print(f"  ! Pago {cliente_id}/{fecha_raw}: {e}")
        print(f"  → {migrados} pagos migrados")

        # ── 5. CONFIG ────────────────────────────────────────────
        print("\n[5/7] Migrando CONFIG → config...")
        try:
            hoja = sh_api.worksheet("CONFIG")
            rows = hoja.get_all_records()
            migrados = 0
            for r in rows:
                param = str(r.get("PARAMETRO", "")).strip().upper()
                valor = str(r.get("VALOR", "")).strip()
                if not param or not valor:
                    continue
                cur.execute(
                    "INSERT INTO config (parametro, valor) VALUES (%s, %s) ON CONFLICT (parametro) DO UPDATE SET valor=EXCLUDED.valor",
                    (param, valor),
                )
                migrados += 1
            print(f"  → {migrados} parámetros migrados")
        except Exception as e:
            print(f"  ! CONFIG: {e} (saltando)")

        # ── 6. ENVIOS 2026 → envios ──────────────────────────────
        print("\n[6/7] Migrando ENVIOS 2026 → envios...")
        try:
            sh_op = gc.open_by_key(TAURO_2026_SHEET_ID)
            hoja = sh_op.worksheet("ENVIOS 2026")
            valores = hoja.get_all_values()
            IDX_FECHA = 1
            IDX_NRO_FC = 9
            IDX_FACTURADO = 10
            IDX_EMPRESA = 21
            IDX_ESTADO_S = 18
            IDX_ESTADO_W = 22

            migrados = 0
            for fila in valores[1:]:
                if len(fila) <= IDX_EMPRESA:
                    continue
                cliente_id = str(fila[IDX_EMPRESA]).strip().upper()
                if not cliente_id:
                    continue
                monto = _parse_monto(fila[IDX_FACTURADO] if len(fila) > IDX_FACTURADO else 0)
                if monto <= 0:
                    continue
                # Estado
                estado_s = str(fila[IDX_ESTADO_S]).strip().upper() if len(fila) > IDX_ESTADO_S else ""
                estado_w = str(fila[IDX_ESTADO_W]).strip().upper() if len(fila) > IDX_ESTADO_W else ""
                if any(x in estado_s for x in ("CANCEL",)) or any(x in estado_w for x in ("CANCEL",)):
                    estado = "CANCELADO"
                elif any(x in estado_s for x in ("NC",)) or any(x in estado_w for x in ("NC",)):
                    estado = "NC"
                else:
                    estado = "ACTIVO"

                fecha_raw = str(fila[IDX_FECHA]).strip()
                fecha = None
                for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
                    try:
                        fecha = datetime.strptime(fecha_raw, fmt).date()
                        break
                    except ValueError:
                        continue
                if not fecha:
                    continue

                nro_fc = str(fila[IDX_NRO_FC]).strip() if len(fila) > IDX_NRO_FC else ""

                # Solo insertar si el cliente existe en la tabla
                cur.execute("SELECT 1 FROM clientes WHERE cliente_id=%s", (cliente_id,))
                if not cur.fetchone():
                    continue

                try:
                    cur.execute(
                        """
                        INSERT INTO envios (cliente_id, fecha, nro_fc, monto_ars, estado)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (cliente_id, fecha, nro_fc or None, monto, estado),
                    )
                    migrados += 1
                except Exception as e:
                    print(f"  ! Envío {cliente_id}/{fecha_raw}: {e}")

            print(f"  → {migrados} envíos migrados")
        except Exception as e:
            print(f"  ! ENVIOS 2026: {e} (saltando)")

        # ── 7. SESSIONS (solo las activas) ───────────────────────
        print("\n[7/7] Migrando SESSIONS activas → sessions...")
        try:
            hoja = sh_api.worksheet("SESSIONS")
            rows = hoja.get_all_records()
            ahora = datetime.now(tz=timezone.utc)
            migrados = 0
            for r in rows:
                token = str(r.get("TOKEN", "")).strip()
                email = str(r.get("EMAIL", "")).strip().lower()
                cliente_id = str(r.get("CLIENTE", "")).strip().upper()
                usado = _parse_bool(r.get("USADO", "FALSE"))
                if not token or usado:
                    continue
                expira_str = str(r.get("EXPIRA", "")).strip()
                expira = None
                for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y"):
                    try:
                        expira = datetime.strptime(expira_str, fmt).replace(tzinfo=timezone.utc)
                        break
                    except ValueError:
                        continue
                if not expira or expira < ahora:
                    continue
                # Solo si el cliente existe
                cur.execute("SELECT 1 FROM clientes WHERE cliente_id=%s", (cliente_id,))
                if not cur.fetchone():
                    continue
                try:
                    cur.execute(
                        """
                        INSERT INTO sessions (token, email, cliente_id, expira_at)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (token) DO NOTHING
                        """,
                        (token, email, cliente_id, expira),
                    )
                    migrados += 1
                except Exception as e:
                    print(f"  ! Session {token[:8]}...: {e}")
            print(f"  → {migrados} sesiones migradas")
        except Exception as e:
            print(f"  ! SESSIONS: {e} (saltando)")

        conn.commit()
        print("\n✅ Migración completada con éxito.")

    except Exception as e:
        conn.rollback()
        print(f"\n✗ Error en migración: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
