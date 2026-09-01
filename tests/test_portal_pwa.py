"""
La PWA del portal: manifest, service worker, pantalla offline y barra móvil.

Por qué existen estos tests: el portal se vuelve instalable (Android/iPhone)
y un service worker mal escrito es una FUGA DE DATOS en potencia — si cachea
una navegación autenticada, el saldo o los envíos de un cliente quedan
legibles en el teléfono después del logout. Acá se fija el contrato:

- red-only para todo lo autenticado (el SW no escribe NINGUNA respuesta);
- el precache es exactamente UNA página: la offline, pública y neutra;
- las rutas nuevas heredan el no-store del middleware de seguridad;
- la barra móvil vive sólo en el shell autenticado (nunca en login);
- no se piden permisos ni se implementa push en esta fase.

Sin red y sin base de datos: las rutas se ejercitan hablando ASGI crudo con
la app (el entorno de tests no trae httpx/TestClient) y el shell autenticado
se renderiza con Jinja directo, reemplazando por fakes los globals que van a
la base (pendientes_menu, saldo_menu, ayuda).
"""
import asyncio
import json
import pathlib
import re
import shutil
import struct
import subprocess
from types import SimpleNamespace

import pytest

RAIZ = pathlib.Path(__file__).resolve().parent.parent
SW_PATH = RAIZ / "static" / "js" / "portal-sw.js"
PWA_JS_PATH = RAIZ / "static" / "js" / "portal-pwa.js"
BASE_HTML_PATH = RAIZ / "templates" / "base.html"
CSS_PATH = RAIZ / "static" / "css" / "tauro.css"


# ── Mini cliente ASGI (sin httpx): middleware real + rutas reales ──

def _get(path, cookie=None):
    from main import app

    inicio = {}
    cuerpo = bytearray()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(mensaje):
        if mensaje["type"] == "http.response.start":
            inicio["status"] = mensaje["status"]
            inicio["headers"] = [
                (clave.decode("latin-1").lower(), valor.decode("latin-1"))
                for clave, valor in mensaje.get("headers", [])
            ]
        elif mensaje["type"] == "http.response.body":
            cuerpo.extend(mensaje.get("body", b""))

    headers = [(b"host", b"testserver"), (b"accept", b"text/html")]
    if cookie:
        headers.append((b"cookie", cookie.encode("latin-1")))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": path,
        "raw_path": path.encode("latin-1"), "query_string": b"",
        "root_path": "", "headers": headers,
        "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))

    headers_min = {}
    for clave, valor in inicio.get("headers", []):
        headers_min.setdefault(clave, valor)
    return inicio.get("status"), headers_min, bytes(cuerpo).decode("utf-8")


def _png_dimensiones(ruta):
    datos = ruta.read_bytes()
    assert datos[:8] == b"\x89PNG\r\n\x1a\n", f"{ruta} no es un PNG válido"
    ancho, alto = struct.unpack(">II", datos[16:24])
    return ancho, alto


def _js_sin_comentarios(ruta):
    """El contrato se audita sobre CÓDIGO: un comentario que explica la
    regla ("no existe cache.put") no puede disparar el denylist."""
    src = ruta.read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"^\s*//.*$", "", src, flags=re.M)


# ── Manifest ────────────────────────────────────────────────

def test_manifest_valido_e_identidad_estable():
    status, headers, body = _get("/portal/manifest.webmanifest")
    assert status == 200
    assert headers["content-type"].startswith("application/manifest+json")
    data = json.loads(body)

    assert data["name"] == "TAURO"
    assert data["short_name"] == "TAURO"
    assert data["lang"] == "es-AR"
    assert data["display"] == "standalone"
    assert data["start_url"] == "/portal/home"
    assert data["scope"] == "/portal/"
    descripcion = data["description"].lower()
    assert "cotizá, emití y seguí tus envíos nacionales" not in descripcion
    assert "envíos internacionales" in descripcion
    assert "organizá por separado los nacionales" in descripcion
    # id estable: si cambia, los teléfonos quedan con dos apps distintas.
    assert data["id"] == "/portal/"
    assert data["start_url"].startswith(data["scope"])
    for color in (data["theme_color"], data["background_color"]):
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", color), color


def test_manifest_iconos_192_512_y_maskable_existen_de_verdad():
    _status, _headers, body = _get("/portal/manifest.webmanifest")
    iconos = json.loads(body)["icons"]

    vistos = {(icono["sizes"], icono["purpose"]) for icono in iconos}
    assert vistos == {
        ("192x192", "any"), ("512x512", "any"),
        ("192x192", "maskable"), ("512x512", "maskable"),
    }

    for icono in iconos:
        assert icono["type"] == "image/png"
        # Versionados y públicos: el SW no necesita cachearlos y el
        # navegador puede guardarlos sin tocar nada privado.
        assert icono["src"].startswith("/static/img/pwa/")
        assert "?v=" in icono["src"]
        ruta = RAIZ / icono["src"].split("?")[0].lstrip("/")
        assert ruta.is_file(), f"falta el archivo {ruta}"
        lado = int(icono["sizes"].split("x")[0])
        assert _png_dimensiones(ruta) == (lado, lado)


def test_manifest_publico_y_sin_cache_persistente():
    status, headers, _body = _get("/portal/manifest.webmanifest")
    assert status == 200                       # sin cookie: no exige sesión
    assert "no-store" in headers["cache-control"]


# ── Service worker ──────────────────────────────────────────

def test_sw_servido_desde_el_scope_del_portal():
    status, headers, body = _get("/portal/sw.js")
    assert status == 200                       # público: se registra desde login también
    assert headers["content-type"].startswith("application/javascript")
    # no-store: el navegador re-chequea el worker en cada navegación y una
    # versión vieja no puede quedar pegada en un teléfono.
    assert "no-store" in headers["cache-control"]
    # Servirlo bajo /portal/ le fija el alcance máximo: jamás controla
    # el admin ni la web pública.
    assert body == SW_PATH.read_text(encoding="utf-8")


def test_sw_solo_precachea_la_pantalla_offline():
    src = _js_sin_comentarios(SW_PATH)
    # Las únicas URLs del portal escritas en el worker: la pantalla offline
    # (precache) y el prefijo del cinturón de scope. Si alguien agrega una
    # ruta con datos acá, este set delata la fuga.
    urls_portal = set(re.findall(r"[\"'](/portal[^\"']*)[\"']", src))
    assert urls_portal == {"/portal/offline", "/portal/"}

    # Una sola escritura a Cache Storage (el precache del install) y nada más.
    assert src.count("cache.add(") == 1
    assert "cache.put" not in src
    assert "caches.put" not in src


def test_sw_es_red_only_para_todo_lo_autenticado():
    src = _js_sin_comentarios(SW_PATH)
    # Mutaciones y subrecursos ni se interceptan.
    assert 'request.method !== "GET"' in src
    assert 'request.mode !== "navigate"' in src
    # Una única respuesta interceptada: red primero, y el catch cae en la
    # pantalla offline. La respuesta viva nunca se escribe.
    assert src.count("respondWith(") == 1
    assert "respondWith(fetch(request).catch(pantallaOffline))" in src
    # Nada de almacenamiento fuera de Cache Storage.
    assert "localStorage" not in src
    assert "indexedDB" not in src
    assert "importScripts" not in src


def test_sw_actualiza_controlado_y_limpia_caches_viejos():
    src = _js_sin_comentarios(SW_PATH)
    assert "caches.delete" in src
    assert 'var CACHE_PREFIX = "tauro-portal-"' in src
    assert "nombre.indexOf(CACHE_PREFIX) === 0" in src
    assert "clients.claim()" in src
    # skipWaiting sólo cuando la página lo pide (mensaje), nunca solo.
    assert src.count("self.skipWaiting()") == 1
    assert 'event.data.type === "SKIP_WAITING"' in src
    # Si se purga la caché, responde un HTML mínimo neutro (fallar cerrado).
    assert "503" in src


def test_sw_sin_push_ni_permisos_en_esta_fase():
    for ruta in (SW_PATH, PWA_JS_PATH):
        src = _js_sin_comentarios(ruta)
        assert "pushManager" not in src, ruta.name
        assert "requestPermission" not in src, ruta.name
        assert "Notification." not in src, ruta.name
        assert 'addEventListener("push"' not in src, ruta.name


# ── Pantalla offline ────────────────────────────────────────

def test_offline_es_publica_neutra_y_autocontenida():
    status, headers, body = _get("/portal/offline")
    assert status == 200                       # sin cookie: es la única página precacheada
    assert "no-store" in headers["cache-control"]
    assert '<html lang="es">' in body

    # Neutra: nada de sesión, saldos ni navegación privada.
    assert "side-nav" not in body
    assert "tabbar" not in body
    assert "saldo" not in body.lower()

    # Autocontenida: sin /static ni Google Fonts no renderiza offline.
    assert "/static/" not in body
    assert "fonts.googleapis" not in body

    # El inline script lleva el nonce del MISMO request: el precache guarda
    # el par HTML+CSP consistente y el retry funciona servido desde caché.
    nonce = re.search(r"'nonce-([A-Za-z0-9_-]+)'", headers["content-security-policy"])
    assert nonce, "la CSP del offline perdió el nonce"
    assert f'nonce="{nonce.group(1)}"' in body


def test_offline_explica_que_hace_falta_conexion_y_permite_reintentar():
    _status, _headers, body = _get("/portal/offline")
    for palabra in ("cotizar", "emitir", "cuenta", "sincronizar"):
        assert palabra in body.lower(), palabra
    assert "Reintentar" in body
    assert 'addEventListener("online"' in body   # reintento al volver la red


# ── Wiring del portal (head, login, start_url) ──────────────

def test_login_trae_manifest_y_meta_pwa_pero_ni_tabbar_ni_cta():
    status, _headers, body = _get("/portal/login")
    assert status == 200
    assert '<link rel="manifest" href="/portal/manifest.webmanifest">' in body
    assert "viewport-fit=cover" in body
    assert 'name="theme-color"' in body
    assert 'name="apple-mobile-web-app-title" content="TAURO"' in body
    assert 'rel="apple-touch-icon"' in body
    assert "/static/js/portal-pwa.js?v=" in body
    # La barra móvil y la CTA de instalación NO existen en login.
    assert 'class="tabbar"' not in body
    assert "pwa-banner" not in body


def test_start_url_sin_sesion_redirige_a_login():
    status, headers, _body = _get("/portal/home")
    assert status == 303
    assert headers["location"] == "/portal/login"


def test_superficies_pwa_mantienen_no_store():
    # La instalación no debilita el contrato de caché del portal.
    for ruta in ("/portal/manifest.webmanifest", "/portal/sw.js",
                 "/portal/offline", "/portal/login"):
        _status, headers, _body = _get(ruta)
        cache = headers["cache-control"]
        for directiva in ("no-store", "no-cache", "must-revalidate", "private"):
            assert directiva in cache, f"{ruta}: falta {directiva}"


# ── Shell autenticado: tabbar y CTA (render Jinja directo) ──

def _render_shell(path="/portal/home", pendientes=0):
    """Renderiza home.html con cliente logueado y sin tocar la base."""
    from endpoints import portal_cliente as portal

    env = portal.templates.env
    originales = {
        nombre: env.globals[nombre]
        for nombre in ("pendientes_menu", "saldo_menu", "ayuda")
    }
    env.globals["pendientes_menu"] = lambda cliente: {"envios": pendientes, "tienda": 0}
    env.globals["saldo_menu"] = lambda cliente, ya=None: None
    env.globals["ayuda"] = lambda: {
        "whatsapp_url": None,
        "mail_url": "mailto:cotizaciones@taurosolutions.ar",
    }
    request = SimpleNamespace(
        url=SimpleNamespace(path=path),
        state=SimpleNamespace(csp_nonce="nonce-de-test"),
    )
    try:
        return env.get_template("portal/home.html").render(
            request=request,
            cliente="CLIENTE_TEST",
            saldo={"facturado_ars": 0, "pagado_ars": 0, "saldo_pendiente_ars": 0},
            solicitudes_nacionales=[],
            solicitudes_internacionales=[],
            embudo=[],
        )
    finally:
        env.globals.update(originales)


def _seccion_tabbar(html):
    inicio = html.index('<nav class="tabbar"')
    fin = html.index("</nav>", inicio)
    return html[inicio:fin]


def test_tabbar_del_shell_tiene_los_cinco_accesos_en_orden():
    tabbar = _seccion_tabbar(_render_shell())
    assert 'aria-label="Accesos rápidos del portal"' in tabbar
    hrefs = re.findall(r'href="([^"]+)"', tabbar)
    assert hrefs == [
        "/portal/home", "/portal/cotizar", "/portal/envios/nuevo",
        "/portal/envios", "/portal/cuenta",
    ]
    for etiqueta in ("Inicio", "Cotizar", "Nuevo envío", "Mis envíos", "Cuenta"):
        assert etiqueta in tabbar, etiqueta


def test_tabbar_marca_la_pestana_activa_con_aria_current():
    en_home = _seccion_tabbar(_render_shell(path="/portal/home"))
    assert en_home.count('aria-current="page"') == 1
    assert 'href="/portal/home" class="tabbar-item active" aria-current="page"' in en_home

    en_cuenta = _seccion_tabbar(_render_shell(path="/portal/cuenta"))
    assert en_cuenta.count('aria-current="page"') == 1
    assert 'href="/portal/cuenta" class="tabbar-item active" aria-current="page"' in en_cuenta

    # "Mis envíos" no puede robarse el activo cuando estás creando uno nuevo.
    en_nuevo = _seccion_tabbar(_render_shell(path="/portal/envios/nuevo"))
    assert 'href="/portal/envios/nuevo" class="tabbar-item tabbar-cta active"' in en_nuevo
    assert en_nuevo.count('aria-current="page"') == 1


def test_tabbar_muestra_el_globo_de_guias_listas():
    con_pendientes = _seccion_tabbar(_render_shell(pendientes=3))
    assert 'class="tabbar-badge"' in con_pendientes
    assert ">3</span>" in con_pendientes
    sin_pendientes = _seccion_tabbar(_render_shell(pendientes=0))
    assert "tabbar-badge" not in sin_pendientes


def test_cta_de_instalacion_arranca_oculta_y_con_guia_ios():
    html = _render_shell()
    banner = re.search(r'<div id="pwa-banner"[^>]*>', html)
    assert banner, "falta el banner de instalación en el shell"
    assert "hidden" in banner.group(0)          # sólo lo muestra portal-pwa.js
    assert 'role="region"' in banner.group(0)
    assert "Agregar a inicio" in html            # guía iPhone/iPad
    assert 'id="pwa-instalar"' in html
    assert 'id="pwa-cerrar"' in html
    assert 'aria-label="No volver a mostrar"' in html


# ── JS de instalación y registro ────────────────────────────

def test_pwa_js_registra_con_scope_portal_y_maneja_instalacion():
    src = _js_sin_comentarios(PWA_JS_PATH)
    assert 'navigator.serviceWorker.register("/portal/sw.js", { scope: "/portal/" })' in src
    assert "beforeinstallprompt" in src
    assert "event.preventDefault()" in src
    assert "appinstalled" in src
    # Una actualización descubierta durante un formulario queda esperando;
    # sólo una carga posterior activa registration.waiting.
    assert "activar(registro.waiting)" in src
    assert "updatefound" not in src
    # Standalone: la CTA no existe dentro de la app instalada.
    assert '(display-mode: standalone)' in src
    assert "navigator.standalone" in src
    # El descarte guarda UN timestamp con clave fija — nunca tokens ni datos.
    assert src.count("localStorage.setItem") == 1
    assert 'localStorage.setItem(DESCARTE_KEY' in src
    # La CTA se inserta en el flujo; nunca puede tapar un CTA sticky.
    assert 'contenido.insertBefore(banner, contenido.firstChild)' in src


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node no está instalado; el chequeo de sintaxis JS se saltea")
def test_js_nuevos_parsean_con_node():
    for ruta in (SW_PATH, PWA_JS_PATH):
        r = subprocess.run(["node", "--check", str(ruta)],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"{ruta.name} no parsea:\n{r.stderr}"


# ── CSS: la tabbar no tapa nada y desaparece donde no va ────

def test_css_tabbar_respeta_safe_area_touch_print_y_escritorio():
    css = CSS_PATH.read_text(encoding="utf-8")
    # Apagada por defecto (escritorio); visible sólo en el bloque móvil.
    assert ".tabbar, .pwa-banner { display: none; }" in css
    assert "env(safe-area-inset-bottom" in css
    assert "min-height: 48px" in css
    # El contenido y las barras sticky del pie se corren para no quedar tapados.
    assert ".shell .main-inner" in css
    assert ".shell .submit-bar" in css
    assert ".shell .wizard-compacto .paso-nav" in css
    # El aviso de instalación no es una capa fixed sobre el formulario.
    inicio_banner = css.rindex("  .pwa-banner {")
    bloque_banner = css[inicio_banner:css.index(".pwa-banner-txt", inicio_banner)]
    assert "position: relative" in bloque_banner
    assert "position: fixed" not in bloque_banner
    # Impresión: la navegación de pantalla no sale en papel.
    bloque_print = css[css.index("@media print"):]
    assert ".tabbar" in bloque_print and ".pwa-banner" in bloque_print
    # hidden manda siempre sobre el display móvil del banner.
    assert ".pwa-banner[hidden] { display: none; }" in css


def test_base_html_versiona_el_css_nuevo():
    base = BASE_HTML_PATH.read_text(encoding="utf-8")
    assert "tauro.css?v=36" in base
    # El manifest y el SW viven en el head compartido de TODO el portal
    # (login incluido); la tabbar queda adentro del bloque autenticado.
    assert base.index("{% if cliente %}") < base.index('class="tabbar"')


def test_ultimo_paso_del_wizard_tiene_un_solo_pie_interactivo():
    html = (RAIZ / "templates" / "portal" / "envio_nuevo.html").read_text(encoding="utf-8")
    assert 'id="wizard-back-final"' in html
    assert "if (i === secs.length - 1) return" in html
    assert 'volverFinal.addEventListener("click"' in html
