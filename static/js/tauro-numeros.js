/* Parseo compartido ES/EN; sin acceso al DOM. */
(function () {
  "use strict";
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

  // Compartido con el resumen de invoice: misma interpretación al escribir,
  // al perder foco y al enviar (incluye importes como 1.000,50).
  window.TauroNumeros = {canonico: numeroCanonico};

})();
