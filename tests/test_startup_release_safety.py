"""Guardas de release: una migración fallida no puede quedar "saludable"."""

from pathlib import Path
import inspect


def test_healthcheck_falla_si_hay_database_url_y_el_schema_no_migro(monkeypatch):
    import main

    monkeypatch.setenv("DATABASE_URL", "postgresql://configurada")
    monkeypatch.setattr(main, "_db_init_error", "duplicado histórico")

    respuesta = main.health_check()

    assert respuesta.status_code == 503
    assert b"database_schema_not_ready" in respuesta.body


def test_arranque_productivo_aborta_antes_de_jobs_si_falla_schema():
    import main

    fuente = inspect.getsource(main)
    bloque_init = fuente.index("init_db()")
    bloque_abort = fuente.index('if os.getenv("DATABASE_URL"):', bloque_init)
    migracion_secundaria = fuente.index("_ensure_hash_migrado()", bloque_init)

    assert bloque_init < bloque_abort < migracion_secundaria
    assert "se aborta el arranque" in fuente[bloque_abort:migracion_secundaria]


def test_schema_migra_timestamp_y_tracking_unico_antes_del_piloto():
    schema = (Path(__file__).resolve().parents[1] / "sql" / "schema.sql").read_text(
        encoding="utf-8"
    )

    assert "ADD COLUMN IF NOT EXISTS updated_at" in schema
    assert "uq_solicitudes_guia_courier_tracking" in schema
    assert "UPPER(courier), UPPER(BTRIM(tracking))" in schema


def test_schema_agrega_factura_despues_de_crear_envios():
    schema = (Path(__file__).resolve().parents[1] / "sql" / "schema.sql").read_text(
        encoding="utf-8"
    )

    crear = schema.index("CREATE TABLE IF NOT EXISTS envios")
    factura_pdf = schema.index(
        "ALTER TABLE IF EXISTS envios ADD COLUMN IF NOT EXISTS factura_pdf"
    )
    factura_nombre = schema.index(
        "ALTER TABLE IF EXISTS envios ADD COLUMN IF NOT EXISTS factura_nombre"
    )
    assert crear < factura_pdf < factura_nombre
