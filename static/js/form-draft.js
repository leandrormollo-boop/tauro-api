/* Borradores de esta pestaña. Sólo datos del formulario: nunca tarifas,
   credenciales, archivos ni confirmaciones de operaciones. */
(function () {
  "use strict";
  var completedPrefix = "tauro:completed:v1:", drafts = [];
  var prefix = "tauro:draft:v1:", ttl = 4 * 60 * 60 * 1000;
  function scope() { return document.body.dataset.draftScope || ""; }
  function entries() {
    try { return Object.keys(sessionStorage).filter(function (k) { return k.indexOf(prefix) === 0; }); }
    catch (_) { return []; }
  }
  function remove(key) { try { sessionStorage.removeItem(key); } catch (_) {} }
  function read(key) {
    try {
      var data = JSON.parse(sessionStorage.getItem(key));
      if (!data || Date.now() - data.at > ttl) { remove(key); return null; }
      return data;
    } catch (_) { remove(key); return null; }
  }
  function fields(form) {
    return Array.from(form.elements).filter(function (el) {
      return el.name && /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName)
        && !/^(file|password|submit|button)$/.test(el.type)
        && (el.type !== "hidden" || el.hasAttribute("data-draft-persist"))
        && !/password|token|secret|csrf/i.test(el.name);
    });
  }
  function values(form) {
    var out = Object.create(null);
    fields(form).forEach(function (el) {
      if (!out[el.name]) out[el.name] = [];
      out[el.name].push(/^(checkbox|radio)$/.test(el.type) ? el.checked : el.value);
    });
    return out;
  }
  function fill(form, data) {
    var indexes = Object.create(null);
    fields(form).forEach(function (el) {
      var i = indexes[el.name] || 0;
      indexes[el.name] = i + 1;
      if (!data || !Array.isArray(data[el.name]) || data[el.name][i] === undefined) return;
      if (/^(checkbox|radio)$/.test(el.type)) el.checked = data[el.name][i] === true;
      else el.value = String(data[el.name][i]);
    });
  }
  function attach(form, hooks) {
    if (!form || !scope()) return null;
    if (form.tauroDraft) return form.tauroDraft;
    hooks = hooks || {};
    var url = new URL(location.href);
    url.searchParams.delete("error"); url.searchParams.delete("borrador_listo"); url.searchParams.sort();
    var key = prefix + scope() + ":" + (form.dataset.draftKey || url.pathname + url.search);
    var receipt = form.querySelector('[name="borrador_token"]');
    if (!receipt) {
      receipt = document.createElement("input"); receipt.type = "hidden";
      receipt.name = "borrador_token"; form.appendChild(receipt);
    }
    // Un error de POST conserva la identidad del borrador de origen.
    if (receipt.value) entries().some(function (k) {
      var d = read(k);
      if (d && d.scope === scope() && d.token === receipt.value) { key = k; return true; }
      return false;
    });
    var saved = read(key);
    if (saved && saved.scope !== scope()) saved = null;
    receipt.value = (saved && saved.token) || receipt.value || crypto.randomUUID();
    var status = document.createElement("small"); status.className = "draft-status";
    status.setAttribute("role", "status"); status.textContent = "Tu avance se guarda en esta pestaña.";
    var bar = document.createElement("div"); bar.className = "draft-bar";
    bar.appendChild(status); form.prepend(bar);
    var dirty = Boolean(saved), stopped = false;
    function completed() {
      try { return Number(sessionStorage.getItem(completedPrefix + scope() + ":" + receipt.value)) > Date.now() - ttl; }
      catch (_) { return false; }
    }
    function showCompleted() {
      if (!completed()) return false;
      stopped = true;
      status.textContent = "Este formulario ya se guardó. Podés empezar uno nuevo.";
      form.querySelectorAll('button[type="submit"], input[type="submit"]').forEach(function (el) { el.disabled = true; });
      return true;
    }
    function save() {
      if (stopped || !dirty || completed()) return;
      try {
        var data = JSON.stringify({at:Date.now(), scope:scope(), token:receipt.value,
          fields:values(form), extra:hooks.capture ? hooks.capture() : {},
          scroll:window.scrollY, url:url.pathname + url.search,
          attachments:Array.from(form.querySelectorAll('input[type="file"]')).filter(function (el) {
            return (el.files && el.files.length) || el.dataset.draftAttachment;
          }).map(function (el) { return el.name; }),
          details:Array.from(form.querySelectorAll("details")).map(function (d) { return d.open; }),
          focus:fields(form).indexOf(document.activeElement)});
        if (data.length > 150000) throw new Error("Borrador demasiado grande");
        sessionStorage.setItem(key, data);
        status.textContent = "Avance guardado en esta pestaña";
      } catch (_) { status.textContent = "No se pudo guardar el avance. Mantené esta página abierta."; }
    }
    if (saved && form.dataset.draftServer !== "1") {
      try {
        if (hooks.prepare) hooks.prepare(saved.extra || {});
        fill(form, saved.fields);
        form.querySelectorAll("details").forEach(function (d, i) { d.open = Boolean((saved.details || [])[i]); });
        if (hooks.restore) hooks.restore(saved.extra || {});
        status.textContent = "Retomaste tu avance guardado";
        form.querySelectorAll('input[type="file"]').forEach(function (el) {
          if ((saved.attachments || []).indexOf(el.name) < 0) return;
          el.dataset.draftAttachment = "1";
          var note = document.createElement("small"); note.textContent = "Volvé a adjuntar el archivo.";
          el.after(note);
          el.addEventListener("change", function () { note.hidden = Boolean(el.files && el.files.length); });
        });
      } catch (_) { remove(key); status.textContent = "No pudimos recuperar el borrador completo. Revisá los datos."; }
    }
    var reset = document.createElement("button"); reset.type = "button";
    reset.className = "btn btn-ghost btn-sm draft-reset"; reset.textContent = "Empezar de nuevo";
    status.after(reset);
    reset.addEventListener("click", function () {
      if (!window.confirm("¿Descartar lo cargado en este formulario y empezar de nuevo?")) return;
      stopped = true; dirty = false; remove(key);
      var target = new URL(saved && saved.url || url.href, location.origin);
      if (form.dataset.draftKey && form.dataset.draftKey.indexOf("cotizar:") === 0) {
        target = new URL("/portal/cotizar", location.origin);
        target.searchParams.set("ambito", form.dataset.draftKey.split(":")[1]);
      }
      if (target.pathname === "/portal/envios/nuevo" && !target.searchParams.has("ambito")) target.searchParams.set("ambito", "internacional");
      location.assign(target);
    });
    if (saved && form.dataset.draftServer !== "1" && !form.closest("dialog")) {
      requestAnimationFrame(function () { requestAnimationFrame(function () {
        var focus = fields(form)[saved.focus];
        if (focus && focus.getClientRects().length) focus.focus({preventScroll:true});
        window.scrollTo(0, Number(saved.scroll) || 0);
      }); });
    }
    var api = {save:function () { dirty = true; save(); },
      stop:function () { stopped = true; }, restored: saved};
    form.tauroDraft = api; drafts.push(api);
    ["input", "change", "click"].forEach(function (name) {
      form.addEventListener(name, function () { dirty = true; queueMicrotask(save); });
    });
    form.addEventListener("submit", function (event) {
      if (showCompleted()) { event.preventDefault(); return; }
      api.save();
    });
    window.addEventListener("pageshow", showCompleted);
    window.addEventListener("pagehide", save);
    var scrollTimer;
    window.addEventListener("scroll", function () {
      clearTimeout(scrollTimer); scrollTimer = setTimeout(save, 200);
    }, {passive:true});
    return api;
  }
  window.TauroDraft = {attach:attach, values:values, fill:fill};
  document.addEventListener("DOMContentLoaded", function () {
    var url = new URL(location.href), done = url.searchParams.get("borrador_listo");
    entries().forEach(function (key) {
      var d = read(key);
      if (d && d.scope === scope() && d.token === done) {
        try { sessionStorage.setItem(completedPrefix + scope() + ":" + done, String(Date.now())); } catch (_) {}
      }
      var kind = scope().split(":")[0];
      var logoutKind = location.pathname.indexOf("/admin/") === 0 ? "admin" : "portal";
      if (d && ((!scope() && d.scope.split(":")[0] === logoutKind)
        || (scope() && d.scope.split(":")[0] === kind && d.scope !== scope())
        || (d.scope === scope() && d.token === done))) remove(key);
    });
    if (done) {
      url.searchParams.delete("borrador_listo"); history.replaceState(history.state, "", url);
    }
    document.querySelectorAll("form[data-draft-auto]").forEach(function (form) { attach(form); });
    document.addEventListener("click", function (event) {
      var link = event.target.closest('a[href*="/logout"]');
      if (link) drafts.forEach(function (draft) { draft.stop(); });
      if (link) entries().forEach(function (key) {
        var d = read(key); if (d && d.scope === scope()) remove(key);
      });
    });
  });
})();
