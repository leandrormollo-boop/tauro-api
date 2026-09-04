"""Contratos de claridad del portal del cliente.

Estos tests no prueban estilos visuales; evitan que vuelvan mensajes o bloques
que contradicen el flujo real (catálogo obligatorio, precio cerrado, pagos ya
aplicados) y preservan las puertas principales del inicio.
"""
from pathlib import Path


RAIZ = Path(__file__).resolve().parent.parent


def _template(nombre: str) -> str:
    return (RAIZ / "templates" / "portal" / nombre).read_text(encoding="utf-8")


def test_home_prioriza_nuevo_envio_y_cotizar_sin_onboarding_generico():
    html = _template("home.html")
    assert 'href="/portal/envios/nuevo"' in html
    assert 'href="/portal/cotizar"' in html
    assert "home-primary-actions" in html
    assert "checklist" not in html
    assert "arranque" not in html
    assert "Cargá tu primer producto" not in html


def test_acciones_principales_comparten_jerarquia_sin_afectar_el_admin():
    base = (RAIZ / "templates" / "base.html").read_text(encoding="utf-8")
    selector = _template("_ambito_selector.html")
    css = (RAIZ / "static" / "css" / "tauro.css").read_text(encoding="utf-8")

    assert 'class="side-quick-actions"' in base
    assert 'class="side-item cta"' not in base
    assert "Cotizar envío" in _template("home.html")
    assert selector.count('class="scope-cta"') == 2
    assert ".shell .btn-primary:not(.is-loading)" in css
    assert "tauro.css?v=41" in base


def test_recordatorio_del_home_solo_muestra_acciones_del_cliente():
    html = _template("home.html")
    assert "Requiere tu acción" in html
    assert "paso.accion_de == 'cliente'" in html
    assert "paso.cantidad" in html


def test_paises_largos_tienen_busqueda():
    cotizar = _template("cotizar.html")
    nuevo = _template("envio_nuevo.html")
    for select_id in ("origen_pais", "destino_pais"):
        assert f'id="{select_id}" data-searchable' in cotizar
    assert 'id="rem_pais" data-searchable' in nuevo
    assert 'id="destino_pais" data-searchable' in nuevo
    assert 'class="bulto-pais-fab" data-searchable' in nuevo


def test_cotizador_no_promete_precio_cerrado_ni_conversion_completa():
    html = _template("cotizar.html")
    assert "Precio cerrado" not in html
    assert "lo convertís en envío" not in html
    assert "Obtené una estimación" in html
    assert "volvemos a cotizar" in html
    assert "Recomendado" not in html


def test_cotizador_no_apila_formulario_y_resultados_en_la_misma_vista():
    html = _template("cotizar.html")
    assert 'quote-form-card {% if opciones %}is-hidden{% endif %}' in html
    assert 'class="quote-result-panel fade-up"' in html
    assert "result-box" not in html
    assert "Elegir →" not in html
    assert "Continuar →" not in html
    assert "Elegir {{ op.carrier_nombre }}" in html
    assert "courier={{ op.carrier_id }}" in html
    # Una opción compacta por courier. No truncar en dos: si UPS también
    # cotiza, DHL no puede quedar escondido dentro de otro desplegable.
    assert "{% for op in opciones %}" in html
    assert "opciones[:2]" not in html
    assert "opciones[2:]" not in html
    assert "Modificar datos" in html
    assert "result.focus({ preventScroll: true })" in html


def test_cotizador_destaca_dhl_y_da_profundidad_al_formulario():
    html = _template("cotizar.html")
    css = (RAIZ / "static" / "css" / "tauro.css").read_text(encoding="utf-8")

    assert 'class="quote-operator-logo" src="{{ operador.logo }}"' in html
    assert 'class="quote-operator-layout"' in html
    assert 'class="quote-operator-state"><i aria-hidden="true"></i>{{ operador.estado_label }}' in html
    assert 'data-carrier="{{ op.carrier_id }}"' in html
    assert 'class="quote-form-overview"' in html
    assert 'id="quote-route-summary"' in html
    assert 'id="quote-package-summary"' in html
    assert 'id="quote-progress-bar"' in html
    assert 'data-quote-section="route"' in html
    assert 'data-quote-section="packages"' in html
    assert '.quote-operator-chip.ready .quote-operator-logo-shell' in css
    assert 'width: 92px;' in css
    assert '.quote-carrier-logo {' in css
    assert 'width: 88px;' in css
    assert 'box-shadow:\n    0 26px 70px rgba(0,0,0,.32)' in css


def test_logos_de_courier_no_se_recortan_y_el_strip_refluye_antes_de_colisionar():
    css = (RAIZ / "static" / "css" / "tauro.css").read_text(encoding="utf-8")

    logo_start = css.index(".quote-operator-logo {")
    logo_end = css.index('.quote-operator-chip[data-carrier="dhl"]', logo_start)
    logo_rule = css[logo_start:logo_end]

    assert "position: absolute;" in logo_rule
    assert "inset: 0;" in logo_rule
    assert "width: 100%;" in logo_rule
    assert "height: 100%;" in logo_rule
    assert "object-fit: contain;" in logo_rule
    assert '.quote-operator-chip[data-carrier="fedex"] .quote-operator-logo' in css
    assert '.quote-operator-chip[data-carrier="ups"] .quote-operator-logo' in css
    assert "@container quote-operators (max-width: 820px)" in css
    assert ".quote-operator-chip.ready {\n    grid-column: 1 / -1;" in css
    assert ".quote-operator-chip.pending { opacity" not in css
    operator_start = css.index(".quote-operator-strip {")
    operator_end = css.index(".national-eyebrow", operator_start)
    assert "font-size: 6.5px" not in css[operator_start:operator_end]
    assert ".quote-operator-state > i" in css


def test_cotizador_actualiza_resumen_progreso_y_cajas_en_vivo():
    html = _template("cotizar.html")

    assert "function syncQuotePreview()" in html
    assert 'form.addEventListener("input", syncQuotePreview)' in html
    assert 'form.addEventListener("change", syncQuotePreview)' in html
    assert 'totalWeight.toLocaleString("es-AR"' in html
    assert 'progressBar.style.width' in html
    assert 'last.classList.add("is-entering")' in html
    assert 'row.classList.add("is-removing")' in html


def test_cotizar_y_nuevo_envio_exigen_elegir_ambito_primero():
    cotizar = _template("cotizar.html")
    nuevo = _template("envio_nuevo.html")
    selector = _template("_ambito_selector.html")

    assert "{% if not ambito %}" in cotizar
    assert "{% if not ambito %}" in nuevo
    assert "🇦🇷" in selector
    assert "🌐" in selector
    assert "Envío nacional" in selector
    assert "Envío internacional" in selector
    assert 'name="ambito" value="internacional"' in cotizar
    assert 'name="ambito" value="internacional"' in nuevo


def test_cotizador_exige_elegir_destino_en_vez_de_tomar_el_primero():
    html = _template("cotizar.html")
    assert '<option value="" disabled' in html
    assert ">Elegí destino</option>" in html


def test_remitente_precargado_se_resume_sin_perder_campos_editables():
    html = _template("envio_nuevo.html")
    assert 'class="remitente-collapsible"' in html
    assert 'id="rem-summary-name"' in html
    assert "actualizarResumenRemitente" in html
    for campo in (
        "rem_nombre", "rem_contacto", "rem_documento", "rem_email",
        "rem_telefono", "rem_direccion", "rem_pais", "rem_ciudad",
        "rem_estado", "rem_zip",
    ):
        assert f'name="{campo}"' in html


def test_nuevo_envio_mantiene_un_paso_compacto_por_pantalla():
    html = _template("envio_nuevo.html")
    css = (RAIZ / "static" / "css" / "tauro.css").read_text(encoding="utf-8")
    assert 'id="shipment-wizard" data-step="1"' in html
    assert "shipment-step-recipient" in html
    assert "shipment-step-package" in html
    assert "shipment-step-invoice" in html
    assert html.index("shipment-step-package") < html.index("shipment-step-invoice")
    assert "<label>Invoice</label>" in html
    assert "wizard.dataset.step = String(actual + 1)" in html
    assert "if (i > actual + 1) i = actual + 1" in html
    assert 'data-step]:not([data-step="1"]) > details.card' in css
    assert ".main-inner:has(.wizard-compacto) { padding-top: 20px; padding-bottom: 0; }" in css
    assert "shipment-step-recipient .form-grid-2" in css
    assert '/portal/clientes?nuevo=1' in html
    assert '{% if not remitente %}disabled{% endif %}' not in html
    for campo in ("rem_nombre", "rem_direccion", "rem_ciudad", "rem_zip"):
        assert f'name="{campo}" id="{campo}" required' in html


def test_paquete_e_invoice_estan_separados_y_sin_perder_multibulto():
    html = _template("envio_nuevo.html")
    css = (RAIZ / "static" / "css" / "tauro.css").read_text(encoding="utf-8")

    paquete = html[html.index('class="form-section shipment-step shipment-step-package"'):
                   html.index('class="form-section shipment-step shipment-step-invoice"')]
    invoice = html[html.index('class="form-section shipment-step shipment-step-invoice"'):
                   html.index('class="submit-bar"')]
    for campo in ("bulto_peso", "bulto_largo", "bulto_ancho", "bulto_alto",
                  "bulto_cantidad"):
        assert f'name="{campo}"' in paquete
        assert f'name="{campo}"' not in invoice
    for campo in ("bulto_desc_en", "bulto_unidades_aduana", "bulto_valor_usd",
                  "bulto_hs", "bulto_pais_fab"):
        assert f'name="{campo}"' in invoice
        assert f'name="{campo}"' not in paquete

    assert 'name="bulto_valor_usd" class="bulto-valor"' in invoice
    assert "DHL calcula cantidad × valor unitario" in invoice
    assert "data-invoice-line-total" in invoice
    hs = invoice[invoice.index('name="bulto_hs"'):invoice.index('name="bulto_hs"') + 180]
    assert "required" not in hs

    assert 'id="invoice-list"' in html
    assert "function invoiceDe(row)" in html
    assert "invoiceList.appendChild(nuevaInvoice)" in html
    assert "if (invoice) invoice.remove()" in html
    assert "renumerarBultos()" in html
    assert ".shipment-package-main" in css
    assert ".wizard-compacto .bulto-invoice" in css
    assert "grid-template-columns: repeat(6" in css


def test_error_aduanero_reabre_la_hoja_de_invoice():
    fuente = (RAIZ / "endpoints" / "portal_cliente.py").read_text(encoding="utf-8")
    assert "errores_paquete = []" in fuente
    assert "errores_invoice = []" in fuente
    assert "error_step = 4" in fuente
    assert "te falta el nombre del producto en inglés" in fuente
    assert "te falta el valor unitario en USD" in fuente


def test_opciones_secundarias_del_paquete_no_alargan_el_paso_principal():
    html = _template("envio_nuevo.html")
    assert '<details class="shipment-extra-options"' in html
    assert "Courier, impuestos, precio de reventa y notas" in html
    assert html.index('<details class="shipment-extra-options"') < html.index('id="courier-btns"')


def test_catalogo_se_presenta_como_opcional_para_revendedores():
    html = _template("envio_nuevo.html")
    assert "El catálogo es opcional" in html
    assert "integrar una tienda" in html
    assert "Todavía no tenés productos en el catálogo" not in html


def test_pago_pendiente_se_muestra_en_revision_y_sin_impacto():
    html = _template("cuenta.html")
    assert "El pago está en revisión y no modifica el saldo" in html
    assert "Todavía no impacta el saldo" in html
    assert "Facturado" in html
    assert "A facturar" in html
    assert "Envíos" in html
    assert "Pagos" in html
    assert "A favor" in html


def test_informar_pago_esta_antes_del_historial_y_es_compacto():
    html = _template("cuenta.html")
    assert 'class="card account-payment-card"' in html
    assert html.index('id="informar-pago"') < html.index('{% set filas = movimientos["items"] %}')


def test_alta_y_edicion_de_clientes_abren_un_dialogo_sin_bajar_al_formulario():
    html = _template("clientes.html")
    assert '<dialog class="client-dialog"' in html
    assert "data-client-form-open" in html
    assert "showModal" in html
    assert "scrollIntoView" not in html
    assert "data-open-on-load=" in html
    assert 'dialog.dataset.openOnLoad === "true"' in html


def test_envios_distingue_precio_inicial_diferencias_y_cuenta_corriente():
    html = _template("envios.html")
    assert "Monto activo del período" in html
    assert "Precio del envío" in html
    assert "Inicial" in html
    assert "Diferencia" in html
    assert "TAX" in html
    assert "Total final" in html
    assert "total final en esta página" in html
    assert "Cargos y facturas:" in html
    assert "No suma guías canceladas o reemplazadas" in html
    assert "?ambito={{ tipo_filtro }}{% endif %}" in html
    assert "Ver cuenta corriente" in html
    assert "Tu costo" not in html


def test_historial_de_envios_no_queda_truncado_en_cien():
    endpoint = (RAIZ / "endpoints" / "portal_cliente.py").read_text(encoding="utf-8")
    assert "listar_solicitudes_cliente(cliente, limite=None)" in endpoint
    assert "movimientos_cuenta_paginados(" in endpoint


def test_tselect_busca_codigo_y_cierra_al_salir_del_componente():
    js = (RAIZ / "static" / "js" / "tauro-ui.js").read_text(encoding="utf-8")
    assert 'normalizar(opt.text).trim()' in js
    assert 'normalizar(opt.value).trim()' in js
    assert 'wrap.addEventListener("focusout"' in js
    assert 'wrap.classList.toggle("open-up"' in js
    assert 'wrap.style.setProperty("--tselect-space"' in js


def test_desplegable_del_remitente_no_se_recorta_cuando_esta_abierto():
    css = (RAIZ / "static" / "css" / "tauro.css").read_text(encoding="utf-8")
    assert ".remitente-collapsible[open] { overflow: visible; }" in css
