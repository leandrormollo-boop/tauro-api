import ast
from pathlib import Path

import pytest

from servicios import runtime_jobs


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("value", ["STAGING", "staging", "  StAgInG  "])
def test_staging_bloquea_automatizaciones_sin_override(value):
    environ = {
        "ENV": value,
        # Incluso una variable genérica tentadora no debe abrir el runtime.
        "TAURO_STAGING_BACKGROUND_JOBS_ENABLED": "true",
    }

    assert runtime_jobs.automatic_background_jobs_enabled(environ) is False


@pytest.mark.parametrize("value", ["PROD", "production", "DEV", "", "qa"])
def test_entornos_existentes_conservan_el_runtime(value):
    assert runtime_jobs.automatic_background_jobs_enabled({"ENV": value}) is True


def test_main_no_arranca_scheduler_ni_hilos_fuera_del_guard_de_staging():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    guarded_if = next(
        node
        for node in tree.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "_AUTOMATIC_BACKGROUND_JOBS_ENABLED"
        and any(
            isinstance(child, ast.Expr)
            and isinstance(child.value, ast.Call)
            and ast.unparse(child.value.func) == "scheduler.start"
            for child in node.body
        )
    )

    guarded_source = ast.get_source_segment(source, guarded_if) or ""
    assert "threading.Thread(target=_tarifas_al_arrancar" in guarded_source
    assert "target=actualizar_trackings_diarios_seguro" in guarded_source

    module_level_starts = [
        node for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and ast.unparse(node.value.func).endswith(".start")
    ]
    assert module_level_starts == []
