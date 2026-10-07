"""Contrato de ámbitos del cotizador público (web, sin login)."""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WIDGET = (ROOT / "web/components/02-quote-widget.jsx").read_text(encoding="utf-8")
GLOBE = (ROOT / "web/components/02-quote-globe.jsx").read_text(encoding="utf-8")
WEB_HTML = (ROOT / "web/Tauro Solutions.html").read_text(encoding="utf-8")


def test_widget_ofrece_ambitos_y_conserva_internacional():
    assert 'const [ambito, setAmbito] = useStateQ("internacional")' in WIDGET
    assert '[["nacional", "Nacional"], ["internacional", "Internacional"]]' in WIDGET
    assert 'endpoint = `${API_URL}/cotizar-web`' in WIDGET
    assert "origen_pais: origenIso" in WIDGET
    assert "destino_pais: destinoIso" in WIDGET
    assert "valor_declarado_usd: parsed.valor" in WIDGET


def test_widget_nacional_envia_solo_contrato_publico_oca():
    assert 'endpoint = `${API_URL}/cotizar-web/nacional`' in WIDGET
    for fragment in (
        "origen_cp: origenCp",
        "destino_cp: destinoCp",
        "cantidad_bultos: cantidad",
        "peso_kg: parsed.peso",
        "largo_cm: parsed.largo",
        "ancho_cm: parsed.ancho",
        "alto_cm: parsed.alto",
        "valor_declarado_ars: parsed.valor",
    ):
        assert fragment in WIDGET
    bloque = WIDGET.split('if (ambito === "nacional") {', 1)[1].split("} else {", 1)[0]
    assert "quote_id" not in bloque
    assert "precio_ars" not in bloque


def test_cada_ambito_conserva_su_valor_declarado_y_enriquecer_el_mismo_cp_no_invalida():
    assert "const [valorInternacional, setValorInternacional] = useStateQ(100)" in WIDGET
    assert "const [valorNacional, setValorNacional] = useStateQ(100)" in WIDGET
    assert 'ambito === "nacional" ? valorNacional : valorInternacional' in WIDGET
    assert "function effectiveArgentinaPostalCode" in WIDGET
    assert "if (previousPostal !== nextPostal) invalidateQuote()" in WIDGET


def test_ubicaciones_publicas_solo_autocompletan_coincidencia_exacta():
    assert 'fetch(`${API_URL}/cotizar-web/ubicaciones?${params}`' in WIDGET
    assert 'new URLSearchParams({ q: trimmed, tipo: mode })' in WIDGET
    assert "if (data?.automatic)" in WIDGET
    assert "onChange({ ...data.automatic, input: trimmed })" in WIDGET
    assert 'role="combobox"' in WIDGET
    assert 'role="listbox"' in WIDGET
    assert 'role="option"' in WIDGET
    assert 'placeholder="Ciudad o código postal"' in WIDGET
    assert "normalizeArgentinaPostalCode(next)" in WIDGET
    assert "const focusRef = useRefQ(false)" in WIDGET
    assert "setOpen(focusRef.current &&" in WIDGET
    assert "focusRef.current = false" in WIDGET


def test_campos_comparten_base_aunque_sus_labels_ocupen_distintas_lineas():
    assert 'display: "flex", flexDirection: "column", alignSelf: "stretch"' in WIDGET
    assert 'gap: 4, flex: "1 1 auto"' in WIDGET
    assert '<label htmlFor={id} style={{ flex: 1 }}>' in WIDGET


def test_nacional_no_crea_lead_ni_promete_preservar_tarifa():
    nacional = WIDGET.split("{resultIsNational ? (", 1)[1].split(") : (", 1)[0]
    assert 'href="/portal/cotizar?ambito=nacional"' in nacional
    assert "Continuar en el portal" in nacional
    assert "Todavía no tengo cuenta" in nacional
    assert "mailto:cotizaciones@taurosolutions.ar" in nacional
    assert "EmailCapture" not in nacional
    assert "quote_id" not in nacional
    assert "preserv" not in nacional.lower()
    assert "IVA incluido" in WIDGET


def test_requests_obsoletos_se_cancelan_al_cambiar_datos_o_ambito():
    assert "const quoteRequestRef = useRefQ(null)" in WIDGET
    assert "quoteRequestRef.current?.abort()" in WIDGET
    assert "signal: controller.signal" in WIDGET
    assert 'e?.name === "AbortError"' in WIDGET
    assert "requestRef.current?.abort()" in WIDGET
    edit = WIDGET.split("const edit = (next) => {", 1)[1].split("};", 1)[0]
    assert "requestRef.current?.abort()" in edit
    assert "setSuggestions([])" in edit
    assert "setActiveIndex(0)" in edit
    assert "setOpen(false)" in edit


def test_globo_se_recrea_por_ambito_y_nacional_usa_provincias_verificadas():
    assert "window.TauroQuoteMap?.dispose(root)" in GLOBE
    assert "}, [mode]);" in GLOBE
    assert 'data-quote-active={mode}' in GLOBE
    assert 'data-unified-form="nacional"' in GLOBE
    assert 'name="origen_provincia"' in GLOBE
    assert 'name="destino_provincia"' in GLOBE
    assert "nationalOrigin?.province || \"\"" in GLOBE
    assert "nationalDestination?.province || \"\"" in GLOBE
    assert "sin validar cobertura" in GLOBE


def test_resultado_acepta_shape_backend_mayuscula_y_cp_explicitos():
    assert 'normalizeCountry(result?.ambito) === "NACIONAL"' in WIDGET
    assert "result.origen_cp || result.origen" in WIDGET
    assert "result.destino_cp || result.destino" in WIDGET
    assert 'carrier.id === "oca" ? "/static/img/carriers/oca.png"' in WIDGET


def test_html_publico_referencia_el_bundle_nuevo():
    assert '/static/js/app.js?v=22' in WEB_HTML
    assert '/static/js/app.js?v=21' not in WEB_HTML
