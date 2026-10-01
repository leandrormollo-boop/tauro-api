"""Contratos del escritorio operativo del cliente."""
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "static" / "css" / "portal-operacion.css").read_text(encoding="utf-8")
BASE = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
HOME = (ROOT / "templates" / "portal" / "home.html").read_text(encoding="utf-8")
STATS = (ROOT / "templates" / "portal" / "estadisticas.html").read_text(encoding="utf-8")
PORTAL = (ROOT / "endpoints" / "portal_cliente.py").read_text(encoding="utf-8")


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


def test_paneles_del_inicio_abren_la_pantalla_que_explican():
    assert 'href="/portal/envios" class="home-panel home-monthly-panel home-panel-link"' in HOME
    assert 'href="/portal/estadisticas" class="home-panel home-destinations-panel home-panel-link"' in HOME
    assert 'href="/portal/cuenta" class="home-account-entry"' in HOME
    assert 'href="/portal/envios?tipo={{ ambito }}" class="home-scope-name"' in HOME
    assert ".portal-operacion .home-panel-link:hover" in CSS
    assert ".portal-operacion .home-summary-item:hover" in CSS


def test_estadisticas_usa_datos_reales_y_lleva_al_historial_filtrado():
    assert '@router.get("/estadisticas"' in PORTAL
    assert "resumen_inicio_cliente(historial, embudo)" in PORTAL
    assert "r.serie_mensual" in STATS
    assert "r.destinos_frecuentes" in STATS
    assert "{% for paso in embudo %}" in STATS
    assert '/portal/envios?anio={{ mes.anio }}&amp;mes={{ mes.mes }}' in STATS
    assert "574" not in STATS


def test_estadisticas_no_desborda_en_mobile():
    assert ".portal-operacion .stats-layout { grid-template-columns: 1fr; }" in CSS
    assert ".portal-operacion .stats-summary { grid-template-columns: 1fr; }" in CSS
    assert ".portal-operacion .stats-destinations .home-destinations-list { grid-template-columns: 1fr; }" in CSS
    assert 'portal-operacion.css?v=6' in BASE


def test_estadisticas_renderiza_aun_sin_historial():
    from endpoints import portal_cliente as portal

    env = portal.templates.env
    originales = {
        nombre: env.globals[nombre]
        for nombre in ("pendientes_menu", "saldo_menu", "ayuda")
    }
    env.globals["pendientes_menu"] = lambda cliente: {"envios": 0, "tienda": 0}
    env.globals["saldo_menu"] = lambda cliente, ya=None: None
    env.globals["ayuda"] = lambda: {"whatsapp_url": None, "mail_url": "mailto:test@example.com"}
    request = SimpleNamespace(
        url=SimpleNamespace(path="/portal/estadisticas"),
        state=SimpleNamespace(csp_nonce="nonce-de-test"),
    )
    try:
        html = env.get_template("portal/estadisticas.html").render(
            request=request,
            cliente="CLIENTE_TEST",
            embudo=[],
            resumen_inicio={
                "envios_mes": 0,
                "envios_total": 0,
                "serie_mensual": [],
                "maximo_mensual": 0,
                "destinos_frecuentes": [],
                "paises_total": 0,
            },
        )
    finally:
        env.globals.update(originales)

    assert "Estadísticas de envíos" in html
    assert 'href="/portal/envios"' in html
    assert "Los destinos aparecerán" in html
