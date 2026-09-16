/* Navegación GET progresiva. Los formularios financieros conservan su flujo. */
(function () {
  if (window.tauroFinanceReady) return;
  window.tauroFinanceReady = true;
  var pending, sequence = 0;
  function panel() { return document.querySelector('[data-finance-panel]'); }
  async function navigate(url, push, focusKey) {
    var target = new URL(url, location.href);
    if (target.origin !== location.origin || target.pathname !== location.pathname) {
      location.assign(target.href); return;
    }
    var current = panel(), status = document.querySelector('[data-finance-status]');
    if (!current) { location.assign(target.href); return; }
    var turn = ++sequence;
    if (pending) pending.abort();
    pending = new AbortController();
    var controller = pending, timedOut = false;
    var timeout = setTimeout(function () { timedOut = true; controller.abort(); }, 15000);
    current.setAttribute('aria-busy', 'true');
    if (status) status.textContent = 'Actualizando…';
    try {
      var response = await fetch(target.href, { credentials: 'same-origin', signal: pending.signal, headers: { Accept: 'text/html' } });
      if (turn !== sequence) return;
      if (response.redirected && new URL(response.url).pathname !== target.pathname) { location.assign(response.url); return; }
      if (!response.ok) throw new Error('No se pudo cargar el control');
      var html = await response.text();
      if (turn !== sequence) return;
      var next = new DOMParser().parseFromString(html, 'text/html').querySelector('[data-finance-panel]');
      if (!next) throw new Error('Respuesta incompleta');
      current.replaceWith(next);
      if (push && target.href !== location.href) history.pushState({}, '', target.href);
      var count = next.querySelector('[data-result-count]');
      if (status) status.textContent = count ? 'Actualizado: ' + count.textContent.trim() + '.' : 'Resumen actualizado.';
      if (focusKey) {
        var focus = Array.from(next.querySelectorAll('[data-focus-key]')).find(function (el) { return el.dataset.focusKey === focusKey; });
        if (focus) focus.focus({ preventScroll: true });
      }
    } catch (error) {
      if (turn !== sequence || (error.name === 'AbortError' && !timedOut)) return;
      // Atrás/Adelante ya cambió la URL: recargar evita mostrar otro filtro
      // bajo esa dirección si falló la actualización parcial.
      if (!push) { location.assign(target.href); return; }
      current.removeAttribute('aria-busy');
      if (status) {
        status.textContent = 'No pudimos actualizar la vista. Los datos anteriores siguen visibles. ';
        var retry = document.createElement('a'); retry.href = target.href; retry.textContent = 'Reintentar'; status.appendChild(retry);
      }
    } finally {
      clearTimeout(timeout);
      if (turn === sequence && panel()) panel().removeAttribute('aria-busy');
    }
  }
  document.addEventListener('click', function (event) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    var link = event.target.closest && event.target.closest('a[data-control-nav]');
    if (!link || !panel() || !panel().contains(link) || link.target || link.hasAttribute('download')) return;
    event.preventDefault(); navigate(link.href, true, link.dataset.focusKey);
  });
  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (event.defaultPrevented || !form.matches('form[data-finance-filter]') || form.method.toLowerCase() !== 'get') return;
    event.preventDefault();
    var url = new URL(form.action, location.href); url.search = new URLSearchParams(new FormData(form)).toString();
    navigate(url.href, true, document.activeElement && document.activeElement.dataset.focusKey);
  });
  document.addEventListener('change', function (event) {
    if (event.target.matches('form[data-finance-filter] select')) event.target.form.requestSubmit();
  });
  window.addEventListener('popstate', function () { if (panel()) navigate(location.href, false); });
})();
