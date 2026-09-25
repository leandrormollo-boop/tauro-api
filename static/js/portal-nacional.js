/* Pasos cortos: Atrás conserva el formulario; sólo Ver tarifa envía el POST. */
(function () {
  'use strict';
  var form = document.querySelector('[data-national-form]');
  if (!form) return;
  var panels = Array.from(form.querySelectorAll('[data-national-panel]'));
  var nav = form.querySelector('.ns-steps'), back = form.querySelector('[data-national-back]');
  var next = form.querySelector('[data-national-next]'), submit = form.querySelector('[data-national-submit]');
  var status = form.querySelector('[data-national-status]'), current = 1;
  function show(step, focus) {
    current = Math.min(3, Math.max(1, step));
    panels.forEach(function (panel) {panel.hidden = Number(panel.dataset.nationalPanel) !== current;});
    nav.querySelectorAll('button').forEach(function (button) {button.setAttribute('aria-current', Number(button.dataset.nationalStep) === current ? 'step' : 'false');});
    back.hidden = false; back.disabled = current === 1; next.hidden = current === 3; submit.hidden = current !== 3;
    status.textContent = current + ' de 3';
    if (focus) {var heading = panels[current-1].querySelector('h2'); heading.tabIndex=-1; heading.focus({preventScroll:true});}
    if (form.tauroDraft) form.tauroDraft.save();
  }
  if (window.TauroDraft) window.TauroDraft.attach(form, {
    capture:function () {return {step:current};},
    prepare:function (saved) {current=Number(saved.step) || 1;}
  });
  var draftBar = form.querySelector('.draft-bar');
  if (draftBar) form.appendChild(draftBar);
  function valid(panel) {
    var invalid = Array.from(panel.querySelectorAll('input,select')).find(function (input) {
      if (input.dataset.numero && window.TauroNumeros) {
        var parsed = window.TauroNumeros.canonico(input.value, input.dataset.numero);
        var value = parsed.error ? NaN : Number(parsed.valor);
        var max = input.name === 'cantidad_bultos' ? 20 : input.name === 'peso_kg' ? 1000 : input.name === 'valor_declarado_ars' ? 999999999 : 300;
        input.setCustomValidity(!Number.isFinite(value) || value <= 0 ? 'Ingresá un valor mayor que cero.' : value > max ? 'El máximo es ' + max + '.' : '');
      }
      return !input.validity.valid;
    });
    if (!invalid) return true;
    show(Number(panel.dataset.nationalPanel), false); invalid.reportValidity(); return false;
  }
  function go(step) {
    if (step > current && !panels.slice(0,step-1).every(valid)) return;
    show(step, true);
  }
  nav.hidden = false; show(current, false);
  nav.addEventListener('click', function (event) {var button=event.target.closest('[data-national-step]'); if (button) go(Number(button.dataset.nationalStep));});
  next.addEventListener('click', function () {go(current+1);});
  back.addEventListener('click', function () {go(current-1);});
  form.addEventListener('invalid', function (event) {var panel=event.target.closest('[data-national-panel]'); if (panel) show(Number(panel.dataset.nationalPanel), false);}, true);
  form.addEventListener('keydown', function (event) {
    if (event.key==='Enter' && event.target.tagName==='INPUT' && current<3) {event.preventDefault(); go(current+1);}
  });
  form.addEventListener('submit', function (event) {
    if (!panels.every(valid)) {event.preventDefault(); return;}
    if (form.dataset.submitting) {event.preventDefault(); return;}
    form.dataset.submitting='1'; submit.disabled=true; submit.textContent='Consultando…';
  });
  window.addEventListener('pageshow', function () {delete form.dataset.submitting; submit.disabled=false; submit.textContent='Ver tarifa';});
})();
