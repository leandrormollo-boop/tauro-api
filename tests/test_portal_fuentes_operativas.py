from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_detalle_identifica_fuente_y_no_infiere_retiro_por_estado_despachado():
    plantilla = (ROOT / "templates" / "portal" / "envio_detalle.html").read_text(
        encoding="utf-8"
    )

    assert "Fuente: {{ courier_nombre }}" in plantilla
    assert "El envío figura despachado" in plantilla
    assert "ya recolectó tu envío" not in plantilla


def test_historial_conserva_filtros_documentos_y_acciones():
    plantilla = (ROOT / "templates" / "portal" / "envios.html").read_text(
        encoding="utf-8"
    )

    for contrato in (
        'id="envios-search-form"',
        'class="envios-date-filters"',
        'aria-label="Filtrar por estado"',
        "Ver guía en pantalla",
        "Descargar guía",
        "Gestionar envío",
    ):
        assert contrato in plantilla
