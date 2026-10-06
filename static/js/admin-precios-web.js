/* Sólo presenta la regla elegida. El servidor valida el valor y el modo. */
(function () {
  'use strict';
  document.querySelectorAll('[data-web-price-dhl]').forEach(function (form) {
    var mode = form.querySelector('[data-dhl-mode]');
    var rows = form.querySelector('[data-dhl-range-rows]');
    var add = form.querySelector('[data-dhl-add-range]');
    function update() {
      form.querySelectorAll('[data-dhl-rule]').forEach(function (group) {
        var active = group.dataset.dhlRule === mode.value;
        group.hidden = !active;
        if (group.tagName === 'FIELDSET') group.disabled = !active;
        group.querySelectorAll('input,select,button').forEach(function (input) {
          input.disabled = !active;
          if (input.tagName !== 'BUTTON' && input.name) {
            input.required = active && input.name !== 'dhl_rango_hasta';
          }
        });
      });
      if (add) add.disabled = mode.value !== 'RANGOS_USD' || rows.children.length >= 30;
    }
    function bindRow(row) {
      var type = row.querySelector('[name="dhl_rango_tipo"]');
      var amount = row.querySelector('[name="dhl_rango_valor"]');
      function unit() { amount.dataset.numero = type.value === 'PCT' ? 'decimal' : 'importe'; }
      type.addEventListener('change', unit);
      row.querySelector('[data-dhl-remove-range]').addEventListener('click', function () {
        row.remove();
        update();
      });
      unit();
    }
    if (rows && add) {
      rows.querySelectorAll('[data-dhl-range-row]').forEach(bindRow);
      add.addEventListener('click', function () {
        if (rows.children.length >= 30) return;
        var row = document.querySelector('#dhl-range-template').content.firstElementChild.cloneNode(true);
        var previous = rows.lastElementChild;
        row.querySelector('[name="dhl_rango_desde"]').value = previous
          ? previous.querySelector('[name="dhl_rango_hasta"]').value : '0';
        rows.appendChild(row);
        bindRow(row);
        update();
        row.querySelector('[name="dhl_rango_desde"]').focus();
      });
    }
    mode.addEventListener('change', update);
    update();
    window.addEventListener('pageshow', update);
  });
})();
