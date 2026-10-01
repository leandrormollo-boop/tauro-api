/* Gestión del envío, siempre con endpoints propios y validación del servidor. */
(function () {
  'use strict';
  var hub = document.querySelector('[data-shipment-hub]');
  function tab(name) {
    if (!hub) return;
    hub.querySelectorAll('[data-hub-panel]').forEach(function (p) {p.hidden = p.dataset.hubPanel !== name;});
    hub.querySelectorAll('[data-hub-tab]').forEach(function (b) {b.setAttribute('aria-pressed', String(b.dataset.hubTab === name));});
    var cta = hub.querySelector('[data-hub-pickup-cta]');
    if (cta) cta.hidden = name === 'retiro';
  }
  if (hub) {
    hub.addEventListener('click', function (e) {var b = e.target.closest('[data-hub-tab]'); if (b) tab(b.dataset.hubTab);});
    if (new URL(location.href).searchParams.get('retiro') === '1') tab('retiro');
  }
  var embedded = document.body.classList.contains('portal-embedded');
  if (embedded) {
    document.addEventListener('submit', function (event) {
      if (event.defaultPrevented || event.target.method.toLowerCase() !== 'post') return;
      if (event.target.dataset.managerBusy) {event.preventDefault(); return;}
      event.target.dataset.managerBusy = '1';
      event.target.querySelectorAll('button[type=submit]').forEach(function (b) {b.disabled = true; b.textContent = 'Procesando…';});
      if (window.parent !== window) window.parent.postMessage({type:'tauro:shipment-busy'}, location.origin);
    });
    return;
  }
  var dialog = document.getElementById('shipment-manager-dialog');
  if (!dialog) return;
  var frame = dialog.querySelector('[data-manager-frame]'), close = dialog.querySelector('[data-manager-close]');
  var busy = false, opener = null, previousOverflow = '', busyTimer;
  function open(href, from) {
    var url = new URL(href, location.origin);
    if (url.origin !== location.origin || !/^\/portal\/envios\/\d+\/gestion$/.test(url.pathname)) return;
    if (!dialog.showModal) {location.href = url.href; return;}
    opener = from || document.activeElement;
    url.searchParams.set('ventana', '1');
    frame.src = url.pathname + url.search;
    previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    dialog.showModal(); close.focus();
  }
  document.addEventListener('click', function (event) {
    var link = event.target.closest('[data-shipment-manage]');
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault(); open(link.href, link);
  });
  window.addEventListener('message', function (event) {
    if (event.origin === location.origin && event.source === frame.contentWindow && event.data && event.data.type === 'tauro:shipment-busy') {
      busy = true; close.disabled = true;
      clearTimeout(busyTimer);
      // Una conexión interrumpida no debe encerrar al cliente. Al cerrar se
      // relee el estado; nunca se reenvía automáticamente la operación.
      busyTimer = setTimeout(function () {busy = false; close.disabled = false;}, 60000);
    }
  });
  frame.addEventListener('load', function () {clearTimeout(busyTimer); busy = false; close.disabled = false;});
  close.addEventListener('click', function () {if (!busy) dialog.close();});
  dialog.addEventListener('cancel', function (event) {if (busy) event.preventDefault();});
  dialog.addEventListener('close', function () {
    clearTimeout(busyTimer);
    document.body.style.overflow = previousOverflow;
    frame.removeAttribute('src');
    if (opener && opener.isConnected) opener.focus();
    // Leer de nuevo después de una operación evita dejar un precio/estado viejo debajo.
    location.reload();
  });
  var url = new URL(location.href);
  if (url.searchParams.get('gestionar') === '1' && /^\/portal\/envios\/\d+$/.test(url.pathname)) {
    url.searchParams.delete('gestionar');
    history.replaceState(history.state, '', url.pathname + url.search + url.hash);
    open(url.pathname + '/gestion' + url.search);
  }
})();
