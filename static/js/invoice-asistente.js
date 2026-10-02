/* Asistente de invoice: lee un archivo o texto y completa los artículos de
   UNA caja con el mismo mecanismo de "+ Agregar otro artículo a esta caja".
   No envía ni emite nada: el cliente revisa y sigue con el flujo normal. */
(function () {
  'use strict';
  if (window.tauroInvoiceAsistenteReady) return;
  window.tauroInvoiceAsistenteReady = true;

  var rellenando = false;
  var CAMPOS = {
    descripcion_en: '.bulto-desc', unidades_aduana: '.bulto-unidades-aduana',
    valor_total_usd: '.bulto-valor', hs_code: '.bulto-hs',
    pais_origen: '.bulto-pais-fab', peso_neto_kg: '.invoice-peso-neto'
  };

  function numeroCampo(valor) {
    return valor === null || valor === undefined || !isFinite(valor) ? '' : String(valor);
  }

  // Valores que van a cada campo del artículo y las marcas a mostrar.
  // Un valor que no está en USD no se convierte: queda vacío para cargar.
  function camposDeItem(item, paisesDisponibles) {
    var enUsd = !item.moneda || item.moneda === 'USD';
    var pais = item.pais_origen && paisesDisponibles.indexOf(item.pais_origen) >= 0 ? item.pais_origen : '';
    var valores = {
      descripcion_en: item.descripcion_en || '',
      unidades_aduana: numeroCampo(item.cantidad),
      valor_total_usd: enUsd && item.valor_total !== null && item.valor_total !== undefined
        ? Number(item.valor_total).toFixed(2) : '',
      hs_code: item.hs_code || '',
      pais_origen: pais,
      peso_neto_kg: numeroCampo(item.peso_neto_kg)
    };
    var marcas = {};
    if (item.hs_code && item.hs_origen === 'sugerido') marcas.hs_code = 'sugerido';
    if (pais && item.pais_origen_tipo === 'asumido') marcas.pais_origen = 'asumido';
    return { valores: valores, marcas: marcas };
  }

  function articulos(caja) {
    return Array.prototype.slice.call(caja.querySelectorAll('[data-invoice-item]'));
  }

  function vacio(item) {
    return !item.querySelector('.bulto-desc').value.trim() && !item.querySelector('.bulto-valor').value.trim();
  }

  function quitarMarcas(item) {
    item.querySelectorAll('[data-invoice-asistente-marca]').forEach(function (el) { el.remove(); });
  }

  function marcar(campo, texto) {
    var grupo = campo.closest('.form-group');
    if (!grupo) return;
    var marca = document.createElement('small');
    marca.className = 'invoice-line-total';
    marca.setAttribute('data-invoice-asistente-marca', '');
    marca.textContent = texto;
    grupo.appendChild(marca);
  }

  // Filas reutilizables: vacías o completadas por una lectura anterior.
  // Las cargadas a mano por el cliente no se tocan.
  function prepararFilas(caja, cantidad) {
    var reutilizables = articulos(caja).filter(function (item) {
      return item.hasAttribute('data-invoice-asistente-item') || vacio(item);
    });
    var sobrantes = reutilizables.slice(cantidad);
    sobrantes.forEach(function (item) {
      var quitar = item.querySelector('.invoice-item-remove');
      if (item !== articulos(caja)[0] && quitar) quitar.click();
      else {
        item.removeAttribute('data-invoice-asistente-item');
        quitarMarcas(item);
      }
    });
    var filas = reutilizables.slice(0, cantidad);
    var agregar = caja.querySelectorAll('.invoice-item-add');
    agregar = agregar[agregar.length - 1];
    while (filas.length < cantidad && agregar) {
      var antes = articulos(caja).length;
      agregar.click();
      var ahora = articulos(caja);
      if (ahora.length === antes) break; // tope de artículos del formulario
      filas.push(ahora[ahora.length - 1]);
    }
    return filas;
  }

  function completar(caja, items) {
    rellenando = true;
    try { return completarFilas(caja, items); } finally { rellenando = false; }
  }

  function completarFilas(caja, items) {
    var filas = prepararFilas(caja, items.length);
    filas.forEach(function (fila, i) {
      quitarMarcas(fila);
      var select = fila.querySelector(CAMPOS.pais_origen);
      var paises = Array.prototype.map.call(select ? select.options : [], function (o) { return o.value; });
      var datos = camposDeItem(items[i], paises);
      Object.keys(CAMPOS).forEach(function (clave) {
        var campo = fila.querySelector(CAMPOS[clave]);
        if (!campo) return;
        campo.value = datos.valores[clave];
        // "change" recalcula los totales de la caja sin disparar la búsqueda
        // HS automática de cada artículo.
        campo.dispatchEvent(new Event('change', { bubbles: true }));
      });
      Object.keys(datos.marcas).forEach(function (clave) {
        marcar(fila.querySelector(CAMPOS[clave]), datos.marcas[clave]);
      });
      fila.setAttribute('data-invoice-asistente-item', '');
    });
    return filas.length;
  }

  function mostrarAvisos(contenedor, avisos) {
    contenedor.replaceChildren();
    contenedor.hidden = !avisos.length;
    if (!avisos.length) return;
    var titulo = document.createElement('p');
    var fuerte = document.createElement('strong');
    fuerte.textContent = 'Revisá esto';
    titulo.appendChild(fuerte);
    var lista = document.createElement('ul');
    avisos.forEach(function (aviso) {
      var li = document.createElement('li');
      li.textContent = aviso;
      lista.appendChild(li);
    });
    contenedor.appendChild(titulo);
    contenedor.appendChild(lista);
  }

  async function leer(bloque) {
    var caja = bloque.closest('[data-invoice-bulto]');
    var boton = bloque.querySelector('[data-invoice-asistente-leer]');
    var archivo = bloque.querySelector('[data-invoice-asistente-archivo]');
    var texto = bloque.querySelector('[data-invoice-asistente-texto]');
    var estado = bloque.querySelector('[data-invoice-asistente-estado]');
    var avisos = bloque.querySelector('[data-invoice-asistente-avisos]');
    var decir = function (mensaje) { estado.textContent = mensaje; estado.hidden = !mensaje; };
    if (!caja || boton.disabled) return;
    var file = archivo.files && archivo.files[0];
    if (!file && !texto.value.trim()) { decir('Subí un archivo o pegá el texto de la invoice.'); return; }
    if (file && file.size > 10 * 1024 * 1024) { decir('El archivo supera los 10 MB. Subí uno más liviano o pegá el texto.'); return; }
    var datos = new FormData();
    if (file) datos.append('archivo', file);
    datos.append('texto', texto.value);
    datos.append('pais_origen_envio', ((document.getElementById('rem_pais') || {}).value || ''));
    boton.classList.add('is-loading');
    boton.disabled = true;
    decir('Leyendo invoice…');
    mostrarAvisos(avisos, []);
    try {
      var response = await fetch('/portal/api/invoice/leer', {
        method: 'POST', credentials: 'same-origin', body: datos, headers: { Accept: 'application/json' }
      });
      if (response.redirected) throw new Error('Tu sesión venció. Volvé a ingresar al portal.');
      var data = await response.json().catch(function () { return {}; });
      if (!response.ok) throw new Error(data.error || 'No pudimos leer la invoice. Cargá los artículos a mano.');
      var items = data.items || [];
      var cargados = items.length ? completar(caja, items) : 0;
      var lista = (data.avisos || []).slice();
      if (cargados < items.length) lista.unshift('Cargamos ' + cargados + ' de ' + items.length + ' artículos: se alcanzó el máximo del formulario.');
      decir(cargados ? 'Cargamos ' + cargados + (cargados === 1 ? ' artículo' : ' artículos') + ' en esta caja. Revisalos antes de seguir.' : '');
      mostrarAvisos(avisos, lista);
    } catch (error) {
      decir(error.message || 'No pudimos leer la invoice. Cargá los artículos a mano.');
    } finally {
      boton.classList.remove('is-loading');
      boton.disabled = false;
    }
  }

  document.addEventListener('click', function (event) {
    if (!event.target.closest) return;
    var boton = event.target.closest('[data-invoice-asistente-leer]');
    if (boton) { leer(boton.closest('[data-invoice-asistente]')); return; }
    // Un artículo agregado a mano clona el primero: no hereda las marcas.
    var agregar = !rellenando && event.target.closest('.invoice-item-add');
    var caja = agregar && agregar.closest('[data-invoice-bulto]');
    if (!caja) return;
    var nuevo = articulos(caja).pop();
    if (nuevo && nuevo !== articulos(caja)[0] && vacio(nuevo)) {
      nuevo.removeAttribute('data-invoice-asistente-item');
      quitarMarcas(nuevo);
    }
  });
  // Si el cliente corrige un campo marcado, la marca ya no aplica.
  ['input', 'change'].forEach(function (tipo) {
    document.addEventListener(tipo, function (event) {
      if (rellenando || !event.target.closest) return;
      var grupo = event.target.closest('[data-invoice-item] .form-group');
      var marca = grupo && grupo.querySelector('[data-invoice-asistente-marca]');
      if (marca) marca.remove();
    });
  });

  window.TauroInvoiceAsistente = { camposDeItem: camposDeItem };
})();
