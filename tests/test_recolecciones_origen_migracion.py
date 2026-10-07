"""Migración de unicidad de retiros por origen, sin tocar datos reales."""
from __future__ import annotations

import json
import os
from pathlib import Path
import uuid

import pytest


psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_SQL = (ROOT / "sql" / "schema.sql").read_text(encoding="utf-8")
MIGRACION = SCHEMA_SQL[
    SCHEMA_SQL.index("CREATE TABLE IF NOT EXISTS recolecciones"):
    SCHEMA_SQL.index("-- ── Configuración global")
]


def _url_pruebas() -> str:
    url = os.getenv("TAURO_TEST_DATABASE_URL")
    if not url:
        pytest.skip("TAURO_TEST_DATABASE_URL no configurada")
    return url.replace("postgres://", "postgresql://", 1)


@pytest.fixture()
def db():
    conn = psycopg2.connect(_url_pruebas())
    conn.autocommit = True
    esquema = "test_recoleccion_origen_" + uuid.uuid4().hex[:12]
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{esquema}"')
            cur.execute(f'SET search_path TO "{esquema}"')
            cur.execute("""
                CREATE TABLE clientes (cliente_id TEXT PRIMARY KEY);
                CREATE TABLE solicitudes_guia (
                    id SERIAL PRIMARY KEY,
                    cliente_id TEXT NOT NULL REFERENCES clientes(cliente_id),
                    remitente_pais TEXT,
                    remitente_estado TEXT,
                    remitente_ciudad TEXT,
                    remitente_zip TEXT,
                    remitente_direccion TEXT
                );
                CREATE TABLE recolecciones (
                    id SERIAL PRIMARY KEY,
                    cliente_id TEXT NOT NULL REFERENCES clientes(cliente_id),
                    courier TEXT NOT NULL DEFAULT 'FEDEX',
                    fecha DATE NOT NULL,
                    ready_time TEXT NOT NULL DEFAULT '09:00',
                    close_time TEXT NOT NULL DEFAULT '17:00',
                    bultos INTEGER NOT NULL DEFAULT 1,
                    peso_kg REAL NOT NULL DEFAULT 1,
                    direccion TEXT,
                    instrucciones TEXT,
                    estado TEXT NOT NULL DEFAULT 'AGENDADA',
                    confirmation_code TEXT,
                    ubicacion TEXT,
                    solicitud_id INTEGER REFERENCES solicitudes_guia(id),
                    courier_message_reference TEXT,
                    error_operativo TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE UNIQUE INDEX uq_recoleccion_cliente_fecha_abierta_v2
                    ON recolecciones (cliente_id, fecha)
                    WHERE estado IN
                        ('AGENDANDO', 'AGENDADA', 'CANCELANDO', 'VERIFICAR_COURIER');
                CREATE UNIQUE INDEX uq_recoleccion_solicitud_abierta_v2
                    ON recolecciones (solicitud_id)
                    WHERE solicitud_id IS NOT NULL
                      AND estado IN
                        ('AGENDANDO', 'AGENDADA', 'CANCELANDO', 'VERIFICAR_COURIER');
            """)
        yield conn
    finally:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute(f'DROP SCHEMA IF EXISTS "{esquema}" CASCADE')
        conn.close()


def _insertar_cliente_y_guias(cur):
    cur.execute("INSERT INTO clientes(cliente_id) VALUES ('WAIMAO')")
    cur.executemany(
        """
        INSERT INTO solicitudes_guia
            (id, cliente_id, remitente_pais, remitente_estado,
             remitente_ciudad, remitente_zip, remitente_direccion)
        VALUES (%s, 'WAIMAO', %s, %s, %s, %s, %s)
        """,
        [
            (11, "AR", "Buenos Aires", "Buenos Aires", "1425", "Fray Justo 100"),
            (12, "CN", "Zhejiang", "Yiwu", "322000", "Floor 12 Building 1"),
            (13, "CN", "Zhejiang", "Yiwu", "322000", "Floor 12 Building 1"),
        ],
    )


def _origen_yiwu():
    return {
        "pais": "CN",
        "estado": "Zhejiang",
        "ciudad": "Yiwu",
        "zip": "322000",
        "calle": "Floor 12 Building 1",
    }


def test_migra_backfill_conservador_y_permite_otro_origen_misma_fecha(db):
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        _insertar_cliente_y_guias(cur)
        cur.execute("""
            INSERT INTO recolecciones
                (id, cliente_id, courier, fecha, direccion, estado, solicitud_id)
            VALUES
                (4, 'WAIMAO', 'DHL', '2026-10-02',
                 'Fray Justo 100, Buenos Aires', 'AGENDADA', 11)
        """)

        cur.execute(MIGRACION)

        cur.execute("""
            SELECT origen_retiro, origen_clave
            FROM recolecciones WHERE id=4
        """)
        fila = cur.fetchone()
        assert fila["origen_retiro"] == {
            "pais": "AR",
            "estado": "Buenos Aires",
            "ciudad": "Buenos Aires",
            "zip": "1425",
            "calle": "Fray Justo 100",
        }
        assert fila["origen_clave"] == (
            "ar|buenosaires|buenosaires|1425|frayjusto100"
        )

        cur.execute(
            """
            INSERT INTO recolecciones
                (cliente_id, courier, fecha, direccion, estado, solicitud_id,
                 origen_retiro)
            VALUES
                ('WAIMAO', 'DHL', '2026-10-02', %s, 'AGENDANDO', 12, %s::jsonb)
            RETURNING id, origen_clave
            """,
            ("Floor 12 Building 1, Yiwu", json.dumps(_origen_yiwu())),
        )
        nueva = cur.fetchone()
        assert nueva["id"] != 4
        assert nueva["origen_clave"] == (
            "cn|zhejiang|yiwu|322000|floor12building1"
        )

        cur.execute("SELECT to_regclass('uq_recoleccion_cliente_fecha_abierta_v2')")
        assert cur.fetchone()["to_regclass"] is None
        cur.execute("SELECT to_regclass('uq_recoleccion_origen_fecha_abierta_v3')")
        assert cur.fetchone()["to_regclass"] is not None


def test_rerun_no_recrea_indice_global_con_dos_origenes(db):
    with db.cursor() as cur:
        _insertar_cliente_y_guias(cur)
        cur.execute(MIGRACION)
        cur.execute(
            """
            INSERT INTO recolecciones
                (cliente_id, courier, fecha, direccion, estado, solicitud_id,
                 origen_retiro)
            VALUES
                ('WAIMAO', 'DHL', '2026-10-02',
                 'Fray Justo 100, Buenos Aires', 'AGENDADA', 11,
                 %s::jsonb),
                ('WAIMAO', 'DHL', '2026-10-02',
                 'Floor 12 Building 1, Yiwu', 'AGENDADA', 12,
                 %s::jsonb)
            """,
            (
                json.dumps({
                    "pais": "AR", "estado": "Buenos Aires",
                    "ciudad": "Buenos Aires", "zip": "1425",
                    "calle": "Fray Justo 100",
                }),
                json.dumps(_origen_yiwu()),
            ),
        )

        cur.execute(MIGRACION)
        cur.execute("SELECT count(*) FROM recolecciones")
        assert cur.fetchone()[0] == 2


def test_worker_legacy_deriva_origen_y_misma_guia_sigue_bloqueada(db):
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        _insertar_cliente_y_guias(cur)
        cur.execute(MIGRACION)

        # Simula un worker anterior al deploy: omite ambas columnas nuevas.
        cur.execute("""
            INSERT INTO recolecciones
                (cliente_id, courier, fecha, direccion, estado, solicitud_id)
            VALUES
                ('WAIMAO', 'DHL', '2026-10-05',
                 'Floor 12 Building 1, Yiwu', 'AGENDANDO', 12)
            RETURNING origen_retiro, origen_clave
        """)
        fila = cur.fetchone()
        assert fila["origen_retiro"] == _origen_yiwu()
        assert fila["origen_clave"].startswith("cn|zhejiang|yiwu|")

        with pytest.raises(psycopg2.IntegrityError) as exc:
            cur.execute("""
                INSERT INTO recolecciones
                    (cliente_id, courier, fecha, direccion, estado, solicitud_id)
                VALUES
                    ('WAIMAO', 'DHL', '2026-10-06',
                     'Floor 12 Building 1, Yiwu', 'AGENDANDO', 12)
            """)
        assert exc.value.diag.constraint_name == (
            "uq_recoleccion_solicitud_abierta_v2"
        )


def test_origen_legacy_desconocido_falla_cerrado_solo_en_su_scope(db):
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        _insertar_cliente_y_guias(cur)
        # No coincide con la guía: la migración no inventa un origen.
        cur.execute("""
            INSERT INTO recolecciones
                (id, cliente_id, courier, fecha, direccion, estado, solicitud_id)
            VALUES
                (9, 'WAIMAO', 'DHL', '2026-10-07',
                 'Ubicación histórica sin fuente', 'AGENDADA', 11)
        """)
        cur.execute(MIGRACION)
        cur.execute("SELECT origen_retiro, origen_clave FROM recolecciones WHERE id=9")
        legacy = cur.fetchone()
        assert legacy["origen_retiro"] == {"_legacy_desconocido": True}
        assert legacy["origen_clave"] == "legacy-desconocido:9"

        with pytest.raises(psycopg2.IntegrityError) as exc:
            cur.execute(
                """
                INSERT INTO recolecciones
                    (cliente_id, courier, fecha, direccion, estado, solicitud_id,
                     origen_retiro)
                VALUES
                    ('WAIMAO', 'DHL', '2026-10-07', %s, 'AGENDANDO', 12,
                     %s::jsonb)
                """,
                ("Floor 12 Building 1, Yiwu", json.dumps(_origen_yiwu())),
            )
        assert exc.value.diag.constraint_name == (
            "uq_recoleccion_origen_pendiente_v3"
        )

        # El marcador desconocido no bloquea otra fecha ni otro courier.
        cur.execute(
            """
            INSERT INTO recolecciones
                (cliente_id, courier, fecha, direccion, estado, solicitud_id,
                 origen_retiro)
            VALUES
                ('WAIMAO', 'FEDEX', '2026-10-07', %s, 'AGENDANDO', 12,
                 %s::jsonb)
            RETURNING id
            """,
            ("Floor 12 Building 1, Yiwu", json.dumps(_origen_yiwu())),
        )
        assert cur.fetchone()["id"]


def test_reactivar_legacy_desconocido_no_esquiva_guard(db):
    with db.cursor() as cur:
        _insertar_cliente_y_guias(cur)
        cur.execute("""
            INSERT INTO recolecciones
                (id, cliente_id, courier, fecha, direccion, estado, solicitud_id)
            VALUES
                (9, 'WAIMAO', 'DHL', '2026-10-08',
                 'Ubicación histórica sin fuente', 'CANCELADA', 11)
        """)
        cur.execute(MIGRACION)
        cur.execute(
            """
            INSERT INTO recolecciones
                (cliente_id, courier, fecha, direccion, estado, solicitud_id,
                 origen_retiro)
            VALUES
                ('WAIMAO', 'DHL', '2026-10-08', %s, 'AGENDADA', 12,
                 %s::jsonb)
            """,
            ("Floor 12 Building 1, Yiwu", json.dumps(_origen_yiwu())),
        )

        with pytest.raises(psycopg2.IntegrityError) as exc:
            cur.execute("UPDATE recolecciones SET estado='AGENDADA' WHERE id=9")
        assert exc.value.diag.constraint_name == (
            "uq_recoleccion_origen_pendiente_v3"
        )


def test_reactivar_origen_conocido_preserva_clave_y_unicidad(db):
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        _insertar_cliente_y_guias(cur)
        cur.execute("""
            INSERT INTO recolecciones
                (cliente_id, courier, fecha, direccion, estado, solicitud_id)
            VALUES
                ('WAIMAO', 'DHL', '2026-10-09',
                 'Floor 12 Building 1, Yiwu', 'CANCELADA', 12),
                ('WAIMAO', 'DHL', '2026-10-09',
                 'Floor 12 Building 1, Yiwu', 'CANCELADA', 13)
        """)
        cur.execute(MIGRACION)
        cur.execute("""
            SELECT id, origen_clave FROM recolecciones ORDER BY id
        """)
        filas = cur.fetchall()
        assert filas[0]["origen_clave"] == filas[1]["origen_clave"]
        clave = filas[0]["origen_clave"]

        cur.execute(
            "UPDATE recolecciones SET estado='AGENDADA' WHERE id=%s RETURNING origen_clave",
            (filas[0]["id"],),
        )
        assert cur.fetchone()["origen_clave"] == clave

        with pytest.raises(psycopg2.IntegrityError) as exc:
            cur.execute(
                "UPDATE recolecciones SET estado='AGENDADA' WHERE id=%s",
                (filas[1]["id"],),
            )
        assert exc.value.diag.constraint_name == (
            "uq_recoleccion_origen_fecha_abierta_v3"
        )


def test_clave_normaliza_case_espacios_puntuacion_y_acentos(db):
    with db.cursor() as cur:
        cur.execute(MIGRACION)
        primero = {
            "pais": " AR ", "estado": "Córdoba", "ciudad": "C.A.B.A.",
            "zip": "", "calle": "Av. Santa Fé 100",
        }
        segundo = {
            "pais": "ar", "estado": "cordoba", "ciudad": "c a b a",
            "zip": "", "calle": "AV SANTA FE 100",
        }
        cur.execute(
            """
            SELECT tauro_recoleccion_origen_clave(%s::jsonb),
                   tauro_recoleccion_origen_clave(%s::jsonb)
            """,
            (json.dumps(primero), json.dumps(segundo)),
        )
        claves = cur.fetchone()
        assert claves[0] == claves[1]


def test_readiness_exige_contrato_v3_y_ausencia_del_indice_global():
    fuente = (ROOT / "core" / "database.py").read_text(encoding="utf-8")
    for campo in (
        "recolecciones_origen_columnas_listas",
        "recolecciones_origen_indice_listo",
        "recolecciones_origen_trigger_listo",
        "recolecciones_indice_global_retirado",
    ):
        assert fuente.count(campo) >= 2
    assert "uq_recoleccion_origen_fecha_abierta_v3" in fuente
    assert "trg_recoleccion_origen" in fuente
    assert "TO_REGCLASS('uq_recoleccion_cliente_fecha_abierta_v2') IS NULL" in fuente
