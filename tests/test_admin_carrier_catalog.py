from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_admin_muestra_integraciones_futuras_sin_habilitarlas():
    endpoint = (ROOT / "endpoints/admin.py").read_text()
    template = (ROOT / "templates/admin/cliente_acceso_precios.html").read_text()
    assert "CARRIER_SPECS" in endpoint
    assert "futuros_couriers" in endpoint
    assert "Próximas integraciones" in template
    assert "API pendiente" in template
    # Los cards futuros no generan nombres de campos POST ni permisos.
    assert 'name="{{ c.id }}_cotizar"' not in template.split(
        'class="future-integrations"', 1
    )[-1]
