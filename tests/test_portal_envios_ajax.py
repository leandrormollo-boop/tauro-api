"""Actualización interna y progresiva de los filtros de Mis envíos."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "templates/portal/envios.html").read_text(encoding="utf-8")
JAVASCRIPT = (ROOT / "static/js/portal-envios.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/portal-envios.css").read_text(encoding="utf-8")


def test_region_actualizable_contiene_filtros_resumen_y_tabla():
    inicio = TEMPLATE.index('data-envios-region')
    fin = TEMPLATE.index('<dialog class="shipment-verification-dialog"')
    region = TEMPLATE[inicio:fin]

    assert 'data-envios-status' in region
    assert 'id="envios-periodo-form"' in region
    assert 'class="chips-tipo envios-scope-tabs"' in region
    assert 'id="envios-search-form"' in region
    assert 'class="chips-embudo"' in region
    assert 'class="card envios-card"' in region
    assert 'class="envios-pagination"' in region
    assert '/static/js/portal-envios.js?v=1' in TEMPLATE[fin:]


def test_filtros_conservan_fallback_sin_javascript():
    assert '<form id="envios-periodo-form" method="GET" action="/portal/envios"' in TEMPLATE
    assert '<noscript><button type="submit"' in TEMPLATE
    assert '<form method="GET" action="/portal/envios"' in TEMPLATE
    assert 'href="/portal/envios?tipo=nacional' in TEMPLATE
    assert 'href="/portal/envios?paso={{ c.clave }}' in TEMPLATE
    assert 'href="/portal/envios?pagina={{ numero }}' in TEMPLATE


def test_javascript_intercepta_todos_los_filtros_sin_recargar_documento():
    assert 'document.addEventListener("change"' in JAVASCRIPT
    assert 'document.addEventListener("submit"' in JAVASCRIPT
    assert 'document.addEventListener("click"' in JAVASCRIPT
    assert 'window.fetch(url.href' in JAVASCRIPT
    assert 'credentials: "same-origin"' in JAVASCRIPT
    assert '"X-Tauro-Partial": "envios"' in JAVASCRIPT
    assert 'root.replaceWith(imported)' in JAVASCRIPT
    assert 'window.history.pushState' in JAVASCRIPT
    assert 'window.addEventListener("popstate"' in JAVASCRIPT
    assert 'window.scrollTo({left: previousScroll.x, top: previousScroll.y' in JAVASCRIPT
    assert 'window.location.reload' not in JAVASCRIPT


def test_actualizacion_falla_cerrada_y_no_inyecta_html_sin_parsearlo():
    assert 'new DOMParser().parseFromString(html, "text/html")' in JAVASCRIPT
    assert 'parsed.querySelector("[data-envios-region]")' in JAVASCRIPT
    assert 'document.importNode(next, true)' in JAVASCRIPT
    assert 'new AbortController()' in JAVASCRIPT
    assert 'if (error.name !== "AbortError")' in JAVASCRIPT
    assert 'showError(loadingRoot)' in JAVASCRIPT
    assert 'if (!keepErrorVisible) stopLoading(loadingRoot)' in JAVASCRIPT
    assert 'window.location.assign(finalUrl.href)' in JAVASCRIPT
    assert '.innerHTML = html' not in JAVASCRIPT


def test_carga_interna_es_visible_accesible_y_respeta_movimiento_reducido():
    assert 'aria-busy="false"' in TEMPLATE
    assert 'role="status" aria-live="polite" hidden' in TEMPLATE
    assert '.envios-async-status[hidden]' in CSS
    assert '.envios-async-spinner' in CSS
    assert '@media (prefers-reduced-motion: reduce)' in CSS
