const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('static/js/portal-agenda.js','utf8');
function fixture(data){
  const handlers={},docHandlers={},link={},events=[];
  const placeholder={cloneNode(){return this;}};
  const select={dataset:{nationalContact:'destino'},value:'1',options:[placeholder],children:[],replaceChildren(n){this.children=[n];},appendChild(n){this.children.push(n);}};
  const fields={nombre:'Editado por el usuario',numero:'999',paquete:'3'};
  const document={hidden:false,body:{dataset:{draftScope:'portal:A'}},
    querySelectorAll(q){return q==='[data-agenda-manage]'?[{addEventListener(k,fn){link[k]=fn;}}]:q==='[data-national-form]'?[{dispatchEvent(e){events.push(e);}}]:[select];},
    createElement(){return {dataset:{}};},addEventListener(k,fn){docHandlers[k]=fn;}};
  let count=0;
  vm.runInNewContext(source,{document,window:{addEventListener(k,fn){handlers[k]=fn;}},fetch:async()=>{count++;return {ok:true,json:async()=>data};},CustomEvent:class{constructor(type,init){this.type=type;this.detail=init.detail;}}});
  return {handlers,docHandlers,link,select,fields,events,count:()=>count};
}
const data={scope:'portal:A',contactos:[],nacionales:[{id:'1',tipo:'DESTINATARIO',label:'Ana',fields:{localidad:'Wilde'}},{id:'2',tipo:'DESTINATARIO',label:'Nuevo',fields:{localidad:'CABA'}}]};
test('actualiza opciones tras volver sin cambiar selección ni datos manuales',async()=>{
 const f=fixture(data);f.link.click();await f.handlers.focus();
 assert.equal(f.select.children.length,3);assert.equal(f.select.value,'1');
 assert.deepEqual(f.fields,{nombre:'Editado por el usuario',numero:'999',paquete:'3'});
 assert.equal(f.events[0].type,'tauro:agenda');assert.equal(f.count(),1);
});
test('una sesión cambiada no mezcla agendas de cuentas diferentes',async()=>{
 const f=fixture({...data,scope:'portal:B'});f.link.click();await f.handlers.focus();
 assert.equal(f.select.children.length,0);assert.equal(f.events.length,0);
});
test('contacto eliminado quita selección pero conserva datos del envío',async()=>{
 const f=fixture({...data,nacionales:[]});f.link.click();await f.handlers.focus();
 assert.equal(f.select.value,'');assert.equal(f.fields.numero,'999');
});
test('señal de guardado en otra pestaña vuelve a habilitar la actualización',async()=>{
 const f=fixture(data);f.link.click();await f.handlers.focus();
 f.handlers.storage({key:'tauro:agenda:portal:A'});await new Promise(r=>setImmediate(r));
 assert.equal(f.count(),2);
 f.handlers.storage({key:'tauro:agenda:portal:B'});await new Promise(r=>setImmediate(r));
 assert.equal(f.count(),2);
});
