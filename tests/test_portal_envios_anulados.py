"""Un cargo anulado deja de verse sin borrar la historia administrativa."""

from pathlib import Path


RAIZ = Path(__file__).resolve().parents[1]


def _bloque(fuente: str, inicio: str, fin: str) -> str:
    return fuente.split(inicio, 1)[1].split(fin, 1)[0]


def test_listado_del_portal_excluye_solicitud_y_cargo_cancelados():
    fuente = (RAIZ / "servicios" / "solicitudes_guia.py").read_text(
        encoding="utf-8"
    )
    listado = _bloque(
        fuente,
        "def listar_solicitudes_cliente(",
        "def listar_envios_api(",
    )

    assert "s.estado <> 'CANCELADO'" in listado
    assert "NOT EXISTS" in listado
    assert "e.solicitud_id = s.id" in listado
    assert "e.cliente_id = s.cliente_id" in listado
    assert "e.estado = 'CANCELADO'" in listado


def test_anulado_tampoco_se_abre_por_url_directa_ni_descarga_documentos():
    fuente = (RAIZ / "servicios" / "solicitudes_guia.py").read_text(
        encoding="utf-8"
    )

    detalle = _bloque(
        fuente,
        "def obtener_solicitud_de_cliente(",
        "def obtener_label_de_cliente(",
    )
    label_api = _bloque(
        fuente,
        "def obtener_label_de_cliente(",
        "# ── Emisión de guía real",
    )
    documentos = _bloque(
        fuente,
        "def obtener_label_pdf(",
        "def cargar_envio_externo(",
    )

    for bloque in (detalle, label_api, documentos):
        assert "s.estado <> 'CANCELADO'" in bloque
        assert "e.estado = 'CANCELADO'" in bloque


def test_contadores_del_portal_tampoco_incluyen_cargos_anulados():
    solicitudes = (RAIZ / "servicios" / "solicitudes_guia.py").read_text(
        encoding="utf-8"
    )
    panel = (RAIZ / "servicios" / "panel_cliente.py").read_text(
        encoding="utf-8"
    )

    contador_guias = _bloque(
        solicitudes,
        "def contar_guias_listas(",
        "def listar_solicitudes_admin(",
    )
    embudo = _bloque(panel, "def embudo_envios(", "def checklist_arranque(")

    assert "e.estado = 'CANCELADO'" in contador_guias
    assert embudo.count("e.estado = 'CANCELADO'") >= 1
