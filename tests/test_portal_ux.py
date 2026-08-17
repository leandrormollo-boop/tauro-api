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
    assert "wizard.dataset.step = String(actual + 1)" in html
    assert "if (i > actual + 1) i = actual + 1" in html
    assert 'data-step]:not([data-step="1"]) > details.card' in css
    assert "shipment-step-recipient .form-grid-2" in css
    assert '/portal/clientes?nuevo=1' in html
    assert '{% if not remitente %}disabled{% endif %}' not in html
    for campo in ("rem_nombre", "rem_direccion", "rem_ciudad", "rem_zip"):
        assert f'name="{campo}" id="{campo}" required' in html


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
    assert "Pendiente de facturación" in html
    assert "Pagos aplicados" in html
    assert "Saldo a favor" in html


def test_informar_pago_esta_antes_del_historial_y_es_compacto():
    html = _template("cuenta.html")
    assert 'class="card account-payment-card"' in html
    assert html.index('id="informar-pago"') < html.index("{% if movimientos %}")


def test_alta_y_edicion_de_clientes_abren_un_dialogo_sin_bajar_al_formulario():
    html = _template("clientes.html")
    assert '<dialog class="client-dialog"' in html
    assert "data-client-form-open" in html
    assert "showModal" in html
    assert "scrollIntoView" not in html
    assert "data-open-on-load=" in html
    assert 'dialog.dataset.openOnLoad === "true"' in html


def test_envios_distingue_cotizacion_de_cuenta_corriente():
    html = _template("envios.html")
    assert "Importe cotizado" in html
    assert "cotizado en esta página" in html
    assert "Cargos y facturas en el resumen total" in html
    assert "Tu costo" not in html


def test_historial_de_envios_no_queda_truncado_en_cien():
    endpoint = (RAIZ / "endpoints" / "portal_cliente.py").read_text(encoding="utf-8")
    assert "listar_solicitudes_cliente(cliente, limite=None)" in endpoint
    assert "get_facturas_recientes(cliente, limite=None)" in endpoint


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
