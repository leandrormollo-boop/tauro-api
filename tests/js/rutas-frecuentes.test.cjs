const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('static/js/rutas-frecuentes.js', 'utf8');

function setup() {
  const listeners = {}, saved = [];
  const context = {window: {}, document: {querySelectorAll: () => []}, Event: class {
    constructor(type, options = {}) { this.type = type; Object.assign(this, options); }
  }};
  vm.runInNewContext(source, context);
  const buttons = [{dataset: {routeOrigin: 'CN', routeDestination: 'AR'}, attrs: {}, disabled: false,
    setAttribute(k,v) {this.attrs[k]=v;}, hasAttribute: () => false}];
  const reverse = {dataset: {}, disabled: true, hasAttribute: name => name === 'data-route-reverse'};
  const inputs = {};
  for (const [name,value] of Object.entries({origen_pais:'AR', destino_pais:'US', origen_ciudad:'Rosario',
    origen_cp_internacional:'2000', destino_ciudad_internacional:'New York', destino_cp_internacional:'10001',
    bulto_peso:'4.5', bulto_largo:'30', bulto_cantidad:'2', valor_declarado_usd:'250'})) {
    inputs[name] = {name, value, options: ['AR','US','CN','IN'].map(value => ({value, disabled:false})),
      dispatchEvent(event) {
        if (name.endsWith('_pais') && event.type === 'change') {
          const prefix = name === 'origen_pais' ? ['origen_ciudad','origen_cp_internacional'] : ['destino_ciudad_internacional','destino_cp_internacional'];
          prefix.forEach(field => {inputs[field].value = 'referencia';});
        }
        form.dispatchEvent(event);
      }};
  }
  const shortcuts = {hidden:true};
  const form = {querySelector(selector) {
      if (selector === '[data-route-reverse]') return reverse;
      if (selector === '[data-route-shortcuts]') return shortcuts;
      const match = selector.match(/name="([^"]+)"/); return match && inputs[match[1]];
    }, querySelectorAll: () => buttons,
    addEventListener(name, fn) {(listeners[name] ??= []).push(fn);},
    dispatchEvent(event) {(listeners[event.type] || []).forEach(fn => fn(event));},
    contains: el => buttons.includes(el) || el === reverse,
    tauroDraft: {save() {saved.push(Object.fromEntries(Object.entries(inputs).map(([k,v]) => [k,v.value])));}}};
  const attach = () => context.window.TauroRutasFrecuentes.attach({querySelectorAll: () => [form]});
  return {form, inputs, saved, buttons, reverse, shortcuts, listeners, attach, api: context.window.TauroRutasFrecuentes};
}

test('changing one country clears only its location and preserves boxes and declared value', () => {
  const e = setup(); e.attach();
  assert.equal(e.api.apply(e.form, 'AR', 'CN'), true);
  assert.equal(e.inputs.origen_ciudad.value, 'Rosario');
  assert.equal(e.inputs.origen_cp_internacional.value, '2000');
  assert.equal(e.inputs.destino_ciudad_internacional.value, '');
  assert.equal(e.inputs.destino_cp_internacional.value, '');
  assert.equal(e.inputs.bulto_peso.value, '4.5');
  assert.equal(e.inputs.bulto_cantidad.value, '2');
  assert.equal(e.inputs.valor_declarado_usd.value, '250');
  assert.equal(e.saved.length, 1);
  assert.equal(e.saved[0].destino_ciudad_internacional, '');
});
test('same route preserves entered locations and asks the wizard to stay on the route', () => {
  const e = setup(); let editing = false;
  e.form.addEventListener('tauro:route-choice', () => {editing = true;});
  e.api.apply(e.form, 'AR', 'US');
  assert.equal(e.inputs.destino_ciudad_internacional.value, 'New York');
  assert.equal(editing, true);
});
test('reverse works within its form without submitting a quote or touching another form', () => {
  const e = setup(), other = setup(); e.attach();
  let submitted = false; e.form.addEventListener('submit', () => {submitted = true;});
  e.form.dispatchEvent({type:'click',target:{closest:()=>e.reverse},preventDefault(){}});
  assert.equal(e.inputs.origen_pais.value, 'US'); assert.equal(e.inputs.destino_pais.value, 'AR');
  assert.equal(e.inputs.origen_ciudad.value, ''); assert.equal(e.inputs.destino_ciudad_internacional.value, '');
  assert.equal(other.inputs.origen_pais.value, 'AR'); assert.equal(other.saved.length, 0);
  assert.equal(submitted, false);
});
test('invalid or national shortcut never partially changes data', () => {
  const e = setup();
  for (const route of [['XX','AR'], ['CN','XX'], ['AR','AR'], ['', 'US']]) {
    assert.equal(e.api.apply(e.form, ...route), false);
    assert.equal(e.inputs.origen_pais.value, 'AR'); assert.equal(e.inputs.destino_pais.value, 'US');
  }
  assert.equal(e.saved.length, 0);
});
test('buttons reflect manual changes and attach is idempotent for cached dialogs', () => {
  const e = setup(); e.attach(); e.attach();
  assert.equal(e.shortcuts.hidden, false); assert.equal(e.listeners.click.length, 1);
  e.api.apply(e.form, 'CN', 'AR'); assert.equal(e.buttons[0].attrs['aria-pressed'], 'true');
  e.inputs.destino_pais.value = '';
  e.form.dispatchEvent({type:'change'});
  assert.equal(e.buttons[0].attrs['aria-pressed'], 'false'); assert.equal(e.reverse.disabled, true);
});
