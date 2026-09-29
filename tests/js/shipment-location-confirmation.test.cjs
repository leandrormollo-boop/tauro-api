const test = require('node:test');
const assert = require('node:assert/strict');
const attach = require('../../static/js/shipment-location-confirmation.js');

function setup(reference = '1', checked = false) {
  const events = {}, page = {}, groups = {};
  const form = {elements: {}, saves: 0, addEventListener(type, fn) {
    (events[type] ||= []).push(fn);
  }};
  form.tauroDraft = {save() { form.saves++; }};
  for (const side of ['origen', 'destino']) {
    form.elements[side + '_referencia'] = {value: reference};
    form.elements[side + '_ubicacion_confirmada'] = {checked};
    groups[side] = {dataset: {locationConfirmation: side,
      locationFields: `${side}_cp ${side}_localidad ${side}_calle ${side}_agenda_id`},
      closest() { return form; }};
  }
  global.window = {addEventListener(type, fn) { (page[type] ||= []).push(fn); }};
  attach({querySelectorAll() { return Object.values(groups); }});
  return {form, groups, check: side => form.elements[side + '_ubicacion_confirmada'],
    edit: (name, type = 'input') => events[type].forEach(fn => fn({target: {name}})),
    back: () => page.pageshow.forEach(fn => fn())};
}

test('referencia conservada requiere confirmación sin preaceptarla', () => {
  const ui = setup();
  assert.equal(ui.groups.origen.hidden, false);
  assert.equal(ui.check('origen').checked, false);
  assert.equal(ui.check('origen').required, true);
  assert.equal(ui.check('origen').disabled, false);
});

test('editar CP o elegir otra ficha invalida sólo la confirmación de ese domicilio', () => {
  const ui = setup('1', true);
  ui.edit('origen_cp');
  assert.equal(ui.check('origen').checked, false);
  assert.equal(ui.check('destino').checked, true);
  assert.equal(ui.form.elements.origen_referencia.value, '1');
  ui.edit('destino_agenda_id', 'change');
  assert.equal(ui.check('destino').checked, false);
  assert.equal(ui.form.saves, 2);
});

test('volver atrás o editar paquetes conserva una dirección ya confirmada', () => {
  const ui = setup('1', true);
  ui.edit('peso_kg');
  ui.back();
  assert.equal(ui.check('origen').checked, true);
  assert.equal(ui.check('destino').checked, true);
});

test('datos manuales no requieren confirmar una sugerencia inexistente', () => {
  const ui = setup('', true);
  assert.equal(ui.groups.origen.hidden, true);
  assert.equal(ui.check('origen').disabled, true);
  assert.equal(ui.check('origen').required, false);
  assert.equal(ui.check('origen').checked, false);
});
