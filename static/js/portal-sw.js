/* ==========================================================
   TAURO — Service worker del portal del cliente (alcance /portal/)
   ----------------------------------------------------------
   Se sirve desde GET /portal/sw.js (endpoints/portal_cliente.py) para
   que su alcance máximo sea /portal/: jamás controla el admin ni la
   web pública.

   CONTRATO DE SEGURIDAD (innegociable; tests/test_portal_pwa.py lo vigila):
   - RED-ONLY para todo lo autenticado: nunca se guarda en caché HTML de
     páginas, respuestas de API, PDFs/etiquetas/facturas/comprobantes ni
     tokens. Las navegaciones exitosas se entregan tal cual llegan de la
     red y JAMÁS se escriben (no existe cache.put en este archivo).
   - Métodos distintos de GET: ni se interceptan.
   - Subrecursos, descargas y /portal/api/*: camino default del navegador
     (red directa), el worker los deja pasar sin respondWith.
   - Lo único precacheado es la pantalla offline PÚBLICA y NEUTRA.
   - Sin red, una navegación del portal recibe SÓLO esa pantalla; si la
     caché fue purgada por el sistema, un HTML mínimo equivalente.
     Fallar cerrado: nunca contenido de otra sesión, nunca datos.
   - Al activar una versión nueva se borran los cachés con otro nombre.
     Si cambia templates/portal/offline.html, subir VERSION acá: el
     byte-diff dispara la reinstalación y el precache fresco.
   - Push/notificaciones: este worker es el punto de anclaje futuro,
     pero en esta fase NO hay suscripción, permisos ni handlers.
   ========================================================== */
"use strict";

var VERSION = "v1";
var CACHE_PREFIX = "tauro-portal-";
var CACHE_NAME = CACHE_PREFIX + VERSION;
var OFFLINE_URL = "/portal/offline";

// Último recurso si el sistema purgó la Cache Storage: la misma pantalla
// neutra en versión mínima. Sin datos, sin enlaces privados.
var HTML_RESPALDO =
  '<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">' +
  '<meta name="viewport" content="width=device-width, initial-scale=1">' +
  '<title>Sin conexión · Tauro Solutions</title></head>' +
  '<body style="margin:0;min-height:100vh;display:flex;align-items:center;' +
  'justify-content:center;background:#0c0a14;color:#f4f5f7;text-align:center;' +
  'font-family:system-ui,sans-serif;"><div style="max-width:420px;padding:32px;">' +
  '<h1 style="font-size:26px;">Estás sin conexión</h1>' +
  '<p style="color:#b9bfc7;line-height:1.6;">Para usar el portal de TAURO ' +
  'necesitás internet. Revisá tu conexión y volvé a intentar.</p></div></body></html>';

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function (cache) {
      // "reload" saltea la caché HTTP: se precachea lo que el servidor
      // sirve HOY. Si la ruta falla, la instalación falla entera y sigue
      // atendiendo la versión anterior (fallar cerrado).
      return cache.add(new Request(OFFLINE_URL, { cache: "reload" }));
    })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (nombres) {
      return Promise.all(
        nombres
          // Cache Storage se comparte entre todas las aplicaciones del
          // origen. Limpiar únicamente versiones anteriores de ESTE portal;
          // jamás borrar cachés de la web pública u otra integración futura.
          .filter(function (nombre) {
            return nombre.indexOf(CACHE_PREFIX) === 0 && nombre !== CACHE_NAME;
          })
          .map(function (nombre) { return caches.delete(nombre); })
      );
    }).then(function () { return self.clients.claim(); })
  );
});

// Actualización controlada: la página avisa cuándo activar (al abrir una
// navegación fresca), nunca a mitad de un formulario. Ver portal-pwa.js.
self.addEventListener("message", function (event) {
  if (event.data && event.data.type === "SKIP_WAITING") self.skipWaiting();
});

function pantallaOffline() {
  return caches.open(CACHE_NAME)
    .then(function (cache) { return cache.match(OFFLINE_URL); })
    .then(function (guardada) {
      return guardada || new Response(HTML_RESPALDO, {
        status: 503,
        headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
      });
    })
    .catch(function () {
      return new Response(HTML_RESPALDO, {
        status: 503,
        headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
      });
    });
}

self.addEventListener("fetch", function (event) {
  var request = event.request;

  // Mutaciones (POST/PUT/DELETE…): el worker no participa.
  if (request.method !== "GET") return;

  // Sólo navegaciones de documento. Assets, APIs, PDFs y descargas
  // siguen el camino default del navegador: red directa, sin caché SW.
  if (request.mode !== "navigate") return;

  var url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  // Cinturón extra al scope: sólo documentos del portal.
  if (url.pathname.indexOf("/portal/") !== 0) return;

  // Red primero y SIN guardar: la respuesta viva se entrega y se descarta.
  // Sólo el fallo de red (sin conexión) muestra la pantalla neutra.
  event.respondWith(fetch(request).catch(pantallaOffline));
});
