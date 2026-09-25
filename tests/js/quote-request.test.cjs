const test = require('node:test');
const assert = require('node:assert/strict');
const make = require('../../static/js/portal-cotizador.js');
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
function harness() {
  const pending=[], rendered=[], statuses=[];
  let ready=true;
  const c=make({delay:10,ready:()=>ready,invalidate:()=>statuses.push('invalid'),loading:()=>statuses.push('loading'),
    fetch:signal=>new Promise((resolve,reject)=>pending.push({resolve,reject,signal})),render:value=>rendered.push(value),error:error=>statuses.push(error.message)});
  return {c,pending,rendered,statuses,setReady:v=>ready=v};
}
test('ediciones rápidas consultan una sola vez después de la pausa', async()=>{
  const h=harness();h.c.changed();h.c.changed();h.c.changed();await wait(25);
  assert.equal(h.pending.length,1);h.pending[0].resolve('actual');await wait(0);assert.deepEqual(h.rendered,['actual']);
});
test('respuesta vieja no reaparece después de editar ni de una nueva respuesta', async()=>{
  const h=harness();h.c.run();h.c.changed();assert.equal(h.pending[0].signal.aborted,true);
  await wait(25);h.pending[1].resolve('nueva');await wait(0);h.pending[0].resolve('vieja');await wait(0);
  assert.deepEqual(h.rendered,['nueva']);
});
test('borrar un dato invalida la tarifa y no consulta con campos incompletos', async()=>{
  const h=harness();h.c.run();h.setReady(false);h.c.changed();h.pending[0].resolve('vieja');await wait(25);
  assert.equal(h.pending.length,1);assert.deepEqual(h.rendered,[]);
});
test('cerrar o cambiar ámbito ignora la consulta pendiente y retomar consulta de nuevo', async()=>{
  const h=harness();h.c.run();h.c.pause();h.pending[0].resolve('vieja');await wait(0);assert.deepEqual(h.rendered,[]);
  h.c.resume();await wait(25);assert.equal(h.pending.length,2);h.pending[1].resolve('nueva');await wait(0);assert.deepEqual(h.rendered,['nueva']);
});
test('un fallo permite reintentar sin perder los datos', async()=>{
  const h=harness();h.c.run();h.pending[0].reject(new Error('falló'));await wait(0);
  assert.ok(h.statuses.includes('falló'));h.c.run();h.pending[1].resolve('ok');await wait(0);assert.deepEqual(h.rendered,['ok']);
});
