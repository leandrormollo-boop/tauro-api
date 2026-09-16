/* HS local: búsqueda automática, selección explícita, borradores por artículo. */
(function () {
  'use strict';
  if (window.tauroHSReady) return;
  window.tauroHSReady = true;
  var states = new WeakMap();
  function fields(scope) {
    return { description: scope.querySelector('[data-hs-description]'),
      code: scope.querySelector('[data-hs-input]'), details: scope.querySelector('[data-hs-details]'),
      status: scope.querySelector('[data-hs-status]'), results: scope.querySelector('[data-hs-results]') };
  }
  function init(scope) {
    if (states.has(scope)) return states.get(scope);
    var state = { sequence: 0, timer: null, controller: null, selected: null };
    states.set(scope, state);
    var f = fields(scope);
    // cloneNode copia el panel visual, pero no sus solicitudes ni elecciones.
    if (f.results) f.results.replaceChildren();
    if (f.status) f.status.textContent = '';
    if (f.details) f.details.value = '';
    return state;
  }
  function node(tag, text) {
    var el = document.createElement(tag); el.textContent = text; return el;
  }
  function invalidate(scope) {
    var s = init(scope), f = fields(scope);
    s.sequence++; clearTimeout(s.timer);
    if (s.controller) s.controller.abort();
    f.results.replaceChildren(); f.status.textContent = '';
    if (s.selected && f.code.value === s.selected) {
      f.code.value = ''; f.code.dispatchEvent(new Event('input', { bubbles: true }));
      f.status.textContent = 'Cambió la descripción. Volvé a elegir el código correspondiente.';
    } else if (f.code.value) {
      f.status.textContent = 'Revisá también el HS actual si cambiaste el producto.';
    }
    s.selected = null;
  }
  async function search(scope) {
    var s = init(scope), f = fields(scope);
    if (!f.description || !f.code || !f.results) return;
    var description = f.description.value.trim(), details = f.details.value.trim();
    if (description.length < 3) { f.status.textContent = 'Primero describí qué producto vas a enviar.'; return; }
    if (s.controller) s.controller.abort();
    var controller = new AbortController(), turn = ++s.sequence;
    s.controller = controller;
    var timeout = setTimeout(function () { controller.abort(); }, 12000);
    f.status.textContent = 'Buscando en la referencia HS…';
    f.results.replaceChildren();
    try {
      var response = await fetch('/portal/api/hs-code', {
        method: 'POST', credentials: 'same-origin', signal: controller.signal,
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ descripcion: description, detalle: details })
      });
      if (response.redirected) throw new Error('Tu sesión venció. Volvé a ingresar al portal.');
      var data = await response.json();
      if (!response.ok) throw new Error(data.error || 'No pudimos buscar ahora. Probá otra vez.');
      if (turn !== s.sequence || !scope.isConnected || f.description.value.trim() !== description || f.details.value.trim() !== details) return;
      f.status.textContent = data.message;
      if (data.questions && data.questions.length) {
        var questions = node('ul', '');
        data.questions.forEach(function (question) { questions.appendChild(node('li', question)); });
        f.results.appendChild(questions);
        scope.querySelector('.hs-assistant details').open = true;
      }
      (data.candidates || []).forEach(function (candidate) {
        var card = node('article', ''); card.className = 'hs-candidate';
        card.appendChild(node('strong', 'HS ' + candidate.formatted));
        var desc = node('p', candidate.summary || candidate.description); desc.lang = 'en'; card.appendChild(desc);
        if (candidate.summary && candidate.summary !== candidate.description) {
          var full = node('details', ''); full.appendChild(node('summary', 'Ver referencia completa'));
          var fullText = node('p', candidate.description); fullText.lang = 'en'; full.appendChild(fullText); card.appendChild(full);
        }
        var choose = node('button', 'Usar ' + candidate.formatted); choose.type = 'button'; choose.className = 'btn btn-ghost btn-sm';
        choose.addEventListener('click', function () {
          if (turn !== s.sequence || f.description.value.trim() !== description || f.details.value.trim() !== details) { invalidate(scope); return; }
          f.code.value = candidate.formatted;
          f.code.dispatchEvent(new Event('input', { bubbles: true }));
          f.code.dispatchEvent(new Event('change', { bubbles: true }));
          s.selected = candidate.formatted;
          f.status.textContent = 'HS ' + candidate.formatted + ' seleccionado. Verificá que describa tu producto y los requisitos del destino.';
          f.results.replaceChildren(); f.code.focus();
        });
        card.appendChild(choose); f.results.appendChild(card);
      });
      if ((data.candidates || []).length) {
        var source = node('a', 'Referencia HS 2022 · USITC');
        source.href = 'https://www.usitc.gov/harmonized_tariff_information'; source.target = '_blank'; source.rel = 'noopener noreferrer';
        f.results.appendChild(source);
      }
    } catch (error) {
      if (turn !== s.sequence || !scope.isConnected) return;
      f.status.textContent = error.name === 'AbortError' ? 'La búsqueda tardó demasiado. Podés reintentar o ingresar el código manualmente.' : (error.message || 'No pudimos buscar ahora.');
    } finally { clearTimeout(timeout); }
  }
  document.addEventListener('input', function (event) {
    var scope = event.target.closest('[data-hs-scope]');
    if (!scope) return;
    var state = init(scope);
    if (event.target.matches('[data-hs-input]')) { state.selected = null; return; }
    if (!event.target.matches('[data-hs-description], [data-hs-details]')) return;
    invalidate(scope);
    if (fields(scope).description.value.trim().length >= 3) {
      state.timer = setTimeout(function () { search(scope); }, 700);
    }
  });
  document.addEventListener('click', function (event) {
    var button = event.target.closest('[data-hs-search]');
    if (!button) return;
    var scope = button.closest('[data-hs-scope]');
    clearTimeout(init(scope).timer); search(scope);
  });
  document.querySelectorAll('[data-hs-scope]').forEach(init);
  new MutationObserver(function (records) {
    records.forEach(function (record) { record.addedNodes.forEach(function (el) {
      if (el.nodeType !== 1) return;
      if (el.matches('[data-hs-scope]')) init(el);
      el.querySelectorAll('[data-hs-scope]').forEach(init);
    }); });
  }).observe(document.body, { childList: true, subtree: true });
})();
