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
   3. Ripple: cada click en algo interactivo responde con una
      onda violeta — feedback visual en TODA la web.
   4. Los componentes sobreviven al clonado de filas (multi-bulto):
      un MutationObserver re-construye lo que llegue clonado.
   ========================================================== */
(function () {
  "use strict";

  var enhancedSelects = new WeakSet();
  var enhancedQty = new WeakSet();
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
    panel.setAttribute("role", "listbox");

    wrap.appendChild(btn);
    wrap.appendChild(panel);

    function armarOpciones() {
      panel.innerHTML = "";
      Array.prototype.forEach.call(select.options, function (opt, i) {
        var item = document.createElement("div");
        item.className = "tselect-option" +
          (i === select.selectedIndex ? " selected" : "") +
          (opt.disabled ? " disabled" : "");
        item.setAttribute("role", "option");
        item.dataset.idx = String(i);
        item.innerHTML = "<span class='tselect-check'>✓</span><span>" + opt.text.replace(/</g, "&lt;") + "</span>";
        if (!opt.disabled) {
          item.addEventListener("click", function (e) {
            e.stopPropagation();
            elegir(i);
            cerrar();
          });
        }
        panel.appendChild(item);
      });
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
      armarOpciones();
      wrap.classList.add("open");
      var sel = panel.querySelector(".selected");
      if (sel) sel.scrollIntoView({ block: "nearest" });
    }
    function cerrar() { wrap.classList.remove("open"); }

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
        if (wrap.classList.contains("open")) armarOpciones();
      } else if (k === "Escape") {
        cerrar();
      } else if (/^[a-zA-Z0-9]$/.test(k)) {
        // salto por letra
        var low = k.toLowerCase();
        for (var j = 1; j <= select.options.length; j++) {
          var idx = (select.selectedIndex + j) % select.options.length;
          if (select.options[idx].text.trim().toLowerCase().indexOf(low) === 0) {
            elegir(idx);
            if (wrap.classList.contains("open")) armarOpciones();
            break;
          }
        }
      }
    });
    select.addEventListener("blur", cerrar);

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
      w.classList.remove("open");
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

  /* ── 3. Ripple violeta en cada click ────────────────────── */

  var RIPPLE_SEL = ".btn, .side-item, .action-tile, .tab, .tselect-btn, " +
    ".tselect-option, .qty-btn, .btn-add-bulto, .bulto-quitar, .track-link, .step span";

  document.addEventListener("pointerdown", function (e) {
    var t = e.target.closest ? e.target.closest(RIPPLE_SEL) : null;
    if (!t) return;
    var rect = t.getBoundingClientRect();
    var d = Math.max(rect.width, rect.height) * 2;
    var r = document.createElement("span");
    r.className = "ripple";
    r.style.width = r.style.height = d + "px";
    r.style.left = (e.clientX - rect.left - d / 2) + "px";
    r.style.top = (e.clientY - rect.top - d / 2) + "px";
    t.appendChild(r);
    setTimeout(function () { r.remove(); }, 650);
  }, { passive: true });

  /* ── 4. Init + supervivencia al clonado ─────────────────── */

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
