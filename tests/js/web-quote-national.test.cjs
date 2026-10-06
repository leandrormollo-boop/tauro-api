const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const root = path.join(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'web/components/02-quote-widget.jsx'), 'utf8');
const helpers = source.slice(source.indexOf('function publicApiError'), source.indexOf('function OperatorStatus()'));
const context = {};
vm.runInNewContext(`${helpers}\nthis.helpers = {publicApiError, normalizeArgentinaPostalCode, effectiveArgentinaPostalCode, locationLookupMode, nationalLocationLabel, formatArs};`, context);
const {
  publicApiError,
  normalizeArgentinaPostalCode,
  effectiveArgentinaPostalCode,
  locationLookupMode,
  nationalLocationLabel,
  formatArs,
} = context.helpers;

test('acepta CP argentino completo sin inventar ciudad', () => {
  assert.equal(normalizeArgentinaPostalCode('1900'), '1900');
  assert.equal(normalizeArgentinaPostalCode(' b1875abc '), 'B1875ABC');
  assert.equal(normalizeArgentinaPostalCode('B1875'), 'B1875');
  for (const invalid of ['', '187', 'Wilde', '12345', 'B1875AB']) {
    assert.equal(normalizeArgentinaPostalCode(invalid), '');
  }
  assert.equal(effectiveArgentinaPostalCode('1875'), '1875');
  assert.equal(effectiveArgentinaPostalCode('B1875ABC'), '1875');
  assert.equal(effectiveArgentinaPostalCode('B1875DEF'), '1875');
});

test('elige el lookup por forma y conserva etiquetas verificadas', () => {
  assert.equal(locationLookupMode('1900'), 'postal');
  assert.equal(locationLookupMode('B1875ABC'), 'postal');
  assert.equal(locationLookupMode('Wilde'), 'city');
  assert.equal(nationalLocationLabel({city: 'Wilde', postal_code: '1875'}), 'Wilde · 1875');
  assert.equal(nationalLocationLabel({input: '1900', postal_code: '1900'}), '1900');
});

test('formatea ARS y sólo muestra errores públicos de texto', () => {
  assert.equal(formatArs('123456.5'), '123.456,50');
  assert.equal(publicApiError({detail: 'Código postal inválido.'}, 'Error'), 'Código postal inválido.');
  assert.equal(publicApiError({detail: [{msg: 'interno'}]}, 'Revisá los datos.'), 'Revisá los datos.');
  assert.equal(publicApiError(null, 'Revisá los datos.'), 'Revisá los datos.');
});
