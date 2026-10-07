const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const context = {window: {}, document: {addEventListener() {}}, Event: class {}};
vm.runInNewContext(fs.readFileSync('static/js/invoice-asistente.js', 'utf8'), context);
const {camposDeItem} = context.window.TauroInvoiceAsistente;

test('completa los campos del artículo y marca HS sugerido y origen asumido', () => {
  const r = camposDeItem({descripcion_en: 'Fishing reels', cantidad: 2, valor_total: 80, moneda: 'USD',
    hs_code: '9507.30', hs_origen: 'sugerido', pais_origen: 'AR', pais_origen_tipo: 'asumido',
    peso_neto_kg: null}, ['', 'AR', 'CN']);
  assert.deepEqual({...r.valores}, {descripcion_en: 'Fishing reels', unidades_aduana: '2',
    valor_total_usd: '80.00', hs_code: '9507.30', pais_origen: 'AR', peso_neto_kg: ''});
  assert.deepEqual({...r.marcas}, {hs_code: 'sugerido', pais_origen: 'asumido'});
});

test('no convierte monedas ni inventa datos faltantes', () => {
  const r = camposDeItem({descripcion_en: 'Glass beads', cantidad: null, valor_total: 35, moneda: 'EUR',
    hs_code: '7018.10.00.00', hs_origen: 'documento', pais_origen: 'CZ', pais_origen_tipo: 'documento'}, ['', 'AR']);
  assert.equal(r.valores.valor_total_usd, '');
  assert.equal(r.valores.unidades_aduana, '');
  assert.equal(r.valores.pais_origen, '');
  assert.equal(r.valores.hs_code, '7018.10.00.00');
  assert.deepEqual({...r.marcas}, {});
});
