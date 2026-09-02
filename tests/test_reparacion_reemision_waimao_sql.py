from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_reparacion_52_53_es_idempotente_y_falla_si_cambia_la_evidencia():
    sql = (ROOT / "scripts/reparar_reemision_waimao_52_53.sql").read_text()

    assert "PG_ADVISORY_XACT_LOCK" in sql
    assert "codex_backup_solicitudes_52_53_20260902" in sql
    assert "codex_backup_envios_52_53_20260902" in sql
    assert "La evidencia de #52/#53 cambió" in sql
    assert "ON CONFLICT (solicitud_id)" not in sql
    assert "NOT EXISTS (" in sql
    assert "ON CONFLICT (solicitud_anterior_id) DO NOTHING" in sql
    assert "RECONSTRUCCION_PRECIO_2026" in sql
    assert "IMPORT_SHEET_2026" not in sql
    assert "costo_inferido_usd" in sql


def test_reparacion_conserva_cargos_y_snapshots_separados():
    sql = (ROOT / "scripts/reparar_reemision_waimao_52_53.sql").read_text()

    assert "s.id IN (52, 53)" in sql
    assert "solicitud_anterior_id = 52" in sql
    assert "solicitud_nueva_id = 53" in sql
    assert "solicitud_id = 52 AND estado = 'CANCELADO'" in sql
    assert "solicitud_id = 53 AND estado = 'ACTIVO'" in sql
    assert "UPDATE envios" not in sql
    assert "DELETE FROM" not in sql.upper()
