const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const { execFileSync } = require('node:child_process');

const root = path.join(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'web/components/02-quote-widget.jsx'), 'utf8');
const helpers = source.slice(source.indexOf('const ACCOUNT_SERVICES_HELP'), source.indexOf('function OperatorStatus()'));
const context = {};
vm.runInNewContext(`${helpers}\nthis.helpers = {estimatedDeliveryLabel, operatorStatusCopy, quotedCountryLabel};`, context);
const { estimatedDeliveryLabel, operatorStatusCopy, quotedCountryLabel } = context.helpers;

test('un plazo desconocido nunca se convierte en una cantidad de días', () => {
  for (const value of [null, undefined, '', 'A confirmar', 'Próximamente', 0, -1, '5-3', '0-5']) {
    assert.equal(estimatedDeliveryLabel(value), 'Plazo a confirmar');
  }
});

test('conserva plazos y rangos numéricos sin repetir la unidad', () => {
  for (const [input, expected] of [[1, '1 día'], ['1 día', '1 día'], [4, '4 días'], ['3-5', '3–5 días'], ['3 a 5 días', '3–5 días']]) {
    assert.equal(estimatedDeliveryLabel(input), expected);
  }
});

test('el catálogo público real no se confunde con permisos del cliente', () => {
  const catalog = JSON.parse(execFileSync('python3', ['-c',
    'import json; from servicios.carrier_contract import public_catalog; print(json.dumps(public_catalog(canal="publico")))',
  ], { cwd: root, encoding: 'utf8' }));
  for (const id of ['dhl', 'oca']) {
    const item = catalog.find(operator => operator.id === id);
    assert.equal(item.estado, 'integracion_preparada');
    assert.equal(operatorStatusCopy(item).label, 'Según cuenta');
  }
  const fedex = operatorStatusCopy(catalog.find(operator => operator.id === 'fedex'));
  assert.equal(fedex.label, 'No disponible aquí');
  assert.equal(catalog.find(operator => operator.id === 'fedex').capacidades.length, 0);
  const tarifarioLegado = operatorStatusCopy({estado:'tarifario_publico'});
  assert.equal(tarifarioLegado.label, 'Cotización estimada');
  assert.match(tarifarioLegado.help, /referencia de precio/);
  assert.equal(operatorStatusCopy({estado:'desconocido'}).label, 'No disponible aquí');
});

// El resumen del backend lleva el ISO; el cambio de presentación no altera el dato.
test('Malvinas no vuelve a mostrar el código técnico al recibir la cotización', () => {
  const response = {destino: 'Islas Malvinas (FK)'};
  assert.equal(quotedCountryLabel(response.destino), 'Islas Malvinas');
  assert.equal(response.destino, 'Islas Malvinas (FK)');
  assert.equal(quotedCountryLabel('Argentina (AR)'), 'Argentina (AR)');
});
