const test = require('node:test');
const assert = require('node:assert/strict');
const install = require('../../static/js/tauro-dropdowns.js');

function fixture() {
  const listeners = [], nodes = [];
  const doc = {
    activeElement: null,
    addEventListener(type, fn, capture = false) { listeners.push({type, fn, capture}); },
    querySelectorAll() { return nodes.filter(n => n.dropdown && n.open); },
  };
  function node(parent = null, dropdown = false) {
    const n = {
      parent, dropdown, open: false, value: '',
      contains(target) { for (; target; target = target.parent) if (target === this) return true; return false; },
      closest() { for (let n = this; n; n = n.parent) if (n.dropdown && n.open) return n; return null; },
      matches() { return this.dropdown; },
      querySelector() { return this.summary; },
      focus() { doc.activeElement = this; },
    };
    nodes.push(n);
    return n;
  }
  function menu(parent = null, dropdown = true) {
    const n = node(parent, dropdown); n.open = true; n.summary = node(n); return n;
  }
  function send(type, target, props = {}) {
    const e = {type, target, defaultPrevented: false, stopped: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() { this.stopped = true; }, ...props};
    listeners.filter(l => l.type === type).forEach(l => l.fn(e));
    return e;
  }
  install(doc);
  return {doc, listeners, node, menu, send};
}

test('mouse, touch and keyboard clicks outside close without swallowing the next action or changing data', () => {
  for (const type of ['pointerdown', 'click']) {
    const f = fixture(), menu = f.menu(), input = f.node(menu), outside = f.node();
    input.value = '30';
    const e = f.send(type, outside, {pointerType: 'touch'});
    assert.equal(menu.open, false);
    assert.equal(input.value, '30');
    assert.equal(e.defaultPrevented, false);
    assert.equal(e.stopped, false);
    assert.equal(f.listeners.find(l => l.type === type).capture, true);
  }
});

test('clicks on a nested option or its icon keep the menu available for the option handler', () => {
  const f = fixture(), menu = f.menu(), button = f.node(menu), icon = f.node(button);
  f.send('pointerdown', icon); f.send('click', icon);
  assert.equal(menu.open, true);
});

test('Escape closes the nearest menu and restores focus without closing its dialog', () => {
  const f = fixture(), menu = f.menu(), option = f.node(menu);
  const e = f.send('keydown', option, {key: 'Escape'});
  assert.equal(menu.open, false);
  assert.equal(f.doc.activeElement, menu.summary);
  assert.equal(e.defaultPrevented, true);
  assert.equal(e.stopped, true);
});

test('a nested selector consumes its own Escape before the parent menu', () => {
  const f = fixture(), menu = f.menu(), select = f.node(menu);
  f.send('keydown', select, {key: 'Escape', defaultPrevented: true});
  assert.equal(menu.open, true);
});

test('moving focus outside dismisses only menus, preserving inline forms and fields', () => {
  const f = fixture(), menu = f.menu(), form = f.menu(null, false), input = f.node(form);
  input.value = '1000'; f.send('focusin', input);
  assert.equal(menu.open, false);
  assert.equal(form.open, true);
  assert.equal(input.value, '1000');
});

test('menus inserted after loading use the same dismissal and opening closes other menus', () => {
  const f = fixture(), first = f.menu(), second = f.menu();
  f.send('toggle', second);
  assert.equal(first.open, false); assert.equal(second.open, true);
  f.send('click', f.node());
  assert.equal(second.open, false);
});

test('opening a nested menu leaves its parent open and Escape closes only the nested menu', () => {
  const f = fixture(), parent = f.menu(), nested = f.menu(parent);
  f.send('toggle', nested);
  assert.equal(parent.open, true);
  f.send('keydown', f.node(nested), {key: 'Escape'});
  assert.equal(nested.open, false); assert.equal(parent.open, true);
});
