/* Swap the entire route as one edit. Packages and declared value are untouched. */
(function (factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory;
  else window.TauroReverseQuoteRoute = factory;
})(function (form) {
  var national = form.dataset.unifiedForm === 'nacional';
  var names = national
    ? [['origen_provincia','destino_provincia'], ['origen_localidad','destino_localidad'], ['origen_cp','destino_cp'], ['origen_referencia','destino_referencia']]
    : [['origen_pais','destino_pais'], ['origen_ciudad','destino_ciudad_internacional'], ['origen_cp_internacional','destino_cp_internacional'], ['origen_referencia','destino_referencia']];
  var snapshot = names.map(function (pair) { return pair.map(function (name) { return form.elements[name].value; }); });
  // Country/province listeners must invalidate old lookups and old quotes first.
  names[0].forEach(function (name, side) {
    var input = form.elements[name]; input.value = snapshot[0][1 - side];
    input.dispatchEvent(new Event('change', {bubbles:true}));
  });
  names.slice(1).forEach(function (pair, index) {
    pair.forEach(function (name, side) { form.elements[name].value = snapshot[index + 1][1 - side]; });
  });
});
