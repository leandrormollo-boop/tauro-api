const test = require('node:test');
const assert = require('node:assert/strict');
const make = require('../../static/js/quote-locations.js');
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const query = value => ({country:'AR',query:value,mode:'city',province:''});

test('una consulta tras varias letras y sin búsqueda por campos vacíos', async () => {
  const calls = [];
  const c = make({delay:5, fetch:async q => {calls.push(q.query); return q;},result:()=>{},error:()=>{}});
  c.search(query('C')); assert.equal(c.pending(), false);
  c.search(query('CA')); c.search(query('CAB')); c.search(query('CABA'));
  assert.equal(c.pending(),true); await wait(20);
  assert.deepEqual(calls,['CABA']); assert.equal(c.pending(),false);
});
test('una respuesta vieja nunca rellena otra ciudad o país', async () => {
  const pending = [], seen = [];
  const c = make({delay:0,fetch:q=>new Promise(resolve=>pending.push({q,resolve})),result:r=>seen.push(r),error:()=>{}});
  c.search(query('CABA')); await wait(5);
  c.search({...query('Miami'),country:'US'}); await wait(5);
  pending[1].resolve('Miami'); await wait(1); pending[0].resolve('CABA'); await wait(1);
  assert.deepEqual(seen,['Miami']);
});
test('cerrar o cambiar de ámbito impide el autocompletado pendiente', async () => {
  let resolve, rendered = false;
  const c = make({delay:0,fetch:()=>new Promise(r=>resolve=r),result:()=>rendered=true,error:()=>{}});
  c.search(query('CABA')); await wait(5); c.cancel(); resolve('CABA'); await wait(1);
  assert.equal(rendered,false); assert.equal(c.pending(),false);
});
test('si falla la búsqueda no bloquea la carga manual', async () => {
  let failed = false;
  const c = make({delay:0,fetch:async()=>{throw Error('offline');},result:()=>{},error:()=>failed=true});
  c.search(query('CABA')); await wait(5);
  assert.equal(failed,true); assert.equal(c.pending(),false);
});
