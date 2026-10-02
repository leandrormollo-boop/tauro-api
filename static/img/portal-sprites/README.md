# Piezas del portal TAURO

Colección aprobada por Leandro el 2 de octubre de 2026: facetas de violeta
metálico sobre fondo transparente. Esta elección explícita prevalece sobre las
reglas generales de `tauro-taste` de usar íconos lineales y violeta sólo como luz.
No reemplaza el logo de TAURO ni representa estados o métricas.

| Archivo | Función |
| --- | --- |
| cotizar.svg | Cotizar |
| envios.svg | Mis envíos |
| datos.svg | Estadísticas |
| clientes.svg | Mis clientes |
| recolecciones.svg | Recolecciones |
| cuenta.svg | Mi cuenta |
| guias.svg | Encabezado de la guía del envío |
| seguimiento.svg | Seguimiento del envío |

Se incluyen con `portal/_sprites.html`; los tamaños dependen del contexto en
`portal-sprites.css`. Los rótulos permanecen visibles y el ícono es decorativo
(`alt=""`, `aria-hidden="true"`). El dock conserva su animación de interacción
y respeta movimiento reducido. No requieren JavaScript ni recursos externos.

Los SVG son la segunda revisión aprobada, sin modificar sus geometrías o colores.
Al reemplazarlos, incrementar la versión en el macro para invalidar la caché.
