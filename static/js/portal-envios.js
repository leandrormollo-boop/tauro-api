/* Filtros progresivos de "Mis envíos". Todos los href y formularios siguen
   siendo el fallback canónico cuando JavaScript o la red interna fallan. */
(function () {
  "use strict";

  var root = document.querySelector("[data-envios-region]");
  var currentRequest = null;
  if (!root || typeof window.fetch !== "function" || typeof window.DOMParser !== "function") return;

  function statusFor(container) {
    return container && container.querySelector("[data-envios-status]");
  }

  function startLoading(container) {
    var status = statusFor(container);
    container.setAttribute("aria-busy", "true");
    container.classList.add("is-loading");
    if (status) {
      status.classList.remove("is-error");
      status.querySelector("[data-envios-status-text]").textContent = "Actualizando tus envíos…";
      status.hidden = false;
    }
  }

  function stopLoading(container) {
    var status = statusFor(container);
    if (!container || !container.isConnected) return;
    container.setAttribute("aria-busy", "false");
    container.classList.remove("is-loading");
    if (status) status.hidden = true;
  }

  function showError(container) {
    var status = statusFor(container);
    if (!container || !container.isConnected || !status) return;
    container.setAttribute("aria-busy", "false");
    container.classList.remove("is-loading");
    status.classList.add("is-error");
    status.querySelector("[data-envios-status-text]").textContent =
      "No pudimos actualizar. Intentá nuevamente.";
    status.hidden = false;
    window.setTimeout(function () {
      if (status.isConnected && status.classList.contains("is-error")) status.hidden = true;
    }, 5000);
  }

  function internalUrl(value) {
    var url = new URL(value, window.location.href);
    if (url.origin !== window.location.origin || url.pathname !== "/portal/envios") return null;
    return url;
  }

  function replaceRegion(html, responseUrl, historyMode, previousScroll) {
    var parsed = new DOMParser().parseFromString(html, "text/html");
    var next = parsed.querySelector("[data-envios-region]");
    if (!next) throw new Error("Respuesta de envíos incompleta");

    var imported = document.importNode(next, true);
    root.replaceWith(imported);
    root = imported;
    document.title = parsed.title || document.title;

    if (historyMode === "push") {
      window.history.pushState({tauroEnvios: true}, "", responseUrl.pathname + responseUrl.search);
    }
    window.scrollTo({left: previousScroll.x, top: previousScroll.y, behavior: "auto"});
    window.requestAnimationFrame(function () {
      window.scrollTo({left: previousScroll.x, top: previousScroll.y, behavior: "auto"});
    });
  }

  function load(value, historyMode) {
    var url = internalUrl(value);
    if (!url) return false;
    if (currentRequest) currentRequest.abort();

    var controller = new AbortController();
    var loadingRoot = root;
    var previousScroll = {x: window.scrollX, y: window.scrollY};
    var keepErrorVisible = false;
    currentRequest = controller;
    startLoading(loadingRoot);

    window.fetch(url.href, {
      method: "GET",
      credentials: "same-origin",
      headers: {"Accept": "text/html", "X-Tauro-Partial": "envios"},
      signal: controller.signal
    }).then(function (response) {
      var finalUrl = new URL(response.url || url.href, window.location.href);
      if (finalUrl.pathname !== "/portal/envios") {
        window.location.assign(finalUrl.href);
        return null;
      }
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.text().then(function (html) {
        return {html: html, url: finalUrl};
      });
    }).then(function (result) {
      if (!result || currentRequest !== controller) return;
      replaceRegion(result.html, result.url, historyMode, previousScroll);
    }).catch(function (error) {
      if (error.name !== "AbortError") {
        keepErrorVisible = true;
        showError(loadingRoot);
      }
    }).finally(function () {
      if (currentRequest === controller) {
        currentRequest = null;
        if (!keepErrorVisible) stopLoading(loadingRoot);
      }
    });
    return true;
  }

  document.addEventListener("change", function (event) {
    var field = event.target.closest && event.target.closest("[data-periodo-envios]");
    if (!field || !root.contains(field)) return;
    var form = field.form;
    if (field.name === "anio" && !field.value) {
      form.elements.mes.value = "";
      form.elements.mes.disabled = true;
      form.elements.semana.value = "";
      form.elements.semana.disabled = true;
    }
    if (field.name === "mes" && !field.value) {
      form.elements.semana.value = "";
      form.elements.semana.disabled = true;
    }
    form.requestSubmit();
  });

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || !root.contains(form) || internalUrl(form.action) === null || form.method.toUpperCase() !== "GET") return;
    event.preventDefault();
    var url = new URL(form.action, window.location.href);
    url.search = new URLSearchParams(new FormData(form)).toString();
    load(url.href, "push");
  });

  document.addEventListener("click", function (event) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    var link = event.target.closest && event.target.closest("a[href]");
    if (!link || !root.contains(link) || link.target || link.hasAttribute("download")) return;
    var url = internalUrl(link.href);
    if (!url) return;
    event.preventDefault();
    load(url.href, "push");
  });

  window.addEventListener("popstate", function () {
    if (!internalUrl(window.location.href)) return;
    load(window.location.href, "none");
  });
})();
