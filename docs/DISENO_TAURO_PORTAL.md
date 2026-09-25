# Criterio de diseño TAURO — web y portales

Preferencia expresa de Leandro, 25 de septiembre de 2026: reducir al mínimo el desplazamiento vertical y mantener una imagen coherente en toda la web, el portal de clientes y el Admin.

- Priorizar información de trabajo y acciones dentro de la ventana. En escritorio, cotizador con ruta, paquetes y tarifas visibles juntos.
- No esconder datos esenciales con overflow ni reducir el texto hasta volverlo ilegible para aparentar que todo entra. Con zoom, teclado móvil, pantallas muy bajas o listados extensos debe conservarse acceso a todo el contenido.
- Para formularios largos, agrupar en pasos cortos con Atrás/Siguiente que conserven los valores; ninguna vuelta debe reiniciar la carga.
- Mantener la tipografía, violeta, radios suaves, campos alineados y componentes comunes de TAURO; revisar temas claro y oscuro.
- Aclaraciones secundarias en pequeños controles de información accesibles con mouse, teclado y toque. Errores que requieren una acción deben seguir visibles.
- Rutas frecuentes cerradas inicialmente. Invertir debe estar visible y cambiar toda la ruta —país/provincia, localidad y CP— sin tocar cajas ni valor declarado.
- El autocompletado de ciudad/CP usa coincidencias exactas. Prefijos muestran sugerencias; varias localidades requieren elección. La referencia genérica sirve para cotizar y no reemplaza la validación del domicilio al emitir.

## Implementación de esta revisión

Cotizador: distribución compacta desde 780 px, cajas adicionales en solapas, inversión completa, y propuesta móvil Ruta → Paquetes → Tarifas. El usuario confirmó la selección manual ante coincidencias múltiples; la preferencia móvil quedó planteada para revisar en la vista previa.

Nacional OCA: campos y selectores con el estilo común, Origen → Destino → Paquetes, borrador de la pestaña, validaciones y recuperación de los datos exactos desde la revisión de tarifa mediante un GET autenticado y restringido al cliente propietario.

Esto fija el criterio de las próximas modificaciones; no implica que en esta revisión se haya rediseñado cada pantalla del sitio o del Admin.
