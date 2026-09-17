/* Ayudas optativas. Sin JavaScript, el contenido permanece visible. */
(function () {
  'use strict';
  const initialized = new WeakSet();
  let sequence = 0;
  let active = null;

  function mount(root) {
    if (initialized.has(root)) return;
    initialized.add(root);
    // Las cajas del formulario se clonan: renovar IDs y listeners también
    // cuando el clon contiene una ayuda que ya había sido inicializada.
    const previous = root.querySelector('.tauro-help-panel');
    const panel = document.createElement('span');
    panel.className = 'tauro-help-panel';
    panel.id = 'tauro-help-' + (++sequence);
    panel.setAttribute('role', 'tooltip');
    while ((previous || root).firstChild) panel.append((previous || root).firstChild);
    root.replaceChildren();
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'tauro-help-button';
    button.setAttribute('aria-label', 'Información: ' + root.dataset.help);
    button.setAttribute('aria-describedby', panel.id);
    button.setAttribute('aria-controls', panel.id);
    button.setAttribute('aria-expanded', 'false');
    const icon = document.createElement('span');
    icon.setAttribute('aria-hidden', 'true');
    icon.textContent = 'i';
    button.append(icon);
    root.append(button, panel);
    root.classList.add('tauro-help');
    panel.hidden = true;
    const nativePopover = typeof panel.showPopover === 'function';
    if (nativePopover) panel.setAttribute('popover', 'manual');
    let open = false;
    let pinned = false;
    let hovering = false;
    let dismissed = false;
    let timer;
    let pointerFocus = false;

    function position() {
      if (!open) return;
      if (!root.isConnected) return close();
      const rect = button.getBoundingClientRect();
      if (rect.bottom < 0 || rect.top > window.innerHeight) return close();
      const box = panel.getBoundingClientRect();
      const left = Math.max(12, Math.min(rect.left, window.innerWidth - box.width - 12));
      const below = rect.bottom + 6;
      const top = below + box.height <= window.innerHeight - 12
        ? below : Math.max(12, rect.top - box.height - 6);
      panel.style.left = left + 'px';
      panel.style.top = top + 'px';
    }
    function close() {
      clearTimeout(timer);
      if (open && nativePopover && panel.matches(':popover-open')) panel.hidePopover();
      panel.hidden = true;
      open = false;
      pinned = false;
      button.setAttribute('aria-expanded', 'false');
      if (active && active.root === root) active = null;
    }
    function show() {
      clearTimeout(timer);
      if (dismissed) return;
      if (active && active.root !== root) active.close();
      if (!open) {
        panel.hidden = false;
        if (nativePopover) panel.showPopover();
        open = true;
        button.setAttribute('aria-expanded', 'true');
      }
      active = { root, button, panel, close, position, dismiss: function () { dismissed = true; close(); } };
      position();
    }
    function leave() {
      hovering = false;
      // Permite cruzar el pequeño espacio entre el ícono y la ayuda.
      timer = setTimeout(function () {
        if (!pinned && !hovering && document.activeElement !== button) close();
      }, 160);
    }
    button.addEventListener('pointerenter', function (event) {
      if (event.pointerType === 'touch') return;
      hovering = true;
      dismissed = false;
      show();
    });
    button.addEventListener('pointerleave', leave);
    panel.addEventListener('pointerenter', function () { hovering = true; clearTimeout(timer); });
    panel.addEventListener('pointerleave', leave);
    button.addEventListener('pointerdown', function () { pointerFocus = true; });
    button.addEventListener('pointercancel', function () { pointerFocus = false; });
    button.addEventListener('focus', function () {
      dismissed = false;
      // En una pantalla táctil, el click abre/cierra sin un doble cambio.
      if (!pointerFocus) show();
    });
    button.addEventListener('blur', function () {
      pointerFocus = false;
      dismissed = false;
      if (!hovering) close();
    });
    button.addEventListener('click', function (event) {
      event.preventDefault();
      event.stopPropagation();
      pointerFocus = false;
      if (pinned) { dismissed = true; close(); }
      else { dismissed = false; show(); pinned = true; }
    });
  }

  function scan(node) {
    if (node.nodeType !== 1) return;
    if (node.matches('[data-help]')) mount(node);
    node.querySelectorAll('[data-help]').forEach(mount);
  }
  // React es dueño del contenedor vacío; este módulo, de su contenido.
  window.TauroHelp = {
    mount,
    unmount: function (root) {
      if (active && active.root === root) active.close();
      const panel = root.querySelector('.tauro-help-panel');
      if (panel) root.replaceChildren(...panel.childNodes);
      root.classList.remove('tauro-help');
      initialized.delete(root);
    }
  };
  function start() {
    scan(document.documentElement);
    new MutationObserver(function (changes) {
      changes.forEach(function (change) { change.addedNodes.forEach(scan); });
      if (active && !active.root.isConnected) active.close();
    }).observe(document.body, { childList: true, subtree: true });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && active) {
        // El primer Escape cierra la ayuda, no el formulario modal que la contiene.
        event.preventDefault();
        event.stopPropagation();
        active.dismiss();
      }
    }, true);
    document.addEventListener('pointerdown', function (event) {
      if (active && !active.root.contains(event.target)) active.dismiss();
    });
    document.addEventListener('scroll', function () { if (active) active.position(); }, true);
    window.addEventListener('resize', function () { if (active) active.position(); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
  else start();
})();
