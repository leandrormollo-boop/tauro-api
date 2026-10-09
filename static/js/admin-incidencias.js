/* Sólo Admin. La alerta nunca emite, reintenta ni dispara llamadas de IA. */
(() => {
  const aviso = document.querySelector('[data-inc-notification]');
  let timer, consultando = false, cerrado = false;
  async function consultar() {
    clearTimeout(timer);
    if (!aviso || cerrado || document.hidden || consultando) return;
    consultando = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch('/admin/incidencias/resumen', {
        credentials: 'same-origin', cache: 'no-store', signal: controller.signal
      });
      if (response.status === 401) { cerrado = true; throw new Error('session'); }
      const datos = await response.json();
      if (!response.ok || !datos.disponible || !Number.isSafeInteger(datos.total)) throw new Error('unavailable');
      aviso.dataset.status = datos.total > 0 ? 'pending' : 'ok';
      aviso.querySelector('[data-inc-count]').textContent = String(datos.total);
      aviso.setAttribute('aria-label', `Incidencias: ${datos.total} pendientes`);
      aviso.title = `${datos.total} incidencias pendientes de revisión`;
    } catch (_) {
      aviso.dataset.status = 'unavailable';
      aviso.querySelector('[data-inc-count]').textContent = '?';
      aviso.setAttribute('aria-label', 'Incidencias: no pudimos consultar las alertas');
      aviso.title = cerrado ? 'Iniciá sesión para ver las alertas' : 'No pudimos consultar las alertas';
    } finally {
      clearTimeout(timeout);
      consultando = false;
      if (!cerrado && !document.hidden) timer = setTimeout(consultar, 45000);
    }
  }
  document.addEventListener('visibilitychange', () => {
    clearTimeout(timer);
    if (!document.hidden) consultar();
  });
  document.querySelectorAll('[data-inc-analysis]').forEach(form => {
    form.addEventListener('submit', () => {
      const button = form.querySelector('button[type="submit"]');
      if (button) { button.disabled = true; button.textContent = 'Revisando…'; }
    });
  });
  consultar();
})();
