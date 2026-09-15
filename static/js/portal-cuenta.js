(function () {
  "use strict";
  var form = document.getElementById("payment-report-form");
  if (!form) return;
  var paymentDetails = document.getElementById("informar-pago");
  var total = document.getElementById("pago-monto");
  var destinations = Array.from(form.querySelectorAll('[name="destinos"]'));
  var summary = document.getElementById("payment-allocation-summary");
  var money = new Intl.NumberFormat("es-AR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  function numberFromInput(value) {
    value = String(value || "").trim().replace(/\s/g, "");
    if (!value) return 0;
    if (value.includes(".") && value.includes(",")) {
      value = value.lastIndexOf(",") > value.lastIndexOf(".")
        ? value.replace(/\./g, "").replace(",", ".") : value.replace(/,/g, "");
    } else if (/^\d{1,3}([.,]\d{3})+$/.test(value)) {
      value = value.replace(/[.,]/g, "");
    } else {
      value = value.replace(",", ".");
    }
    var parsed = Number(value);
    return Number.isFinite(parsed) && parsed >= 0 ? Math.round(parsed * 100) : 0;
  }

  // The DOM balance is emitted by the server as Decimal, never user-formatted.
  function availableCents(destination) {
    var value = Number(destination.dataset.saldo);
    return Number.isFinite(value) && value > 0 ? Math.round(value * 100) : 0;
  }

  function updateAllocation() {
    var totalValue = numberFromInput(total.value);
    var selected = destinations.filter(function (item) { return item.checked && !item.disabled; })
      .reduce(function (sum, item) { return sum + availableCents(item); }, 0);
    var allocated = Math.min(totalValue, selected);
    var credit = Math.max(0, totalValue - selected);
    var pending = Math.max(0, selected - totalValue);
    summary.textContent = "Solicitás aplicar $ " + money.format(allocated / 100)
      + " · A favor, al aprobarse: $ " + money.format(credit / 100)
      + (pending ? " · Pendiente en los documentos: $ " + money.format(pending / 100) : "");
  }

  function openPayment() {
    paymentDetails.open = true;
    paymentDetails.scrollIntoView({ behavior: "auto", block: "start" });
    total.focus({ preventScroll: true });
  }

  document.querySelectorAll("[data-open-payment]").forEach(function (opener) {
    opener.addEventListener("click", function (event) {
      var key = opener.dataset.paymentDestination;
      if (key) {
        var destination = destinations.find(function (item) { return item.value === key && !item.disabled; });
        if (!destination || !availableCents(destination)) return;
        destinations.forEach(function (item) { item.checked = item === destination; });
        total.value = (availableCents(destination) / 100).toFixed(2).replace(".", ",");
        updateAllocation();
      }
      event.preventDefault();
      openPayment();
    });
  });
  destinations.forEach(function (checkbox) { checkbox.addEventListener("change", updateAllocation); });
  total.addEventListener("input", updateAllocation);
  if (window.location.hash === "#informar-pago") openPayment();
  updateAllocation();

  var region = document.querySelector("[data-account-region]");
  var currentRequest = null;
  if (!region || !window.fetch || !window.DOMParser || !window.AbortController) return;

  function accountUrl(value) {
    var url = new URL(value, window.location.href);
    return url.origin === window.location.origin && url.pathname === "/portal/cuenta"
      && !url.searchParams.has("pagar") ? url : null;
  }

  function setStatus(text, isError) {
    var status = region.querySelector("[data-account-status]");
    if (!status) return;
    status.textContent = text;
    status.classList.toggle("is-error", Boolean(isError));
    status.hidden = !text;
  }

  function updateSurroundingLinks(url) {
    var exportLink = document.querySelector("[data-account-export]");
    if (exportLink) {
      var exportUrl = new URL("/portal/cuenta/exportar.xlsx", url);
      exportUrl.search = url.search;
      exportUrl.searchParams.delete("pagina");
      exportUrl.searchParams.delete("pagar");
      exportLink.href = exportUrl.href;
    }
    document.querySelectorAll(".account-chart-month").forEach(function (link) {
      var chartUrl = new URL(link.href);
      // El gráfico muestra ambos ámbitos; su detalle debe sumar el mismo total.
      chartUrl.searchParams.set("ambito", "consolidado");
      link.href = chartUrl.href;
      link.classList.toggle("is-selected", (url.searchParams.get("ambito") || "consolidado") === "consolidado"
        && url.searchParams.get("tipo") === "costos" && !url.searchParams.get("q")
        && Boolean(url.searchParams.get("desde"))
        && url.searchParams.get("desde") === chartUrl.searchParams.get("desde")
        && url.searchParams.get("hasta") === chartUrl.searchParams.get("hasta"));
    });
    document.querySelectorAll(".account-scope-summary").forEach(function (link) {
      var scope = new URL(link.href).searchParams.get("ambito");
      var scopeUrl = new URL(url);
      scopeUrl.searchParams.set("ambito", scope);
      scopeUrl.searchParams.delete("pagina");
      scopeUrl.hash = "movimientos";
      link.href = scopeUrl.href;
      var selected = url.searchParams.get("ambito") === scope;
      link.classList.toggle("is-active", selected);
      if (selected) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    document.querySelectorAll("[data-account-link]").forEach(function (link) {
      var linkUrl = new URL(url);
      linkUrl.searchParams.set("tipo", link.dataset.accountLink);
      linkUrl.searchParams.delete("pagina");
      linkUrl.hash = "movimientos";
      link.href = linkUrl.href;
    });
  }

  function loadAccount(url, pushHistory) {
    if (currentRequest) currentRequest.abort();
    var controller = new AbortController();
    var loadingRegion = region;
    var timedOut = false;
    var keepError = false;
    var timeout = window.setTimeout(function () { timedOut = true; controller.abort(); }, 15000);
    currentRequest = controller;
    region.setAttribute("aria-busy", "true");
    setStatus("Actualizando movimientos…", false);
    window.fetch(url.href, {
      method: "GET", credentials: "same-origin", signal: controller.signal,
      headers: { "Accept": "text/html", "X-Tauro-Partial": "cuenta" }
    }).then(function (response) {
      var finalUrl = new URL(response.url || url.href);
      if (finalUrl.origin !== window.location.origin) throw new Error("Respuesta inesperada");
      if (finalUrl.pathname !== "/portal/cuenta") { window.location.assign(finalUrl.href); return null; }
      if (!response.ok) throw new Error("No se pudieron consultar los movimientos");
      return response.text();
    }).then(function (html) {
      if (html === null || currentRequest !== controller) return;
      var parsed = new DOMParser().parseFromString(html, "text/html");
      var next = parsed.querySelector("[data-account-region]");
      if (!next) throw new Error("Respuesta incompleta");
      url = accountUrl(next.dataset.accountUrl) || url;
      region.replaceWith(document.importNode(next, true));
      region = document.querySelector("[data-account-region]");
      if (pushHistory) window.history.pushState({ tauroCuenta: true }, "", url.pathname + url.search + "#movimientos");
      updateSurroundingLinks(url);
      region.scrollIntoView({ behavior: "auto", block: "start" });
      region.focus({ preventScroll: true });
    }).catch(function (error) {
      if (currentRequest !== controller || (error.name === "AbortError" && !timedOut)) return;
      keepError = true;
      setStatus(timedOut ? "La consulta está demorando. Conservamos tus movimientos; volvé a intentar."
        : "No pudimos actualizar los movimientos. Conservamos la lista anterior; volvé a intentar.", true);
    }).finally(function () {
      window.clearTimeout(timeout);
      if (currentRequest !== controller) return;
      currentRequest = null;
      loadingRegion.removeAttribute("aria-busy");
      region.removeAttribute("aria-busy");
      if (!keepError) setStatus("", false);
    });
  }

  document.addEventListener("submit", function (event) {
    var filterForm = event.target;
    if (!region.contains(filterForm) || !filterForm.matches(".account-unified-filters")) return;
    var url = accountUrl(filterForm.action);
    if (!url) return;
    event.preventDefault();
    url.search = new URLSearchParams(new FormData(filterForm)).toString();
    loadAccount(url, true);
  });
  document.addEventListener("click", function (event) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    var link = event.target.closest && event.target.closest("a[href]");
    if (!link || link.target || link.hasAttribute("download")) return;
    if (!region.contains(link) && !link.matches(".account-chart-month, .account-scope-summary, [data-account-link]")) return;
    var url = accountUrl(link.href);
    if (!url) return;
    event.preventDefault();
    loadAccount(url, true);
  });
  window.addEventListener("popstate", function () {
    var url = accountUrl(window.location.href);
    if (url) loadAccount(url, false);
  });
})();
