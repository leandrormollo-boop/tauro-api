const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('static/js/portal-cuenta.js', 'utf8');

function setup({amount='150,25', existing=false, selected=[true,true]}={}) {
  const el = () => ({textContent:'',hidden:false,value:'',listeners:{},classList:{toggle(){}},
    addEventListener(event,fn){this.listeners[event]=fn;}});
  const fields=Object.fromEntries(['pago-monto','payment-allocation-summary','payment-selected-count',
    'payment-document-search','payment-clear-documents','payment-no-documents'].map(id=>[id,el()]));
  fields['pago-monto'].value=amount;
  const lines=selected.map(el), labels=selected.map((_,i)=>({...el(),textContent:'Destino '+i,querySelector:()=>lines[i]}));
  const checks=selected.map((checked,i)=>({...el(),checked,disabled:false,value:'E:'+i,
    dataset:{saldo:'100.00',kind:'ENVIO'},closest:()=>labels[i]}));
  const submit={disabled:false};
  const form={querySelectorAll:()=>checks,querySelector:()=>submit,hasAttribute:()=>existing};
  const document={querySelector:s=>s==='[data-payment-allocation-form]'?form:null,
    getElementById:id=>fields[id]||null,querySelectorAll:()=>[]};
  vm.runInNewContext(source,{document,window:{location:{hash:''}},Intl});
  return {fields,checks,lines,labels,submit};
}
test('preview shows exact partial amounts for each shipment',()=>{
  const e=setup();
  assert.match(e.lines[0].textContent,/100,00/);
  assert.match(e.lines[1].textContent,/50,25.*Parcial/);
  assert.match(e.fields['payment-allocation-summary'].textContent,/2 envíos:.*150,25/);
  assert.equal(e.submit.disabled,false);
});
test('surplus stays on account rather than increasing selected shipments',()=>{
  const e=setup({amount:'250,00'});
  assert.match(e.fields['payment-allocation-summary'].textContent,/A cuenta:.*50,00/);
  assert.match(e.lines[1].textContent,/100,00/);
});
test('selection exceeding available funds cannot claim payment to zero-funded shipments',()=>{
  const e=setup({amount:'100',selected:[true,true,true]});
  assert.equal(e.submit.disabled,true);
  assert.match(e.fields['payment-allocation-summary'].textContent,/1 envío:/);
  assert.equal(e.lines[1].textContent,'Sin importe para asignar');
});
test('search preserves selected shipments and their amounts',()=>{
  const e=setup({selected:[true,false]});
  e.fields['payment-document-search'].value='inexistente';
  e.fields['payment-document-search'].listeners.input();
  assert.equal(e.checks[0].checked,true);
  assert.equal(e.labels[0].hidden,false);
  assert.equal(e.labels[1].hidden,true);
});
test('new payment allows account credit; existing payment needs an allocation',()=>{
  assert.equal(setup({selected:[false,false]}).submit.disabled,false);
  assert.equal(setup({selected:[false,false],existing:true}).submit.disabled,true);
});
test('changing amount recalculates every line and clears a previous zero-funds warning',()=>{
  const e=setup({amount:'100'});
  assert.equal(e.submit.disabled,true);
  e.fields['pago-monto'].value='200,00';e.fields['pago-monto'].listeners.input();
  assert.equal(e.submit.disabled,false);
  assert.match(e.lines[1].textContent,/100,00/);
});
