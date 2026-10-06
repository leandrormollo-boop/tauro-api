/* Sólo presenta la regla elegida. El servidor valida el valor y el modo. */
(function () {
  'use strict';
  document.querySelectorAll('[data-web-price-dhl]').forEach(function (form) {
    var mode = form.querySelector('[data-dhl-mode]');
    function update() {
      form.querySelectorAll('[data-dhl-rule]').forEach(function (group) {
        var active = group.dataset.dhlRule === mode.value;
        group.hidden = !active;
        group.querySelectorAll('input').forEach(function (input) {
          input.disabled = !active;
          input.required = active;
        });
      });
    }
    mode.addEventListener('change', update);
    update();
    window.addEventListener('pageshow', update);
  });
})();
