from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_shell_autenticado_incluye_cotizador_en_ventana_y_fallback():
    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")

    assert 'id="quote-window-dialog"' in base
    assert 'data-cotizar-contenido' in base
    assert 'href="/portal/cotizar" data-cotizar-ventana' in base
    assert 'href="/portal/cotizar?ambito=nacional"' in base
    assert "Primero elegí la ruta. Después completá la caja." in base


def test_cotizador_en_ventana_reutiliza_el_post_canonico():
    javascript = (ROOT / "static" / "js" / "portal-cotizador.js").read_text(
        encoding="utf-8"
    )

    assert 'fetch("/portal/cotizar?ambito=" + normalized' in javascript
    assert 'method: "POST"' in javascript
    assert 'body: new FormData(form)' in javascript
    assert 'querySelector(".quote-screen")' in javascript
    assert 'contains("national-quote-screen")' in javascript
    assert "window.location.pathname" in javascript


def test_cotizador_internacional_se_resuelve_en_dos_pasos_compactos():
    template = (ROOT / "templates" / "portal" / "cotizar.html").read_text(
        encoding="utf-8"
    )
    javascript = (ROOT / "static" / "js" / "portal-cotizador.js").read_text(
        encoding="utf-8"
    )
    css = (ROOT / "static" / "css" / "tauro.css").read_text(encoding="utf-8")

    assert "Paso 1 de 2 · Elegí la ruta" in template
    assert "Paso 2 de 2 · Completá la caja" in template
    assert 'id="quote-edit-route"' in template
    assert 'id="quote-submit-row"' in template
    assert 'form.classList.add("quote-flow-enabled")' in javascript
    assert 'submit.disabled = !quoteReady' in javascript
    assert 'destination.value = ""' in javascript
    assert 'destination.dispatchEvent(new Event("change"' in javascript
    assert ".quote-flow-enabled .quote-form-block.is-step-hidden { display: none; }" in css
    assert "max-width: 880px;" in css
    assert ".quote-screen-window .quote-operator-strip { display: none; }" in css


def test_ruta_internacional_pide_ubicacion_real_y_precarga_referencias():
    template = (ROOT / "templates" / "portal" / "cotizar.html").read_text(
        encoding="utf-8"
    )
    javascript = (ROOT / "static" / "js" / "portal-cotizador.js").read_text(
        encoding="utf-8"
    )

    for campo in (
        "origen_ciudad", "origen_cp_internacional",
        "destino_ciudad_internacional", "destino_cp_internacional",
    ):
        assert f'name="{campo}"' in template
        assert f'id="{campo}"' in template
    assert 'data-ref-city=' in template
    assert 'data-ref-postal=' in template
    assert "applyLocationReference" in javascript
    assert "Completá ciudad y código postal de la ruta." in javascript


def test_recoleccion_se_presenta_como_accion_explicita_del_envio():
    envios = (ROOT / "templates" / "portal" / "envios.html").read_text(
        encoding="utf-8"
    )
    recolecciones = (
        ROOT / "templates" / "portal" / "recolecciones.html"
    ).read_text(encoding="utf-8")

    assert "Programar retiro" in envios
    assert "Confirmar retiro con el courier" in recolecciones
