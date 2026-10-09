(function () {
  "use strict";

  var formularios = Array.from(document.querySelectorAll("[data-catalog-sync-form]"));
  if (!formularios.length) return;

  var espera;
  var intentos = 0;
  var sincronizacionEsperada = null;

  function textoFecha(valor) {
    if (!valor) return "";
    var fecha = new Date(valor);
    if (Number.isNaN(fecha.getTime())) return "";
    return fecha.toLocaleString("es-AR", {
      day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit"
    });
  }

  function actualizarPantalla(datos) {
    var estado = (datos.sincronizacion || {}).estado || "";
    var etiquetas = {
      SINCRONIZANDO: "Sincronizando catálogo y stock…",
      COMPLETADO: "Stock sincronizado",
      REAUTORIZAR: "Falta autorizar catálogo y stock",
      ERROR: "No pudimos completar la sincronización"
    };
    document.querySelectorAll("[data-catalog-sync-status]").forEach(function (el) {
      if (etiquetas[estado]) el.textContent = etiquetas[estado];
    });
    document.querySelectorAll("[data-catalog-sync-variants]").forEach(function (el) {
      el.textContent = String((datos.resumen || {}).variantes || 0);
    });
    document.querySelectorAll("[data-catalog-sync-units]").forEach(function (el) {
      el.textContent = String((datos.resumen || {}).unidades_disponibles || 0);
    });
    document.querySelectorAll("[data-catalog-sync-out-of-stock]").forEach(function (el) {
      var agotadas = Number((datos.resumen || {}).agotadas || 0);
      el.textContent = String(agotadas);
      el.classList.toggle("is-active", agotadas > 0);
    });
    var actualizada = textoFecha((datos.sincronizacion || {}).ultima_sincronizacion_at);
    document.querySelectorAll("[data-catalog-sync-updated]").forEach(function (el) {
      el.textContent = actualizada ? "Actualizado " + actualizada : "";
    });
  }

  function estadoBotones(cargando) {
    formularios.forEach(function (formulario) {
      var boton = formulario.querySelector("button[type='submit']");
      if (!boton) return;
      if (!boton.dataset.catalogSyncLabel) boton.dataset.catalogSyncLabel = boton.textContent.trim();
      boton.disabled = cargando;
      boton.textContent = cargando ? "Sincronizando…" : boton.dataset.catalogSyncLabel;
    });
  }

  async function refrescarContenido() {
    var respuesta = await fetch(window.location.href, {
      credentials: "same-origin",
      headers: {"X-Requested-With": "catalog-sync"}
    });
    if (!respuesta.ok) return;
    var documento = new DOMParser().parseFromString(await respuesta.text(), "text/html");
    var resultadosActuales = document.querySelector("[data-catalog-results]");
    var resultadosNuevos = documento.querySelector("[data-catalog-results]");
    if (resultadosActuales && resultadosNuevos) {
      var destinoActual = document.getElementById("cat-destino");
      var codigoDestino = destinoActual ? destinoActual.value : "";
      resultadosActuales.replaceWith(document.importNode(resultadosNuevos, true));
      var destinoNuevo = document.getElementById("cat-destino");
      if (destinoNuevo && codigoDestino) {
        destinoNuevo.value = codigoDestino;
        destinoNuevo.dispatchEvent(new Event("change", {bubbles: true}));
      }
      return;
    }
    var nuevas = new Map();
    documento.querySelectorAll("[data-product-thumb-key]").forEach(function (miniatura) {
      var clave = miniatura.dataset.productThumbKey;
      if (clave) nuevas.set(clave, miniatura);
    });
    document.querySelectorAll("[data-product-thumb-key]").forEach(function (actual) {
      var nueva = nuevas.get(actual.dataset.productThumbKey);
      if (nueva) actual.replaceWith(document.importNode(nueva, true));
    });
  }

  async function consultarEstado() {
    intentos += 1;
    try {
      var respuesta = await fetch("/portal/api/catalogo/sync-estado", {
        credentials: "same-origin",
        headers: {"Accept": "application/json"}
      });
      var datos = await respuesta.json();
      if (!respuesta.ok || !datos.ok) throw new Error(datos.error || "No se pudo consultar el estado.");
      var sincronizacion = datos.sincronizacion || {};
      var estado = sincronizacion.estado;
      if (!sincronizacionEsperada || sincronizacion.sync_intento_id !== sincronizacionEsperada) {
        espera = window.setTimeout(consultarEstado, 2000);
        return;
      }
      actualizarPantalla(datos);
      if (estado === "COMPLETADO") {
        await refrescarContenido();
        estadoBotones(false);
        intentos = 0;
        return;
      }
      if (estado === "ERROR" || estado === "REAUTORIZAR") {
        estadoBotones(false);
        intentos = 0;
        return;
      }
    } catch (_) {
      if (intentos >= 30) {
        estadoBotones(false);
        return;
      }
    }
    if (intentos < 30) espera = window.setTimeout(consultarEstado, 2000);
    else estadoBotones(false);
  }

  formularios.forEach(function (formulario) {
    formulario.addEventListener("submit", async function (event) {
      event.preventDefault();
      window.clearTimeout(espera);
      intentos = 0;
      sincronizacionEsperada = null;
      estadoBotones(true);
      document.querySelectorAll("[data-catalog-sync-status]").forEach(function (el) {
        el.textContent = "Iniciando sincronización…";
      });
      try {
        var respuesta = await fetch(formulario.action, {
          method: "POST",
          body: new FormData(formulario),
          credentials: "same-origin",
          headers: {"Accept": "application/json"}
        });
        var datos = await respuesta.json();
        if (!respuesta.ok || !datos.ok) throw new Error(datos.error || "No se pudo sincronizar.");
        sincronizacionEsperada = datos.sync_intento_id || null;
        document.querySelectorAll("[data-catalog-sync-status]").forEach(function (el) {
          el.textContent = datos.en_curso ? "Sincronización en curso…" : "Sincronizando catálogo y stock…";
        });
        consultarEstado();
      } catch (_) {
        document.querySelectorAll("[data-catalog-sync-status]").forEach(function (el) {
          el.textContent = "No pudimos iniciar la sincronización";
        });
        estadoBotones(false);
      }
    });
  });
})();
