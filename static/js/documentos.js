/* Visor privado, sin iframes ni servicios externos. PDF.js se carga a demanda. */
(() => {
  'use strict';
  const dialog = document.getElementById('document-viewer');
  if (!dialog || typeof dialog.showModal !== 'function') return;
  const find = name => dialog.querySelector(`[data-viewer-${name}]`);
  const canvas = find('canvas'), stage = find('stage'), status = find('status');
  const pagination = find('pagination'), pages = find('pages'), transcript = find('text');
  const prev = find('prev'), next = find('next'), zoomIn = find('in'), zoomOut = find('out'), fit = find('fit');
  let sequence = 0, renderSequence = 0, abort, pdfTask, pdf, picture, renderTask;
  let pageNumber = 1, zoom = 1, opener, scrollX = 0, scrollY = 0, originalStyle;
  let library, renderQueue = Promise.resolve();
  const enableControls = enabled => {
    for (const button of [prev, next, zoomIn, zoomOut, fit]) button.disabled = !enabled;
    if (enabled) {
      prev.disabled = !pdf || pageNumber <= 1;
      next.disabled = !pdf || pageNumber >= pdf.numPages;
      zoomOut.disabled = zoom <= .5;
      zoomIn.disabled = zoom >= 3;
    }
  };
  const message = text => { status.textContent = text; status.hidden = false; canvas.hidden = true; };
  function cancelDocument() {
    abort?.abort(); abort = null;
    renderTask?.cancel(); renderTask = null;
    // Release worker/fonts/bitmaps even if it was closed while still loading.
    if (pdfTask) { pdfTask.destroy().catch(() => {}); pdfTask = null; }
    pdf = null;
    picture?.close(); picture = null;
    transcript.textContent = '';
  }
  function close() {
    ++sequence; ++renderSequence;
    cancelDocument();
    canvas.width = canvas.height = 1;
    if (originalStyle !== undefined) {
      document.body.style.cssText = originalStyle;
      originalStyle = undefined;
      window.scrollTo({ left: scrollX, top: scrollY, behavior: 'instant' });
      opener?.focus({ preventScroll: true });
    }
  }
  dialog.addEventListener('close', close);
  find('close').addEventListener('click', () => dialog.close());
  let backdropDown = false;
  dialog.addEventListener('pointerdown', event => { backdropDown = event.target === dialog; });
  dialog.addEventListener('click', event => {
    if (backdropDown && event.target === dialog) dialog.close();
    backdropDown = false;
  });
  function render() {
    const current = sequence, revision = ++renderSequence;
    renderTask?.cancel();
    const number = pageNumber, scale = zoom;
    // Serialize draws: PDF.js cannot paint two pages onto the same canvas.
    renderQueue = renderQueue.catch(() => {}).then(async () => {
      if (current !== sequence || revision !== renderSequence || !dialog.open || (!pdf && !picture)) return;
      enableControls(false);
      message('Cargando página…');
      const page = pdf ? await pdf.getPage(number) : null;
      if (current !== sequence || revision !== renderSequence) return;
      const natural = page ? page.getViewport({ scale: 1 }) : picture;
      const available = Math.max(160, stage.clientWidth - (window.innerWidth <= 600 ? 24 : 48));
      const availableHeight = Math.max(160, stage.clientHeight - (window.innerWidth <= 600 ? 24 : 48));
      const factor = Math.min(available / natural.width, availableHeight / natural.height, 1.5) * scale;
      const width = natural.width * factor, height = natural.height * factor;
      // Evita canvases gigantes incluso en páginas de tamaño atípico.
      const ratio = Math.min(window.devicePixelRatio || 1, 2, Math.sqrt(8_000_000 / (width * height)), 8192 / Math.max(width, height));
      canvas.width = Math.max(1, Math.floor(width * ratio));
      canvas.height = Math.max(1, Math.floor(height * ratio));
      canvas.style.width = `${Math.round(width)}px`;
      canvas.style.height = `${Math.round(height)}px`;
      const context = canvas.getContext('2d', { alpha: false });
      if (page) {
        renderTask = page.render({ canvasContext: context, viewport: page.getViewport({ scale: factor }), transform: [ratio, 0, 0, ratio, 0, 0], background: 'white' });
        await renderTask.promise;
        if (current !== sequence || revision !== renderSequence) return;
        renderTask = null;
        page.getTextContent().then(text => {
          if (current === sequence && revision === renderSequence) transcript.textContent = text.items.map(item => item.str || '').join(' ').slice(0, 100000);
        }).catch(() => {});
      } else {
        context.fillStyle = 'white'; context.fillRect(0, 0, canvas.width, canvas.height);
        context.drawImage(picture, 0, 0, canvas.width, canvas.height);
      }
      if (current !== sequence || revision !== renderSequence) return;
      canvas.setAttribute('aria-label', `${document.getElementById('document-viewer-title').textContent}${pdf ? `, página ${number} de ${pdf.numPages}` : ''}`);
      canvas.hidden = false; status.hidden = true;
      pagination.hidden = !pdf;
      pages.textContent = pdf ? `${number} / ${pdf.numPages}` : '';
      fit.textContent = scale === 1 ? 'Ajustar' : `${Math.round(scale * 100)}%`;
      enableControls(true);
    }).catch(error => {
      if (current !== sequence || revision !== renderSequence || error?.name === 'RenderingCancelledException') return;
      message('No pudimos mostrar esta página. Podés descargar el archivo.');
      enableControls(true);
    });
    return renderQueue;
  }
  async function open(link) {
    ++sequence; ++renderSequence;
    cancelDocument();
    const current = sequence;
    opener = link;
    pageNumber = 1; zoom = 1;
    document.getElementById('document-viewer-title').textContent = link.dataset.documentTitle;
    document.getElementById('document-viewer-detail').textContent = link.dataset.documentDetail || '';
    find('download').href = link.dataset.documentDownload;
    find('download').textContent = link.dataset.documentDownloadLabel || 'Descargar';
    pagination.hidden = true; fit.textContent = 'Ajustar';
    enableControls(false); message('Abriendo documento…');
    scrollX = window.scrollX; scrollY = window.scrollY;
    originalStyle = document.body.style.cssText;
    const scrollbar = window.innerWidth - document.documentElement.clientWidth;
    document.body.style.position = 'fixed';
    document.body.style.top = `-${scrollY}px`;
    document.body.style.left = `-${scrollX}px`;
    document.body.style.width = '100%';
    document.body.style.paddingRight = `${scrollbar}px`;
    dialog.showModal();
    stage.scrollTop = stage.scrollLeft = 0;
    abort = new AbortController();
    let protectedDocument = false;
    const loadingTimeout = setTimeout(() => {
      if (current !== sequence) return;
      ++sequence;
      cancelDocument();
      message('El documento está tardando demasiado. Cerrá el visor y probá nuevamente o descargalo.');
    }, 20000);
    try {
      const response = await fetch(link.dataset.documentUrl, { credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: abort.signal });
      if (!response.ok) {
        message(response.status === 404 ? 'Este documento ya no está disponible.' : 'No pudimos abrir el documento. Probá nuevamente o descargalo.');
        return;
      }
      const mime = (response.headers.get('Content-Type') || '').split(';')[0];
      const blob = await response.blob();
      if (current !== sequence) return;
      if (mime === 'application/pdf') {
        library ||= import('/static/vendor/pdfjs/pdf.mjs?v=6.3.289').catch(error => { library = null; throw error; });
        const pdfjs = await library;
        const buffer = await blob.arrayBuffer();
        if (current !== sequence) return;
        pdfjs.GlobalWorkerOptions.workerSrc = '/static/vendor/pdfjs/pdf.worker.mjs?v=6.3.289';
        const task = pdfTask = pdfjs.getDocument({
          data: new Uint8Array(buffer), isEvalSupported: false, disableFontFace: true,
          useWasm: false, cMapUrl: '/static/vendor/pdfjs/cmaps/', cMapPacked: true,
          standardFontDataUrl: '/static/vendor/pdfjs/standard_fonts/',
          wasmUrl: '/static/vendor/pdfjs/wasm/',
        });
        // Los originales siguen disponibles para PDFs protegidos por contraseña.
        task.onPassword = () => {
          if (current !== sequence) return;
          protectedDocument = true;
          clearTimeout(loadingTimeout);
          message('Este PDF tiene contraseña. Descargalo para abrirlo en tu equipo.');
          task.destroy().catch(() => {});
        };
        const loaded = await task.promise;
        if (current !== sequence) { await loaded.destroy(); return; }
        pdf = loaded;
      } else if (['image/jpeg', 'image/png', 'image/webp'].includes(mime)) {
        const loaded = await createImageBitmap(blob);
        if (current !== sequence) { loaded.close(); return; }
        picture = loaded;
      } else {
        throw new Error('Formato no compatible');
      }
      await render();
    } catch (error) {
      if (current !== sequence || error.name === 'AbortError') return;
      message(protectedDocument ? 'Este PDF tiene contraseña. Descargalo para abrirlo en tu equipo.' : 'No pudimos mostrar el documento. Podés descargarlo para abrirlo en tu equipo.');
    } finally {
      clearTimeout(loadingTimeout);
    }
  }
  document.addEventListener('click', event => {
    const link = event.target.closest('[data-document-open]');
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault(); open(link);
  });
  prev.addEventListener('click', () => { if (pdf && pageNumber > 1) { --pageNumber; stage.scrollTop = 0; render(); } });
  next.addEventListener('click', () => { if (pdf && pageNumber < pdf.numPages) { ++pageNumber; stage.scrollTop = 0; render(); } });
  zoomOut.addEventListener('click', () => { zoom = Math.max(.5, zoom - .25); render(); });
  zoomIn.addEventListener('click', () => { zoom = Math.min(3, zoom + .25); render(); });
  fit.addEventListener('click', () => { zoom = 1; stage.scrollTop = stage.scrollLeft = 0; render(); });
  let resizeTimer;
  window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { if (dialog.open) render(); }, 150); });
  // Miniaturas: visibles únicamente y como máximo dos solicitudes simultáneas.
  // Los <details> cerrados no cargan archivos. Un fallo deja un ícono legible.
  const queue = [], registered = new WeakSet(); let active = 0;
  function drain() {
    while (active < 2 && queue.length) {
      const img = queue.shift();
      if (!img.isConnected) continue;
      ++active;
      let finished = false;
      const finish = () => { if (!finished) { finished = true; clearTimeout(timer); --active; drain(); } };
      const timer = setTimeout(finish, 12000);
      img.addEventListener('load', () => { img.classList.add('is-loaded'); finish(); }, { once: true });
      img.addEventListener('error', finish, { once: true });
      img.src = img.dataset.documentThumb;
    }
  }
  if ('IntersectionObserver' in window) {
    const observer = new IntersectionObserver(entries => {
      for (const entry of entries) if (entry.isIntersecting) { observer.unobserve(entry.target); queue.push(entry.target); }
      drain();
    });
    const register = root => {
      const images = root.matches?.('[data-document-thumb]') ? [root] : root.querySelectorAll?.('[data-document-thumb]') || [];
      for (const img of images) if (!registered.has(img)) { registered.add(img); observer.observe(img); }
    };
    register(document);
    // Cuenta y Mis envíos reemplazan sus filas al filtrar/paginar sin recargar.
    new MutationObserver(records => {
      for (const record of records) {
        record.addedNodes.forEach(register);
        record.removedNodes.forEach(root => {
          if (root.matches?.('[data-document-thumb]')) observer.unobserve(root);
          root.querySelectorAll?.('[data-document-thumb]').forEach(img => observer.unobserve(img));
        });
      }
    }).observe(document.body, { childList: true, subtree: true });
  }
})();
