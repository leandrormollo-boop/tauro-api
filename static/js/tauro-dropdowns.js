/* Menús de opciones: cerrar no modifica campos, borradores ni formularios.
   Marcar sólo paneles desplegables con data-dropdown; los acordeones de datos
   y formularios conservan su estado al seguir trabajando fuera de ellos. */
(function (install) {
  if (typeof module === 'object' && module.exports) module.exports = install;
  else install(document);
})(function (doc) {
  'use strict';
  var selector = 'details[data-dropdown]';

  function openMenus() {
    return Array.from(doc.querySelectorAll(selector + '[open]'));
  }

  function closeOutside(event) {
    openMenus().forEach(function (menu) {
      if (!menu.contains(event.target)) menu.open = false;
    });
  }

  // Captura: también funciona si otro botón corta la propagación. No
  // cancelamos el evento: el campo o botón de destino recibe el primer clic.
  doc.addEventListener('pointerdown', closeOutside, true);
  doc.addEventListener('click', closeOutside, true);
  doc.addEventListener('focusin', closeOutside);
  doc.addEventListener('toggle', function (event) {
    var menu = event.target;
    if (!menu.matches || !menu.matches(selector) || !menu.open) return;
    openMenus().forEach(function (other) {
      if (!other.contains(menu) && !menu.contains(other)) other.open = false;
    });
  }, true);
  doc.addEventListener('keydown', function (event) {
    // Los selects o ayudas anidados consumen primero su propio Escape.
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    var menus = openMenus();
    var menu = event.target.closest && event.target.closest(selector + '[open]');
    menu = menu || menus[menus.length - 1];
    if (!menu) return;
    menu.open = false;
    event.preventDefault();
    event.stopPropagation();
    var summary = menu.querySelector('summary');
    if (summary) summary.focus({preventScroll: true});
  });
});
