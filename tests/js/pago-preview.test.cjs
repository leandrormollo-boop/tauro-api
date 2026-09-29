const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const template = fs.readFileSync(
  path.join(__dirname, '../../templates/admin/pago_form.html'),
  'utf8',
);

function funcionesPreview() {
  const canonical = template.match(
    /const parseCanonicalDecimal = value => \{[\s\S]*?\n  \};/,
  );
  const human = template.match(
    /const parseHumanAmount = value => \{[\s\S]*?\n  \};/,
  );
  assert.ok(canonical && human, 'faltan parsers separados en el formulario');
  const context = {};
  vm.runInNewContext(
    `${canonical[0]}\n${human[0]}\nthis.parsers = {parseCanonicalDecimal, parseHumanAmount};`,
    context,
  );
  return context.parsers;
}

test('dataset canónico conserva el punto decimal', () => {
  const {parseCanonicalDecimal} = funcionesPreview();
  assert.equal(parseCanonicalDecimal('60200.00'), 60200);
  assert.equal(parseCanonicalDecimal('349544.25'), 349544.25);
});

test('input humano admite formatos ES y EN sin tocar el dataset', () => {
  const {parseHumanAmount} = funcionesPreview();
  assert.equal(parseHumanAmount('60.200,25'), 60200.25);
  assert.equal(parseHumanAmount('60,200.25'), 60200.25);
  assert.equal(parseHumanAmount('80.000'), 80000);
  assert.equal(parseHumanAmount('80,000'), 80000);
});
