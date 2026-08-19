"""Contrato del combobox inteligente de la web pública."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WIDGET = (ROOT / "web" / "components" / "02-quote-widget.jsx").read_text(
    encoding="utf-8"
)
STYLES = (ROOT / "web" / "styles.css").read_text(encoding="utf-8")
HTML = (ROOT / "web" / "Tauro Solutions.html").read_text(encoding="utf-8")
APP = (ROOT / "web" / "components" / "05-app.jsx").read_text(encoding="utf-8")


def test_web_publica_busca_incremental_y_prioriza_prefijos():
    assert "function normalizeSearchText" in WIDGET
    assert "function rankedSelectOptions" in WIDGET
    assert "label.startsWith(needle)" in WIDGET
    assert "value.startsWith(needle)" in WIDGET
    assert 'placeholder="Buscar país o código"' in WIDGET
    assert "setQuery(e.target.value)" in WIDGET


def test_web_publica_navega_confirma_y_cierra_con_teclado_o_click():
    for fragment in (
        'e.key === "ArrowDown"',
        'e.key === "ArrowUp"',
        'e.key === "Enter"',
        'e.key === "Escape"',
        "choose(filtered[activeIndex] || filtered[0])",
        "onMouseDown={(event) => { event.preventDefault(); }}",
        "onClick={(event) => { event.preventDefault(); choose(o); }}",
        'aria-haspopup="listbox"',
        'role="combobox"',
        'role="listbox"',
        'role="option"',
    ):
        assert fragment in WIDGET


def test_web_publica_adapta_el_panel_al_viewport_y_bumpea_assets():
    assert "window.visualViewport" in WIDGET
    assert "setOpenUp(shouldOpenUp)" in WIDGET
    assert "setPanelMaxHeight" in WIDGET
    assert ".tweb-select-panel.open-up" in STYLES
    assert ".tweb-select-options" in STYLES
    assert 'styles.css?v=12' in HTML
    assert '/static/js/app.js?v=16' in HTML


def test_boton_publico_expone_el_valor_seleccionado_en_su_nombre_accesible():
    assert 'aria-labelledby={`${listIdRef.current}-label ${listIdRef.current}-value`}' in WIDGET
    assert 'id={`${listIdRef.current}-value`}' in WIDGET


def test_web_publica_normaliza_numeros_solo_al_salir_del_campo():
    assert "const parsed = parseHumanNumber(e.target.value, { money })" in WIDGET
    assert "if (Number.isFinite(parsed)) onChange(String(parsed))" in WIDGET
    assert 'money />' in WIDGET


def test_cta_publico_desplaza_al_cotizador_sin_volver_al_inicio():
    assert 'id="cotizador"' in WIDGET
    assert 'document.querySelector("#cotizador")?.scrollIntoView' in APP
    assert "window.scrollTo({ top: 0" not in APP
