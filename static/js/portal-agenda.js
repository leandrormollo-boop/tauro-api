/* Volver de Mis clientes actualiza las opciones, sin pisar el envío en curso. */
(function () {
  'use strict';
  var dirty = false, busy = false;
  var scope = document.body.dataset.draftScope;
  document.querySelectorAll('[data-agenda-manage]').forEach(function (link) {
    link.addEventListener('click', function () {dirty = true;});
  });
  async function refresh() {
    if (!dirty || busy || document.hidden) return;
    busy = true;
    try {
      var response = await fetch('/portal/agenda', {credentials:'same-origin',cache:'no-store',headers:{Accept:'application/json'}});
      if (!response.ok) throw new Error('agenda');
      var data = await response.json();
      if (data.scope !== scope) throw new Error('scope');
      if (!Array.isArray(data.contactos) || !Array.isArray(data.nacionales)) throw new Error('agenda');
      document.querySelectorAll('[data-national-form]').forEach(function (form) {form.dispatchEvent(new CustomEvent('tauro:agenda', {detail:data}));});
      document.querySelectorAll('[data-agenda-role],[data-national-contact]').forEach(function (select) {
        var selected = select.value, national = !!select.dataset.nationalContact;
        var role = select.dataset.agendaRole || (select.dataset.nationalContact==='origen' ? 'REMITENTE' : 'DESTINATARIO');
        var rows = (national ? data.nacionales : data.contactos).filter(function (row) {return row.tipo===role;});
        var placeholder = select.options[0].cloneNode(true);
        select.replaceChildren(placeholder);
        rows.forEach(function (row) {
          var option = document.createElement('option');
          option.value = String(row.id);
          option.textContent = row.label + ' · ' + (national ? row.fields.localidad : [row.ciudad,row.cp,row.pais].filter(Boolean).join(' '));
          if (!national) Object.keys(row).forEach(function (key) {option.dataset[key]=String(row[key] || '');});
          select.appendChild(option);
        });
        select.value = rows.some(function (row) {return String(row.id)===selected;}) ? selected : '';
        // No change event: existing manual edits and the rest of the draft win.
      });
      dirty = false;
    } catch (error) { /* Keep the existing agenda and retry on the next focus. */ }
    finally {busy = false;}
  }
  window.addEventListener('storage', function (event) {if (event.key==='tauro:agenda:'+scope) {dirty=true;refresh();}});
  document.addEventListener('focusin', refresh);
  window.addEventListener('focus', refresh);
  document.addEventListener('visibilitychange', refresh);
})();
