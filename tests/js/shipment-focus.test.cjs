const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/js/shipment-focus.js', 'utf8');

function escenario({campoBottom, barraTop, viewport, contenido = true, tipo = true}) {
  const eventos = {}, movimientos = [];
  const campo = {matches: () => tipo, getBoundingClientRect: () => ({bottom: campoBottom})};
  const wizard = {
    contains: () => contenido,
    querySelectorAll: () => [
      {getBoundingClientRect: () => ({top: 0, height: 0})}, // barra de otro paso, oculta
      {getBoundingClientRect: () => ({top: barraTop, height: 60})},
    ],
    addEventListener: (evento, fn) => {eventos[evento] = fn;},
  };
  const window = {
    innerHeight: 700,
    visualViewport: viewport && {...viewport, addEventListener: (e, fn) => {eventos['viewport-' + e] = fn;}},
    requestAnimationFrame: fn => fn(),
    scrollBy: movimiento => movimientos.push(movimiento.top),
    addEventListener: (evento, fn) => {eventos[evento] = fn;},
  };
  vm.runInNewContext(source, {document: {activeElement: campo, getElementById: () => wizard}, window});
  return {eventos, movimientos};
}

test('a 1100 px sube País por encima de Atrás/Siguiente y deja margen', () => {
  const s = escenario({campoBottom: 650, barraTop: 620});
  s.eventos.focusin();
  assert.deepEqual(s.movimientos, [46]);
});

test('a 390 px reserva la botonera que está sobre la navegación inferior', () => {
  const s = escenario({campoBottom: 630, barraTop: 558});
  s.eventos.focusin();
  assert.deepEqual(s.movimientos, [88]);
});

test('al abrir el teclado mantiene el campo dentro del viewport visible', () => {
  const s = escenario({campoBottom: 550, barraTop: 558, viewport: {offsetTop: 0, height: 400}});
  s.eventos['viewport-resize']();
  assert.deepEqual(s.movimientos, [166]);
});

test('el foco ya visible y las acciones de navegación no producen saltos', () => {
  for (const config of [{campoBottom: 300, barraTop: 620},
                        {campoBottom: 650, barraTop: 620, contenido: false},
                        {campoBottom: 650, barraTop: 620, tipo: false}]) {
    const s = escenario(config);
    s.eventos.focusin();
    assert.deepEqual(s.movimientos, []);
  }
});
