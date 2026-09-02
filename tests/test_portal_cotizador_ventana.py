from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_shell_autenticado_incluye_cotizador_en_ventana_y_fallback():
    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")

    assert 'id="quote-window-dialog"' in base
    assert 'data-cotizar-contenido' in base
    assert 'href="/portal/cotizar" data-cotizar-ventana' in base
    assert 'href="/portal/cotizar?ambito=nacional"' in base


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


def test_recoleccion_se_presenta_como_accion_explicita_del_envio():
    envios = (ROOT / "templates" / "portal" / "envios.html").read_text(
        encoding="utf-8"
    )
    recolecciones = (
        ROOT / "templates" / "portal" / "recolecciones.html"
    ).read_text(encoding="utf-8")

    assert "Programar retiro" in envios
    assert "Confirmar retiro con el courier" in recolecciones
