from pathlib import Path
from test_cotizador_unificado import render_quote

ROOT = Path(__file__).resolve().parents[1]


def test_shell_autenticado_incluye_ventana_y_fallback():
    base=(ROOT/'templates/base.html').read_text()
    assert 'id="quote-window-dialog"' in base
    assert 'href="/portal/cotizar" data-cotizar-ventana' in base
    assert 'Completá los datos y compará tus tarifas.' in base
    assert '🇦🇷' not in base and '🌐' not in base


def test_cotizador_reutiliza_post_canonico_sin_navegar_entre_pasos():
    html=render_quote()
    assert html.count('action="/portal/cotizar"')==2
    assert 'data-quote-scope="nacional"' in html
    assert 'data-quote-scope="internacional"' in html
    assert 'quote-route-next' not in html
    js=(ROOT/'static/js/portal-cotizador.js').read_text()
    assert 'body: new FormData(form)' in js
    assert 'credentials:' in js
    assert 'window.location.pathname' in js


def test_pide_ubicacion_real_y_mantiene_cajas_visibles():
    html=render_quote()
    for field in ('origen_ciudad','origen_cp_internacional','destino_ciudad_internacional','destino_cp_internacional'):
        assert f'id="{field}"' in html and f'name="{field}"' in html
    assert 'id="quote-add-package"' in html
    assert 'data-remove-package' in html
    assert 'data-ref-city=' not in html


def test_recoleccion_se_presenta_como_accion_explicita_del_envio():
    envios=(ROOT/'templates/portal/envios.html').read_text()
    recolecciones=(ROOT/'templates/portal/recolecciones.html').read_text()
    assert 'Programar retiro' in envios
    assert 'Programar recolección con {{ envio_pre.courier }}' in recolecciones
