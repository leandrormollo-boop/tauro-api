/* Una pantalla, dos borradores. Las respuestas pertenecen a la revisión exacta
   de los datos: nunca se puede elegir una tarifa de una edición anterior. */
(function (factory) {
  var request = factory();
  if (typeof module === 'object' && module.exports) module.exports = request;
  else {
    window.TauroQuoteRequest = request;
    window.TauroQuoteStream = request.readNdjson;
  }
})(function () {
  function interrupted() {
    return new Error('La respuesta se interrumpió. Volvé a consultar las tarifas.');
  }
  async function readNdjson(response, onMessage) {
    var buffer = '', complete = false;
    function consume(final) {
      var newline;
      while ((newline = buffer.indexOf('\n')) !== -1) {
        var line = buffer.slice(0, newline).trim(); buffer = buffer.slice(newline + 1);
        if (line) parse(line);
        if (complete) { buffer = ''; return; }
      }
      if (final && buffer.trim()) { parse(buffer.trim()); buffer = ''; }
    }
    function parse(line) {
      var event;
      try { event = JSON.parse(line); } catch (_) { throw interrupted(); }
      if (!event || typeof event.html !== 'string' || typeof event.complete !== 'boolean') throw interrupted();
      onMessage(event);
      if (event.complete) complete = true;
    }
    if (!response.body || typeof response.body.getReader !== 'function') {
      buffer = await response.text(); consume(true);
    } else {
      var reader = response.body.getReader(), decoder = new TextDecoder();
      var failed = false;
      try {
        while (true) {
          var chunk = await reader.read();
          if (chunk.done) break;
          if (complete) continue;
          buffer += decoder.decode(chunk.value, {stream: true}); consume(false);
        }
        if (!complete) { buffer += decoder.decode(); consume(true); }
        if (!complete) throw interrupted();
      } catch (error) {
        if (!complete) { failed = true; throw error; }
      } finally {
        if (failed) { try { await reader.cancel(); } catch (_) {} }
        reader.releaseLock();
      }
    }
    if (!complete) throw interrupted();
  }
  function copyResellerInputs(current, next) {
    var drafts = new Map(), focusKey = null, selection = null;
    current.querySelectorAll('.uq-price[data-carrier] .uq-reseller').forEach(function (form) {
      var card = form.closest('.uq-price[data-carrier]');
      var quote = form.querySelector('[name="quote_id"]'), price = form.querySelector('[name="precio"]');
      if (!card || !quote || !price || !quote.value) return;
      var key = card.dataset.carrier + '\u0000' + quote.value;
      drafts.set(key, price.value);
      if (price.ownerDocument.activeElement === price) {
        focusKey = key; selection = [price.selectionStart, price.selectionEnd];
      }
    });
    var focusTarget = null;
    next.querySelectorAll('.uq-price[data-carrier] .uq-reseller').forEach(function (form) {
      var card = form.closest('.uq-price[data-carrier]');
      var quote = form.querySelector('[name="quote_id"]'), price = form.querySelector('[name="precio"]');
      if (!card || !quote || !price) return;
      var key = card.dataset.carrier + '\u0000' + quote.value;
      if (!drafts.has(key)) return;
      price.value = drafts.get(key);
      if (key === focusKey) focusTarget = price;
    });
    return function () {
      if (!focusTarget || !focusTarget.isConnected) return;
      focusTarget.focus({preventScroll: true});
      if (selection && selection[0] !== null && typeof focusTarget.setSelectionRange === 'function') {
        focusTarget.setSelectionRange(selection[0], selection[1]);
      }
    };
  }
  function armDeadline(signal, callback, delay) {
    var timer = setTimeout(function () { if (!signal.aborted) callback(); }, delay);
    return function () { clearTimeout(timer); };
  }
  function focusShift(rect, safeTop, safeBottom, padding) {
    var gap = padding === undefined ? 12 : padding;
    if (rect.bottom + gap > safeBottom) return rect.bottom + gap - safeBottom;
    if (rect.top - gap < safeTop) return rect.top - gap - safeTop;
    return 0;
  }
  function resultState(block, complete) {
    var hasPrice = Boolean(block.querySelector('.uq-price'));
    var hasError = Boolean(block.querySelector('.uq-error'));
    var hasUnavailable = Boolean(block.querySelector('.uq-unavailable'));
    return {
      hasPrice: hasPrice,
      failed: hasError || (!hasPrice && hasUnavailable),
      current: hasPrice && !hasError,
      button: hasPrice && !hasError ? 'Ver tarifas' : 'Volver a consultar',
      status: !complete ? 'Consultando otros operadores…'
        : hasError && hasPrice ? 'Podés usar las tarifas recibidas o volver a consultar.'
        : !hasPrice && hasUnavailable ? 'No recibimos una tarifa. Podés volver a consultar.'
        : hasPrice ? 'Tarifas actualizadas.' : 'Revisá el resultado de la consulta.',
    };
  }
  function quoteRequest(io) {
    var revision = 0, timer, controller, paused = false;
    var activeFingerprint = null, currentFingerprint = null;
    function fingerprint() { return io.fingerprint ? io.fingerprint() : null; }
    function cancel() {
      revision += 1;
      clearTimeout(timer); timer = null;
      if (controller) controller.abort();
      controller = null; activeFingerprint = null;
    }
    async function run(force) {
      clearTimeout(timer); timer = null;
      if (paused || !io.ready()) return;
      var nextFingerprint = fingerprint();
      if (nextFingerprint !== null && activeFingerprint === nextFingerprint) return;
      if (!force && nextFingerprint !== null && currentFingerprint === nextFingerprint) return;
      cancel(); currentFingerprint = null;
      var current = revision, completed = false;
      controller = new AbortController(); activeFingerprint = nextFingerprint;
      var signal = controller.signal;
      io.loading();
      function progress(response, complete) {
        if (current !== revision || paused) return false;
        io.render(response, Boolean(complete));
        if (complete) { completed = true; currentFingerprint = nextFingerprint; }
        return true;
      }
      try {
        var response = await io.fetch(signal, progress);
        if (current !== revision || paused) return;
        if (response !== undefined) progress(response, true);
        else if (!completed) throw interrupted();
      } catch (error) {
        if (current === revision && !paused && error.name !== 'AbortError') io.error(error);
      } finally {
        if (current === revision) { controller = null; activeFingerprint = null; }
      }
    }
    function changed() {
      var complete = io.ready();
      var nextFingerprint = complete ? fingerprint() : null;
      if (complete && nextFingerprint !== null
          && (activeFingerprint === nextFingerprint || currentFingerprint === nextFingerprint)) return;
      cancel(); currentFingerprint = null;
      io.invalidate(complete);
      if (!paused && complete) timer = setTimeout(function () { run(false); }, io.delay === undefined ? 350 : io.delay);
    }
    return {changed: changed, run: run, pause: function () {
      paused = true; cancel(); currentFingerprint = null; io.invalidate(false);
    },
      resume: function () { paused = false; changed(); }};
  }
  quoteRequest.readNdjson = readNdjson;
  quoteRequest.copyResellerInputs = copyResellerInputs;
  quoteRequest.armDeadline = armDeadline;
  quoteRequest.focusShift = focusShift;
  quoteRequest.resultState = resultState;
  return quoteRequest;
});

(function () {
  'use strict';
  if (typeof document === 'undefined') return;
  var dialog = document.getElementById('quote-window-dialog');
  var content = dialog && dialog.querySelector('[data-cotizar-contenido]');
  var opener, loadingController, initialized = new WeakMap();

  function keepDialogFocusVisible() {
    if (!dialog || !dialog.open || !content || !content.contains(document.activeElement)) return;
    window.requestAnimationFrame(function () {
      var focused = document.activeElement;
      if (!dialog.open || !focused || !content.contains(focused)) return;
      var viewport = window.visualViewport;
      var viewportTop = viewport ? viewport.offsetTop : 0;
      var viewportBottom = viewportTop + (viewport ? viewport.height : window.innerHeight);
      var contentRect = content.getBoundingClientRect();
      var header = dialog.querySelector('.quote-window-head');
      var headerBottom = header ? header.getBoundingClientRect().bottom : contentRect.top;
      var safeTop = Math.max(viewportTop, headerBottom, contentRect.top);
      var safeBottom = Math.min(viewportBottom, contentRect.bottom);
      var shift = window.TauroQuoteRequest.focusShift(
        focused.getBoundingClientRect(), safeTop, safeBottom, 12
      );
      if (shift && typeof content.scrollBy === 'function') content.scrollBy({top: shift, behavior: 'auto'});
    });
  }

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
  function quoteProgress(container) {
    var box = document.createElement('div');
    box.className = 'uq-quote-progress'; box.setAttribute('role', 'status');
    var copy = document.createElement('div');
    var title = document.createElement('strong'); title.textContent = 'Consultando esta ruta';
    var detail = document.createElement('span'); detail.textContent = 'Origen, destino y peso listos. Esperando la tarifa.';
    var track = document.createElement('i'); track.setAttribute('aria-hidden', 'true');
    copy.append(title, detail); box.append(copy, track); container.replaceChildren(box);
  }
  function staleQuote(container, text) {
    if (!container.querySelector('.uq-price')) return false;
    var response = container.querySelector('[data-quote-response]') || container;
    response.querySelectorAll('[data-quote-recovery]').forEach(function (node) { node.remove(); });
    var notice = response.querySelector('[data-quote-stale]');
    if (!notice) {
      notice = document.createElement('p'); notice.className = 'uq-stale';
      notice.dataset.quoteStale = ''; notice.setAttribute('role', 'status');
      response.prepend(notice);
    }
    notice.textContent = text;
    response.querySelectorAll('.uq-choose').forEach(function (link) {
      link.removeAttribute('href'); link.setAttribute('aria-disabled', 'true');
      link.dataset.quoteStaleAction = '';
    });
    response.querySelectorAll('form').forEach(function (form) {
      form.dataset.quoteStaleAction = '';
      form.querySelectorAll('input,select,textarea,button').forEach(function (control) { control.disabled = true; });
    });
    return true;
  }
  function quoteRecovery(container) {
    var response = container.querySelector('[data-quote-response]') || container;
    var recovery = response.querySelector('[data-quote-recovery]');
    if (recovery) return recovery;
    recovery = document.createElement('div'); recovery.className = 'uq-recovery';
    recovery.dataset.quoteRecovery = '';
    var button = document.createElement('button'); button.type = 'button';
    button.className = 'btn btn-ghost btn-sm'; button.dataset.quoteRetry = '';
    button.textContent = 'Volver a consultar'; recovery.appendChild(button);
    response.appendChild(recovery); return recovery;
  }
  function quoteError(container, text) {
    if (!container.querySelector('.uq-price')) { message(container, text, true); return false; }
    var response = container.querySelector('[data-quote-response]') || container;
    var previous = response.querySelector('[data-quote-stream-error]');
    if (previous) previous.remove();
    var warning = document.createElement('p'); warning.className = 'uq-error';
    warning.dataset.quoteStreamError = ''; warning.setAttribute('role', 'alert');
    warning.textContent = text; response.appendChild(warning); return true;
  }
  function formFingerprint(form) {
    var fields = [];
    new FormData(form).forEach(function (value, name) {
      fields.push([name, typeof value === 'string' ? value : [value.name, value.size, value.type]]);
    });
    return JSON.stringify(fields);
  }
  function responseBlock(html, scope) {
    var parsed = new DOMParser().parseFromString(html, 'text/html');
    var block = parsed.querySelector('[data-quote-response][data-scope="' + scope + '"]');
    if (!block) throw new Error('No pudimos recuperar las tarifas. Volvé a ingresar al portal.');
    return document.importNode(block, true);
  }
  function init(root) {
    if (initialized.has(root)) return initialized.get(root);
    var controls = {}, locationControls = {}, active = root.dataset.quoteActive || 'internacional';
    root.querySelectorAll('[data-unified-form]').forEach(function (form) {
      var scope = form.dataset.unifiedForm, panel = form.closest('[data-quote-panel]');
      var result = panel.querySelector('[data-quote-results]');
      var resultPanel = result.closest('.uq-results');
      var status = form.querySelector('[data-quote-status]');
      var resultStatus = panel.querySelector('[data-quote-results-status]');
      var submit = form.querySelector('[data-quote-submit]'), initial = true, hasCurrentQuote = false;
      var packageList = form.querySelector('#quote-package-list');
      var template = form.querySelector('#quote-package-template');
      var activeBox = 0, mobileStep = 1;
      var mobile = window.matchMedia('(max-width: 779px)');
      var tabs = form.querySelector('[data-package-tabs]');
      if (tabs) form.querySelector('.uq-section-title').insertBefore(tabs, form.querySelector('#quote-add-package'));
      var stepNav = panel.querySelector('.uq-step-nav'), actions = panel.querySelector('.uq-mobile-actions');
      result.setAttribute('aria-busy', 'false');
      resultPanel.dataset.quoteState = result.querySelector('.uq-price') ? 'ready'
        : result.querySelector('.uq-error,.uq-unavailable') ? 'error' : 'idle';
      function announce(text) { status.textContent = text; resultStatus.textContent = text; }
      function showStep(step, focus) {
        mobileStep = Math.max(1, Math.min(3, step)); panel.dataset.mobileStep = mobileStep;
        stepNav.hidden = !mobile.matches; actions.hidden = !mobile.matches;
        stepNav.querySelectorAll('[data-quote-step]').forEach(function (button) {
          var selected = Number(button.dataset.quoteStep) === mobileStep;
          button.setAttribute('aria-current', selected ? 'step' : 'false');
        });
        actions.querySelector('[data-quote-back]').disabled = mobileStep === 1;
        actions.querySelector('[data-quote-next]').hidden = mobileStep === 3;
        actions.querySelector('[data-quote-step-status]').textContent = mobileStep + ' de 3';
        status.setAttribute('aria-live', !mobile.matches || mobileStep !== 3 ? 'polite' : 'off');
        resultStatus.setAttribute('aria-live', mobile.matches && mobileStep === 3 ? 'polite' : 'off');
        if (focus && mobile.matches) {
          var heading = mobileStep === 3 ? panel.querySelector('.uq-results h2') : mobileStep === 2 ? form.querySelector('.uq-section-title h2') : stepNav;
          heading.tabIndex = -1; heading.focus({preventScroll:true});
          var viewport = root.closest('[data-cotizar-contenido]'); if (viewport) viewport.scrollTop = 0;
        }
      }
      function validate(section) {
        var invalid = Array.from(section.querySelectorAll('[required]')).find(function (input) {
          if (input.dataset.numero) {
            var value = numeric(input.value, input.dataset.numero);
            input.setCustomValidity(!Number.isFinite(value) || value <= 0 ? 'Ingresá un valor mayor que cero.'
              : input.dataset.numero === 'entero' && !Number.isInteger(value) ? 'Ingresá una cantidad entera.' : '');
          }
          return !input.validity.valid;
        });
        if (!invalid) return true;
        if (invalid.closest('[data-package-row]')) {
          activeBox = Array.from(packageList.children).indexOf(invalid.closest('[data-package-row]')); renumber();
        }
        invalid.reportValidity(); return false;
      }
      function advance(step) {
        if (step > mobileStep && mobileStep === 1 && !validate(form.querySelector('.uq-route-grid'))) return;
        if (step === 3 && !validate(form.querySelector('.uq-packages'))) {showStep(2, false); return;}
        showStep(step, true); if (form.tauroDraft) form.tauroDraft.save();
      }
      panel.querySelectorAll('[data-quote-step]').forEach(function (button) {button.addEventListener('click', function () {advance(Number(button.dataset.quoteStep));});});
      actions.querySelector('[data-quote-back]').addEventListener('click', function () {advance(mobileStep - 1);});
      actions.querySelector('[data-quote-next]').addEventListener('click', function () {advance(mobileStep + 1);});
      mobile.addEventListener('change', function () {showStep(mobileStep, false);});
      function renumber() {
        if (!packageList) return;
        var rows = packageList.querySelectorAll('[data-package-row]');
        activeBox = Math.max(0, Math.min(activeBox, rows.length - 1));
        tabs.replaceChildren(); tabs.hidden = rows.length < 2;
        rows.forEach(function (row, i) {
          row.hidden = i !== activeBox;
          var tab = document.createElement('button'); tab.type = 'button'; tab.textContent = 'Caja ' + (i + 1);
          tab.setAttribute('aria-pressed', String(i === activeBox));
          tab.addEventListener('click', function () {activeBox = i; renumber(); if (form.tauroDraft) form.tauroDraft.save();}); tabs.appendChild(tab);
          if (i === activeBox && rows.length > 1) {
            var close = document.createElement('button'); close.type='button'; close.textContent='×';
            close.dataset.removePackageIndex=String(i); close.className='uq-box-remove';
            close.setAttribute('aria-label','Quitar caja ' + (i + 1)); tabs.appendChild(close);
          }
          row.querySelector('[data-package-number]').textContent = i + 1;
          var remove = row.querySelector('[data-remove-package]');
          remove.hidden = rows.length === 1;
          row.querySelector('.uq-box-title').hidden = true;
          remove.setAttribute('aria-label', 'Quitar caja ' + (i + 1));
        });
        form.querySelector('#quote-add-package').disabled = rows.length >= 20;
      }
      if (window.TauroDraft) window.TauroDraft.attach(form, {
        capture: function () { return {packages: packageList ? packageList.children.length : 1, activeBox:activeBox, mobileStep:mobileStep}; },
        prepare: function (saved) {
          activeBox = Number(saved.activeBox) || 0; mobileStep = Number(saved.mobileStep) || 1;
          if (!packageList) return;
          var count = Math.max(1, Math.min(20, Number(saved.packages) || 1));
          while (packageList.children.length < count) packageList.appendChild(template.content.cloneNode(true));
          Array.from(packageList.children).slice(count).forEach(function (row) { row.remove(); });
        }
      });
      var draftBar = form.querySelector('.draft-bar');
      if (draftBar) form.querySelector('.uq-route-tools').prepend(draftBar);
      renumber(); showStep(mobileStep, false);
      var locations = window.TauroQuoteLocations && window.TauroQuoteLocations.attach(form);
      locationControls[scope] = locations;
      if (locations && scope === active) locations.resume();
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
          summary.querySelector('[data-weight-' + item[0] + ']').textContent = item[1].toLocaleString('es-AR',{minimumFractionDigits:2,maximumFractionDigits:2}) + ' kg';
        });
      }
      function ready() {
        weights();
        if (locations && locations.pending()) return false;
        return Array.from(form.querySelectorAll('[required]')).every(function (input) {
          if (!input.value.trim() || !input.validity.valid) return false;
          if (!input.dataset.numero) return true;
          var number = numeric(input.value, input.dataset.numero);
          return Number.isFinite(number) && number > 0 && (input.dataset.numero !== 'entero' || Number.isInteger(number));
        });
      }
      var control = window.TauroQuoteRequest({
        ready: ready,
        fingerprint: function () { return formFingerprint(form); },
        invalidate: function (complete) {
          hasCurrentQuote = false; submit.textContent = 'Consultar tarifas';
          if (initial && !complete) { initial = false; return; }
          initial = false;
          resultPanel.dataset.quoteState = complete ? 'editing' : 'idle';
          result.classList.remove('is-revealing');
          result.querySelectorAll('[data-quote-stream-error]').forEach(function (node) { node.remove(); });
          result.setAttribute('aria-busy', 'false');
          announce(complete ? 'Actualizando con tus datos…' : 'Completá los datos para ver las tarifas.');
          if (!staleQuote(result, complete
            ? 'Datos modificados. La tarifa visible puede estar desactualizada mientras consultamos nuevamente.'
            : 'Datos incompletos. La tarifa visible puede estar desactualizada; completá los datos para actualizarla.')) {
            message(result, complete ? 'Las tarifas se actualizan automáticamente.' : 'Tus opciones aparecerán al completar los datos.');
          }
        },
        loading: function () {
          resultPanel.dataset.quoteState = 'loading';
          result.classList.remove('is-revealing');
          result.querySelectorAll('[data-quote-stream-error]').forEach(function (node) { node.remove(); });
          result.setAttribute('aria-busy', 'true');
          announce('Consultando la tarifa para esta ruta…');
          if (!staleQuote(result, 'Actualizando la tarifa. La opción visible corresponde a la última consulta.')) quoteProgress(result);
        },
        fetch: async function (signal, onProgress) {
          var clearDeadline = window.TauroQuoteRequest.armDeadline(signal, function () {
            control.pause(); showError('La consulta demoró demasiado. Volvé a consultar.');
          }, 90000);
          try {
            var headers = {'X-Requested-With': 'TauroQuoteWindow'};
            if (scope === 'internacional') headers.Accept = 'application/x-ndjson';
            var response = await fetch(form.action, {method: 'POST', body: new FormData(form), credentials: 'same-origin', signal: signal, headers: headers});
            if (response.redirected && new URL(response.url).pathname.includes('/login')) throw new Error('Tu sesión venció. Volvé a ingresar al portal.');
            if (response.status === 429) throw new Error('Realizaste varias consultas seguidas. Esperá un minuto y volvé a consultar.');
            if (!response.ok) throw new Error('No pudimos consultar las tarifas. Revisá los datos e intentá nuevamente.');
            var contentType = response.headers.get('content-type') || '';
            if (scope === 'internacional' && contentType.includes('application/x-ndjson')) {
              await window.TauroQuoteStream(response, function (event) {
                if (event.complete) clearDeadline();
                onProgress(responseBlock(event.html, scope), event.complete);
              });
              return;
            }
            return responseBlock(await response.text(), scope);
          } finally { clearDeadline(); }
        },
        render: function (block, complete) {
          var restoreResellerFocus = window.TauroQuoteRequest.copyResellerInputs(result, block);
          var state = window.TauroQuoteRequest.resultState(block, complete);
          result.classList.remove('is-revealing');
          result.replaceChildren(block); result.setAttribute('aria-busy', String(!complete));
          restoreResellerFocus();
          resultPanel.dataset.quoteState = !complete ? 'loading'
            : state.failed ? 'error' : state.hasPrice ? 'ready' : 'empty';
          if (complete && state.failed) quoteRecovery(result);
          window.requestAnimationFrame(function () { result.classList.add('is-revealing'); });
          hasCurrentQuote = state.current; submit.textContent = state.button;
          announce(state.status);
        },
        error: function (error) { showError(error.message); }
      });
      function showError(text) {
        resultPanel.dataset.quoteState = 'error'; result.setAttribute('aria-busy', 'false');
        var keptPrice = quoteError(result, text); hasCurrentQuote = false;
        if (keptPrice) staleQuote(result, 'No pudimos actualizar la tarifa. La opción visible corresponde a la última consulta.');
        quoteRecovery(result); submit.textContent = 'Volver a consultar';
        announce(keptPrice
          ? 'No pudimos actualizar. Conservamos la última tarifa para que puedas volver a consultar.'
          : 'No pudimos consultar. Podés volver a intentar sin cargar los datos otra vez.');
      }
      controls[scope] = control;
      result.addEventListener('click', function (event) {
        if (event.target.closest('[data-quote-stale-action]')) { event.preventDefault(); return; }
        if (!event.target.closest('[data-quote-retry]')) return;
        control.resume(); control.run(true);
      });
      result.addEventListener('submit', function (event) {
        if (event.target.matches('[data-quote-stale-action]')) event.preventDefault();
      });
      result.addEventListener('contextmenu', function (event) {
        if (event.target.closest('[data-quote-stale-action]')) event.preventDefault();
      });
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
        if (hasCurrentQuote) { showStep(3, true); return; }
        if (!validate(form.querySelector('.uq-route-grid')) || !validate(form.querySelector('.uq-packages'))) return;
        if (locations) locations.acceptManual();
        control.resume(); control.run(true);
      });
      form.addEventListener('keydown', function (event) {
        if (mobile.matches && event.key === 'Enter' && event.target.tagName === 'INPUT'
            && event.target.getAttribute('aria-expanded') !== 'true') {
          event.preventDefault(); advance(mobileStep + 1);
        }
      });
      form.addEventListener('click', function (event) {
        if (event.target.closest('[data-quote-reverse]')) {
          if (locations) locations.cancel(); control.pause();
          window.TauroReverseQuoteRoute(form);
          if (locations) {locations.refresh(); locations.resume();}
          if (form.tauroDraft) form.tauroDraft.save();
          control.resume(); return;
        }
        var remove = event.target.closest('[data-remove-package]');
        var removeIndex = event.target.closest('[data-remove-package-index]');
        if (event.target.closest('#quote-add-package') && packageList.children.length < 20) {packageList.appendChild(template.content.cloneNode(true)); activeBox = packageList.children.length - 1;}
        else if (removeIndex && packageList.children.length > 1) packageList.children[Number(removeIndex.dataset.removePackageIndex)].remove();
        else if (remove && packageList.children.length > 1) remove.closest('[data-package-row]').remove();
        else return;
        renumber(); if (form.tauroDraft) form.tauroDraft.save(); control.changed();
      });
      form.addEventListener('invalid', function (event) {
        var row = event.target.closest('[data-package-row]');
        if (row) {activeBox = Array.from(packageList.children).indexOf(row); renumber();}
        showStep(event.target.closest('.uq-packages') ? 2 : 1, false);
      }, true);
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
      Object.keys(controls).forEach(function (scope) {
        if (locationControls[scope]) locationControls[scope].cancel();
        if (scope === active) {
          if (locationControls[scope]) locationControls[scope].resume();
          controls[scope].resume();
        } else controls[scope].pause();
      });
      if (window.TauroQuoteGlobe) window.TauroQuoteGlobe.sync(root);
      if (changeUrl && !root.closest('dialog')) history.replaceState(history.state, '', '/portal/cotizar?ambito=' + active);
    }
    root.addEventListener('click', function (event) {
      var link = event.target.closest('[data-quote-scope]');
      if (link && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.button) { event.preventDefault(); if (active !== link.dataset.quoteScope) select(link.dataset.quoteScope, true); }
    });
    var api = {select: select, pause: function () {
      Object.values(controls).forEach(function (control) { control.pause(); });
      Object.values(locationControls).forEach(function (control) { if (control) control.cancel(); });
    }};
    initialized.set(root, api);
    if (window.TauroQuoteGlobe) window.TauroQuoteGlobe.attach(root);
    // Conservar el resultado de un POST sin JS hasta la primera edición.
    Object.keys(controls).forEach(function (scope) {
      if (scope !== active) controls[scope].pause();
      else if (!root.querySelector('[data-quote-panel="' + scope + '"] .uq-price')) controls[scope].changed();
    });
    return api;
  }
  document.querySelectorAll('.unified-quote').forEach(init);
  if (!dialog) return;
  content.addEventListener('focusin', keepDialogFocusVisible);
  window.addEventListener('resize', keepDialogFocusVisible, {passive: true});
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', keepDialogFocusVisible, {passive: true});
    window.visualViewport.addEventListener('scroll', keepDialogFocusVisible, {passive: true});
  }
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
