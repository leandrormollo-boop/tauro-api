"""Contratos estáticos del desplegable y los números compartidos.

No reemplazan una prueba visual de navegador; evitan que Portal y Admin vuelvan
a bifurcar el componente que acelera cotización y armado de guías.
"""
from pathlib import Path
import shutil
import subprocess

import pytest


RAIZ = Path(__file__).resolve().parent.parent
UI = (RAIZ / "static" / "js" / "tauro-ui.js").read_text(encoding="utf-8")
CSS = (RAIZ / "static" / "css" / "tauro.css").read_text(encoding="utf-8")
PORTAL_BASE = (RAIZ / "templates" / "base.html").read_text(encoding="utf-8")
ADMIN_BASE = (RAIZ / "templates" / "admin" / "base_admin.html").read_text(encoding="utf-8")


def test_portal_y_admin_cargan_la_ui_compartida_con_cache_independiente():
    assert '/static/js/tauro-ui.js?v=8' in PORTAL_BASE
    assert '/static/js/tauro-ui.js?v=8' in ADMIN_BASE
    assert '/static/css/tauro.css?v=33' in PORTAL_BASE
    assert '/static/css/tauro.css?v=26' in ADMIN_BASE


def test_smart_select_busca_por_atributo_o_por_cantidad_y_prioriza_prefijo():
    assert 'select.hasAttribute("data-searchable") || select.options.length >= 8' in UI
    assert 'var etiqueta = normalizar(opt.text).trim()' in UI
    assert 'var codigo = normalizar(opt.value).trim()' in UI
    assert 'if (etiqueta === filtro || codigo === filtro) rango = 0' in UI
    assert 'else if (etiqueta.indexOf(filtro) === 0) rango = 1' in UI
    assert 'candidatas.sort' in UI
    assert 'select.dataset.searchPlaceholder || "Buscar opciones"' in UI


def test_smart_select_tiene_teclado_confirmacion_y_aria_coherentes():
    for fragmento in (
        'btn.setAttribute("aria-haspopup", "listbox")',
        'btn.setAttribute("aria-expanded", "false")',
        'optionsBox.setAttribute("role", "listbox")',
        'itemEl.setAttribute("role", "option")',
        'function moverActivo(direccion)',
        'function confirmarActivo()',
        'if (e.key === "ArrowDown" || e.key === "ArrowUp")',
        'if (e.key === "Enter")',
        'if (e.key === "Escape")',
        'elegir(i);\n            cerrar();',
        'searchInput.focus({ preventScroll: true })',
        'wrap.classList.toggle("open-up", haciaArriba)',
        'wrap.classList.toggle("searchable", tieneBusqueda)',
        'panel.style.left = desplazamiento + "px"',
        'select.addEventListener("invalid"',
        'btn.setAttribute("aria-invalid", "true")',
        'new MutationObserver',
    ):
        assert fragmento in UI
    assert ".tselect-option.active" in CSS
    assert ".tselect.searchable .tselect-panel" in CSS
    assert '.tselect-btn[aria-invalid="true"]' in CSS


def test_smart_number_solo_normaliza_al_salir_o_enviar_y_no_adivina_pricing():
    assert 'input.addEventListener("blur", function () { normalizarCampoNumero(input); })' in UI
    assert 'input.addEventListener("input", function () { input.setCustomValidity(""); })' in UI
    assert 'document.addEventListener("submit"' in UI
    assert 'if (tipo === "pricing")' in UI
    assert 'input.dataset.pricingSelect' in UI
    assert 'modoPricing === "FIJO_ARS" ? "monto" : "decimal"' in UI
    assert 'tipo === "monto" || tipo === "importe"' in UI
    assert 'if (esMonto && primerGrupoValido)' in UI
    assert 'tipo === "entero"' in UI
    assert '/^0+$/.test(parteDecimalEntero)' in UI
    assert 'parteDecimalEntero.length === 3 && Number(parteEnteraEntero) !== 0' in UI
    assert '`0.500` nunca se convierte en 500' in UI
    assert 'se pide corrección humana en vez de cambiar la cantidad' in UI
    assert 'input.value = resultado.valor' in UI


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_smart_ui_parsea_en_node():
    resultado = subprocess.run(
        ["node", "--check", str(RAIZ / "static" / "js" / "tauro-ui.js")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert resultado.returncode == 0, resultado.stderr
