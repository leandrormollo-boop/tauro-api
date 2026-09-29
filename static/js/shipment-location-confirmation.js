/* Una ubicación sugerida se conserva; el cliente confirma su domicilio real. */
(function (factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory;
  else document.addEventListener('DOMContentLoaded', function () { factory(document); });
})(function attachLocationConfirmations(root) {
  root.querySelectorAll('[data-location-confirmation]').forEach(function (group) {
    var form = group.closest('form'), side = group.dataset.locationConfirmation;
    var reference = form.elements[side + '_referencia'];
    var check = form.elements[side + '_ubicacion_confirmada'];
    var names = group.dataset.locationFields.split(' ');
    function refresh() {
      var active = reference.value === '1';
      group.hidden = !active;
      check.disabled = !active;
      check.required = active;
      if (!active) check.checked = false;
    }
    function edited(event) {
      if (names.indexOf(event.target.name) < 0) return;
      check.checked = false;
      refresh();
      if (form.tauroDraft) form.tauroDraft.save();
    }
    form.addEventListener('input', edited);
    form.addEventListener('change', edited);
    // La restauración del borrador ya ocurrió al cargar los pasos. BFCache
    // conserva la confirmación mientras no se edite la dirección.
    refresh();
    window.addEventListener('pageshow', refresh);
  });
});
