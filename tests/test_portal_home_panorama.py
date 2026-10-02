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
    assert "home-bar-fill" in HOME
    assert "home-dest-fill" in HOME
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
    assert "home-bar-bob" in CSS
    assert "home-bar-liquid" in CSS
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
    assert 'portal-operacion.css?v=8' in BASE


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


def _action_portal(monkeypatch, historial, pedidos=0):
    from collections import Counter
    from starlette.requests import Request
    from endpoints import portal_cliente as portal
    from servicios import configuracion_couriers_cliente, recolecciones
    from servicios.panel_cliente import PASOS_EMBUDO, paso_de_estado

    counts = Counter(paso_de_estado(s['estado'], s.get('tracking_estado')) for s in historial)
    counts['por_armar'] = pedidos
    embudo = [{**p, 'cantidad': counts[p['clave']]} for p in PASOS_EMBUDO]
    monkeypatch.setattr(portal, 'get_facturado_real', lambda *_: 0)
    monkeypatch.setattr(portal, 'saldo', lambda *_, **__: {'saldo_pendiente_ars': 0, 'facturado_ars': 0, 'pagado_ars': 0})
    monkeypatch.setattr(portal, 'listar_solicitudes_cliente', lambda *_, **__: [dict(s) for s in historial])
    monkeypatch.setattr(portal, 'periodos_solicitudes_cliente', lambda *_: [(2026, 10)] if historial else [])
    monkeypatch.setattr(portal, 'embudo_envios', lambda cliente: embudo)
    monkeypatch.setattr(configuracion_couriers_cliente, 'mapa_permisos', lambda *_: {})
    monkeypatch.setattr(recolecciones, 'listar_de_solicitudes', lambda *_: {})
    monkeypatch.setitem(portal.templates.env.globals, 'pendientes_menu', lambda *_: {})
    monkeypatch.setitem(portal.templates.env.globals, 'saldo_menu', lambda *_, **__: None)
    monkeypatch.setitem(portal.templates.env.globals, 'ayuda', lambda: {'mail_url': 'mailto:demo@example.invalid'})

    def request(path='/portal/home', query='', parcial=False):
        return Request({'type': 'http', 'method': 'GET', 'path': path,
                        'query_string': query.encode(), 'state': {'csp_nonce': 'test'},
                        'headers': [(b'x-tauro-partial', b'envios')] if parcial else []})
    return portal, request


def _action_shipments():
    from servicios.estados_envio import presentar_estados_envio
    states = [('GUIA_LISTA', None), ('DESPACHADO', 'RETENIDO'),
              ('GUIA_LISTA', 'ENTREGADO'), ('GUIA_LISTA', 'PROCESO_ENTREGA'),
              ('CANCELADO', 'RETENIDO'), ('REEMPLAZADO', 'RETENIDO'),
              ('SOLICITADO', None), ('EN_PROCESO', None),
              ('EMITIENDO', None), ('VERIFICAR_COURIER', None)]
    return [presentar_estados_envio(dict(
        id=n, estado=state, tracking_estado=tracking_state,
        courier='OCA' if n == 2 else 'DHL', remitente_pais='AR',
        destino_pais='AR' if n == 2 else 'US', tracking=f'DEMO-{n}',
        dest_nombre=f'Destinatario DEMO {n}', bultos=[], cantidad=1,
        precio_tauro_ars=0, resumen_pesos={},
    )) for n, (state, tracking_state) in enumerate(states, 1)]


def test_tarjeta_accion_abre_todos_los_pendientes_y_separa_pedidos_de_tienda(monkeypatch):
    from html.parser import HTMLParser
    from urllib.parse import urlsplit, parse_qs

    class SummaryLinks(HTMLParser):
        def __init__(self):
            super().__init__()
            self.links = []
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == 'a' and 'home-summary-item' in attrs.get('class', '').split():
                self.links.append(attrs['href'])

    portal, request = _action_portal(monkeypatch, _action_shipments(), pedidos=3)
    inicio = portal.home(request(), cliente='DEMO')
    assert inicio.context['resumen_inicio']['requieren_accion'] == 5
    parser = SummaryLinks()
    parser.feed(inicio.body.decode())
    link = urlsplit(parser.links[2])
    query = {k: v[0] for k, v in parse_qs(link.query).items()}
    assert query == {'paso': 'requieren_accion'}

    for parcial in (False, True):
        lista = portal.envios_view(request(link.path, link.query, parcial), cliente='DEMO', **query)
        assert [s['id'] for s in lista.context['solicitudes']] == [1, 2]
        assert lista.context['total_resultados'] == 2
        assert lista.context['pedidos_por_armar'] == 3
        assert lista.context['total_nacionales'] == lista.context['total_internacionales'] == 1
        html = lista.body.decode()
        assert '<h1>Requiere tu acción</h1>' in html
        assert 'Mi tienda: 3 pedidos por armar' in html
        assert 'paso=requieren_accion' in html
        assert 'class="chip-e on" aria-current="true"><b>2</b> Requiere tu acción' in html


def test_accion_solo_pedidos_tienda_no_termina_en_una_lista_vacia_sin_salida(monkeypatch):
    portal, request = _action_portal(monkeypatch, [], pedidos=3)
    assert portal.home(request(), cliente='DEMO').context['resumen_inicio']['requieren_accion'] == 3
    lista = portal.envios_view(request('/portal/envios', 'paso=requieren_accion'), cliente='DEMO', paso='requieren_accion')
    html = lista.body.decode()
    assert '<a href="/portal/tienda"><strong>Mi tienda: 3 pedidos por armar</strong></a>' in html
    assert 'No tenés envíos que requieran acción' in html
    assert 'Todavía no hiciste envíos' not in html
    assert lista.context['total_resultados'] == 0


def test_filtro_accion_conserva_busqueda_ambito_y_paginacion():
    from servicios.panel_cliente import preparar_historial_envios
    historial = _action_shipments()
    nacional = preparar_historial_envios(historial, paso='requieren_accion', tipo='nacional')
    assert [s['id'] for s in nacional['solicitudes']] == [2]
    buscada = preparar_historial_envios(historial, paso='requieren_accion', buscar='DEMO-1')
    assert [s['id'] for s in buscada['solicitudes']] == [1]
    assert buscada['total_requieren_accion'] == 1
    historial = [dict(historial[0], id=n) for n in range(11)] + historial[2:]
    segunda = preparar_historial_envios(historial, paso='requieren_accion', pagina=2)
    assert [s['id'] for s in segunda['solicitudes']] == [10]
    assert segunda['total_resultados'] == segunda['total_requieren_accion'] == 11
    assert segunda['paso_filtro'] == 'requieren_accion'
