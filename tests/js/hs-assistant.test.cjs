const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/js/hs-assistant.js', 'utf8');

function fixture(fetcher) {
  const handlers = {}, timers = new Map();
  let timerId = 0, observe;
  function node(tag, key = '') {
    return {tag, key, value: '', textContent: '', children: [], listeners: {}, open: false,
      appendChild(n) {this.children.push(n);}, replaceChildren(...ns) {this.children = ns;},
      addEventListener(name, fn) {this.listeners[name] = fn;}, focus() {this.focused = true;},
      dispatchEvent(e) {if (handlers[e.type]) handlers[e.type]({target: this});},
      closest() {return this.scope;}, matches(q) {return q.split(', ').includes(this.key);}};
  }
  function makeScope() {
    const scope = {isConnected: true, nodeType: 1, nodes: {},
      querySelector(q) {return this.nodes[q];}, querySelectorAll() {return [];},
      matches(q) {return q === '[data-hs-scope]';}};
    for (const key of ['description', 'input', 'details', 'status', 'results', 'panel', 'summary', 'search']) {
      const selector = '[data-hs-' + key + ']';
      scope.nodes[selector] = Object.assign(node('div', selector), {scope});
    }
    return scope;
  }
  const scope = makeScope();
  const document = {body: {}, querySelectorAll() {return [scope];}, createElement: node,
    addEventListener(name, fn) {handlers[name] = fn;}};
  vm.runInNewContext(source, {window: {}, document, AbortController, Event, WeakMap,
    setTimeout(fn, delay) {const id = ++timerId; timers.set(id, {fn, delay}); return id;},
    clearTimeout(id) {timers.delete(id);}, fetch: fetcher,
    MutationObserver: class {constructor(fn) {observe = fn;} observe() {}}});
  function field(key, s = scope) {return s.nodes['[data-hs-' + key + ']'];}
  function input(key, value, s = scope) {field(key, s).value = value; handlers.input({target: field(key, s)});}
  async function runSearch(s = scope) {
    handlers.click({target: {closest() {return field('search', s);}}});
    await new Promise(resolve => setImmediate(resolve));
  }
  return {scope, field, input, runSearch, timers,
    clone() {const s = makeScope(); field('panel', s).open = true;
      field('results', s).children = [node('copied')];
      observe([{addedNodes: [s]}]); return s;}};
}
const result = {message: 'Compará antes de elegir.', questions: ['¿De qué material es?'],
  candidates: [{formatted: '6105.10', summary: 'Cotton shirts', description: 'Cotton shirts'},
               {formatted: '6106.10', summary: 'Cotton blouses', description: 'Cotton blouses'}]};
const response = () => Promise.resolve({ok: true, json: async () => result});

test('buscar mantiene panel cerrado y no selecciona HS automáticamente', async () => {
  const f = fixture(response);
  f.input('description', 'Cotton shirts');
  await f.runSearch();
  assert.equal(f.field('panel').open, false);
  assert.equal(f.field('input').value, '');
  assert.equal(f.field('summary').textContent, 'Ver 2 códigos sugeridos');
  assert.equal(f.field('results').children.filter(n => n.tag === 'article').length, 2);
});

test('seleccionar código cierra sugerencias y editar descripción lo invalida', async () => {
  const f = fixture(response);
  f.input('description', 'Cotton shirts'); await f.runSearch();
  f.field('panel').open = true;
  const card = f.field('results').children.find(n => n.tag === 'article');
  card.children.find(n => n.tag === 'button').listeners.click();
  assert.equal(f.field('input').value, '6105.10');
  assert.equal(f.field('panel').open, false);
  assert.equal(f.field('input').focused, true);
  assert.equal(f.field('results').children.length, 0);
  f.input('description', 'Wool sweater');
  assert.equal(f.field('input').value, '');
});

test('otro artículo inicia cerrado y sin resultados copiados', () => {
  const f = fixture(response), clone = f.clone();
  assert.equal(f.field('panel', clone).open, false);
  assert.equal(f.field('results', clone).children.length, 0);
  assert.equal(f.field('summary', clone).textContent, 'Buscar código aduanero');
});

test('respuesta vieja no vuelve a mostrar sugerencias de otro producto', async () => {
  let resolve;
  const f = fixture(() => new Promise(r => {resolve = r;}));
  f.input('description', 'Cotton shirts'); await f.runSearch();
  f.input('description', 'Wool sweater');
  resolve(await response()); await new Promise(r => setImmediate(r));
  assert.equal(f.field('results').children.length, 0);
  assert.equal(f.field('summary').textContent, 'Buscar código aduanero');
});

test('sin servicio HS se conserva ingreso manual y el panel no se abre solo', async () => {
  const f = fixture(async () => {throw new Error('Sin conexión');});
  f.input('input', '6109.10'); f.input('description', 'Cotton shirts');
  await f.runSearch();
  assert.equal(f.field('input').value, '6109.10');
  assert.equal(f.field('panel').open, false);
  assert.equal(f.field('summary').textContent, 'Reintentar búsqueda del código');
});
