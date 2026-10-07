/* Mantener el campo enfocado por encima de la botonera del paso actual. */
(function () {
  "use strict";
  var wizard = document.getElementById("shipment-wizard");
  if (!wizard) return;

  function mostrarCampo() {
    var campo = document.activeElement;
    if (!campo || !wizard.contains(campo) || !campo.matches("input, select, textarea, .tselect-btn")) return;
    var limite = window.visualViewport
      ? window.visualViewport.offsetTop + window.visualViewport.height
      : window.innerHeight;
    wizard.querySelectorAll(".paso-nav, .submit-bar").forEach(function (barra) {
      var rect = barra.getBoundingClientRect();
      if (rect.height && rect.top < limite) limite = rect.top;
    });
    var rect = campo.getBoundingClientRect();
    if (rect.bottom > limite - 16) {
      window.scrollBy({ top: rect.bottom - limite + 16, behavior: "instant" });
    }
  }

  function ajustar() { window.requestAnimationFrame(mostrarCampo); }
  wizard.addEventListener("focusin", ajustar);
  window.addEventListener("resize", ajustar);
  if (window.visualViewport) window.visualViewport.addEventListener("resize", ajustar);
})();
