const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const context = {window:{}};
vm.runInNewContext(fs.readFileSync('static/js/tauro-numeros.js','utf8'), context);
const template = fs.readFileSync('templates/portal/envio_nuevo.html','utf8');
vm.runInNewContext(template.slice(template.indexOf('  function numeroInvoice('), template.indexOf('  function sincronizarArticulos(')), context);
function invoice(items) {
  return {querySelectorAll: () => items.map(item=>({querySelector:selector=>({value:item[selector] || ''})}))};
}
test('invoice suma totales exactos, nunca unitarios redondeados',()=>{
  const i=invoice([{'.bulto-unidades-aduana':'3','.bulto-valor':'60,25'}, {'.bulto-unidades-aduana':'25','.bulto-valor':'39.75'}]);
  assert.equal(context.subtotalArticulos(i),100);
});
test('miles y centavos se interpretan igual al escribir y enviar',()=>{
  for (const value of ['1.000,50','1,000.50','1000.50']) {
    assert.equal(context.subtotalArticulos(invoice([{'.bulto-unidades-aduana':'3','.bulto-valor':value}])),1000.5);
  }
  assert.equal(context.numeroInvoice('1.000'),1000);
  for (const value of ['1 000','1..2','NaN','Infinity']) assert.ok(Number.isNaN(context.numeroInvoice(value)));
});
test('borradores anteriores migran unitario a total una sola vez',()=>{
  assert.equal(context.totalArticuloGuardado({unidades_aduana:'25',valor_unitario_usd:'2,50'}),'62.50');
  assert.equal(context.totalArticuloGuardado({unidades_aduana:3,valor_unitario_usd:33.333,valor_total_usd:100}),100);
  assert.equal(context.totalArticuloGuardado({unidades_aduana:3,valor_unitario_usd:33.333,valor_total_usd:''}),'');
});
test('cantidad incompleta o fraccionaria no muestra un total válido',()=>{
  for (const cantidad of ['', '0', '1.5']) assert.ok(Number.isNaN(context.subtotalArticulos(invoice([{'.bulto-unidades-aduana':cantidad,'.bulto-valor':'100'}]))));
});
