// Calculadora de peso volumétrico (/calculadora-volumetrica).
// Vive como archivo estático (no inline) para que la página cumpla la CSP:
// script-src 'self' — nada de JavaScript incrustado en el HTML.
(function () {
  var ids = ["cv-l", "cv-a", "cv-h", "cv-p"];
  function decimal(id) {
    var raw = String(document.getElementById(id).value || "").trim();
    if (!/^[0-9]+([.,][0-9]+)?$/.test(raw)) return 0;
    var partes = raw.split(/[.,]/);
    // En pesos, 10.000 significa diez mil. En una medida sería ambiguo
    // (10 o 10.000); igual que el servidor, no intentamos adivinarlo.
    if (partes.length === 2 && partes[1].length === 3 && Number(partes[0]) !== 0) {
      return 0;
    }
    var value = Number(raw.replace(",", "."));
    return Number.isFinite(value) ? value : 0;
  }
  function calc() {
    var l = decimal("cv-l");
    var a = decimal("cv-a");
    var h = decimal("cv-h");
    var p = decimal("cv-p");
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
