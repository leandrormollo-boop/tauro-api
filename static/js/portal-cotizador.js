/* Una pantalla, dos borradores. Las respuestas pertenecen a la revisión exacta
   de los datos: nunca se puede elegir una tarifa de una edición anterior. */
(function (factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory;
  else window.TauroQuoteRequest = factory;
})(function quoteRequest(io) {
  var revision = 0, timer, controller, paused = false;
  function cancel() {
    revision += 1;
    clearTimeout(timer);
    if (controller) controller.abort();
    controller = null;
  }
  async function run() {
    cancel();
    if (paused || !io.ready()) return;
    var current = revision;
    controller = new AbortController();
    io.loading();
    try {
      var response = await io.fetch(controller.signal);
      if (current === revision && !paused) io.render(response);
    } catch (error) {
      if (current === revision && !paused && error.name !== 'AbortError') io.error(error);
    }
  }
  function changed() {
    cancel();
    io.invalidate(io.ready());
    if (!paused && io.ready()) timer = setTimeout(run, io.delay === undefined ? 900 : io.delay);
  }
  return {changed: changed, run: run, pause: function () { paused = true; cancel(); },
    resume: function () { paused = false; changed(); }};
});

(function () {
  'use strict';
  if (typeof document === 'undefined') return;
  var dialog = document.getElementById('quote-window-dialog');
  var content = dialog && dialog.querySelector('[data-cotizar-contenido]');
  var opener, loadingController, initialized = new WeakMap();

  function numeric(value, kind) {
    if (window.TauroNumeros) {
      var canonical = window.TauroNumeros.canonico(value, kind || "decimal");
      return canonical.error ? NaN : Number(canonical.valor);
    }
    var text = String(value || '').trim();
    if (text.includes(',')) text = text.replace(/\./g, '').replace(',', '.');
    return Number(text);
  }
  function message(container, text, error) {
    var el = document.createElement('p');
    el.className = error ? 'uq-error' : 'uq-idle';
    el.textContent = text;
    if (error) el.setAttribute('role', 'alert');
    container.replaceChildren(el);
  }
  function init(root) {
    if (initialized.has(root)) return initialized.get(root);
    var controls = {}, active = root.dataset.quoteActive || 'internacional';
    root.querySelectorAll('[data-unified-form]').forEach(function (form) {
      var scope = form.dataset.unifiedForm, panel = form.closest('[data-quote-panel]');
      var result = panel.querySelector('[data-quote-results]');
      var status = form.querySelector('[data-quote-status]');
      var submit = form.querySelector('[data-quote-submit]'), initial = true, hasCurrentQuote = false;
      var packageList = form.querySelector('#quote-package-list');
      var template = form.querySelector('#quote-package-template');
      function renumber() {
        if (!packageList) return;
        var rows = packageList.querySelectorAll('[data-package-row]');
        rows.forEach(function (row, i) {
          row.querySelector('[data-package-number]').textContent = i + 1;
          var remove = row.querySelector('[data-remove-package]');
          remove.hidden = rows.length === 1;
          remove.setAttribute('aria-label', 'Quitar caja ' + (i + 1));
        });
        form.querySelector('#quote-add-package').disabled = rows.length >= 20;
      }
      if (window.TauroDraft) window.TauroDraft.attach(form, packageList ? {
        capture: function () { return {packages: packageList.children.length}; },
        prepare: function (saved) {
          var count = Math.max(1, Math.min(20, Number(saved.packages) || 1));
          while (packageList.children.length < count) packageList.appendChild(template.content.cloneNode(true));
          Array.from(packageList.children).slice(count).forEach(function (row) { row.remove(); });
        }
      } : {});
      renumber();
      function weights() {
        var summary = form.querySelector('[data-quote-weights]');
        if (!summary || !packageList) return;
        var real = 0, volume = 0, billable = 0, complete = true;
        packageList.querySelectorAll('[data-package-row]').forEach(function (row) {
          function val(name) { var el = row.querySelector('[name="' + name + '"]'); return numeric(el.value, el.dataset.numero); }
          var quantity = val('bulto_cantidad'), weight = val('bulto_peso');
          var vol = val('bulto_largo') * val('bulto_ancho') * val('bulto_alto') / 5000;
          if (![quantity, weight, vol].every(function (v) { return Number.isFinite(v) && v > 0; })) complete = false;
          real += quantity * weight; volume += quantity * vol; billable += quantity * Math.max(weight, vol);
        });
        summary.hidden = !complete;
        if (complete) [["real",real],["volume",volume],["billable",billable]].forEach(function (item) {
          summary.querySelector('[data-weight-' + item[0] + ']').textContent = item[1].toLocaleString('es-AR',{maximumFractionDigits:2}) + ' kg';
        });
      }
      function ready() {
        weights();
        return Array.from(form.querySelectorAll('[required]')).every(function (input) {
          if (!input.value.trim() || !input.checkValidity()) return false;
          if (!input.dataset.numero) return true;
          var number = numeric(input.value, input.dataset.numero);
          return Number.isFinite(number) && number > 0 && (input.dataset.numero !== 'entero' || Number.isInteger(number));
        });
      }
      var control = window.TauroQuoteRequest({
        ready: ready,
        invalidate: function (complete) {
          hasCurrentQuote = false; submit.textContent = 'Consultar tarifas';
          if (initial && !complete) { initial = false; return; }
          initial = false;
          result.setAttribute('aria-busy', 'false');
          status.textContent = complete ? 'Actualizando con tus datos…' : 'Completá los datos para ver las tarifas.';
          message(result, complete ? 'Las tarifas se actualizan automáticamente.' : 'Tus opciones aparecerán al completar los datos.');
        },
        loading: function () {
          result.setAttribute('aria-busy', 'true');
          status.textContent = 'Consultando tus operadores…';
          message(result, 'Consultando tarifas disponibles…');
        },
        fetch: async function (signal) {
          var timeout = setTimeout(function () { control.pause(); message(result, 'La consulta demoró demasiado. Volvé a consultar.', true); result.setAttribute('aria-busy','false'); status.textContent='Volvé a consultar las tarifas.'; }, 90000);
          try {
            var response = await fetch(form.action, {method: 'POST', body: new FormData(form), credentials: 'same-origin', signal: signal, headers: {'X-Requested-With': 'TauroQuoteWindow'}});
            if (response.redirected && new URL(response.url).pathname.includes('/login')) throw new Error('Tu sesión venció. Volvé a ingresar al portal.');
            if (response.status === 429) throw new Error('Realizaste varias consultas seguidas. Esperá un minuto y volvé a consultar.');
            if (!response.ok) throw new Error('No pudimos consultar las tarifas. Revisá los datos e intentá nuevamente.');
            var parsed = new DOMParser().parseFromString(await response.text(), 'text/html');
            var block = parsed.querySelector('[data-quote-response][data-scope="' + scope + '"]');
            if (!block) throw new Error('No pudimos recuperar las tarifas. Volvé a ingresar al portal.');
            return document.importNode(block, true);
          } finally { clearTimeout(timeout); }
        },
        render: function (block) {
          result.replaceChildren(block); result.setAttribute('aria-busy', 'false');
          hasCurrentQuote = Boolean(block.querySelector('.uq-price'));
          submit.textContent = hasCurrentQuote ? 'Ver tarifas ↓' : 'Volver a consultar';
          status.textContent = block.querySelector('.uq-price') ? 'Tarifas actualizadas.' : 'Revisá el resultado de la consulta.';
        },
        error: function (error) { result.setAttribute('aria-busy', 'false'); message(result, error.message, true); status.textContent = 'Podés volver a consultar.'; }
      });
      controls[scope] = control;
      form.addEventListener('input', control.changed);
      form.addEventListener('change', function (event) {
        // Cambiar país invalida ciudad/CP; nunca inventamos un domicilio a
        // partir de la capital del país ni conservamos la ubicación anterior.
        if (scope === 'internacional' && /^(origen|destino)_pais$/.test(event.target.name)) {
          var names = event.target.name === 'origen_pais' ? ['origen_ciudad', 'origen_cp_internacional'] : ['destino_ciudad_internacional', 'destino_cp_internacional'];
          names.forEach(function (name) { form.elements[name].value = ''; });
        }
        if (scope === 'nacional' && /^(origen|destino)_provincia$/.test(event.target.name)) {
          var side = event.target.name.split('_')[0];
          form.elements[side + '_localidad'].value = ''; form.elements[side + '_cp'].value = '';
        }
        control.changed();
      });
      form.addEventListener('submit', function (event) {
        event.preventDefault();
        if (hasCurrentQuote) { result.scrollIntoView({behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'center'}); return; }
        if (!form.reportValidity()) return;
        control.resume(); control.run();
      });
      form.addEventListener('click', function (event) {
        var remove = event.target.closest('[data-remove-package]');
        if (event.target.closest('#quote-add-package') && packageList.children.length < 20) packageList.appendChild(template.content.cloneNode(true));
        else if (remove && packageList.children.length > 1) remove.closest('[data-package-row]').remove();
        else return;
        renumber(); if (form.tauroDraft) form.tauroDraft.save(); control.changed();
      });
    });
    if (window.TauroRutasFrecuentes) window.TauroRutasFrecuentes.attach(root);
    function select(scope, changeUrl) {
      active = scope === 'nacional' ? 'nacional' : 'internacional'; root.dataset.quoteActive = active;
      root.querySelectorAll('[data-quote-panel]').forEach(function (panel) { panel.hidden = panel.dataset.quotePanel !== active; });
      root.querySelectorAll('[data-quote-scope]').forEach(function (link) {
        var selected = link.dataset.quoteScope === active;
        link.classList.toggle('is-active', selected);
        if (selected) link.setAttribute('aria-current', 'true'); else link.removeAttribute('aria-current');
      });
      Object.keys(controls).forEach(function (scope) { if (scope === active) controls[scope].resume(); else controls[scope].pause(); });
      if (changeUrl && !root.closest('dialog')) history.replaceState(history.state, '', '/portal/cotizar?ambito=' + active);
    }
    root.addEventListener('click', function (event) {
      var link = event.target.closest('[data-quote-scope]');
      if (link && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.button) { event.preventDefault(); if (active !== link.dataset.quoteScope) select(link.dataset.quoteScope, true); }
    });
    var api = {select: select, pause: function () { Object.values(controls).forEach(function (control) { control.pause(); }); }};
    initialized.set(root, api);
    // Conservar el resultado de un POST sin JS hasta la primera edición.
    Object.keys(controls).forEach(function (scope) {
      if (scope !== active) controls[scope].pause();
      else if (!root.querySelector('[data-quote-panel="' + scope + '"] .uq-price')) controls[scope].changed();
    });
    return api;
  }
  document.querySelectorAll('.unified-quote').forEach(init);
  if (!dialog) return;
  async function open(trigger) {
    opener = trigger;
    if (!dialog.showModal) { location.assign(trigger.href); return; }
    if (!dialog.open) dialog.showModal();
    var sideToggle = document.getElementById('side-toggle'); if (sideToggle) sideToggle.checked = false;
    var root = content.querySelector('.unified-quote');
    var scope = new URL(trigger.href).searchParams.get('ambito') || (root && root.dataset.quoteActive) || 'internacional';
    if (root) { init(root).select(scope, false); return; }
    if (loadingController) loadingController.abort();
    loadingController = new AbortController();
    message(content, 'Preparando el cotizador…');
    try {
      var response = await fetch('/portal/cotizar?ambito=' + scope, {credentials: 'same-origin', signal: loadingController.signal});
      if (!response.ok) throw new Error('No pudimos abrir el cotizador. Intentá nuevamente.');
      var parsed = new DOMParser().parseFromString(await response.text(), 'text/html');
      var quote = parsed.querySelector('.unified-quote');
      if (!quote) throw new Error('Tu sesión venció. Volvé a ingresar al portal.');
      if (!dialog.open) return;
      root = document.importNode(quote, true); content.replaceChildren(root); init(root);
    } catch (error) {
      if (error.name === 'AbortError') return;
      message(content, error.message, true);
      var fallback = document.createElement('a'); fallback.href='/portal/cotizar?ambito=' + scope; fallback.textContent='Abrir cotizador'; fallback.className='btn btn-primary'; content.appendChild(fallback);
    }
  }
  document.addEventListener('click', function (event) {
    var trigger = event.target.closest('a[data-cotizar-ventana]');
    if (!trigger || event.ctrlKey || event.metaKey || event.shiftKey || event.button) return;
    if (window.location.pathname === '/portal/cotizar') return;
    event.preventDefault(); open(trigger);
  });
  dialog.querySelector('[data-cotizar-cerrar]').addEventListener('click', function () { dialog.close(); });
  dialog.addEventListener('click', function (event) { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener('close', function () {
    if (loadingController) loadingController.abort();
    var root = content.querySelector('.unified-quote'); if (root) init(root).pause();
    if (opener && document.contains(opener)) opener.focus({preventScroll: true});
  });
})();
