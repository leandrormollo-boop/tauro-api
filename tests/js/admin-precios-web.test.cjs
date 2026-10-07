const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const script = fs.readFileSync(path.join(__dirname, '../../static/js/admin-precios-web.js'), 'utf8');

function setup(initialMode) {
  const events = {};
  const windowEvents = {};
  const rangeEvents = {};
  const mode = { value: initialMode, addEventListener: (name, fn) => { events[name] = fn; } };
  const fields = { PCT: { name: 'dhl_markup_pct', value: '20,5' }, FIJO_ARS: { name: 'dhl_margen_fijo_ars', value: '135.000' } };
  const groups = Object.entries(fields).map(([rule, input]) => ({
    dataset: { dhlRule: rule }, hidden: false,
    querySelectorAll: () => [input],
  }));
  const rangeType = {name:'dhl_rango_tipo',tagName:'SELECT',value:'FIJO_USD',addEventListener:(name,fn) => {rangeEvents[name]=fn;}};
  const rangeAmount = {
    name:'dhl_rango_valor',tagName:'INPUT',dataset:{},attributes:{},
    setAttribute(name,value){this.attributes[name]=value;},
  };
  const rangeInputs = [
    {name:'dhl_rango_desde',tagName:'INPUT'},
    {name:'dhl_rango_hasta',tagName:'INPUT'},
    rangeType,
    rangeAmount,
  ];
  const gainLabel = {textContent:''};
  const gainUnit = {textContent:''};
  const gainExample = {textContent:''};
  const remove = {tagName:'BUTTON',addEventListener:()=>{}};
  const rangeRow = {
    querySelector: selector => ({
      '[name="dhl_rango_tipo"]': rangeType,
      '[name="dhl_rango_valor"]': rangeAmount,
      '[data-dhl-gain-label]': gainLabel,
      '[data-dhl-gain-unit]': gainUnit,
      '[data-dhl-gain-example]': gainExample,
      '[data-dhl-remove-range]': remove,
    })[selector],
  };
  const rows = {children:[rangeRow],querySelectorAll:()=>[rangeRow]};
  const add = {tagName:'BUTTON',addEventListener:()=>{}};
  const rangeGroup = {
    tagName:'FIELDSET',disabled:true,hidden:true,dataset:{dhlRule:'RANGOS_USD'},
    querySelectorAll: () => [...rangeInputs,remove,add],
  };
  groups.push(rangeGroup);
  const form = {
    querySelector: (selector) => ({
      '[data-dhl-mode]': mode,
      '[data-dhl-range-rows]': rows,
      '[data-dhl-add-range]': add,
    })[selector] || null,
    querySelectorAll: () => groups,
  };
  vm.runInNewContext(script, {
    document: { querySelectorAll: () => [form] },
    window: { addEventListener: (name, fn) => { windowEvents[name] = fn; } },
  });
  return {
    mode, fields, groups, events, windowEvents, rangeEvents,
    rangeGroup, rangeInputs, rangeType, rangeAmount,
    gainLabel, gainUnit, gainExample,
  };
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

test('la unidad y la explicación siguen el tipo de ganancia elegido', () => {
  const ui = setup('RANGOS_USD');
  assert.equal(ui.gainLabel.textContent, 'Ganancia (USD)');
  assert.equal(ui.gainUnit.textContent, 'USD');
  assert.equal(ui.rangeAmount.dataset.numero, 'importe');
  assert.equal(ui.rangeAmount.attributes['aria-label'], 'Ganancia que suma TAURO (USD)');
  assert.equal(ui.gainExample.textContent, 'Se suma este importe fijo en dólares.');

  ui.rangeType.value = 'PCT';
  ui.rangeEvents.change();
  assert.equal(ui.gainLabel.textContent, 'Ganancia (%)');
  assert.equal(ui.gainUnit.textContent, '%');
  assert.equal(ui.rangeAmount.dataset.numero, 'decimal');
  assert.equal(ui.rangeAmount.attributes['aria-label'], 'Ganancia que suma TAURO (%)');
  assert.equal(ui.gainExample.textContent, 'Se suma este porcentaje sobre el costo DHL.');

  ui.rangeType.value = 'FIJO_ARS';
  ui.rangeEvents.change();
  assert.equal(ui.gainLabel.textContent, 'Ganancia (ARS)');
  assert.equal(ui.gainUnit.textContent, 'ARS');
  assert.equal(ui.rangeAmount.dataset.numero, 'importe');
  assert.equal(ui.rangeAmount.attributes['aria-label'], 'Ganancia que suma TAURO (ARS)');
  assert.equal(ui.gainExample.textContent, 'Se suma este importe fijo en pesos.');
});
