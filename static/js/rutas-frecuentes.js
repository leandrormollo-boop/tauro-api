/* Atajos de países: respetan cajas, borradores y datos del otro extremo. */
(function () {
  'use strict';
  var initialized = new WeakSet();

  function fields(form) {
    return {origin: form.querySelector('[name="origen_pais"]'),
      destination: form.querySelector('[name="destino_pais"]')};
  }

  function supports(select, value) {
    return select && value && Array.from(select.options).some(function (option) {
      return option.value === value && !option.disabled;
    });
  }

  function sync(form) {
    var route = fields(form);
    if (!route.origin || !route.destination) return;
    form.querySelectorAll('[data-route-origin]').forEach(function (button) {
      button.setAttribute('aria-pressed', String(button.dataset.routeOrigin === route.origin.value
        && button.dataset.routeDestination === route.destination.value));
    });
    var reverse = form.querySelector('[data-route-reverse]');
    if (reverse) reverse.disabled = !route.origin.value || !route.destination.value
      || route.origin.value === route.destination.value
      || !supports(route.origin, route.destination.value)
      || !supports(route.destination, route.origin.value);
  }

  function apply(form, origin, destination) {
    var route = fields(form);
    if (!supports(route.origin, origin) || !supports(route.destination, destination)
        || origin === destination) return false;
    var originChanged = route.origin.value !== origin;
    var destinationChanged = route.destination.value !== destination;
    // La ruta queda abierta para revisar ubicaciones, aun si había cajas listas.
    form.dispatchEvent(new Event('tauro:route-choice'));
    [[route.origin, origin, originChanged], [route.destination, destination, destinationChanged]]
      .forEach(function (item) {
        if (item[2]) {
          item[0].value = item[1];
          item[0].dispatchEvent(new Event('change', {bubbles: true}));
        }
      });
    // No trasladar una ciudad/CP anterior ni convertir una capital de referencia
    // en domicilio real. Los campos del país que no cambió se conservan.
    var clear = [];
    if (originChanged) clear.push('origen_ciudad', 'origen_cp_internacional');
    if (destinationChanged) clear.push('destino_ciudad_internacional', 'destino_cp_internacional');
    clear.forEach(function (name) {
      var input = form.querySelector('[name="' + name + '"]');
      if (input) input.value = '';
    });
    form.dispatchEvent(new Event('input', {bubbles: true}));
    sync(form);
    if (form.tauroDraft) form.tauroDraft.save();
    return true;
  }

  function attach(root) {
    root.querySelectorAll('#form-cotizar').forEach(function (form) {
      if (initialized.has(form)) return;
      initialized.add(form);
      var shortcuts = form.querySelector('[data-route-shortcuts]');
      if (shortcuts) shortcuts.hidden = false;
      form.addEventListener('change', function () { sync(form); });
      form.addEventListener('click', function (event) {
        var button = event.target.closest('[data-route-origin], [data-route-reverse]');
        if (!button || !form.contains(button) || button.disabled) return;
        event.preventDefault();
        var route = fields(form);
        if (button.hasAttribute('data-route-reverse')) {
          apply(form, route.destination.value, route.origin.value);
        } else {
          apply(form, button.dataset.routeOrigin, button.dataset.routeDestination);
        }
      });
      sync(form);
    });
  }

  window.TauroRutasFrecuentes = {attach: attach, apply: apply};
  attach(document);
})();
