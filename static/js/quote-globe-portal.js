/* Presentation only. The canonical quote forms own locations, drafts and prices. */
(function () {
  'use strict';
  var instances = new WeakMap();
  function attach(root) {
    if (instances.has(root) || !window.TauroQuoteMap) return;
    var template = root.querySelector('[data-quote-map-template]');
    if (!template) return;
    var map, scope = root.dataset.quoteActive, changing = false;
    var mobile = window.matchMedia('(max-width: 779px)');
    var disclosure = root.querySelector('.uq-mobile-map');
    var panels = Array.from(root.querySelectorAll('[data-quote-panel]'));
    var shown = {};

    function panelFor(value) { return panels.find(function (panel) { return panel.dataset.quotePanel === value; }); }
    function place() {
      var target = mobile.matches ? root.querySelector('[data-mobile-map-slot]')
        : panelFor(root.dataset.quoteActive).querySelector('[data-quote-map-slot]');
      if (map && map.parentNode !== target) target.appendChild(map);
    }
    function visibility() {
      panels.forEach(function (panel) {
        var key = panel.dataset.quotePanel, results = panel.querySelector('[data-quote-results]');
        var hasResults = Boolean(results.querySelector('.uq-price,.uq-error,.uq-unavailable'));
        if (!hasResults) shown[key] = false;
        var showMap = !mobile.matches && (!hasResults || shown[key]);
        panel.querySelector('[data-quote-map-slot]').hidden = !showMap;
        results.hidden = !mobile.matches && hasResults && Boolean(shown[key]);
        var button = panel.querySelector('[data-quote-show-map]');
        button.hidden = mobile.matches || !hasResults;
        button.textContent = shown[key] ? 'Ver tarifas' : 'Ver mapa';
        button.setAttribute('aria-pressed', String(Boolean(shown[key])));
      });
    }
    function mount() {
      if (map) { window.TauroQuoteMap.dispose(root); map.remove(); }
      var fragment = template.content.cloneNode(true);
      map = fragment.querySelector('[data-quote-map]');
      place();
      window.TauroQuoteMap.attach(root);
      root.classList.toggle('has-quote-globe', root.classList.contains('has-route-map'));
      visibility();
    }
    async function sync() {
      place(); visibility();
      if (changing || scope === root.dataset.quoteActive) return;
      changing = true;
      try {
        // If scope changes repeatedly, finish at the latest requested scope.
        while (scope !== root.dataset.quoteActive) {
          var next = root.dataset.quoteActive;
          map.inert = true;
          await window.TauroQuoteMap.transition(root, next);
          if (next !== root.dataset.quoteActive) continue;
          scope = next; mount();
        }
      } catch (_) {
        // A map failure never disables forms, prices or navigation.
        scope = root.dataset.quoteActive; mount();
      } finally { changing = false; if (map) map.inert = false; }
    }
    panels.forEach(function (panel) {
      var results = panel.querySelector('[data-quote-results]');
      new MutationObserver(visibility).observe(results, {childList:true,subtree:true});
      panel.querySelector('[data-quote-show-map]').addEventListener('click', function () {
        var key = panel.dataset.quotePanel; shown[key] = !shown[key]; visibility();
      });
    });
    mobile.addEventListener('change', function () { place(); visibility(); });
    disclosure.addEventListener('toggle', function () {
      disclosure.querySelector('summary').textContent = disclosure.open ? 'Ocultar mapa de la ruta' : 'Ver mapa de la ruta';
    });
    mount();
    instances.set(root, {sync:sync});
  }
  window.TauroQuoteGlobe = {attach:attach, sync:function (root) { var api=instances.get(root); if(api) api.sync(); }};
})();
