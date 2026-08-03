// Calculadora de peso volumétrico (/calculadora-volumetrica).
// Vive como archivo estático (no inline) para que la página cumpla la CSP:
// script-src 'self' — nada de JavaScript incrustado en el HTML.
(function () {
  var ids = ["cv-l", "cv-a", "cv-h", "cv-p"];
  function calc() {
    var l = parseFloat(document.getElementById("cv-l").value) || 0;
    var a = parseFloat(document.getElementById("cv-a").value) || 0;
    var h = parseFloat(document.getElementById("cv-h").value) || 0;
    var p = parseFloat(document.getElementById("cv-p").value) || 0;
    var out = document.getElementById("cv-out");
    if (!(l && a && h)) {
      out.innerHTML = "Completá las medidas y te mostramos el peso que te van a facturar.";
      out.style.color = "#6f6a85";
      return;
    }
    var vol = l * a * h / 5000;
    var fact = Math.max(vol, p);
    var manda = vol > p ? "el volumétrico" : "el peso real";
    out.style.color = "#d7d3e4";
    out.innerHTML = "Peso volumétrico: <b style='color:#fff'>" + vol.toFixed(2) +
      " kg</b> · Peso real: <b style='color:#fff'>" + (p || 0).toFixed(2) + " kg</b>" +
      "<div style='margin-top:8px;font-size:18px;color:#a78bfa;'>Se factura: <b>" +
      fact.toFixed(2) + " kg</b> <span style='font-size:13px;color:#6f6a85;'>(manda " +
      manda + ")</span></div>";
  }
  ids.forEach(function (id) {
    document.getElementById(id).addEventListener("input", calc);
  });
})();
