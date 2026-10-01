"""Contratos del escritorio operativo del cliente."""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "static" / "css" / "portal-operacion.css").read_text(encoding="utf-8")
BASE = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
HOME = (ROOT / "templates" / "portal" / "home.html").read_text(encoding="utf-8")


def test_inicio_es_un_escritorio_y_no_una_portada_decorativa():
    assert 'class="home-workspace-head"' in HOME
    assert 'class="home-summary-strip"' in HOME
    assert '<section class="home-hero">' not in HOME
    assert "avion-hero" not in HOME


def test_acciones_y_ambitos_estan_visibles_sin_copiar_otras_marcas():
    for texto in (
        "Nuevo envío",
        "Cotizar",
        "Mis envíos",
        "Nacionales",
        "Internacionales",
        "Cuenta corriente",
    ):
        assert texto in HOME
    assert "BOXFLY" not in HOME.upper()
    assert "🌐" not in HOME
    assert "🇦🇷" not in HOME
    assert "scope_icon(ambito)" in HOME


def test_metricas_y_graficos_salen_de_datos_reales():
    assert "r.envios_mes" in HOME
    assert "r.en_seguimiento" in HOME
    assert "r.serie_mensual" in HOME
    assert "r.destinos_frecuentes" in HOME
    assert "<progress" in HOME
    assert "574" not in HOME
    assert "8.669.507" not in HOME


def test_inicio_compacta_sin_scroll_horizontal_en_mobile():
    assert ".home-dashboard-layout" in CSS
    assert ".home-activity-scopes" in CSS
    assert "@media (max-width: 620px)" in CSS
    assert ".portal-operacion .home-activity-scopes { grid-template-columns: 1fr; }" in CSS
    assert ".portal-operacion .home-dashboard-rail { grid-template-columns: 1fr; }" in CSS
    assert 'portal-operacion.css?v=' in BASE


def test_barras_mensuales_entran_una_vez_y_respetan_movimiento_reducido():
    assert "@keyframes home-bar-rise" in CSS
    assert "animation: home-bar-rise" in CSS
    assert "--bar-delay" in CSS
    assert "@media (prefers-reduced-motion: reduce)" in CSS
