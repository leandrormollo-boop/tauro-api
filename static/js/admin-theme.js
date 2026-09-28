/* Admin appearance is independent from the customer portal preference. */
(function () {
  'use strict';
  var key = 'tauro.admin.theme';
  var root = document.documentElement;
  function apply(theme) {
    root.dataset.theme = theme === 'light' ? 'light' : 'dark';
    root.style.colorScheme = root.dataset.theme;
    document.querySelectorAll('[data-admin-theme]').forEach(function (button) {
      button.setAttribute('aria-pressed', String(button.dataset.adminTheme === root.dataset.theme));
    });
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = root.dataset.theme === 'light' ? '#f8f7fc' : '#0c0a14';
  }
  var stored;
  try { stored = localStorage.getItem(key); } catch (_) {}
  apply(stored);
  document.addEventListener('DOMContentLoaded', function () { apply(root.dataset.theme); });
  document.addEventListener('click', function (event) {
    var button = event.target.closest('[data-admin-theme]');
    if (!button) return;
    var theme = button.dataset.adminTheme;
    if (theme !== 'light' && theme !== 'dark') return;
    apply(theme);
    try { localStorage.setItem(key, theme); } catch (_) {}
  });
  window.addEventListener('storage', function (event) {
    if (event.key === key) apply(event.newValue);
  });
})();
