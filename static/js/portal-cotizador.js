/* Cotizador nacional/internacional dentro de una ventana nativa <dialog>.
   La página /portal/cotizar sigue siendo el fallback sin JavaScript y la
   única fuente del formulario/resultados: no hay dos motores de precios. */
(function () {
  "use strict";

  var dialog = document.getElementById("quote-window-dialog");
  if (!dialog) return;

  var content = dialog.querySelector("[data-cotizar-contenido]");
  var closeButton = dialog.querySelector("[data-cotizar-cerrar]");
  var scopeLinks = dialog.querySelectorAll("[data-cotizar-scope]");
  var opener = null;
  var requestSerial = 0;

  function numberFrom(value) {
    var normalized = String(value || "").trim();
    if (normalized.indexOf(",") >= 0) {
      normalized = normalized.replace(/\./g, "").replace(",", ".");
    }
    var parsed = Number(normalized);
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function selectedName(select) {
    if (!select || !select.value) return "";
    var option = select.options[select.selectedIndex];
    return option
      ? option.text.replace(/\s*\([A-Z]{2}\)\s*$/, "")
      : select.value;
  }

  function loading(message) {
    content.replaceChildren();
    var state = document.createElement("div");
    state.className = "quote-window-loading";
    state.setAttribute("role", "status");
    state.textContent = message || "Preparando el cotizador…";
    content.appendChild(state);
  }

  function failure(message) {
    content.replaceChildren();
    var state = document.createElement("div");
    state.className = "quote-window-failure";

    var title = document.createElement("strong");
    title.textContent = "No pudimos abrir el cotizador";
    var detail = document.createElement("p");
    detail.textContent = message || "Revisá tu conexión y volvé a intentar.";
    var fallback = document.createElement("a");
    fallback.className = "btn btn-primary btn-sm";
    fallback.href = "/portal/cotizar?ambito=internacional";
    fallback.textContent = "Abrir la página completa";

    state.appendChild(title);
    state.appendChild(detail);
    state.appendChild(fallback);
    content.appendChild(state);
  }

  function quoteFromHtml(html) {
    var parsed = new DOMParser().parseFromString(html, "text/html");
    var quote = parsed.querySelector(".quote-screen");
    if (!quote) {
      throw new Error("La sesión puede haber vencido o la respuesta no contiene el cotizador.");
    }
    var imported = document.importNode(quote, true);
    imported.classList.add("quote-screen-window");
    return imported;
  }

  function setSectionState(root, section, complete) {
    var block = root.querySelector('[data-quote-section="' + section + '"]');
    var label = root.querySelector('[data-quote-state="' + section + '"]');
    if (block) block.classList.toggle("is-complete", complete);
    if (label) label.textContent = complete ? "Listo" : "Pendiente";
  }

  function markScope(scope) {
    scopeLinks.forEach(function (link) {
      var active = link.dataset.cotizarScope === scope;
      link.classList.toggle("on", active);
      if (active) link.setAttribute("aria-current", "true");
      else link.removeAttribute("aria-current");
    });
  }

  function showInlineError(root, stage, message) {
    var notice = root.querySelector("[data-quote-window-error]");
    if (!notice) {
      notice = document.createElement("div");
      notice.className = "msg error quote-error";
      notice.dataset.quoteWindowError = "1";
      root.insertBefore(notice, stage || null);
    }
    notice.textContent = message || "No pudimos consultar la tarifa.";
  }

  function initializeNationalQuote(root) {
    if (!root || root.dataset.quoteWindowReady === "1") return;
    root.dataset.quoteWindowReady = "1";

    var form = root.querySelector("#form-cotizar-nacional");
    var panel = root.querySelector("#national-quote-form-panel");
    var result = root.querySelector("#national-result");
    var loader = root.querySelector("#national-loading");
    var modify = root.querySelector("#national-modify");
    var stage = root.querySelector("#national-quote-stage");

    if (form) {
      form.addEventListener("submit", function (event) {
        if (event.defaultPrevented) return;
        event.preventDefault();
        if (!form.reportValidity()) return;

        if (panel) panel.classList.add("is-hidden");
        if (result) result.classList.add("is-hidden");
        if (loader) loader.hidden = false;
        if (stage) stage.setAttribute("aria-busy", "true");

        var serial = ++requestSerial;
        fetch(form.action, {
          method: "POST",
          body: new FormData(form),
          credentials: "same-origin",
          headers: { "X-Requested-With": "TauroQuoteWindow" }
        })
          .then(function (response) {
            if (!response.ok) throw new Error("El servidor respondió " + response.status + ".");
            return response.text();
          })
          .then(function (html) {
            if (serial !== requestSerial || !dialog.open) return;
            mountQuote(quoteFromHtml(html));
          })
          .catch(function (error) {
            if (serial !== requestSerial || !dialog.open) return;
            if (loader) loader.hidden = true;
            if (panel) panel.classList.remove("is-hidden");
            if (stage) stage.setAttribute("aria-busy", "false");
            showInlineError(root, stage, error.message);
          });
      });
    }

    if (modify) {
      modify.addEventListener("click", function () {
        if (result) result.classList.add("is-hidden");
        if (panel) panel.classList.remove("is-hidden");
        modify.hidden = true;
        var province = root.querySelector("#origen_provincia");
        if (province) province.focus({ preventScroll: true });
      });
    }
    if (result) {
      window.setTimeout(function () { result.focus({ preventScroll: true }); }, 0);
    }
  }

  function initializeQuote(root) {
    if (root && root.classList.contains("national-quote-screen")) {
      initializeNationalQuote(root);
      return;
    }
    if (!root || root.dataset.quoteWindowReady === "1") return;
    root.dataset.quoteWindowReady = "1";

    var form = root.querySelector("#form-cotizar");
    var panel = root.querySelector("#quote-form-panel");
    var result = root.querySelector("#resultado");
    var loader = root.querySelector("#toro-loading");
    var modify = root.querySelector("#quote-modify");
    var stage = root.querySelector("#quote-stage");
    var packageList = root.querySelector("#quote-package-list");
    var packageTemplate = root.querySelector("#quote-package-template");
    var addPackage = root.querySelector("#quote-add-package");
    var routeSummary = root.querySelector("#quote-route-summary");
    var packageSummary = root.querySelector("#quote-package-summary");
    var progressLabel = root.querySelector("#quote-progress-label");
    var progressBar = root.querySelector("#quote-progress-bar");
    var submit = root.querySelector("#quote-submit");

    function syncPreview() {
      if (!form || !packageList) return;
      var origin = root.querySelector("#origen_pais");
      var destination = root.querySelector("#destino_pais");
      var declared = root.querySelector("#valor_declarado_usd");
      var rows = Array.from(packageList.querySelectorAll("[data-package-row]"));
      var routeReady = Boolean(origin && origin.value && destination && destination.value);
      var packagesReady = rows.length > 0;
      var physicalBoxes = 0;
      var totalWeight = 0;

      rows.forEach(function (row) {
        var quantityInput = row.querySelector('[name="bulto_cantidad"]');
        var weightInput = row.querySelector('[name="bulto_peso"]');
        var lengthInput = row.querySelector('[name="bulto_largo"]');
        var widthInput = row.querySelector('[name="bulto_ancho"]');
        var heightInput = row.querySelector('[name="bulto_alto"]');
        var quantity = Math.max(1, Math.round(numberFrom(quantityInput && quantityInput.value)) || 1);
        var weight = numberFrom(weightInput && weightInput.value);
        var length = numberFrom(lengthInput && lengthInput.value);
        var width = numberFrom(widthInput && widthInput.value);
        var height = numberFrom(heightInput && heightInput.value);
        physicalBoxes += quantity;
        totalWeight += quantity * weight;
        if (!(weight > 0 && length > 0 && width > 0 && height > 0)) packagesReady = false;
      });

      var declaredReady = numberFrom(declared && declared.value) > 0;
      var originName = selectedName(origin) || "Origen";
      var destinationName = selectedName(destination);
      if (routeSummary) {
        routeSummary.textContent = originName + " → " + (destinationName || "Elegí destino");
      }
      if (packageSummary) {
        var boxesText = physicalBoxes + " caja" + (physicalBoxes === 1 ? "" : "s");
        packageSummary.textContent = boxesText + (totalWeight > 0
          ? " · " + totalWeight.toLocaleString("es-AR", { maximumFractionDigits: 2 }) + " kg"
          : "");
      }

      setSectionState(root, "route", routeReady);
      setSectionState(root, "packages", packagesReady && declaredReady);

      var progress = 10 + (routeReady ? 30 : 0) + (packagesReady ? 40 : 0) + (declaredReady ? 20 : 0);
      if (progressBar) progressBar.style.width = Math.min(progress, 100) + "%";
      if (progressLabel) {
        if (!routeReady) progressLabel.textContent = "Elegí el destino para comenzar.";
        else if (!packagesReady) progressLabel.textContent = "Completá el peso y las medidas de cada caja.";
        else if (!declaredReady) progressLabel.textContent = "Indicá el valor declarado total.";
        else progressLabel.textContent = "Todo listo para consultar la tarifa.";
      }
      if (submit) submit.classList.toggle("is-ready", routeReady && packagesReady && declaredReady);
    }

    function syncPackages() {
      if (!packageList) return;
      var rows = packageList.querySelectorAll("[data-package-row]");
      rows.forEach(function (row, index) {
        var number = row.querySelector("[data-package-number]");
        var remove = row.querySelector("[data-remove-package]");
        if (number) number.textContent = String(index + 1);
        if (remove) {
          remove.hidden = rows.length === 1;
          remove.setAttribute("aria-label", "Quitar caja " + String(index + 1));
        }
      });
      if (addPackage) addPackage.disabled = rows.length >= 20;
      syncPreview();
    }

    if (addPackage && packageList && packageTemplate) {
      addPackage.addEventListener("click", function () {
        if (packageList.querySelectorAll("[data-package-row]").length >= 20) return;
        packageList.appendChild(packageTemplate.content.cloneNode(true));
        syncPackages();
        var rows = packageList.querySelectorAll("[data-package-row]");
        var last = rows[rows.length - 1];
        if (last) last.classList.add("is-entering");
        var weight = last && last.querySelector('[name="bulto_peso"]');
        if (weight) weight.focus({ preventScroll: true });
      });

      packageList.addEventListener("click", function (event) {
        var remove = event.target.closest("[data-remove-package]");
        if (!remove) return;
        var row = remove.closest("[data-package-row]");
        if (row && packageList.querySelectorAll("[data-package-row]").length > 1) {
          row.classList.add("is-removing");
          window.setTimeout(function () {
            row.remove();
            syncPackages();
          }, 160);
        }
      });
      syncPackages();
    }

    if (form) {
      form.addEventListener("input", syncPreview);
      form.addEventListener("change", syncPreview);
      syncPreview();

      form.addEventListener("submit", function (event) {
        if (event.defaultPrevented) return;
        event.preventDefault();
        if (!form.reportValidity()) return;

        if (panel) panel.classList.add("is-hidden");
        if (result) result.classList.add("is-hidden");
        if (loader) loader.hidden = false;
        if (stage) stage.setAttribute("aria-busy", "true");
        if (submit) submit.disabled = true;

        var serial = ++requestSerial;
        fetch(form.action, {
          method: "POST",
          body: new FormData(form),
          credentials: "same-origin",
          headers: { "X-Requested-With": "TauroQuoteWindow" }
        })
          .then(function (response) {
            if (!response.ok) throw new Error("El servidor respondió " + response.status + ".");
            return response.text();
          })
          .then(function (html) {
            if (serial !== requestSerial || !dialog.open) return;
            mountQuote(quoteFromHtml(html));
          })
          .catch(function (error) {
            if (serial !== requestSerial || !dialog.open) return;
            if (loader) loader.hidden = true;
            if (panel) panel.classList.remove("is-hidden");
            if (stage) stage.setAttribute("aria-busy", "false");
            if (submit) submit.disabled = false;
            showInlineError(root, stage, error.message);
          });
      });
    }

    if (modify) {
      modify.addEventListener("click", function () {
        if (result) result.classList.add("is-hidden");
        if (panel) panel.classList.remove("is-hidden");
        modify.hidden = true;
        var origin = root.querySelector("#origen_pais");
        if (origin) origin.focus({ preventScroll: true });
      });
    }

    if (result) {
      window.setTimeout(function () { result.focus({ preventScroll: true }); }, 0);
    }
  }

  function mountQuote(quote) {
    content.replaceChildren(quote);
    content.scrollTop = 0;
    markScope(quote.classList.contains("national-quote-screen") ? "nacional" : "internacional");
    initializeQuote(quote);
  }

  function loadScope(scope) {
    var normalized = scope === "nacional" ? "nacional" : "internacional";
    markScope(normalized);
    loading("Preparando el cotizador " + normalized + "…");
    var serial = ++requestSerial;
    fetch("/portal/cotizar?ambito=" + normalized, {
      credentials: "same-origin",
      headers: { "X-Requested-With": "TauroQuoteWindow" }
    })
      .then(function (response) {
        if (!response.ok) throw new Error("El servidor respondió " + response.status + ".");
        return response.text();
      })
      .then(function (html) {
        if (serial !== requestSerial || !dialog.open) return;
        mountQuote(quoteFromHtml(html));
      })
      .catch(function (error) {
        if (serial !== requestSerial || !dialog.open) return;
        failure(error.message);
      });
  }

  function openQuote(event, trigger) {
    var href = new URL(trigger.href, window.location.origin);
    // En la página completa se conserva la navegación tradicional para no
    // duplicar IDs ni anidar un segundo cotizador sobre el primero.
    if (window.location.pathname === "/portal/cotizar") return;

    event.preventDefault();
    opener = trigger;
    if (typeof dialog.showModal !== "function") {
      window.location.assign(trigger.href);
      return;
    }
    if (!dialog.open) dialog.showModal();
    var sideToggle = document.getElementById("side-toggle");
    if (sideToggle) sideToggle.checked = false;
    loadScope(href.searchParams.get("ambito"));
  }

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest && event.target.closest("a[data-cotizar-ventana]");
    if (trigger) openQuote(event, trigger);
  });

  if (closeButton) closeButton.addEventListener("click", function () { dialog.close(); });
  dialog.addEventListener("click", function (event) {
    var scope = event.target.closest && event.target.closest("[data-cotizar-scope]");
    if (scope) {
      event.preventDefault();
      loadScope(scope.dataset.cotizarScope);
      return;
    }
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener("close", function () {
    requestSerial += 1;
    content.replaceChildren();
    if (opener && document.contains(opener)) opener.focus({ preventScroll: true });
    opener = null;
  });
})();
