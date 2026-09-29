from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from contextlib import closing
from pathlib import Path

import psycopg2
import pytest

from scripts import verificar_restauracion as restore


ROOT = Path(__file__).resolve().parents[1]


def _pg_tool(nombre: str) -> str:
    detectado = shutil.which(nombre)
    if detectado:
        return detectado
    return str(Path("/opt/homebrew/bin") / nombre)


def test_destino_restringido_a_base_local_de_ensayo():
    with pytest.raises(restore.ErrorVerificacion, match="host"):
        restore.validar_destino("db.example.com", "tauro_restore_test_demo")
    with pytest.raises(restore.ErrorVerificacion, match="base"):
        restore.validar_destino("127.0.0.1", "tauro_produccion")
    restore.validar_destino("localhost", "tauro_restore_test_demo_1")


def test_pg_restore_usa_flags_seguros_sin_dsn_en_argumentos(tmp_path, monkeypatch):
    archive = tmp_path / "fixture.dump"
    archive.write_bytes(b"PGDMP")
    llamadas = []

    def falso_run(argv, **kwargs):
        llamadas.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(restore.subprocess, "run", falso_run)
    monkeypatch.setenv("DATABASE_URL", "postgresql://dato-sensible.example/prod")
    monkeypatch.setenv("PGPASSWORD", "secreto-local")
    restore._ejecutar_pg_restore(
        archive,
        host="127.0.0.1",
        port=5432,
        user="tester",
        database="tauro_restore_test_demo",
        pg_restore="/opt/homebrew/bin/pg_restore",
    )

    argv, opciones = llamadas[1]
    assert argv == [
        "/opt/homebrew/bin/pg_restore",
        "--exit-on-error",
        "--single-transaction",
        "--no-owner",
        "--no-privileges",
        "--dbname",
        "tauro_restore_test_demo",
        str(archive),
    ]
    assert "--clean" not in argv and "--create" not in argv
    assert all("secreto-local" not in parte and "postgresql://" not in parte for parte in argv)
    assert opciones["env"]["PGDATABASE"] == "tauro_restore_test_demo"
    assert opciones["env"]["PGPASSWORD"] == "secreto-local"
    assert opciones["env"]["PGHOSTADDR"] == "127.0.0.1"
    assert "DATABASE_URL" not in opciones["env"]


def test_manifest_detecta_diferencias_sin_inspeccionar_filas():
    actual = {
        "row_counts": {"envios": 1, "pagos": 1, "solicitudes_guia": 1},
        "document_bytes": {"envios.factura_pdf": 4},
    }
    restore.comparar_manifest({"row_counts": {"envios": 1}}, actual)
    with pytest.raises(restore.ErrorVerificacion, match="esperado 2, restaurado 1"):
        restore.comparar_manifest({"row_counts": {"envios": 2}}, actual)


def _pg_env(database: str) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    env.update(PGHOST="127.0.0.1", PGPORT="5432", PGDATABASE=database)
    return env


def _herramientas_y_servidor_disponibles() -> None:
    requeridas = ["createdb", "dropdb", "pg_dump", "psql", "pg_restore"]
    if not all(Path(_pg_tool(nombre)).is_file() for nombre in requeridas):
        pytest.skip("CLI PostgreSQL local no disponible")
    prueba = subprocess.run(
        [_pg_tool("psql"), "-d", "postgres", "-c", "SELECT 1"],
        env=_pg_env("postgres"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if prueba.returncode:
        pytest.skip("PostgreSQL local no disponible")


def _run_pg(binario: str, *args: str, database: str = "postgres") -> None:
    resultado = subprocess.run(
        [_pg_tool(binario), *args],
        env=_pg_env(database),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    assert resultado.returncode == 0, f"falló {binario} en fixture local"


def test_drill_completo_con_dump_ficticio_local(tmp_path):
    _herramientas_y_servidor_disponibles()
    sufijo = uuid.uuid4().hex[:12]
    source = f"tauro_restore_test_source_{sufijo}"
    destination = f"tauro_restore_test_dest_{sufijo}"
    archive = tmp_path / "fixture.dump"
    manifest = tmp_path / "manifest.json"
    report = tmp_path / "report.json"

    try:
        _run_pg("createdb", source)
        _run_pg("createdb", destination)
        _run_pg("psql", "-v", "ON_ERROR_STOP=1", "-f", str(ROOT / "sql/schema.sql"), database=source)

        with closing(psycopg2.connect(host="127.0.0.1", port=5432, dbname=source)) as conn:
            with conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO clientes (cliente_id, email, nombre, test) VALUES (%s, %s, %s, TRUE)",
                    ("RESTORE_FIXTURE", "restore-fixture@example.invalid", "Fixture restore"),
                )
                cur.execute(
                    """
                    INSERT INTO solicitudes_guia (
                        cliente_id, producto_alias, destino_pais, dest_nombre,
                        dest_direccion, dest_ciudad, dest_zip, estado,
                        label_pdf, commercial_invoice_pdf
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'SOLICITADO', %s, %s)
                    """,
                    (
                        "RESTORE_FIXTURE", "caja-test", "UY", "Destino ficticio",
                        "Calle de prueba 1", "Montevideo", "00000", b"label-test", b"invoice-test",
                    ),
                )
                # La columna documental de envíos es legado de sólo lectura.
                # La fixture representa una fila histórica y restablece el
                # control antes del dump, para que readiness también lo audite.
                cur.execute("ALTER TABLE envios DISABLE TRIGGER trg_proteger_fc_legacy_envios")
                cur.execute(
                    """
                    INSERT INTO envios (cliente_id, fecha, monto_ars, estado, factura_pdf)
                    VALUES (%s, CURRENT_DATE, 100.00, 'ACTIVO', %s)
                    """,
                    ("RESTORE_FIXTURE", b"factura-test"),
                )
                cur.execute("ALTER TABLE envios ENABLE TRIGGER trg_proteger_fc_legacy_envios")
                cur.execute(
                    """
                    INSERT INTO pagos (cliente_id, fecha, monto_ars, estado, comprobante)
                    VALUES (%s, CURRENT_DATE, 100.00, 'APROBADO', %s)
                    """,
                    ("RESTORE_FIXTURE", b"pago-test"),
                )

        _run_pg("pg_dump", "-Fc", "-f", str(archive), database=source)
        esperado = {
            "row_counts": {"envios": 1, "pagos": 1, "solicitudes_guia": 1},
            "document_bytes": {
                "envios.factura_pdf": len(b"factura-test"),
                "pagos.comprobante": len(b"pago-test"),
                "solicitudes_guia.label_pdf": len(b"label-test"),
                "solicitudes_guia.commercial_invoice_pdf": len(b"invoice-test"),
            },
        }
        manifest.write_text(json.dumps(esperado), encoding="utf-8")

        resultado = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/verificar_restauracion.py"),
                "--archive", str(archive),
                "--host", "127.0.0.1",
                "--database", destination,
                "--pg-restore", _pg_tool("pg_restore"),
                "--manifest", str(manifest),
                "--report", str(report),
            ],
            cwd=ROOT,
            env=_pg_env(destination),
            text=True,
            capture_output=True,
            check=False,
        )
        assert resultado.returncode == 0, resultado.stderr
        informe = json.loads(report.read_text(encoding="utf-8"))
        assert informe["status"] == "OK"
        assert informe["row_counts"] == esperado["row_counts"]
        assert informe["document_bytes"] == esperado["document_bytes"]
        assert informe["manifest"] == {"provided": True, "matched": True}
        assert all(informe["readiness"].values())
        assert "RESTORE_FIXTURE" not in resultado.stdout
        assert str(archive) not in resultado.stdout

        segundo_intento = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/verificar_restauracion.py"),
                "--archive", str(archive),
                "--host", "127.0.0.1",
                "--database", destination,
                "--pg-restore", _pg_tool("pg_restore"),
            ],
            cwd=ROOT,
            env=_pg_env(destination),
            text=True,
            capture_output=True,
            check=False,
        )
        assert segundo_intento.returncode == 2
        assert "no está vacía" in segundo_intento.stderr
        with closing(psycopg2.connect(host="127.0.0.1", port=5432, dbname=destination)) as conn:
            with conn, conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM envios")
                assert cur.fetchone()[0] == 1
    finally:
        for database in (destination, source):
            subprocess.run(
                [_pg_tool("dropdb"), "--if-exists", database],
                env=_pg_env("postgres"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
