const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('embalajes: moneda y kg con dos decimales, medidas sin ceros superfluos', () => {
  const source = fs.readFileSync('static/js/portal-paquetes.js', 'utf8');
  const context = vm.createContext({Intl, Number});
  vm.runInContext(source.slice(source.indexOf('  const num ='), source.indexOf('  const node =')), context);
  assert.equal(vm.runInContext('money(1234.56)', context), '$ 1.234,56');
  assert.equal(vm.runInContext('num(3.8)+" kg"', context), '3,80 kg');
  assert.equal(vm.runInContext('medida(45)+" × "+medida(37)+" × "+medida(24)+" cm"', context), '45 × 37 × 24 cm');
  assert.equal(vm.runInContext('medida(37.25)', context), '37,25');
});

test('el resumen del wizard acepta la coma y conserva los valores del formulario', () => {
  const source = fs.readFileSync('templates/portal/envio_nuevo.html', 'utf8');
  const ref = {};
  const context = vm.createContext({window:{}, Number, invoiceDe:() => ({querySelector:() => ref})});
  vm.runInContext(fs.readFileSync('static/js/tauro-numeros.js','utf8'), context);
  vm.runInContext('function numeroInvoice(value, kind) {const n=window.TauroNumeros.canonico(value,kind||"monto");return n.error?NaN:Number(n.valor);}',context);
  vm.runInContext(source.slice(source.indexOf('  function actualizarReferenciaPaquete('), source.indexOf('  function renumerarBultos(')),context);
  const valores = {'.bulto-peso':{value:'3,8'},'.bulto-cantidad':{value:'1'},'.bulto-largo':{value:'45.0'},'.bulto-ancho':{value:'37'},'.bulto-alto':{value:'24'}};
  context.row = {querySelector:selector => valores[selector]};
  vm.runInContext('actualizarReferenciaPaquete(row)',context);
  assert.equal(ref.textContent, '3,80 kg · 45 × 37 × 24 cm · 1 caja');
  assert.equal(valores['.bulto-peso'].value, '3,8');
  assert.equal(valores['.bulto-largo'].value, '45.0');
});
