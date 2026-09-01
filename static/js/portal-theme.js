(function () {
  "use strict";

  var STORAGE_KEY = "tauro.portal.theme";
  var root = document.documentElement;
  var media = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)");
  var metaColor = document.getElementById("portal-theme-color");

  function savedTheme() {
    try {
      var value = localStorage.getItem(STORAGE_KEY);
      return value === "light" || value === "dark" ? value : "";
    } catch (_) {
      return "";
    }
  }

  function updateControls(theme) {
    var nextLabel = theme === "light" ? "Modo oscuro" : "Modo claro";
    document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
      button.setAttribute("aria-label", "Cambiar a " + nextLabel.toLowerCase());
      button.setAttribute("title", "Cambiar a " + nextLabel.toLowerCase());
      button.setAttribute("aria-pressed", theme === "light" ? "true" : "false");
      button.querySelectorAll("[data-theme-label]").forEach(function (label) {
        label.textContent = nextLabel;
      });
    });
  }

  function applyTheme(theme, persist) {
    if (theme !== "light" && theme !== "dark") return;
    root.classList.add("theme-switching");
    root.dataset.theme = theme;
    root.style.colorScheme = theme;
    if (metaColor) metaColor.content = theme === "light" ? "#f5f3f8" : "#0c0a14";
    if (persist) {
      try { localStorage.setItem(STORAGE_KEY, theme); } catch (_) {}
    }
    updateControls(theme);
    window.setTimeout(function () { root.classList.remove("theme-switching"); }, 220);
  }

  document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
    button.addEventListener("click", function () {
      applyTheme(root.dataset.theme === "light" ? "dark" : "light", true);
    });
  });

  updateControls(root.dataset.theme || "dark");

  if (media && media.addEventListener) {
    media.addEventListener("change", function (event) {
      if (!savedTheme()) applyTheme(event.matches ? "light" : "dark", false);
    });
  }
})();
