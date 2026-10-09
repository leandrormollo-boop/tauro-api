from pathlib import Path

from test_cotizador_unificado import render_quote


ROOT = Path(__file__).resolve().parents[1]


def test_resultado_conserva_la_ruta_y_no_usa_logos_de_courier():
    html = (ROOT / "templates/portal/_quote_results.html").read_text()

    assert 'class="uq-result-route"' in html
    assert "Ruta consultada" in html
    assert 'class="uq-carrier"' in html
    assert "quote-carrier-logo" not in html


def test_consulta_muestra_progreso_y_revela_el_resultado_sin_demora_artificial():
    js = (ROOT / "static/js/portal-cotizador.js").read_text()

    assert "function quoteProgress(container)" in js
    assert "Esperando la tarifa." in js
    assert "window.requestAnimationFrame" in js
    assert "is-revealing" in js


def test_transicion_es_breve_y_respeta_movimiento_reducido():
    css = (ROOT / "static/css/portal-cotizador.css").read_text()

    assert "animation:uq-result-enter .2s" in css
    assert "@media(prefers-reduced-motion:reduce)" in css
    assert ".uq-quote-progress" in css


def test_resultado_renderiza_ruta_operador_y_precio_sin_imagen_de_marca():
    html = render_quote(
        form={
            "origen_pais": "CN",
            "origen_ciudad": "Yiwu",
            "origen_cp_internacional": "322000",
            "destino_pais": "MX",
            "destino_ciudad_internacional": "Polanco",
            "destino_cp_internacional": "11540",
            "valor_declarado_usd": "50",
            "bultos": [{}],
        },
        opciones=[{
            "carrier_id": "dhl",
            "carrier_nombre": "DHL Express",
            "servicio": "Express Worldwide",
            "precio_final_ars": 776702,
            "dias_estimados": 5,
            "reseller_quote_id": None,
        }],
    )

    assert "Yiwu, CN" in html and "Polanco, MX" in html
    assert "DHL Express" in html and "Express Worldwide" in html
    assert "$ 776.702,00" in html
    assert '<span class="uq-price-label">Precio estimado</span>' in html
    assert "quote-carrier-logo" not in html


def test_planeta_permanece_compacto_al_aparecer_la_tarifa():
    globe = (ROOT / "static/js/quote-globe-portal.js").read_text()
    map_css = (ROOT / "static/css/quote-route-map.css").read_text()

    assert "resultPanel.classList.toggle('has-quote-result', hasResults)" in globe
    assert "mobile.matches && !panelHasResults(activePanel)" in globe
    assert "key === root.dataset.quoteActive && hasResults" in globe
    assert "Ampliar ruta" in globe and "Ver tarifa" in globe
    assert ".uq-results.has-quote-result:not(.is-map-expanded) .quote-route-map" in map_css
    assert "grid-template-columns:74px minmax(0,1fr)" in map_css
    assert "prefers-reduced-motion:reduce" in map_css
