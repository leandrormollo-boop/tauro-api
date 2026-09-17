const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/js/form-draft.js','utf8');
function storage() {
  const data={};
  Object.defineProperties(data, {
    getItem:{value:k=>data[k]??null}, setItem:{value:(k,v)=>{data[k]=v;}}, removeItem:{value:k=>{delete data[k];}}
  });return data;
}
function field(name,value='',type='text') {
  return {name,value,type,tagName:'INPUT',checked:false,
    hasAttribute:k=>k==='data-draft-persist' && name==='idempotency_key',
    getClientRects:()=>[],focus:()=>{}};
}
function environment(store,scope='portal:DEMO',url='https://tauro.example/portal/envios/nuevo?ambito=internacional') {
  const events={}, pageEvents={}, formEvents={};
  const elements=[field('dest_nombre'),field('peso'),field('precio_cotizado_ars','999','hidden'),field('password','','password'),field('guia_pdf','','file'),field('idempotency_key','op-123','hidden')];
  const form={elements,dataset:{},querySelector:()=>elements.find(e=>e.name==='borrador_token'),
    querySelectorAll:()=>[], closest:()=>null,prepend:()=>{},appendChild:e=>elements.push(e),
    addEventListener:(n,f)=>{(formEvents[n]??=[]).push(f);}};
  const document={body:{dataset:{draftScope:scope}},activeElement:null,
    createElement:tag=>({tagName:tag.toUpperCase(),name:'',value:'',type:'',hasAttribute:()=>false,setAttribute:()=>{},after:()=>{},appendChild:()=>{},addEventListener:()=>{}}),
    addEventListener:(n,f)=>events[n]=f,querySelectorAll:()=>[]};
  const window={scrollY:75,scrollTo:()=>{},addEventListener:(n,f)=>{(pageEvents[n]??=[]).push(f);}};
  const context={window,document,sessionStorage:store,location:new URL(url),history:{replaceState:()=>{}},URL,Date,console,
    crypto:require('node:crypto'),setTimeout,clearTimeout,queueMicrotask,requestAnimationFrame:f=>f()};
  vm.runInNewContext(source,context);
  return {form,elements,events,pageEvents,formEvents,api:()=>window.TauroDraft.attach(form)};
}
test('restores fields and operation identity without reviving an old price, password or file',()=>{
  const store=storage(), first=environment(store); const api=first.api();
  first.elements[0].value='QA recipient';first.elements[1].value='3.5';first.elements[3].value='secret';api.save();
  const second=environment(store);second.elements[2].value='';second.elements[5].value='new-key';second.api();
  assert.equal(second.elements[0].value,'QA recipient');assert.equal(second.elements[1].value,'3.5');
  assert.equal(second.elements[2].value,'');assert.equal(second.elements[3].value,'');
  assert.equal(second.elements[5].value,'op-123');
});
test('drafts never cross client accounts',()=>{
  const store=storage(), first=environment(store);first.elements[0].value='Only owner';first.api().save();
  const other=environment(store,'portal:OTHER');other.api();assert.equal(other.elements[0].value,'');
});
test('server validation values prevail over older local fields',()=>{
  const store=storage(), first=environment(store);first.elements[0].value='Old';first.api().save();
  const second=environment(store);second.form.dataset.draftServer='1';second.elements[0].value='Submitted';second.api();
  assert.equal(second.elements[0].value,'Submitted');
});
test('confirmation deletes only its own draft; an error or unrelated token does not',()=>{
  const store=storage(), first=environment(store);first.elements[0].value='Keep';first.api().save();
  const key=Object.keys(store)[0], token=JSON.parse(store[key]).token;
  environment(store,'portal:DEMO','https://tauro.example/portal/envios?error=failed').events.DOMContentLoaded();assert.ok(store[key]);
  environment(store,'portal:DEMO','https://tauro.example/portal/envios?borrador_listo=unrelated').events.DOMContentLoaded();assert.ok(store[key]);
  environment(store,'portal:DEMO','https://tauro.example/portal/envios?borrador_listo='+token).events.DOMContentLoaded();assert.equal(store[key],undefined);
});
test('expired or corrupt drafts do not restore values',()=>{
  for (const corrupt of [false,true]) {
    const store=storage(),first=environment(store);first.elements[0].value='Expired';first.api().save();
    const key=Object.keys(store)[0],data=JSON.parse(store[key]);data.at=Date.now()-5*3600000;
    store[key]=corrupt?'not JSON':JSON.stringify(data);
    const second=environment(store);second.api();assert.equal(second.elements[0].value,'');
  }
});
test('storage failure keeps the editable form usable',()=>{
  const store={getItem:()=>null,setItem:()=>{throw Error('quota');},removeItem:()=>{}};
  const e=environment(store);e.elements[0].value='Still here';assert.doesNotThrow(()=>e.api().save());assert.equal(e.elements[0].value,'Still here');
});
test('detached quote forms cannot overwrite the current draft',()=>{
  const store=storage(),first=environment(store);const api=first.api();first.elements[0].value='First';api.save();api.stop();
  const second=environment(store);const api2=second.api();second.elements[0].value='Current';api2.save();
  first.elements[0].value='Stale';api.save();const third=environment(store);third.api();assert.equal(third.elements[0].value,'Current');
});

test('Back after a confirmed save cannot submit the completed form again',()=>{
  const store=storage(), page=environment(store);const api=page.api();api.save();
  const key=Object.keys(store).find(k=>k.startsWith('tauro:draft:')), token=JSON.parse(store[key]).token;
  environment(store,'portal:DEMO','https://tauro.example/portal/envios?borrador_listo='+token).events.DOMContentLoaded();
  let prevented=false;page.formEvents.submit[0]({preventDefault:()=>{prevented=true;}});
  assert.equal(prevented,true);api.save();assert.equal(store[key],undefined);
});
