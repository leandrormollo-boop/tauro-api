const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('templates/portal/envio_nuevo.html', 'utf8');
const selection = source.slice(source.indexOf('  function mostrarSeleccion() {'), source.indexOf('  document.querySelectorAll("#courier-btns [data-courier]").forEach(function (btn) {\n    btn.addEventListener'));
const formatter = source.slice(source.indexOf('  function fmtARS(n) {'), source.indexOf('  function renderLivePrice'));
function node() { return {children:[], appendChild(el) {this.children.push(el);}}; }
function scenario() {
  const liveBox = node();
  Object.defineProperty(liveBox, 'innerHTML', {set(v) {this.html=v;this.children=[];}});
  const context = {document:{createElement:node}, liveBox, intlNoDisp:[], intlOpciones:[],
    courierElegido:'dhl', intlCourier:{value:'dhl'}, precioCotizado:{value:'100'},
    referencia:{value:'RQ-original'}};
  vm.createContext(context); vm.runInContext(formatter + selection, context);
  return context;
}
test('la referencia original sobrevive a nuevas tarifas; sin diferencia no agrega un aviso', () => {
  const ctx = scenario();
  ctx.intlOpciones = [{id:'dhl', nombre:'DHL', precio_ars:125.55, precio_usd:1,
    comparacion_cotizacion:{texto:'Cotizaste $ 100,00 · con los datos completos: $ 125,55', diferencia:'Diferencia: + $ 25,55', motivo:'Motivo: Se confirma al emitir'}}];
  ctx.mostrarSeleccion();
  assert.equal(ctx.precioCotizado.value, '125.55');
  assert.equal(ctx.referencia.value, 'RQ-original');
  assert.equal(ctx.liveBox.children[0].children[0].textContent, ctx.intlOpciones[0].comparacion_cotizacion.texto);
  assert.match(ctx.liveBox.html, /125,55/);
  ctx.intlOpciones[0].precio_ars=100; ctx.intlOpciones[0].comparacion_cotizacion=null;
  ctx.mostrarSeleccion();
  assert.equal(ctx.liveBox.children.length, 0);
  assert.equal(ctx.precioCotizado.value, '100');
});
test('el motivo usa texto y no interpreta HTML', () => {
  const ctx = scenario();
  ctx.intlOpciones=[{id:'dhl',nombre:'DHL',precio_ars:125,precio_usd:1,
    comparacion_cotizacion:{texto:'Comparación',diferencia:'25',motivo:'<img src=x onerror=alert(1)>'}}];
  ctx.mostrarSeleccion();
  assert.equal(ctx.liveBox.children[0].children[2].textContent, '<img src=x onerror=alert(1)>');
  assert.doesNotMatch(ctx.liveBox.html, /onerror/);
});
