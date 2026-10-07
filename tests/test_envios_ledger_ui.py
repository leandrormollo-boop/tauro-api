from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]


def _leer(ruta: str) -> str:
    return (RAIZ / ruta).read_text(encoding="utf-8")


def test_admin_separa_datos_operativos_y_saldos_por_columnas():
    html = _leer("templates/admin/cliente_detail.html")

    for titulo in (
        "Fecha", "Concepto", "Remitente", "Destinatario", "Tracking",
        "Saldo inicial / final", "Ámbito", "Estado", "Guía", "Acciones",
    ):
        assert f">{titulo}<" in html
    assert "e.remitente" in html
    assert "e.destinatario" in html
    assert "e.estado_operativo_ui.label" in html
    assert "/admin/pedidos/{{ e.solicitud_id }}/guia.pdf" in html
    assert "?repetir={{ e.solicitud_id }}" in html


def test_portal_muestra_libro_operativo_y_acciones_seguras():
    html = _leer("templates/portal/envios.html")

    assert "s.remitente_nombre" in html
    assert "s.dest_nombre" in html
    assert "Cotizado" in html
    assert "Total registrado" not in html
    assert "Precio del envío (ARS)" in html
    assert "s.estado_cliente_ui.label" in html
    assert "/portal/envios/{{ s.id }}/guia.pdf" in html
    assert "Editar / reemplazar" in html
    assert "Cancelar envío" in html
    assert "El sistema volverá a validar con el courier" in html


def test_portal_conserva_el_estado_canonico_sin_override():
    portal = _leer("endpoints/portal_cliente.py")
    admin = _leer("endpoints/admin.py")
    assert '"label": "Proceso de entrega"' not in portal
    assert '"label": "Proceso de entrega"' in admin
