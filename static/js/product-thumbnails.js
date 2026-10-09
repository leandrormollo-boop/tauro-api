(function () {
  "use strict";

  function marcarSinFoto(imagen) {
    var miniatura = imagen && imagen.closest("[data-product-thumb]");
    if (!miniatura) return;
    miniatura.classList.add("product-thumb--missing");
    if (miniatura.tagName === "BUTTON") {
      miniatura.removeAttribute("data-product-image-open");
      miniatura.setAttribute("aria-disabled", "true");
      miniatura.setAttribute("aria-label", "Sin foto disponible");
    }
  }

  document.addEventListener("error", function (event) {
    if (event.target && event.target.matches("[data-product-image]")) {
      marcarSinFoto(event.target);
    }
  }, true);
  document.querySelectorAll("[data-product-image]").forEach(function (imagen) {
    if (imagen.complete && imagen.naturalWidth === 0) marcarSinFoto(imagen);
  });

  var dialogo;
  var imagenGrande;
  var titulo;

  function obtenerDialogo() {
    if (dialogo) return dialogo;
    dialogo = document.createElement("dialog");
    dialogo.className = "product-image-dialog";
    dialogo.setAttribute("aria-labelledby", "product-image-dialog-title");

    var cabecera = document.createElement("div");
    cabecera.className = "product-image-dialog__head";
    titulo = document.createElement("span");
    titulo.id = "product-image-dialog-title";
    titulo.className = "product-image-dialog__title";

    var cerrar = document.createElement("button");
    cerrar.type = "button";
    cerrar.className = "product-image-dialog__close";
    cerrar.setAttribute("aria-label", "Cerrar imagen");
    cerrar.textContent = "×";
    cerrar.addEventListener("click", function () { dialogo.close(); });

    var cuerpo = document.createElement("div");
    cuerpo.className = "product-image-dialog__body";
    imagenGrande = document.createElement("img");
    imagenGrande.className = "product-image-dialog__image";
    imagenGrande.alt = "";
    cuerpo.appendChild(imagenGrande);
    cabecera.appendChild(titulo);
    cabecera.appendChild(cerrar);
    dialogo.appendChild(cabecera);
    dialogo.appendChild(cuerpo);
    dialogo.addEventListener("click", function (event) {
      if (event.target === dialogo) dialogo.close();
    });
    document.body.appendChild(dialogo);
    return dialogo;
  }

  document.addEventListener("click", function (event) {
    var boton = event.target.closest && event.target.closest("[data-product-image-open]");
    if (!boton || boton.getAttribute("aria-disabled") === "true") return;
    var url = boton.dataset.productImageUrl;
    if (!url) return;
    var modal = obtenerDialogo();
    titulo.textContent = boton.dataset.productImageName || "Imagen del producto";
    imagenGrande.src = url;
    modal.showModal();
  });
})();
