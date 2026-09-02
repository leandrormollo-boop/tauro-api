from pathlib import Path

from servicios.paginas_legales import pagina_ayuda_tiendanube


ROOT = Path(__file__).resolve().parents[1]


def test_pagina_ayuda_tiendanube_explica_requisitos_y_falla_cerrada():
    html = " ".join(pagina_ayuda_tiendanube().split())

    assert "TAURO Solutions Ar" in html
    assert "Argentina" in html
    assert "código postal de cuatro dígitos" in html
    assert "peso mayor a cero" in html
    assert "largo, ancho y alto" in html
    assert "pesos argentinos (ARS)" in html
    assert "carrito combina productos bonificados" in html
    assert "precio estimado ni inventado" in html


def test_pagina_ayuda_no_promete_condiciones_aun_no_definidas():
    html = " ".join(pagina_ayuda_tiendanube().split())

    assert "dependen del operador y del acuerdo vigente" in html
    assert "no se publican importes fijos" in html
    assert "SLA" not in html
    assert "etiqueta" not in html.lower()
    assert "cancelación" not in html.lower()
    assert "tracking" not in html.lower()


def test_pagina_ayuda_incluye_privacidad_y_soporte_seguro():
    html = " ".join(pagina_ayuda_tiendanube().split())

    assert 'href="/privacidad"' in html
    assert "No pedimos ni almacenamos datos de tarjetas" in html
    assert 'href="mailto:cotizaciones@taurosolutions.ar"' in html
    assert "No envíes" in html
    assert "contraseñas, tokens ni datos de tarjeta" in html


def test_main_publica_ruta_ayuda_tiendanube_fuera_del_openapi():
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert '@app.get("/ayuda/tiendanube", include_in_schema=False)' in source
    assert "return HTMLResponse(pagina_ayuda_tiendanube())" in source


def test_partner_portal_apunta_a_la_ayuda_publica():
    fields = (ROOT / "docs/tiendanube/PARTNER_PORTAL_FIELDS.md").read_text(
        encoding="utf-8"
    )

    assert "Soporte: `https://taurosolutions.ar/ayuda/tiendanube`" in fields
