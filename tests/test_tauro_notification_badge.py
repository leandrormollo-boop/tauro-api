"""Contrato visual y accesible del sello de notificación TAURO."""
from pathlib import Path


RAIZ = Path(__file__).resolve().parent.parent
CSS = (RAIZ / "static/css/tauro.css").read_text(encoding="utf-8")
BASE = (RAIZ / "templates/base.html").read_text(encoding="utf-8")


def test_badge_usa_identidad_tauro_y_no_el_globo_rojo_generico():
    inicio = CSS.index("/* Sello de notificación TAURO")
    final = CSS.index(".side-item.active", inicio)
    bloque = CSS[inicio:final]

    assert ".side-badge,\n.tabbar-badge" in bloque
    assert 'logo-mark-white.png' in bloque
    assert "linear-gradient(145deg, #a17cf5" in bloque
    assert "#ff5f57" not in bloque
    assert "#e03127" not in bloque
    assert "tauro-badge-arrive" in bloque
    assert "tauro-badge-ring" in bloque


def test_badge_conserva_conteo_y_descripcion_en_portal_y_mobile():
    assert 'class="side-badge" title="{{ pend.envios }} guía(s) lista(s) para descargar"' in BASE
    assert 'class="tabbar-badge" aria-label="{{ pend.envios }} guía(s) lista(s) para descargar"' in BASE
    assert "{{ pend.envios }}</span>" in BASE
