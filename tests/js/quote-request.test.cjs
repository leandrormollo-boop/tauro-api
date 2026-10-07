const test = require('node:test');
const assert = require('node:assert/strict');
const make = require('../../static/js/portal-cotizador.js');
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
function harness() {
  const pending=[], rendered=[], statuses=[];
  let ready=true, fingerprint='A';
  const c=make({delay:10,ready:()=>ready,invalidate:()=>statuses.push('invalid'),loading:()=>statuses.push('loading'),
    fingerprint:()=>fingerprint,
    fetch:(signal,onProgress)=>new Promise((resolve,reject)=>pending.push({resolve,reject,signal,onProgress})),
    render:(value,complete)=>rendered.push([value,complete]),error:error=>statuses.push(error.message)});
  return {c,pending,rendered,statuses,setReady:v=>ready=v,setFingerprint:v=>fingerprint=v};
}
test('ediciones rápidas consultan una sola vez después de la pausa', async()=>{
  const h=harness();h.c.changed();h.c.changed();h.c.changed();await wait(25);
  assert.equal(h.pending.length,1);h.pending[0].resolve('actual');await wait(0);assert.deepEqual(h.rendered,[['actual',true]]);
});
test('respuesta vieja no reaparece después de editar ni de una nueva respuesta', async()=>{
  const h=harness();h.c.run();h.setFingerprint('B');h.c.changed();assert.equal(h.pending[0].signal.aborted,true);
  await wait(25);h.pending[1].resolve('nueva');await wait(0);h.pending[0].resolve('vieja');await wait(0);
  assert.deepEqual(h.rendered,[['nueva',true]]);
});
test('borrar un dato invalida la tarifa y no consulta con campos incompletos', async()=>{
  const h=harness();h.c.run();h.setReady(false);h.c.changed();h.pending[0].resolve('vieja');await wait(25);
  assert.equal(h.pending.length,1);assert.deepEqual(h.rendered,[]);
});
test('cerrar o cambiar ámbito ignora la consulta pendiente y retomar consulta de nuevo', async()=>{
  const h=harness();h.c.run();h.c.pause();h.pending[0].resolve('vieja');await wait(0);assert.deepEqual(h.rendered,[]);
  assert.equal(h.statuses.at(-1),'invalid');
  h.c.resume();await wait(25);assert.equal(h.pending.length,2);h.pending[1].resolve('nueva');await wait(0);assert.deepEqual(h.rendered,[['nueva',true]]);
});
test('un fallo permite reintentar sin perder los datos', async()=>{
  const h=harness();h.c.run();h.pending[0].reject(new Error('falló'));await wait(0);
  assert.ok(h.statuses.includes('falló'));h.c.run();h.pending[1].resolve('ok');await wait(0);assert.deepEqual(h.rendered,[['ok',true]]);
});

test('input y change idénticos no repiten una consulta en vuelo ni un resultado vigente', async()=>{
  const h=harness();h.c.changed();await wait(25);assert.equal(h.pending.length,1);
  h.c.changed();assert.equal(h.pending[0].signal.aborted,false);assert.equal(h.pending.length,1);
  h.pending[0].resolve('actual');await wait(0);h.c.changed();await wait(25);
  assert.equal(h.pending.length,1);assert.deepEqual(h.rendered,[['actual',true]]);
  h.setFingerprint('B');h.c.changed();await wait(25);assert.equal(h.pending.length,2);
});

test('cada parcial se renderiza y una edición descarta parciales viejos', async()=>{
  const h=harness();h.c.run();
  assert.equal(h.pending[0].onProgress('DHL',false),true);
  h.setFingerprint('B');h.c.changed();
  assert.equal(h.pending[0].onProgress('DHL + FedEx',true),false);
  await wait(25);h.pending[1].onProgress('UPS',false);h.pending[1].onProgress('UPS + DHL',true);
  h.pending[1].resolve();await wait(0);
  assert.deepEqual(h.rendered,[['DHL',false],['UPS',false],['UPS + DHL',true]]);
});

test('submit puede forzar una consulta inmediata una vez liberada la ubicación auxiliar', async()=>{
  const h=harness();h.setReady(false);h.c.run(true);assert.equal(h.pending.length,0);
  h.setReady(true);h.c.run(true);assert.equal(h.pending.length,1);
});

function streamed(bytes, cuts) {
  const chunks=[];let start=0;
  cuts.concat(bytes.length).forEach(end=>{chunks.push(bytes.slice(start,end));start=end;});
  return {body:new ReadableStream({start(controller){chunks.forEach(chunk=>controller.enqueue(chunk));controller.close();}})};
}

test('NDJSON recompone líneas y unicode aunque los bytes lleguen cortados', async()=>{
  const text='{"html":"<p>Córdoba</p>","complete":false}\n{"html":"<p>São Paulo</p>","complete":true}\n';
  const bytes=new TextEncoder().encode(text), seen=[];
  await make.readNdjson(streamed(bytes,[7,19,31,48,63]),event=>seen.push(event));
  assert.deepEqual(seen,[
    {html:'<p>Córdoba</p>',complete:false},
    {html:'<p>São Paulo</p>',complete:true},
  ]);
});

test('NDJSON incompleto falla aunque haya entregado un parcial', async()=>{
  const bytes=new TextEncoder().encode('{"html":"<p>DHL</p>","complete":false}\n');
  const seen=[];
  await assert.rejects(make.readNdjson(streamed(bytes,[3,11]),event=>seen.push(event)),/interrumpió/);
  assert.equal(seen.length,1);
});

test('NDJSON libera el reader después del último evento complete', async()=>{
  const bytes=new TextEncoder().encode('{"html":"<p>Final</p>","complete":true}\n');
  let cancelled=false,released=false,reads=0;
  const response={body:{getReader:()=>({
    read:async()=>reads++?{done:true}:{done:false,value:bytes},
    cancel:async()=>{cancelled=true;},releaseLock:()=>{released=true;},
  })}};
  const seen=[];await make.readNdjson(response,event=>seen.push(event));
  assert.equal(cancelled,false);assert.equal(released,true);
  assert.deepEqual(seen,[{html:'<p>Final</p>',complete:true}]);
});

test('NDJSON ignora eventos posteriores al cierre lógico', async()=>{
  const text='{"html":"<p>Final</p>","complete":true}\n{"html":"<p>Tardío</p>","complete":false}\n';
  const seen=[];await make.readNdjson(streamed(new TextEncoder().encode(text),[]),event=>seen.push(event));
  assert.deepEqual(seen,[{html:'<p>Final</p>',complete:true}]);
});

test('NDJSON conserva el cierre lógico si el transporte falla después',async()=>{
  const bytes=new TextEncoder().encode('{"html":"<p>Final</p>","complete":true}\n');
  let reads=0,released=false;
  const response={body:{getReader:()=>({
    read:async()=>reads++?Promise.reject(new Error('socket cerrado')):{done:false,value:bytes},
    cancel:async()=>{},releaseLock:()=>{released=true;},
  })}};
  const seen=[];await make.readNdjson(response,event=>seen.push(event));
  assert.equal(released,true);assert.deepEqual(seen,[{html:'<p>Final</p>',complete:true}]);
});

test('NDJSON cancela y libera el reader si una línea es inválida', async()=>{
  let cancelled=false,released=false,read=false;
  const response={body:{getReader:()=>({
    read:async()=>read?{done:true}:{done:false,value:new TextEncoder().encode('{mal}\n')},
    cancel:async()=>{cancelled=true;},releaseLock:()=>{released=true;},
  })}};
  await assert.rejects(make.readNdjson(response,()=>{}),/interrumpió/);read=true;
  assert.equal(cancelled,true);assert.equal(released,true);
});

function reseller(carrier,quoteId,value,focused=false) {
  const document={activeElement:null};
  const card={dataset:{carrier}};
  const quote={value:quoteId};
  const price={value,ownerDocument:document,selectionStart:1,selectionEnd:4,isConnected:true,
    focused:false,selected:null,focus(){this.focused=true;},setSelectionRange(a,b){this.selected=[a,b];}};
  if(focused)document.activeElement=price;
  const form={closest:()=>card,querySelector:selector=>selector.includes('quote_id')?quote:price};
  return {form,price};
}
function resellerContainer(...rows){return {querySelectorAll:()=>rows.map(row=>row.form)};}

test('un parcial conserva el precio reseller sólo para el mismo carrier y quote_id',()=>{
  const current=reseller('dhl','quote-1','123.456,78',true);
  const same=reseller('dhl','quote-1','100000');
  const other=reseller('fedex','quote-2','200000');
  const restore=make.copyResellerInputs(resellerContainer(current),resellerContainer(same,other));
  assert.equal(same.price.value,'123.456,78');assert.equal(other.price.value,'200000');
  restore();assert.equal(same.price.focused,true);assert.deepEqual(same.price.selected,[1,4]);
});

test('el deadline de una consulta abortada no cancela la siguiente',async()=>{
  const old=new AbortController(),current=new AbortController();let expired=[];
  make.armDeadline(old.signal,()=>expired.push('old'),5);
  const clearCurrent=make.armDeadline(current.signal,()=>expired.push('current'),8);
  old.abort();await wait(15);clearCurrent();
  assert.deepEqual(expired,['current']);
});

test('un cierre con error conserva la tarifa pero habilita volver a consultar',()=>{
  function block(price,error){return {querySelector:selector=>selector==='.uq-price'?(price?{}:null):(error?{}:null)};}
  assert.deepEqual(make.resultState(block(true,false),false),{
    hasPrice:true,current:true,button:'Ver tarifas',status:'Consultando otros operadores…',
  });
  assert.deepEqual(make.resultState(block(true,true),true),{
    hasPrice:true,current:false,button:'Volver a consultar',
    status:'Podés usar las tarifas recibidas o volver a consultar.',
  });
});

test('un error de transporte posterior no vuelve a renderizar sobre el parcial',async()=>{
  const h=harness();h.c.run();h.pending[0].onProgress('DHL',false);
  h.pending[0].reject(new Error('stream cortado'));await wait(0);
  assert.deepEqual(h.rendered,[['DHL',false]]);assert.ok(h.statuses.includes('stream cortado'));
});
