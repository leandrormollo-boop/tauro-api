const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const script = fs.readFileSync(path.join(__dirname, '../../static/js/admin-precios-web.js'), 'utf8');

function setup(initialMode) {
  const events = {};
  const windowEvents = {};
  const mode = { value: initialMode, addEventListener: (name, fn) => { events[name] = fn; } };
  const fields = { PCT: { name: 'dhl_markup_pct', value: '20,5' }, FIJO_ARS: { name: 'dhl_margen_fijo_ars', value: '135.000' } };
  const groups = Object.entries(fields).map(([rule, input]) => ({
    dataset: { dhlRule: rule }, hidden: false,
    querySelectorAll: () => [input],
  }));
  const rangeInputs = ['dhl_rango_desde', 'dhl_rango_hasta', 'dhl_rango_tipo', 'dhl_rango_valor'].map(name => ({name,tagName:'INPUT'}));
  const rangeGroup = {
    tagName:'FIELDSET',disabled:true,hidden:true,dataset:{dhlRule:'RANGOS_USD'},
    querySelectorAll: () => rangeInputs,
  };
  groups.push(rangeGroup);
  const form = {
    querySelector: (selector) => selector === '[data-dhl-mode]' ? mode : null,
    querySelectorAll: () => groups,
  };
  vm.runInNewContext(script, {
    document: { querySelectorAll: () => [form] },
    window: { addEventListener: (name, fn) => { windowEvents[name] = fn; } },
  });
  return { mode, fields, groups, events, windowEvents, rangeGroup, rangeInputs };
}

test('DHL sólo envía la regla activa y conserva ambos borradores al alternar', () => {
  const ui = setup('FIJO_ARS');
  assert.equal(ui.fields.PCT.disabled, true);
  assert.equal(ui.fields.PCT.required, false);
  assert.equal(ui.groups[0].hidden, true);
  assert.equal(ui.fields.FIJO_ARS.disabled, false);
  assert.equal(ui.fields.FIJO_ARS.required, true);
  ui.mode.value = 'PCT';
  ui.events.change();
  assert.equal(ui.fields.PCT.disabled, false);
  assert.equal(ui.fields.PCT.required, true);
  assert.equal(ui.groups[0].hidden, false);
  assert.equal(ui.fields.FIJO_ARS.disabled, true);
  assert.equal(ui.groups[1].hidden, true);
  assert.equal(ui.fields.PCT.value, '20,5');
  assert.equal(ui.fields.FIJO_ARS.value, '135.000');
});

test('DHL sincroniza campos cuando el navegador restaura la selección', () => {
  const ui = setup('PCT');
  ui.mode.value = 'FIJO_ARS';
  ui.windowEvents.pageshow();
  assert.equal(ui.fields.PCT.disabled, true);
  assert.equal(ui.fields.FIJO_ARS.disabled, false);
  assert.equal(ui.groups[1].hidden, false);
});

test('activar rangos habilita el fieldset y permite un último límite abierto', () => {
  const ui = setup('PCT');
  assert.equal(ui.rangeGroup.disabled, true);
  ui.mode.value = 'RANGOS_USD';
  ui.events.change();
  assert.equal(ui.fields.PCT.disabled, true);
  assert.equal(ui.fields.FIJO_ARS.disabled, true);
  assert.equal(ui.rangeGroup.disabled, false);
  assert.equal(ui.rangeGroup.hidden, false);
  assert.equal(ui.rangeInputs.every(field => !field.disabled), true);
  assert.equal(ui.rangeInputs.find(field => field.name === 'dhl_rango_hasta').required, false);
  assert.equal(ui.rangeInputs.find(field => field.name === 'dhl_rango_valor').required, true);
  ui.mode.value = 'PCT';
  ui.events.change();
  assert.equal(ui.rangeGroup.disabled, true);
  assert.equal(ui.rangeInputs.every(field => field.disabled), true);
});
