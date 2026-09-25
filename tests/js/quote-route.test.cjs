const test = require('node:test');
const assert = require('node:assert/strict');
const reverse = require('../../static/js/quote-route.js');
function form(scope, fields) {
  const elements = Object.fromEntries(Object.entries(fields).map(([k,v]) => [k,{value:v,dispatchEvent(){}}]));
  const value = {dataset:{unifiedForm:scope},elements};
  for (const side of ['origen','destino']) {
    const parent = side + (scope==='nacional' ? '_provincia' : '_pais');
    elements[parent].dispatchEvent=()=>{
      const city = scope==='nacional' ? side+'_localidad' : side==='origen' ? 'origen_ciudad' : 'destino_ciudad_internacional';
      elements[city].value=''; elements[side+(scope==='nacional'?'_cp':'_cp_internacional')].value=''; elements[side+'_referencia'].value='';
    };
  }
  return value;
}
test('invertir internacional conserva ciudades, CP completos, referencias y paquetes',()=>{
  const data={origen_pais:'AR',destino_pais:'US',origen_ciudad:'Wilde',destino_ciudad_internacional:'Miami',origen_cp_internacional:'B1875ABC',destino_cp_internacional:'33101',origen_referencia:'',destino_referencia:'1',bulto_peso:'2,5',valor_declarado_usd:'100'};
  const f=form('internacional',data); reverse(f);
  assert.equal(f.elements.origen_ciudad.value,'Miami'); assert.equal(f.elements.destino_ciudad_internacional.value,'Wilde');
  assert.equal(f.elements.destino_cp_internacional.value,'B1875ABC'); assert.equal(f.elements.origen_referencia.value,'1');
  assert.equal(f.elements.bulto_peso.value,'2,5'); assert.equal(f.elements.valor_declarado_usd.value,'100');
  reverse(f); assert.deepEqual(Object.fromEntries(Object.entries(f.elements).map(([k,v])=>[k,v.value])),data);
});
test('invertir nacional intercambia provincias sin que los listeners borren la localidad',()=>{
  const data={origen_provincia:'B',destino_provincia:'C',origen_localidad:'Wilde',destino_localidad:'Buenos Aires',origen_cp:'1875',destino_cp:'1000',origen_referencia:'1',destino_referencia:'1',peso_kg:'10'};
  const f=form('nacional',data); reverse(f);
  assert.equal(f.elements.origen_provincia.value,'C'); assert.equal(f.elements.origen_localidad.value,'Buenos Aires'); assert.equal(f.elements.destino_cp.value,'1875');
  reverse(f); assert.deepEqual(Object.fromEntries(Object.entries(f.elements).map(([k,v])=>[k,v.value])),data);
});
