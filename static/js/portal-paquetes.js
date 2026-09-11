/* Embalajes: datos de catálogo como texto, nunca como HTML ejecutable. */
(() => {
  'use strict';
  const state = JSON.parse(document.getElementById('pkg-data').textContent);
  const $ = id => document.getElementById(id);
  const all = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const products = state.productos.filter(p => p.sync_activo && !p.source_deleted_at);
  const boxes = state.paquetes.filter(b => b.activo);
  const recipes = state.combinaciones.filter(r => r.activo);
  const product = id => products.find(p => String(p.id) === String(id));
  const box = id => state.paquetes.find(p => String(p.id) === String(id));
  const num = n => new Intl.NumberFormat('es-AR', {maximumFractionDigits:3}).format(Number(n));
  const money = n => new Intl.NumberFormat('es-AR', {style:'currency',currency:'ARS',maximumFractionDigits:2}).format(Number(n));
  const node = (tag, text, cls) => { const el = document.createElement(tag); if (text != null) el.textContent = text; if (cls) el.className = cls; return el; };
  const action = (label, fn, cls = 'btn btn-ghost') => { const b = node('button', label, cls); b.type = 'button'; b.addEventListener('click', fn); return b; };
  const text = (parent, tag, content, cls) => { const e = node(tag,content,cls); parent.append(e); return e; };
  const showNotice = (message, error = false) => { const el = $('pkg-notice'); el.textContent = message; el.className = 'msg ' + (error ? 'error' : 'success'); el.hidden = false; el.scrollIntoView({block:'nearest',behavior:'smooth'}); };
  const title = p => (p.titulo_tienda || p.alias_interno) + (p.variante_tienda && p.variante_tienda !== 'Default Title' ? ' · ' + p.variante_tienda : '');
  function options(select, values, label, placeholder) {
    select.replaceChildren(); select.append(new Option(placeholder,'',true,true));
    values.forEach(v => select.append(new Option(label(v),String(v.id))));
    select.dispatchEvent(new Event('change',{bubbles:true}));
  }
  function switchTab(name) {
    if (!['embalajes','productos','tiendas','prueba'].includes(name)) name = 'embalajes';
    all('[data-panel]').forEach(e => { e.hidden = e.dataset.panel !== name; });
    all('[data-tab]').forEach(e => { if (e.dataset.tab === name) e.setAttribute('aria-current','page'); else e.removeAttribute('aria-current'); });
    history.replaceState(null,'','#' + name);
  }
  all('[data-tab]').forEach(b => b.addEventListener('click',() => switchTab(b.dataset.tab)));
  switchTab(location.hash.slice(1));
  const associated = new Set(state.asociaciones.filter(a => box(a.paquete_id)?.activo).map(a => String(a.producto_id)));
  [[boxes.length,'embalajes activos'],[products.filter(p => associated.has(String(p.id))).length + ' / ' + products.length,'productos con embalaje'],[recipes.length,'combinaciones guardadas']].forEach(([n,label]) => {
    const c = node('div',null,'pkg-stat'); text(c,'strong',n); text(c,'span',label); $('pkg-stats').append(c);
  });
  async function post(path, body) {
    const response = await fetch('/portal/paquetes/' + path,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    let result; try { result = await response.json(); } catch (_) { throw new Error('No se pudo completar la operación. Recargá el portal e intentá nuevamente.'); }
    if (!response.ok || result.ok === false) throw new Error(typeof result.error === 'string' ? result.error : (typeof result.detail === 'string' ? result.detail : 'No se pudo guardar. Revisá los datos.'));
    return result;
  }
  async function busy(form, fn, errorTarget) {
    const buttons = all('button',form); buttons.forEach(b => b.disabled = true);
    try { await fn(); } catch (err) {
      if (errorTarget) { errorTarget.textContent = err.message; errorTarget.hidden = false; }
      else showNotice(err.message,true);
    } finally { buttons.forEach(b => b.disabled = false); }
  }
  function reload(message) { sessionStorage.setItem('pkg-flash',message); location.reload(); }
  const flash = sessionStorage.getItem('pkg-flash'); if (flash) { sessionStorage.removeItem('pkg-flash'); showNotice(flash); }
  function openBox(b) {
    const form = $('pkg-box-form'); form.reset(); $('pkg-dialog-error').hidden = true;
    if (b) Object.keys(b).forEach(k => { if (form.elements.namedItem(k)) form.elements.namedItem(k).value = b[k]; });
    $('pkg-dialog-title').textContent = b ? 'Editar embalaje' : 'Nuevo embalaje'; $('pkg-dialog').showModal();
  }
  $('pkg-new').addEventListener('click',() => openBox()); $('pkg-close').addEventListener('click',() => $('pkg-dialog').close());
  $('pkg-box-form').addEventListener('submit',e => {
    e.preventDefault(); const f = e.currentTarget; const d = Object.fromEntries(new FormData(f));
    busy(f,async () => { await post('embalajes' + (d.id ? '/' + d.id : ''),d); reload('Embalaje guardado. Ahora podés asociarlo a tus productos.'); },$('pkg-dialog-error'));
  });
  async function archive(tipo,id) {
    if (!confirm(tipo === 'paquete' ? '¿Archivar este embalaje? Los productos asociados necesitarán otra caja para seguir cotizando.' : '¿Archivar esta combinación? Sus productos usarán las otras reglas y sus embalajes individuales.')) return;
    try { await post('archivar',{tipo,id}); reload('Elemento archivado.'); } catch (e) { showNotice(e.message,true); }
  }
  boxes.forEach(b => {
    const card = node('article',null,'pkg-box'); text(card,'span','◇','pkg-box-icon').setAttribute('aria-hidden','true'); text(card,'h3',b.nombre);
    const dims = text(card,'div',`${num(b.largo_cm)} × ${num(b.ancho_cm)} × ${num(b.alto_cm)} `,'pkg-dims'); text(dims,'small','cm');
    const meta = text(card,'div',null,'pkg-meta');
    [['Caja vacía',b.tara_kg],['Protección',b.proteccion_kg],['Máximo caja llena',b.max_kg]].forEach(([label,n]) => { const line = text(meta,'div'); text(line,'span',label); text(line,'strong',num(n)+' kg'); });
    const actions = text(card,'div',null,'pkg-box-actions'); actions.append(action('Editar',() => openBox(b)),action('Archivar',() => archive('paquete',b.id))); $('pkg-boxes').append(card);
  });
  if (!boxes.length) { const empty = text($('pkg-boxes'),'div',null,'pkg-empty'); text(empty,'h3','Guardá tu primer embalaje'); text(empty,'p','Por ejemplo, una caja de 15 × 15 × 10 cm para un reel. Usá tus medidas y pesos reales.'); empty.append(action('+ Nuevo embalaje',() => openBox(), 'btn btn-primary')); }
  all('.pkg-product-select').forEach(s => options(s,products,p => title(p)+' · '+p.alias_interno,'Elegí un producto'));
  all('.pkg-box-select').forEach(s => options(s,boxes,b => `${b.nombre} · ${num(b.largo_cm)}×${num(b.ancho_cm)}×${num(b.alto_cm)} cm`,'Elegí un embalaje'));
  function line(container) {
    const row = node('div',null,'pkg-line'); const select = node('select'); select.required = true; select.name = 'producto_id'; select.setAttribute('aria-label','Producto');
    options(select,products,p => title(p),'Elegí un producto'); const qty = node('input'); qty.type = 'number'; qty.name='cantidad'; qty.min='1';qty.max='100';qty.value='1';qty.required=true;qty.setAttribute('aria-label','Cantidad de unidades');
    const remove = action('✕',() => { if (container.children.length > 1) row.remove(); }); remove.setAttribute('aria-label','Quitar producto'); row.append(select,qty,remove); container.append(row);
  }
  function items(container) { return all('.pkg-line',container).map(r => ({producto_id:r.querySelector('select[name=producto_id]').value,cantidad:r.querySelector('input[name=cantidad]').value})); }
  line($('pkg-recipe-items')); line($('pkg-recipe-items')); line($('pkg-cart-items'));
  $('pkg-add-recipe').addEventListener('click',() => line($('pkg-recipe-items'))); $('pkg-add-cart').addEventListener('click',() => line($('pkg-cart-items')));
  $('pkg-associate').addEventListener('submit',e => {
    e.preventDefault(); const f=e.currentTarget; const d=Object.fromEntries(new FormData(f)); d.confirmado=f.elements.confirmado.checked;
    busy(f,async () => { await post('asociaciones',d); reload('Asociación guardada.'); });
  });
  function editAssociation(a) {
    const f=$('pkg-associate'); ['producto_id','paquete_id','peso_neto_kg','unidades_por_caja'].forEach(k => f.elements.namedItem(k).value=a[k]);
    f.elements.confirmado.checked=false; f.scrollIntoView({behavior:'smooth',block:'center'});
  }
  const associatedSelector = $('pkg-associate').elements.producto_id;
  associatedSelector.addEventListener('change',() => { const a=state.asociaciones.find(a => String(a.producto_id)===associatedSelector.value); if (a) editAssociation(a); else { $('pkg-associate').elements.confirmado.checked=false; } });
  state.asociaciones.forEach(a => {
    const p=product(a.producto_id); if (!p) return; const b=box(a.paquete_id); const row=node('div',null,'pkg-list-row'); const name=text(row,'div',null,'pkg-product-name');
    if (p.imagen_url && /^https:\/\//.test(p.imagen_url)) { const img=node('img'); img.src=p.imagen_url; img.alt='';img.loading='lazy';name.append(img); }
    const detail=text(name,'div');text(detail,'strong',title(p)); text(detail,'small',`${b?.nombre || 'Embalaje no disponible'}${b?.activo ? '' : ' · Archivado'} · hasta ${a.unidades_por_caja} u. · ${num(a.peso_neto_kg)} kg por unidad`);
    row.append(action('Cambiar',() => editAssociation(a))); $('pkg-associations').append(row);
  });
  if (!state.asociaciones.length) text($('pkg-associations'),'p','Todavía no asociaste productos. Sincronizá tu catálogo desde Mis ventas si no aparecen.','pkg-muted');
  $('pkg-combine').addEventListener('submit',e => { e.preventDefault(); const f=e.currentTarget; const d=Object.fromEntries(new FormData(f)); d.confirmado=f.elements.confirmado.checked; d.contenido=items($('pkg-recipe-items')); busy(f,async () => { await post('combinaciones',d); reload('Combinación guardada.'); }); });
  recipes.forEach(r => { const row=node('div',null,'pkg-list-row'); const d=text(row,'div');text(d,'strong',r.nombre);text(d,'p',r.contenido.map(i => `${i.cantidad} × ${product(i.producto_id)?.alias_interno || 'Producto no disponible'}`).join(' + '),'pkg-muted');text(d,'small',`${box(r.paquete_id)?.nombre || 'Embalaje no disponible'} · Prioridad ${r.prioridad}`);row.append(action('Archivar',() => archive('combinacion',r.id)));$('pkg-recipes').append(row); });
  if (!recipes.length) text($('pkg-recipes'),'p','Las ventas mixtas usarán los embalajes individuales hasta que guardes una combinación.','pkg-muted');
  function input(labelText,name,value,type='text') {
    const label=node('label',labelText), control=node('input');control.name=name;control.value=value;control.type=type;if(type==='text') control.inputMode='decimal';label.append(control);return label;
  }
  function check(labelText,name,checked) { const label=node('label',null,'pkg-check');const c=node('input');c.type='checkbox';c.name=name;c.checked=checked;label.append(c,document.createTextNode(labelText));return label; }
  function policy(form,key,cfg) {
    cfg=cfg||{}; const fs=node('fieldset');text(fs,'legend',key==='nacional'?'Nacional · Argentina':'Internacional');
    fs.append(check('Ofrecer este tipo de envío',key+'_habilitado',cfg.habilitado));
    const label=text(fs,'label','Qué paga el comprador');const select=node('select');select.name=key+'_politica';select.setAttribute('aria-label',(key==='nacional'?'Nacional':'Internacional')+': qué paga el comprador');
    [['real','La tarifa de mi cuenta TAURO'],['markup','Tarifa TAURO + un porcentaje'],['fijo','Un importe fijo'],['gratis','Envío gratis']].forEach(([v,t]) => select.append(new Option(t,v)));select.value=cfg.politica||'real';label.append(select);
    const fixed=input('Importe fijo (ARS)',key+'_precio_fijo_ars',cfg.precio_fijo_ars||'0');const markup=input('Porcentaje adicional (%)',key+'_markup_pct',cfg.markup_pct||'0');const threshold=input('Bonificar desde una compra de (ARS)',key+'_gratis_desde_ars',cfg.gratis_desde_ars||'0');text(threshold,'small','Dejá 0 si no querés establecer un mínimo de envío gratis.');fs.append(fixed,markup,threshold);
    const update=() => { fixed.hidden=select.value!=='fijo';markup.hidden=select.value!=='markup';threshold.hidden=select.value==='gratis'; };select.addEventListener('change',update);update();form.append(fs);
  }
  state.tiendas.forEach(t => {
    $('pkg-quote-store').append(new Option(t.dominio,String(t.id)));
    const card=node('article',null,'card');const header=text(card,'div',null,'pkg-store-title');text(header,'h3',t.dominio);text(header,'span',t.plataforma==='tiendanube'?'Medio de envío en Tiendanube':(t.checkout_activo?'Servicio conectado':'Servicio sin activar'),'pkg-badge');
    const form=node('form');form.append(check('Usar mis embalajes para las ventas de esta tienda','usar_paquetes',t.usar_paquetes));
    const columns=node('div',null,'pkg-columns');policy(columns,'nacional',t.nacional);policy(columns,'internacional',t.internacional);
    if(t.plataforma==='tiendanube') { const international=columns.querySelectorAll('fieldset')[1];international.disabled=true;international.querySelector('input[type=checkbox]').checked=false;text(international,'p','Disponible para cotizar en el portal. El medio de envío actual de Tiendanube admite nacional.','pkg-muted'); }
    form.append(columns);const save=node('button','Guardar configuración','btn btn-primary');save.type='submit';form.append(save);
    form.addEventListener('submit',e => { e.preventDefault();const fd=Object.fromEntries(new FormData(form)),d={usar_paquetes:form.elements.usar_paquetes.checked};['nacional','internacional'].forEach(k => { d[k]={habilitado:form.elements.namedItem(k+'_habilitado').checked};['politica','precio_fijo_ars','markup_pct','gratis_desde_ars'].forEach(f => d[k][f]=fd[k+'_'+f]); });busy(form,async () => { await post('tiendas/'+t.id,d);reload('Políticas de envío guardadas. Probá una cotización y verificá el checkout de tu tienda.'); }); });card.append(form);
    if(t.plataforma==='shopify') {
      const actions=text(card,'div',null,'pkg-store-actions');const authorize=node('form');authorize.method='POST';authorize.action=`/portal/paquetes/shopify/${t.id}/autorizar`;const button=node('button','1. Autorizar tarifas en Shopify','btn btn-ghost');button.type='submit';authorize.append(button);actions.append(authorize);
      actions.append(action('2. Conectar servicio',async () => { await busy(actions,async () => { const r=await post(`shopify/${t.id}/activar`,{});reload(r.mensaje); }); }));
      if(t.checkout_activo) actions.append(action('Pausar tarifas',async () => { await busy(actions,async () => { await post(`shopify/${t.id}/pausar`,{});reload('Tarifas pausadas. Verificá que tu tienda tenga otras opciones de envío.'); }); }));
      text(card,'p','Después de conectar, agregá TAURO en Configuración → Envío y entrega de Shopify y probá una compra. Guardar esta pantalla no completa ese paso.','pkg-muted');
    } else text(card,'p','Tiendanube usará esta configuración cuando su medio de envío TAURO esté activo. La integración actual de Tiendanube ofrece envíos nacionales; internacional requiere habilitar ese circuito en la plataforma.','pkg-muted');
    $('pkg-stores').append(card);
  });
  if(!state.tiendas.length) { const e=text($('pkg-stores'),'div',null,'card');text(e,'h3','Vinculá una tienda para ofrecer tarifas');text(e,'p','Podés guardar tus cajas y probar cotizaciones mientras conectás Shopify o Tiendanube.','pkg-muted');const a=text(e,'a','Ir a integraciones','btn btn-primary');a.href='/portal/tienda'; }
  Object.entries(state.origen).forEach(([k,v]) => { const el=$('pkg-quote').elements.namedItem('origen_'+k);if(el)el.value=v||''; });
  $('pkg-quote').elements.destino_pais.value='AR';
  $('pkg-quote').addEventListener('submit',e => {
    e.preventDefault();const f=e.currentTarget;const fd=Object.fromEntries(new FormData(f));const body={items:items($('pkg-cart-items')),valor_ars:fd.valor_ars,tienda_id:fd.tienda_id};
    ['origen','destino'].forEach(k => {body[k]={};['pais','cp','ciudad','estado'].forEach(field => body[k][field]=fd[k+'_'+field]);});
    busy(f,async () => { const target=$('pkg-quote-result');target.replaceChildren(node('p','Calculando cajas y consultando transportistas…','pkg-muted')); const r=await post('cotizar',body);target.replaceChildren();
      text(target,'span',r.ambito==='nacional'?'ENVÍO NACIONAL':'ENVÍO INTERNACIONAL','pkg-eyebrow');text(target,'h3',`${r.plan.cajas_total} ${r.plan.cajas_total===1?'caja':'cajas'} para ${r.plan.unidades_total} ${r.plan.unidades_total===1?'unidad':'unidades'}`);
      text(target,'p',`Peso real: ${num(r.plan.peso_total_kg)} kg · Peso facturable estimado: ${num(r.plan.peso_facturable_kg)} kg`,'pkg-muted');
      r.plan.bultos.forEach((b,i) => {const row=text(target,'div',null,'pkg-plan-box');text(row,'strong',`${i+1}. ${b.nombre} · ${num(b.largo_cm)} × ${num(b.ancho_cm)} × ${num(b.alto_cm)} cm`);text(row,'span',b.contenido.map(c => `${c.cantidad} × ${c.alias}`).join(' + ')+` · ${num(b.peso_kg)} kg`);});
      if(!r.encontrado) text(target,'p',r.motivo,'pkg-note');
      r.opciones.forEach(o => { const row=text(target,'div',null,'pkg-price');const info=text(row,'div');text(info,'b',o.servicio);text(info,'small','Tu tarifa TAURO: '+money(o.precio_tauro_ars));const amount=text(row,'div');text(amount,'strong',Number(o.precio_comprador_ars)===0?'Gratis':money(o.precio_comprador_ars));text(amount,'small','Precio para el comprador'); });
      text(target,'p',r.checkout_habilitado?'La tarifa se devuelve a la tienda cuando el carrito y la zona usan TAURO. Verificá el resultado también desde el checkout.':'Esta prueba no activa tarifas en la tienda. Completá la conexión y sus zonas de envío.','pkg-note');
      text(target,'p','La tarifa puede cambiar si cambia el destino, el contenido, las cajas o el precio del transportista. Revisá el embalaje antes de generar la guía.','pkg-muted');
    });
  });
})();
