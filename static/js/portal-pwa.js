/* ==========================================================
   TAURO — PWA del portal: registro del service worker + instalación
   ----------------------------------------------------------
   - Registra /portal/sw.js con alcance /portal/. Sólo el PORTAL es
     instalable en esta fase: ni el admin ni la web pública.
   - Actualización controlada: un worker nuevo se activa en la próxima
     carga de página (nunca a mitad de un formulario); al activarse
     limpia los cachés viejos (ver portal-sw.js).
   - CTA de instalación: beforeinstallprompt en Android/Chrome; guía
     manual en iPhone/iPad (Compartir → Agregar a inicio). Nunca se
     muestra si ya corre standalone, y al descartarla sólo se guarda un
     timestamp — jamás datos de la cuenta ni de la sesión.
   - Notificaciones push: la base queda lista (worker + este archivo),
     sin pedir permisos ni suscribir nada todavía.
   ========================================================== */
(function () {
  "use strict";

  var DESCARTE_KEY = "tauro-pwa-cta-descartada";  // timestamp; dato NO sensible
  var DESCARTE_DIAS = 30;
  var promptDiferido = null;
  var ui = null;

  function esStandalone() {
    return (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) ||
      window.navigator.standalone === true;
  }

  function esIOS() {
    var ua = navigator.userAgent || "";
    // iPadOS se presenta como Mac de escritorio, pero con pantalla táctil.
    return /iPhone|iPad|iPod/i.test(ua) ||
      (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  }

  function ctaDescartada() {
    try {
      var marca = Number(window.localStorage.getItem(DESCARTE_KEY) || 0);
      return marca > 0 && (Date.now() - marca) < DESCARTE_DIAS * 24 * 60 * 60 * 1000;
    } catch (error) { return false; }
  }

  function recordarDescarte() {
    try { window.localStorage.setItem(DESCARTE_KEY, String(Date.now())); }
    catch (error) { /* modo privado: la CTA volverá; no es grave */ }
  }

  /* ── Service worker: registro + actualización controlada ── */
  function registrarServiceWorker() {
    if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
    navigator.serviceWorker.register("/portal/sw.js", { scope: "/portal/" })
      .then(function (registro) {
        function activar(worker) {
          if (worker) worker.postMessage({ type: "SKIP_WAITING" });
        }
        // Quedó una versión nueva esperando de una visita anterior: este
        // documento recién carga, es un momento seguro para activarla.
        activar(registro.waiting);
        // Si la comprobación de ESTA carga descubre una versión nueva, se
        // deja en waiting. Recién la próxima navegación fresca encontrará
        // registro.waiting y la activará: nunca se cambia de worker mientras
        // el cliente está completando un formulario abierto.
      })
      .catch(function () { /* sin SW el portal sigue siendo la web de siempre */ });
  }

  /* ── CTA de instalación ── */
  function mostrarCTA(modoIOS) {
    if (!ui || esStandalone() || ctaDescartada()) return;
    ui.banner.hidden = false;
    if (modoIOS) {
      if (ui.guiaIOS) ui.guiaIOS.hidden = false;
    } else if (ui.instalar) {
      ui.instalar.hidden = false;
    }
  }

  function ocultarCTA() {
    if (ui) ui.banner.hidden = true;
  }

  // Chrome puede disparar esto antes o después de DOMContentLoaded: se
  // captura siempre y la UI lo consume cuando está lista.
  window.addEventListener("beforeinstallprompt", function (event) {
    event.preventDefault();  // el prompt nativo sale cuando el cliente toca "Instalar"
    promptDiferido = event;
    mostrarCTA(false);
  });

  window.addEventListener("appinstalled", function () {
    promptDiferido = null;
    recordarDescarte();
    ocultarCTA();
  });

  function inicializarCTA() {
    var banner = document.getElementById("pwa-banner");
    if (!banner) return;          // login y páginas sin shell no ofrecen instalar
    if (esStandalone()) {         // ya corre como app: ninguna CTA, nunca
      banner.remove();
      return;
    }
    // La invitación vive dentro del flujo del documento: empuja el contenido
    // en vez de flotar sobre Siguiente, Atrás o Crear solicitud. Esto importa
    // especialmente en iPhone, donde la guía manual es más alta.
    var contenido = document.querySelector(".main-inner");
    if (contenido && banner.parentNode !== contenido) {
      contenido.insertBefore(banner, contenido.firstChild);
    }
    ui = {
      banner: banner,
      instalar: document.getElementById("pwa-instalar"),
      guiaIOS: document.getElementById("pwa-guia-ios"),
      cerrar: document.getElementById("pwa-cerrar"),
    };

    if (ui.instalar) {
      ui.instalar.addEventListener("click", function () {
        var evento = promptDiferido;
        promptDiferido = null;
        if (!evento) { ocultarCTA(); return; }
        evento.prompt();
        evento.userChoice.then(ocultarCTA, ocultarCTA);
      });
    }
    if (ui.cerrar) {
      ui.cerrar.addEventListener("click", function () {
        recordarDescarte();
        ocultarCTA();
      });
    }

    if (promptDiferido) mostrarCTA(false);   // el evento llegó antes que la UI
    else if (esIOS()) mostrarCTA(true);      // Safari no emite beforeinstallprompt
  }

  function init() {
    registrarServiceWorker();
    inicializarCTA();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
