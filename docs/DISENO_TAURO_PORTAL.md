# Criterio de diseño TAURO — web y portales

Preferencia expresa de Leandro, 25 de septiembre de 2026: reducir al mínimo el desplazamiento vertical y mantener una imagen coherente en toda la web, el portal de clientes y el Admin.

- Priorizar información de trabajo y acciones dentro de la ventana. En escritorio, cotizador con ruta, paquetes y tarifas visibles juntos.
- No esconder datos esenciales con overflow ni reducir el texto hasta volverlo ilegible para aparentar que todo entra. Con zoom, teclado móvil, pantallas muy bajas o listados extensos debe conservarse acceso a todo el contenido.
- Para formularios largos, agrupar en pasos cortos con Atrás/Siguiente que conserven los valores; ninguna vuelta debe reiniciar la carga.
- Mantener la tipografía, violeta, radios suaves, campos alineados y componentes comunes de TAURO; revisar temas claro y oscuro. En oscuro, distinguir los campos editables con un fondo violeta tenue, borde fino y relieve mínimo; reservar el halo para el foco. Mantener el color de error cuando corresponda.
- Aclaraciones secundarias en pequeños controles de información accesibles con mouse, teclado y toque. Errores que requieren una acción deben seguir visibles.
- Rutas frecuentes cerradas inicialmente. Invertir debe estar visible y cambiar toda la ruta —país/provincia, localidad y CP— sin tocar cajas ni valor declarado.
- El autocompletado de ciudad/CP usa coincidencias exactas. Prefijos muestran sugerencias; varias localidades requieren elección. La referencia genérica sirve para cotizar y no reemplaza la validación del domicilio al emitir.

## Implementación de esta revisión

Cotizador: distribución compacta desde 780 px, cajas adicionales en solapas, inversión completa, y propuesta móvil Ruta → Paquetes → Tarifas. El usuario confirmó la selección manual ante coincidencias múltiples; la preferencia móvil quedó planteada para revisar en la vista previa.

Nacional OCA: campos y selectores con el estilo común, Origen → Destino → Paquetes, borrador de la pestaña, validaciones y recuperación de los datos exactos desde la revisión de tarifa mediante un GET autenticado y restringido al cliente propietario.

Esto fija el criterio de las próximas modificaciones; no implica que en esta revisión se haya rediseñado cada pantalla del sitio o del Admin.

## Atajos flotantes en PC

El usuario pidió llevar la barra inferior a escritorio con un tratamiento similar al dock de macOS. Desde 901 px, usar la misma navegación como barra flotante centrada en el área de trabajo: superficie translúcida violeta, bordes redondeados, nombres visibles y elevación suave de iconos. Accesos: Inicio, Cotizar, Nuevo envío, Mis envíos, Mis clientes y Cuenta. En móvil se conservan los cinco accesos existentes. Respetar teclado, movimiento reducido y espacio para acciones al pie; no mostrar el dock al imprimir ni en login.

Verificación local: 26 pruebas existentes de PWA y cotizador aprobadas; PC 1280×720 en claro y oscuro, apertura/cierre del cotizador desde el dock, foco por teclado, cinco accesos en móvil 390×844 y consola sin errores. El dock queda fuera de los botones Atrás/Siguiente en la vista nacional verificada. Implementación con CSS y enlaces del portal, sin bibliotecas ni JavaScript adicional. Pendiente de publicación.

## Emblemas Nacional e Internacional

Usar el Sol de Mayo dorado para Nacional. Internacional retoma el planeta del cotizador: continentes reconocibles, volumen oscuro, halo y ruta violeta #8b6bf7, sin grilla de meridianos. Compartir ambos emblemas entre cotización y selección de envío; servir SVG locales, sin cargar el mapa interactivo para los botones. En los selectores de países, Islas Malvinas se presenta con bandera argentina y sin sigla visible; su valor ISO operativo se conserva.
