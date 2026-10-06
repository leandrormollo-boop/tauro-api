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
  const fields = { PCT: { value: '20,5' }, FIJO_ARS: { value: '135.000' } };
  const groups = Object.entries(fields).map(([rule, input]) => ({
    dataset: { dhlRule: rule }, hidden: false,
    querySelectorAll: () => [input],
  }));
  const form = {
    querySelector: () => mode,
    querySelectorAll: () => groups,
  };
  vm.runInNewContext(script, {
    document: { querySelectorAll: () => [form] },
    window: { addEventListener: (name, fn) => { windowEvents[name] = fn; } },
  });
  return { mode, fields, groups, events, windowEvents };
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
