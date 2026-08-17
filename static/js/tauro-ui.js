/* ==========================================================
   TAURO UI — componentes compartidos del portal y Admin
   ----------------------------------------------------------
   TSelect conserva el <select> real para envío/validación, pero ofrece una
   búsqueda y teclado coherentes en todos los formularios. Los selects largos
   (8+ opciones) y los marcados data-searchable abren con buscador; los chicos
   mantienen una lista compacta y también soportan typeahead con teclado.
   ========================================================== */
(function () {
  "use strict";

  var enhancedSelects = new WeakSet();
  var enhancedQty = new WeakSet();
  var enhancedNumeros = new WeakSet();
  var tselectSeq = 0;
  var selectValueSetter = Object.getOwnPropertyDescriptor(
    HTMLSelectElement.prototype, "value"
  );

  /* ── 1. SmartSelect ─────────────────────────────────────── */

  function labelDe(select) {
    var o = select.options[select.selectedIndex];
    return (o && o.text.trim()) || "Seleccionar";
  }

  function normalizar(texto) {
    var s = String(texto || "").toLowerCase();
    return s.normalize ? s.normalize("NFD").replace(/[\u0300-\u036f]/g, "") : s;
  }

  function etiquetaAccesible(select) {
    var etiqueta = select.getAttribute("aria-label");
    if (etiqueta) return etiqueta;
    if (select.id) {
      var label = document.querySelector("label[for='" + escaparId(select.id) + "']");
      if (label) return label.textContent.trim();
    }
    var grupo = select.closest(".form-group");
    var labelInterno = grupo && grupo.querySelector("label");
    return (labelInterno && labelInterno.textContent.trim()) || "Seleccionar opción";
  }

  function escaparId(id) {
    if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(id);
    return String(id).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }

  function construirTSelect(select) {
    if (enhancedSelects.has(select)) return;
    if (select.multiple || select.hasAttribute("data-no-tselect")) return;

    // Un cloneNode conserva el markup, pero no listeners. Rescatamos el select
    // antes de reconstruir para que las filas multi-bulto sigan siendo seguras.
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

    // El select real queda como fuente de verdad de FormData y validation,
    // pero el botón es el único control de teclado/lector de pantalla.
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");

    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "tselect-btn";
    btn.disabled = select.disabled;
    btn.setAttribute("aria-haspopup", "listbox");
    btn.setAttribute("aria-expanded", "false");
    var textoAccesible = etiquetaAccesible(select);
    btn.setAttribute("aria-label", textoAccesible + ": " + labelDe(select));
    var lbl = document.createElement("span");
    lbl.className = "tselect-label";
    lbl.textContent = labelDe(select);
    var caret = document.createElement("span");
    caret.className = "tselect-caret";
    caret.setAttribute("aria-hidden", "true");
    caret.innerHTML = "<svg width='11' height='7' viewBox='0 0 10 6' fill='none'><path d='M1 1l4 4 4-4' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'/></svg>";
    btn.appendChild(lbl);
    btn.appendChild(caret);

    var panel = document.createElement("div");
    panel.className = "tselect-panel";
    var tieneBusqueda = select.hasAttribute("data-searchable") || select.options.length >= 8;
    wrap.classList.toggle("searchable", tieneBusqueda);
    var searchInput = null;
    if (tieneBusqueda) {
      var searchWrap = document.createElement("div");
      searchWrap.className = "tselect-search-wrap";
      searchWrap.innerHTML = "<svg width='14' height='14' viewBox='0 0 24 24' fill='none' aria-hidden='true'><circle cx='11' cy='11' r='7' stroke='currentColor' stroke-width='1.8'/><path d='m20 20-4-4' stroke='currentColor' stroke-width='1.8' stroke-linecap='round'/></svg>";
      searchInput = document.createElement("input");
      searchInput.type = "search";
      searchInput.className = "tselect-search";
      searchInput.placeholder = select.dataset.searchPlaceholder || "Buscar opciones";
      searchInput.setAttribute("aria-label", searchInput.placeholder);
      searchInput.setAttribute("role", "combobox");
      searchInput.setAttribute("aria-autocomplete", "list");
      searchInput.setAttribute("aria-expanded", "false");
      searchInput.autocomplete = "off";
      searchWrap.appendChild(searchInput);
      panel.appendChild(searchWrap);
    }

    var optionsBox = document.createElement("div");
    optionsBox.className = "tselect-options";
    optionsBox.setAttribute("role", "listbox");
    optionsBox.id = (select.id ? select.id + "-options" : "tselect-options-" + (++tselectSeq));
    btn.setAttribute("aria-controls", optionsBox.id);
    if (searchInput) searchInput.setAttribute("aria-controls", optionsBox.id);
    panel.appendChild(optionsBox);
    wrap.appendChild(btn);
    wrap.appendChild(panel);

    var consulta = "";
    var visibles = [];
    var activo = -1;
    var typeahead = "";
    var timerTypeahead = null;

    function opcionesFiltradas(texto) {
      var filtro = normalizar(texto).trim();
      var candidatas = [];
      Array.prototype.forEach.call(select.options, function (opt, i) {
        var etiqueta = normalizar(opt.text).trim();
        var codigo = normalizar(opt.value).trim();
        var rango = 0;
        if (filtro) {
          if (etiqueta === filtro || codigo === filtro) rango = 0;
          else if (etiqueta.indexOf(filtro) === 0) rango = 1;
          else if (codigo.indexOf(filtro) === 0) rango = 2;
          else if (etiqueta.split(/\s+/).some(function (palabra) { return palabra.indexOf(filtro) === 0; })) rango = 3;
          else if (etiqueta.indexOf(filtro) !== -1) rango = 4;
          else if (codigo.indexOf(filtro) !== -1) rango = 5;
          else return;
        }
        candidatas.push({
          indice: i,
          opcion: opt,
          // Exacto, prefijo de nombre y prefijo de código aparecen primero.
          rango: rango,
          orden: i,
        });
      });
      candidatas.sort(function (a, b) { return a.rango - b.rango || a.orden - b.orden; });
      return candidatas;
    }

    function idOpcion(i) { return optionsBox.id + "-option-" + i; }

    function actualizarActivo(preferido) {
      var candidatos = visibles.filter(function (item) { return !item.opcion.disabled; });
      if (!candidatos.length) {
        activo = -1;
        return;
      }
      var existe = candidatos.some(function (item) { return item.indice === preferido; });
      activo = existe ? preferido : candidatos[0].indice;
    }

    function armarOpciones(nuevaConsulta, activoPreferido) {
      consulta = nuevaConsulta == null ? consulta : nuevaConsulta;
      visibles = opcionesFiltradas(consulta);
      actualizarActivo(activoPreferido == null ? activo : activoPreferido);
      optionsBox.innerHTML = "";
      visibles.forEach(function (item) {
        var opt = item.opcion;
        var i = item.indice;
        var itemEl = document.createElement("div");
        itemEl.className = "tselect-option" +
          (i === select.selectedIndex ? " selected" : "") +
          (i === activo ? " active" : "") +
          (opt.disabled ? " disabled" : "");
        itemEl.id = idOpcion(i);
        itemEl.setAttribute("role", "option");
        itemEl.setAttribute("aria-selected", i === select.selectedIndex ? "true" : "false");
        itemEl.dataset.idx = String(i);
        var check = document.createElement("span");
        check.className = "tselect-check";
        check.textContent = "✓";
        var optionText = document.createElement("span");
        optionText.textContent = opt.text;
        itemEl.appendChild(check);
        itemEl.appendChild(optionText);
        if (!opt.disabled) {
          itemEl.addEventListener("mouseenter", function () {
            activo = i;
            sincronizarActivoVisual();
          });
          // mousedown evita que el focusout cierre el panel antes de confirmar.
          itemEl.addEventListener("mousedown", function (e) {
            e.preventDefault();
            e.stopPropagation();
            elegir(i);
            cerrar();
            btn.focus({ preventScroll: true });
          });
        }
        optionsBox.appendChild(itemEl);
      });
      if (!visibles.length) {
        var vacio = document.createElement("div");
        vacio.className = "tselect-empty";
        vacio.textContent = "No encontramos resultados";
        optionsBox.appendChild(vacio);
      }
      sincronizarActivoVisual();
    }

    function sincronizarActivoVisual() {
      var activoEl = activo >= 0 ? optionsBox.querySelector("#" + escaparId(idOpcion(activo))) : null;
      optionsBox.querySelectorAll(".tselect-option.active").forEach(function (item) {
        item.classList.toggle("active", item === activoEl);
      });
      if (activoEl) {
        btn.setAttribute("aria-activedescendant", activoEl.id);
        if (searchInput) searchInput.setAttribute("aria-activedescendant", activoEl.id);
      } else {
        btn.removeAttribute("aria-activedescendant");
        if (searchInput) searchInput.removeAttribute("aria-activedescendant");
      }
    }

    function elegir(i) {
      if (i < 0 || i >= select.options.length || select.options[i].disabled) return;
      select.selectedIndex = i;
      lbl.textContent = labelDe(select);
      btn.setAttribute("aria-label", textoAccesible + ": " + labelDe(select));
      select.dispatchEvent(new Event("change", { bubbles: true }));
    }

    function reposicionar() {
      if (!wrap.classList.contains("open")) return;
      panel.style.left = "0px";
      panel.style.right = "auto";
      var wrapRect = wrap.getBoundingClientRect();
      var clip = wrap.closest("dialog[open]");
      var clipRect = clip ? clip.getBoundingClientRect() : null;
      var viewportLeft = window.visualViewport ? window.visualViewport.offsetLeft : 0;
      var viewportRight = viewportLeft + (window.visualViewport ? window.visualViewport.width : window.innerWidth);
      var viewportTop = window.visualViewport ? window.visualViewport.offsetTop : 0;
      var viewportBottom = viewportTop + (window.visualViewport ? window.visualViewport.height : window.innerHeight);
      var limiteTop = clipRect ? Math.max(viewportTop, clipRect.top) : viewportTop;
      var limiteBottom = clipRect ? Math.min(viewportBottom, clipRect.bottom) : viewportBottom;
      var espacioAbajo = Math.max(0, limiteBottom - wrapRect.bottom - 7);
      var espacioArriba = Math.max(0, wrapRect.top - limiteTop - 7);
      var haciaArriba = espacioAbajo < 220 && espacioArriba > espacioAbajo;
      var espacio = haciaArriba ? espacioArriba : espacioAbajo;
      wrap.classList.toggle("open-up", haciaArriba);
      wrap.style.setProperty("--tselect-space", Math.max(120, Math.min(320, espacio)) + "px");

      // Los formularios mobile conservan dos columnas, pero el buscador debe
      // seguir siendo legible. El panel puede ser más ancho que su campo y se
      // desplaza dentro del viewport/dialog sin generar overflow horizontal.
      var panelRect = panel.getBoundingClientRect();
      var limiteIzq = clipRect ? Math.max(viewportLeft + 8, clipRect.left + 8) : viewportLeft + 8;
      var limiteDer = clipRect ? Math.min(viewportRight - 8, clipRect.right - 8) : viewportRight - 8;
      var desplazamiento = 0;
      if (panelRect.right > limiteDer) desplazamiento -= panelRect.right - limiteDer;
      if (panelRect.left + desplazamiento < limiteIzq) {
        desplazamiento += limiteIzq - (panelRect.left + desplazamiento);
      }
      panel.style.left = desplazamiento + "px";
    }

    function abrir(textoInicial) {
      if (wrap.classList.contains("open")) return;
      cerrarTodos(wrap);
      consulta = textoInicial || "";
      if (searchInput) searchInput.value = consulta;
      armarOpciones(consulta, consulta ? -1 : select.selectedIndex);
      wrap.classList.add("open");
      btn.setAttribute("aria-expanded", "true");
      if (searchInput) searchInput.setAttribute("aria-expanded", "true");
      reposicionar();
      var activoEl = activo >= 0 ? optionsBox.querySelector("#" + escaparId(idOpcion(activo))) : null;
      if (activoEl) activoEl.scrollIntoView({ block: "nearest" });
      if (searchInput) {
        window.setTimeout(function () { searchInput.focus({ preventScroll: true }); }, 0);
      }
    }

    function cerrar() {
      wrap.classList.remove("open", "open-up");
      wrap.style.removeProperty("--tselect-space");
      panel.style.removeProperty("left");
      panel.style.removeProperty("right");
      btn.setAttribute("aria-expanded", "false");
      if (searchInput) searchInput.setAttribute("aria-expanded", "false");
      btn.removeAttribute("aria-activedescendant");
      if (searchInput) searchInput.removeAttribute("aria-activedescendant");
    }

    function moverActivo(direccion) {
      var candidatos = visibles.filter(function (item) { return !item.opcion.disabled; });
      if (!candidatos.length) return;
      var posicion = candidatos.findIndex(function (item) { return item.indice === activo; });
      var siguiente = Math.max(0, Math.min(posicion + direccion, candidatos.length - 1));
      if (posicion === -1) siguiente = direccion < 0 ? candidatos.length - 1 : 0;
      activo = candidatos[siguiente].indice;
      sincronizarActivoVisual();
      var activoEl = optionsBox.querySelector("#" + escaparId(idOpcion(activo)));
      if (activoEl) activoEl.scrollIntoView({ block: "nearest" });
    }

    function confirmarActivo() {
      if (activo < 0) return;
      elegir(activo);
      cerrar();
      btn.focus({ preventScroll: true });
    }

    function teclaNavegacion(e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (!wrap.classList.contains("open")) abrir();
        moverActivo(e.key === "ArrowDown" ? 1 : -1);
        return true;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        if (wrap.classList.contains("open")) confirmarActivo(); else abrir();
        return true;
      }
      if (e.key === "Escape") {
        if (wrap.classList.contains("open")) {
          e.preventDefault();
          cerrar();
          btn.focus({ preventScroll: true });
        }
        return true;
      }
      return false;
    }

    function acumularTypeahead(tecla) {
      if (!/^[a-zA-Z0-9]$/.test(tecla)) return false;
      typeahead += tecla;
      window.clearTimeout(timerTypeahead);
      timerTypeahead = window.setTimeout(function () { typeahead = ""; }, 700);
      if (!wrap.classList.contains("open")) abrir(typeahead);
      else if (searchInput) {
        searchInput.value += tecla;
        armarOpciones(searchInput.value, -1);
      } else {
        armarOpciones(typeahead, -1);
      }
      return true;
    }

    if (searchInput) {
      searchInput.addEventListener("input", function () {
        armarOpciones(searchInput.value, -1);
      });
      searchInput.addEventListener("keydown", function (e) {
        if (!teclaNavegacion(e) && e.key === "Tab") cerrar();
      });
    }

    btn.addEventListener("click", function () {
      if (wrap.classList.contains("open")) cerrar(); else abrir();
    });
    btn.addEventListener("keydown", function (e) {
      if (!teclaNavegacion(e)) acumularTypeahead(e.key);
    });

    // Compatibilidad con código existente que aún llama select.focus().
    select.addEventListener("keydown", function (e) {
      if (!teclaNavegacion(e)) acumularTypeahead(e.key);
    });
    wrap.addEventListener("focusout", function () {
      window.setTimeout(function () {
        if (!wrap.contains(document.activeElement)) cerrar();
      }, 0);
    });
    window.addEventListener("resize", reposicionar, { passive: true });
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", reposicionar, { passive: true });
      window.visualViewport.addEventListener("scroll", reposicionar, { passive: true });
    }

    // Cambios por código (prefills, parser, form_input) no dejan la etiqueta vieja.
    try {
      Object.defineProperty(select, "value", {
        configurable: true,
        get: function () { return selectValueSetter && Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value").get.call(this); },
        set: function (v) {
          selectValueSetter.set.call(this, v);
          lbl.textContent = labelDe(this);
          btn.setAttribute("aria-label", textoAccesible + ": " + labelDe(this));
        },
      });
    } catch (err) { /* el listener de change cubre navegadores restrictivos */ }
    select.addEventListener("change", function () {
      lbl.textContent = labelDe(select);
      btn.setAttribute("aria-label", textoAccesible + ": " + labelDe(select));
      btn.removeAttribute("aria-invalid");
    });

    // Un select required invisible sigue bloqueando el envío nativo. Si es el
    // primer control inválido, llevamos el error al botón visible y abrimos la
    // lista; así el navegador nunca intenta enfocar un control transparente.
    select.addEventListener("invalid", function (event) {
      event.preventDefault();
      btn.setAttribute("aria-invalid", "true");
      var elementos = select.form ? Array.prototype.slice.call(select.form.elements) : [select];
      var primerInvalido = elementos.find(function (elemento) {
        return elemento && elemento.willValidate && !elemento.validity.valid;
      });
      if (primerInvalido !== select) return;
      abrir();
      window.setTimeout(function () {
        (searchInput || btn).focus({ preventScroll: true });
      }, 0);
    });
  }

  function cerrarTodos(excepto) {
    document.querySelectorAll(".tselect.open").forEach(function (wrap) {
      if (wrap === excepto) return;
      wrap.classList.remove("open", "open-up");
      wrap.style.removeProperty("--tselect-space");
      var panel = wrap.querySelector(".tselect-panel");
      if (panel) {
        panel.style.removeProperty("left");
        panel.style.removeProperty("right");
      }
      var boton = wrap.querySelector(".tselect-btn");
      if (boton) {
        boton.setAttribute("aria-expanded", "false");
        boton.removeAttribute("aria-activedescendant");
      }
      var buscador = wrap.querySelector(".tselect-search");
      if (buscador) {
        buscador.setAttribute("aria-expanded", "false");
        buscador.removeAttribute("aria-activedescendant");
      }
    });
  }
  document.addEventListener("mousedown", function (e) {
    if (!e.target.closest || !e.target.closest(".tselect")) cerrarTodos();
  });

  /* ── 2. SmartNumber ─────────────────────────────────────── */

  // Misma gramática deliberada de servicios/numeros_humanos.py. El servidor
  // conserva la autoridad: esta capa sólo unifica la escritura humana después
  // de salir del campo, sin mover el cursor mientras la persona está tipeando.
  function gruposMilesValidos(grupos) {
    return grupos.length >= 2 && /^[0-9]{1,3}$/.test(grupos[0]) &&
      grupos.slice(1).every(function (grupo) { return /^[0-9]{3}$/.test(grupo); });
  }

  function numeroCanonico(valor, tipo) {
    var texto = String(valor == null ? "" : valor).trim();
    if (!texto) return { valor: "" };
    if (!/^[+-]?[0-9.,]+$/.test(texto)) return { error: "Ingresá sólo números, puntos o comas." };

    var signo = "";
    if (texto[0] === "+" || texto[0] === "-") {
      signo = texto[0];
      texto = texto.slice(1);
    }
    if (!texto) return { error: "Ingresá un número válido." };

    if (tipo === "entero") {
      if (/^[0-9]+$/.test(texto)) return { valor: signo + texto };
      var separadorEntero = texto.indexOf(".") !== -1 ? "." : ",";
      // Un entero admite una agrupación de miles, nunca una fracción.
      if (texto.indexOf(".") !== -1 && texto.indexOf(",") !== -1) {
        return { error: "Para un entero usá sólo una separación de miles." };
      }
      var gruposEntero = texto.split(separadorEntero);
      if (gruposEntero.length === 2) {
        var parteEnteraEntero = gruposEntero[0];
        var parteDecimalEntero = gruposEntero[1];
        var sonDigitos = /^[0-9]+$/.test(parteEnteraEntero) && /^[0-9]+$/.test(parteDecimalEntero);
        // La convención de TAURO manda: `1.000`/`1,000` es mil. En cambio
        // `1.0`, `1.00` o `1.0000` pueden reducirse a 1 porque la fracción es
        // exactamente cero. `0.500` nunca se convierte en 500.
        if (sonDigitos && parteDecimalEntero.length === 3 && Number(parteEnteraEntero) !== 0 &&
            parteEnteraEntero.length <= 3) {
          return { valor: signo + parteEnteraEntero + parteDecimalEntero };
        }
        if (sonDigitos && /^0+$/.test(parteDecimalEntero) &&
            (parteDecimalEntero.length !== 3 || Number(parteEnteraEntero) === 0 ||
             parteEnteraEntero.length > 3)) {
          return { valor: signo + parteEnteraEntero };
        }
        // Con una única separación ya se agotaron las interpretaciones
        // inequívocas. En particular, `0.500` no puede convertirse en 500:
        // se pide corrección humana en vez de cambiar la cantidad.
        return { error: "Ingresá un entero o miles válidos, por ejemplo 1.000." };
      }
      if (!gruposMilesValidos(gruposEntero)) {
        return { error: "Ingresá un entero o miles válidos, por ejemplo 1.000." };
      }
      return { valor: signo + gruposEntero.join("") };
    }

    var esMonto = tipo === "monto" || tipo === "importe";
    var puntos = (texto.match(/\./g) || []).length;
    var comas = (texto.match(/,/g) || []).length;
    var canonico;
    if (puntos && comas) {
      var decimal = texto.lastIndexOf(".") > texto.lastIndexOf(",") ? "." : ",";
      var miles = decimal === "." ? "," : ".";
      if (texto.split(decimal).length !== 2) {
        return { error: "El separador decimal está repetido." };
      }
      var partes = texto.split(decimal);
      var entero = partes[0];
      var fraccion = partes[1];
      var grupos = entero.split(miles);
      if (!/^[0-9]+$/.test(fraccion) || !gruposMilesValidos(grupos)) {
        return { error: "La combinación de miles y decimales no es válida." };
      }
      canonico = grupos.join("") + "." + fraccion;
    } else if (puntos || comas) {
      var separador = puntos ? "." : ",";
      var gruposUnicos = texto.split(separador);
      if (gruposUnicos.length > 2) {
        if (!gruposMilesValidos(gruposUnicos)) {
          return { error: "La agrupación de miles no es válida." };
        }
        canonico = gruposUnicos.join("");
      } else {
        var parteEntera = gruposUnicos[0];
        var parteDecimal = gruposUnicos[1];
        if (!/^[0-9]+$/.test(parteEntera) || !/^[0-9]+$/.test(parteDecimal)) {
          return { error: "Ingresá un número válido." };
        }
        if (parteDecimal.length === 3 && Number(parteEntera) !== 0) {
          var primerGrupoValido = parteEntera.length >= 1 && parteEntera.length <= 3;
          if (esMonto && primerGrupoValido) canonico = parteEntera + parteDecimal;
          else return { error: "Usá un decimal claro, por ejemplo 5,5 o 5.5." };
        } else {
          canonico = parteEntera + "." + parteDecimal;
        }
      }
    } else if (/^[0-9]+$/.test(texto)) {
      canonico = texto;
    } else {
      return { error: "Ingresá un número válido." };
    }
    return { valor: signo + canonico };
  }

  function normalizarCampoNumero(input) {
    var tipo = input.dataset.numero;
    if (!tipo || input.disabled || input.readOnly) return true;
    // En pricing la política está declarada por el select asociado: un fijo
    // en ARS usa gramática de importe (10.000 = 10000), mientras porcentaje
    // y multiplicador usan gramática decimal. Si hereda la regla general y el
    // campo está vacío, no hay nada que normalizar.
    if (tipo === "pricing") {
      var selectPricing = input.dataset.pricingSelect
        ? document.getElementById(input.dataset.pricingSelect)
        : null;
      var modoPricing = String(selectPricing ? selectPricing.value : "").toUpperCase();
      if (!modoPricing) return true;
      tipo = modoPricing === "FIJO_ARS" ? "monto" : "decimal";
    }
    var resultado = numeroCanonico(input.value, tipo);
    if (resultado.error) {
      input.setCustomValidity(resultado.error);
      return false;
    }
    input.value = resultado.valor;
    input.setCustomValidity("");
    return true;
  }

  function construirNumero(input) {
    if (enhancedNumeros.has(input)) return;
    enhancedNumeros.add(input);
    input.addEventListener("blur", function () { normalizarCampoNumero(input); });
    input.addEventListener("input", function () { input.setCustomValidity(""); });
  }

  // Cubrimos Enter directo desde un campo: no todos los navegadores disparan
  // blur antes de submit. El valor enviado queda canónico si era válido.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || !form.querySelectorAll) return;
    var valido = true;
    form.querySelectorAll("[data-numero]").forEach(function (input) {
      if (!normalizarCampoNumero(input)) valido = false;
    });
    if (!valido) {
      event.preventDefault();
      var invalido = form.querySelector("[data-numero]:invalid");
      if (invalido) {
        invalido.focus({ preventScroll: true });
        invalido.reportValidity();
      }
    }
  }, true);

  /* ── 3. QtyStep (− n +) ─────────────────────────────────── */

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

  /* ── Init + supervivencia al clonado ────────────────────── */

  function mejorarTodo(raiz) {
    (raiz.querySelectorAll ? raiz : document)
      .querySelectorAll("select").forEach(construirTSelect);
    (raiz.querySelectorAll ? raiz : document)
      .querySelectorAll("input.bulto-cantidad, input#cantidad")
      .forEach(construirQty);
    (raiz.querySelectorAll ? raiz : document)
      .querySelectorAll("[data-numero]").forEach(construirNumero);
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

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
