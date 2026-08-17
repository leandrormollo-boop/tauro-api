import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HERO = (ROOT / "web/components/01-nav-hero.jsx").read_text(encoding="utf-8")
QUOTE = (ROOT / "web/components/02-quote-widget.jsx").read_text(encoding="utf-8")
SERVICES = (ROOT / "web/components/03-services-tracking.jsx").read_text(encoding="utf-8")
FOOTER = (ROOT / "web/components/04-stats-contact-footer.jsx").read_text(encoding="utf-8")
APP = (ROOT / "web/components/05-app.jsx").read_text(encoding="utf-8")
HTML = (ROOT / "web/Tauro Solutions.html").read_text(encoding="utf-8")


def _word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", text))


def test_arquitectura_3_6_9_es_exacta_y_visible():
    copy_3 = "Conectá. Centralizá. Expandí."
    copy_6 = "Tu logística en un solo portal."
    copy_9 = "Logística nacional e internacional conectada directamente a tu tienda."

    assert _word_count(copy_3) == 3
    assert _word_count(copy_6) == 6
    assert _word_count(copy_9) == 9

    hero_sin_tags = re.sub(r"<[^>]+>", " ", HERO)
    hero_normalizado = " ".join(hero_sin_tags.split())
    assert "Conectá. Centralizá. Expandí." in hero_normalizado
    assert copy_6 in HERO
    assert copy_9 in HERO
    assert copy_3 in APP


def test_las_seis_acciones_reemplazan_despachar_por_automatizar():
    acciones = re.findall(r'name: "([^"]+)"', SERVICES.split("];", 1)[0])
    assert acciones == [
        "Conectá",
        "Centralizá",
        "Cotizá",
        "Automatizá",
        "Seguí",
        "Expandí",
    ]
    assert "Cotizá, centralizá" not in SERVICES
    assert "despachá" not in (HERO + QUOTE + SERVICES + FOOTER + APP).lower()


def test_copy_no_promete_capacidades_no_demostradas():
    copy_publico = HERO + QUOTE + SERVICES + FOOTER + APP + HTML
    frases_retiradas = [
        "decisiones en tiempo real",
        "respuesta inmediata",
        "cotizador instantáneo",
        "respuesta en segundos",
        "volumen sin límite",
        "comparamos fedex, ups y dhl",
        "cotización gratis en menos de 60 segundos",
    ]
    for frase in frases_retiradas:
        assert frase not in copy_publico.lower()
    assert '["FedEx", "DHL"]' not in HERO
    assert ': ["DHL"];' in HERO


def test_ctas_principales_siguen_el_sistema_parise():
    assert HERO.count("Conectá tu tienda") >= 3
    assert HERO.count("Cotizá un envío") >= 3
    assert 'href="/portal/login"' in HERO
    assert "¿No tenés tienda? Cargá tus envíos manualmente." in HERO
    assert "Integrá Shopify o cargá tus envíos manualmente." in FOOTER


def test_seo_social_y_datos_estructurados_quedan_alineados():
    assert "<title>Tauro Solutions | Conectá tu tienda y centralizá envíos</title>" in HTML
    description = re.search(r'<meta name="description" content="([^"]+)"', HTML).group(1)
    assert 120 <= len(description) <= 160
    assert "Shopify" in description
    assert "envíos nacionales e internacionales" in description
    assert '<link rel="canonical" href="https://taurosolutions.ar/"' in HTML
    assert 'property="og:title" content="Conectá. Centralizá. Expandí. | Tauro Solutions"' in HTML
    assert 'name="twitter:card" content="summary_large_image"' in HTML
    assert 'styles.css?v=12' in HTML
    assert '/static/js/app.js?v=10' in HTML

    structured = re.search(
        r'<script type="application/ld\+json">\s*(\{.*?\})\s*</script>',
        HTML,
        flags=re.DOTALL,
    ).group(1)
    data = json.loads(structured)
    assert data["@type"] == "Organization"
    assert "integración Shopify" in data["knowsAbout"]


def test_bundle_publicado_por_el_repo_contiene_el_copy_nuevo():
    bundle = (ROOT / "static/js/app.js").read_text(encoding="utf-8")
    bundle_visible = re.sub(
        r"\\x([0-9A-Fa-f]{2})",
        lambda match: chr(int(match.group(1), 16)),
        bundle,
    )
    assert "Conectá." in bundle_visible
    assert "Centralizá." in bundle_visible
    assert "Expandí." in bundle_visible
    assert "Tu logística en un solo portal." in bundle_visible
    assert "Cotizador instantáneo" not in bundle_visible
