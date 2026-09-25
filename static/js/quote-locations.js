/* Local postal references; deliberately separate from shipment addresses. */
(function (factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory;
  else window.TauroLocationRequest = factory;
})(function (io) {
  var revision = 0, timer, controller, pending = false;
  function cancel() {
    revision++; clearTimeout(timer); if (controller) controller.abort(); pending = false;
  }
  function search(snapshot) {
    cancel();
    if (!snapshot.country || snapshot.query.trim().length < (snapshot.mode === 'city' ? 2 : 3)) return;
    pending = true; var current = revision;
    timer = setTimeout(async function () {
      controller = new AbortController();
      try {
        var data = await io.fetch(snapshot, controller.signal);
        if (revision === current) { pending = false; io.result(data, snapshot); }
      } catch (error) {
        if (revision === current) { pending = false; io.error(error); }
      }
    }, io.delay === undefined ? 400 : io.delay);
  }
  return {search: search, cancel: cancel, pending: function () { return pending; }};
});

(function () {
  'use strict';
  if (typeof document === 'undefined') return;
  function attach(form) {
    var controls = [], scope = form.dataset.unifiedForm;
    form.querySelectorAll('[data-location-side]').forEach(function (group) {
      var side = group.dataset.locationSide;
      var country = form.elements[side + '_pais'];
      var province = scope === 'nacional' ? form.elements[side + '_provincia'] : null;
      var city = form.elements[scope === 'nacional' ? side + '_localidad' : side === 'origen' ? 'origen_ciudad' : 'destino_ciudad_internacional'];
      var postal = form.elements[side + (scope === 'nacional' ? '_cp' : '_cp_internacional')];
      var reference = form.elements[side + '_referencia'];
      var note = group.querySelector('[data-location-note]');
      var list = document.createElement('div'); list.className = 'uq-location-list';
      list.id = scope + '-' + side + '-places'; list.hidden = true;
      list.setAttribute('role', 'listbox'); list.setAttribute('aria-label', 'Ubicaciones sugeridas');
      group.appendChild(list);
      var status = document.createElement('small'); status.className = 'uq-location-status';
      status.setAttribute('role', 'status'); status.hidden = true; group.appendChild(status);
      var selectedInput, items = [], active = -1, applying = false;
      var paired = Boolean(city.value && postal.value);
      function notify() {
        note.hidden = reference.value !== '1';
        if (form.tauroDraft) form.tauroDraft.save();
        form.dispatchEvent(new Event('input', {bubbles: true}));
      }
      function hide() {
        list.hidden = true; items = []; active = -1;
        [city, postal].forEach(function (input) { input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant'); });
      }
      function apply(option, mode) {
        applying = true;
        var enteredPostal = postal.value;
        if (province && option.province && province.value !== option.province) {
          province.value = option.province;
          province.dispatchEvent(new Event('change', {bubbles:true}));
        }
        // Preserve a full postcode the user typed (e.g. a CPA) during reverse lookup.
        city.value = option.city;
        var canonical = enteredPostal.toUpperCase().replace(/[^A-Z0-9]/g, '');
        var preserve = mode === 'postal' && (canonical === option.postal_code.toUpperCase().replace(/[^A-Z0-9]/g, '')
          || (country.value === 'AR' && /^[A-Z]\d{4}[A-Z]{3}$/.test(canonical) && canonical.slice(1,5) === option.postal_code));
        postal.value = preserve ? enteredPostal : option.postal_code;
        reference.value = '1'; paired = true;
        applying = false; status.hidden = true; hide(); notify();
      }
      function show(data, snapshot) {
        status.hidden = true;
        var opposite = snapshot.mode === 'city' ? postal : city;
        if (data.automatic && !opposite.value.trim()) {
          apply(data.automatic, snapshot.mode); return;
        }
        hide();
        if (!data.suggestions.length) {
          status.textContent = 'Podés completar ciudad y CP manualmente.'; status.hidden = false;
        } else if (selectedInput === document.activeElement) {
          items = data.suggestions; list.replaceChildren();
          items.forEach(function (option, index) {
            var button = document.createElement('button'); button.type = 'button';
            button.id = list.id + '-' + index; button.setAttribute('role', 'option');
            button.setAttribute('aria-selected','false');
            var name = document.createElement('strong'); name.textContent = option.city;
            var meta = document.createElement('span'); meta.textContent = option.postal_code + (option.region ? ' · ' + option.region : '');
            button.append(name, meta);
            button.addEventListener('mousedown', function (event) { event.preventDefault(); });
            button.addEventListener('click', function () { apply(option, snapshot.mode); selectedInput.focus(); });
            list.appendChild(button);
          });
          list.hidden = false; selectedInput.setAttribute('aria-expanded', 'true');
        }
        paired = Boolean(city.value && postal.value); notify();
      }
      var control = window.TauroLocationRequest({
        fetch: async function (snapshot, signal) {
          var params = new URLSearchParams({pais:snapshot.country, q:snapshot.query, tipo:snapshot.mode, provincia:snapshot.province});
          // Bound latency, including failures. Manual entry always remains available.
          var timeout = setTimeout(function () { control.cancel(); hide(); status.textContent='Completá la ubicación manualmente.'; status.hidden=false; notify(); }, 12000);
          try {
            var response = await fetch('/portal/cotizar/ubicaciones?' + params, {credentials:'same-origin', signal:signal});
            if (!response.ok || response.redirected) throw new Error('lookup unavailable');
            return await response.json();
          } finally { clearTimeout(timeout); }
        },
        result: show,
        error: function () { hide(); status.textContent = 'Podés completar la ubicación manualmente.'; status.hidden = false; notify(); }
      });
      function lookup(input) {
        selectedInput = input;
        control.search({country:country.value, query:input.value, mode:input === city ? 'city' : 'postal', province:province ? province.value : ''});
      }
      controls.push({pending:control.pending, cancel:function () {control.cancel(); hide();},
        resume:function () {if (city.value && !postal.value) lookup(city); else if (postal.value && !city.value) lookup(postal);}});
      [city, postal].forEach(function (input) {
        input.autocomplete = 'off'; input.setAttribute('role', 'combobox');
        input.setAttribute('aria-autocomplete','list'); input.setAttribute('aria-controls',list.id);
        input.setAttribute('aria-expanded','false');
        input.addEventListener('input', function () {
          if (applying) return;
          var opposite = input === city ? postal : city;
          if (paired || reference.value === '1') opposite.value = '';
          paired = false; reference.value = ''; note.hidden = true; hide(); status.hidden = true;
          lookup(input);
        });
        input.addEventListener('focus', function () { if (input.value && !(input === city ? postal : city).value) lookup(input); });
        input.addEventListener('keydown', function (event) {
          if (list.hidden) return;
          if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); hide(); }
          if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault(); active = Math.max(0, Math.min(items.length - 1, active + (event.key === 'ArrowDown' ? 1 : -1)));
            Array.from(list.children).forEach(function (button, i) { button.setAttribute('aria-selected',String(i === active)); });
            input.setAttribute('aria-activedescendant', list.children[active].id);
            list.children[active].scrollIntoView({block:'nearest'});
          }
          if (event.key === 'Enter' && active >= 0) { event.preventDefault(); apply(items[active], input === city ? 'city' : 'postal'); }
        });
      });
      group.addEventListener('focusout', function () { setTimeout(function () {if (!group.contains(document.activeElement)) hide();}, 0); });
      form.addEventListener('change', function (event) {
        if (!applying && (event.target === country || event.target === province)) {
          control.cancel(); hide(); reference.value = ''; paired = false; note.hidden = true; status.hidden = true;
        }
      });
      note.hidden = reference.value !== '1';
    });
    return {pending:function () {return controls.some(function (c) {return c.pending();});},
      resume:function () {controls.forEach(function (c) {c.resume();});},
      cancel:function () {controls.forEach(function (c) {c.cancel();});}};
  }
  window.TauroQuoteLocations = {attach:attach};
})();
