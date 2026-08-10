/* ==========================================================
   TAURO UI — micro-interacciones y componentes propios
   ----------------------------------------------------------
   1. TSelect: reemplaza el <select> nativo por un desplegable
      con la identidad TAURO. El select original queda invisible
      debajo (validación del browser + eventos intactos): cambiar
      la opción dispara un 'change' REAL, así que todo el JS
      existente (precio en vivo, prefills, parser) sigue andando.
   2. QtyStep: contadores −/+ para cantidades (chau flechitas
      nativas del input number).
   3. (El feedback de click es CSS puro vía :active — ver tauro.css)
   4. Los componentes sobreviven al clonado de filas (multi-bulto):
      un MutationObserver re-construye lo que llegue clonado.
   ========================================================== */
(function () {
  "use strict";

  var enhancedSelects = new WeakSet();
  var enhancedQty = new WeakSet();
  var tselectSeq = 0;
  var selectValueSetter = Object.getOwnPropertyDescriptor(
    HTMLSelectElement.prototype, "value"
  );

  /* ── 1. TSelect ─────────────────────────────────────────── */

  function labelDe(select) {
    var o = select.options[select.selectedIndex];
    return (o && o.text.trim()) || "Seleccionar";
  }

  function construirTSelect(select) {
    if (enhancedSelects.has(select)) return;
    if (select.multiple || select.hasAttribute("data-no-tselect")) return;

    // Cascarón clonado muerto (cloneNode no copia listeners): rescatar
    // el select y tirar el envoltorio viejo antes de re-construir.
    var viejo = select.closest(".tselect");
    if (viejo && viejo.parentNode) {
      viejo.parentNode.insertBefore(select, viejo);
      viejo.remove();
    }
    enhancedSelects.add(select);

    var wrap = document.createElement("div");
    wrap.className = "tselect";
    select.parentNode.insertBefore(wrap, select);
    wrap.appendChild(select);

    var btn = document.createElement("div");
    btn.className = "tselect-btn";
    btn.setAttribute("aria-hidden", "true");
    var lbl = document.createElement("span");
    lbl.className = "tselect-label";
    lbl.textContent = labelDe(select);
    var caret = document.createElement("span");
    caret.className = "tselect-caret";
    caret.innerHTML = "<svg width='11' height='7' viewBox='0 0 10 6' fill='none'><path d='M1 1l4 4 4-4' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'/></svg>";
    btn.appendChild(lbl);
    btn.appendChild(caret);

    var panel = document.createElement("div");
    panel.className = "tselect-panel";

    var searchInput = null;
    if (select.hasAttribute("data-searchable")) {
      var searchWrap = document.createElement("div");
      searchWrap.className = "tselect-search-wrap";
      searchWrap.innerHTML = "<svg width='14' height='14' viewBox='0 0 24 24' fill='none' aria-hidden='true'><circle cx='11' cy='11' r='7' stroke='currentColor' stroke-width='1.8'/><path d='m20 20-4-4' stroke='currentColor' stroke-width='1.8' stroke-linecap='round'/></svg>";
      searchInput = document.createElement("input");
      searchInput.type = "search";
      searchInput.className = "tselect-search";
      searchInput.placeholder = select.dataset.searchPlaceholder || "Buscar país o código";
      searchInput.setAttribute("aria-label", searchInput.placeholder);
      searchInput.autocomplete = "off";
      searchWrap.appendChild(searchInput);
      panel.appendChild(searchWrap);
    }

    var optionsBox = document.createElement("div");
    optionsBox.className = "tselect-options";
    optionsBox.setAttribute("role", "listbox");
    optionsBox.id = (select.id ? select.id + "-options" : "tselect-options-" + (++tselectSeq));
    if (searchInput) searchInput.setAttribute("aria-controls", optionsBox.id);
    panel.appendChild(optionsBox);

    wrap.appendChild(btn);
    wrap.appendChild(panel);

    function normalizar(texto) {
      var s = String(texto || "").toLowerCase();
      return s.normalize ? s.normalize("NFD").replace(/[\u0300-\u036f]/g, "") : s;
    }

    function armarOpciones(consulta) {
      optionsBox.innerHTML = "";
      var filtro = normalizar(consulta).trim();
      var visibles = 0;
      Array.prototype.forEach.call(select.options, function (opt, i) {
        var buscable = normalizar(opt.text + " " + opt.value);
        if (filtro && buscable.indexOf(filtro) === -1) return;
        visibles += 1;
        var item = document.createElement("div");
        item.className = "tselect-option" +
          (i === select.selectedIndex ? " selected" : "") +
          (opt.disabled ? " disabled" : "");
        item.setAttribute("role", "option");
        item.setAttribute("aria-selected", i === select.selectedIndex ? "true" : "false");
        item.dataset.idx = String(i);
        var check = document.createElement("span");
        check.className = "tselect-check";
        check.textContent = "✓";
        var optionText = document.createElement("span");
        optionText.textContent = opt.text;
        item.appendChild(check);
        item.appendChild(optionText);
        if (!opt.disabled) {
          // mousedown, NO click: el click llega DESPUÉS del blur del select
          // (que cierra el panel), así que con click la selección se perdía
          // — el bug de "elijo de la libreta y no carga nada".
          // preventDefault evita robarle el foco al select (no hay blur).
          item.addEventListener("mousedown", function (e) {
            e.preventDefault();
            e.stopPropagation();
            elegir(i);
            cerrar();
            select.focus({ preventScroll: true });
          });
        }
        optionsBox.appendChild(item);
      });
      if (!visibles) {
        var vacio = document.createElement("div");
        vacio.className = "tselect-empty";
        vacio.textContent = "No encontramos resultados";
        optionsBox.appendChild(vacio);
      }
    }

    function elegir(i) {
      if (i < 0 || i >= select.options.length) return;
      select.selectedIndex = i;
      lbl.textContent = labelDe(select);
      select.dispatchEvent(new Event("change", { bubbles: true }));
    }

    function abrir() {
      if (wrap.classList.contains("open")) return;
      cerrarTodos();
      if (searchInput) searchInput.value = "";
      armarOpciones("");
      wrap.classList.add("open");
      // Dentro de dialogs o cerca del borde inferior, el panel se abre hacia
      // el lado con más espacio y ajusta su alto. Así nunca queda recortado ni
      // obliga a desplazar toda la página para elegir un país.
      var wrapRect = wrap.getBoundingClientRect();
      var clip = wrap.closest("dialog[open]");
      var clipRect = clip ? clip.getBoundingClientRect() : null;
      var viewportTop = window.visualViewport ? window.visualViewport.offsetTop : 0;
      var viewportBottom = viewportTop + (window.visualViewport ? window.visualViewport.height : window.innerHeight);
      var limiteTop = clipRect ? Math.max(viewportTop, clipRect.top) : viewportTop;
      var limiteBottom = clipRect ? Math.min(viewportBottom, clipRect.bottom) : viewportBottom;
      var espacioAbajo = Math.max(0, limiteBottom - wrapRect.bottom - 7);
      var espacioArriba = Math.max(0, wrapRect.top - limiteTop - 7);
      var haciaArriba = espacioAbajo < 220 && espacioArriba > espacioAbajo;
      var espacio = haciaArriba ? espacioArriba : espacioAbajo;
      wrap.classList.toggle("open-up", haciaArriba);
      wrap.style.setProperty("--tselect-space", Math.max(120, Math.min(260, espacio)) + "px");
      var sel = optionsBox.querySelector(".selected");
      if (sel) sel.scrollIntoView({ block: "nearest" });
      if (searchInput) {
        window.setTimeout(function () { searchInput.focus({ preventScroll: true }); }, 0);
      }
    }
    function cerrar() {
      wrap.classList.remove("open", "open-up");
      wrap.style.removeProperty("--tselect-space");
    }

    if (searchInput) {
      searchInput.addEventListener("input", function () {
        armarOpciones(searchInput.value);
      });
      searchInput.addEventListener("keydown", function (e) {
        if (e.key === "Escape") {
          e.preventDefault();
          cerrar();
          select.focus({ preventScroll: true });
        } else if (e.key === "Enter") {
          var primera = optionsBox.querySelector(".tselect-option:not(.disabled)");
          if (!primera) return;
          e.preventDefault();
          elegir(parseInt(primera.dataset.idx, 10));
          cerrar();
          select.focus({ preventScroll: true });
        }
      });
    }

    btn.addEventListener("mousedown", function (e) {
      e.preventDefault();
      select.focus({ preventScroll: true });
      if (wrap.classList.contains("open")) cerrar(); else abrir();
    });

    // Teclado sobre el select invisible (que conserva el foco/tab):
    // manejamos nosotros para que NUNCA se abra el picker nativo.
    select.addEventListener("keydown", function (e) {
      var k = e.key;
      if (k === "Enter" || k === " ") {
        e.preventDefault();
        if (wrap.classList.contains("open")) cerrar(); else abrir();
      } else if (k === "ArrowDown" || k === "ArrowUp") {
        e.preventDefault();
        var d = k === "ArrowDown" ? 1 : -1;
        var i = select.selectedIndex + d;
        while (i >= 0 && i < select.options.length && select.options[i].disabled) i += d;
        elegir(Math.max(0, Math.min(i, select.options.length - 1)));
        if (wrap.classList.contains("open")) armarOpciones(searchInput ? searchInput.value : "");
      } else if (k === "Escape") {
        cerrar();
      } else if (/^[a-zA-Z0-9]$/.test(k)) {
        // salto por letra
        var low = k.toLowerCase();
        for (var j = 1; j <= select.options.length; j++) {
          var idx = (select.selectedIndex + j) % select.options.length;
          if (select.options[idx].text.trim().toLowerCase().indexOf(low) === 0) {
            elegir(idx);
            if (wrap.classList.contains("open")) armarOpciones(searchInput ? searchInput.value : "");
            break;
          }
        }
      }
    });
    // El foco puede pasar del select invisible al buscador. Cerramos recién
    // cuando sale de TODO el componente (por Tab o click), no entre ambos.
    wrap.addEventListener("focusout", function () {
      window.setTimeout(function () {
        if (!wrap.contains(document.activeElement)) cerrar();
      }, 0);
    });

    // Cambios de valor por código (parser, prefills, form_input):
    // interceptamos el setter para que la etiqueta nunca quede vieja.
    try {
      Object.defineProperty(select, "value", {
        configurable: true,
        get: function () { return selectValueSetter && Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value").get.call(this); },
        set: function (v) {
          selectValueSetter.set.call(this, v);
          lbl.textContent = labelDe(this);
        },
      });
    } catch (err) { /* sin drama: el listener de change cubre casi todo */ }
    select.addEventListener("change", function () { lbl.textContent = labelDe(select); });
  }

  function cerrarTodos() {
    document.querySelectorAll(".tselect.open").forEach(function (w) {
      w.classList.remove("open", "open-up");
      w.style.removeProperty("--tselect-space");
    });
  }
  document.addEventListener("mousedown", function (e) {
    if (!e.target.closest || !e.target.closest(".tselect")) cerrarTodos();
  });

  /* ── 2. QtyStep (− n +) ─────────────────────────────────── */

  function construirQty(input) {
    if (enhancedQty.has(input)) return;
    var viejo = input.closest(".qtystep");
    if (viejo && viejo.parentNode) {
      viejo.parentNode.insertBefore(input, viejo);
      viejo.remove();
    }
    enhancedQty.add(input);

    var wrap = document.createElement("div");
    wrap.className = "qtystep";
    input.parentNode.insertBefore(wrap, input);

    var menos = document.createElement("button");
    menos.type = "button"; menos.className = "qty-btn"; menos.textContent = "−";
    menos.setAttribute("aria-label", "Restar uno");
    var mas = document.createElement("button");
    mas.type = "button"; mas.className = "qty-btn"; mas.textContent = "+";
    mas.setAttribute("aria-label", "Sumar uno");

    wrap.appendChild(menos);
    wrap.appendChild(input);
    wrap.appendChild(mas);

    function paso(d) {
      var min = parseInt(input.min || "1", 10);
      var max = parseInt(input.max || "999", 10);
      var v = (parseInt(input.value, 10) || min) + d;
      input.value = String(Math.max(min, Math.min(v, max)));
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
    menos.addEventListener("click", function () { paso(-1); });
    mas.addEventListener("click", function () { paso(1); });
  }

  /* El feedback de click ahora es 100% CSS (:active): press + destello.
     Se sacó el ripple JS porque en botones anchos la onda desbordaba
     y parecía un error. */

  /* ── Init + supervivencia al clonado ────────────────────── */

  function mejorarTodo(raiz) {
    (raiz.querySelectorAll ? raiz : document)
      .querySelectorAll("select").forEach(construirTSelect);
    (raiz.querySelectorAll ? raiz : document)
      .querySelectorAll("input[type=number].bulto-cantidad, input[type=number]#cantidad")
      .forEach(construirQty);
  }

  function init() {
    mejorarTodo(document);
    new MutationObserver(function (muts) {
      muts.forEach(function (m) {
        m.addedNodes.forEach(function (n) {
          if (n.nodeType === 1) mejorarTodo(n);
        });
      });
    }).observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
