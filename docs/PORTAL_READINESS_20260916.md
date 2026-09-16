# Portal TAURO — preparación del 16/09/2026

**Estado: implementado y validado localmente; publicación pendiente de autorización.**
Rama `codex/portal-readiness-20260916`, base `origin/main` `2249e4800b24b408636a091d038a44fc38b98ba0`.
Sin migraciones ni dependencias nuevas. Un push a `main` despliega automáticamente en Railway.

## Qué cambia para el cliente

- La cotización conserva cantidades, medidas, pesos y valor declarado al continuar una solicitud. El servidor valida nuevamente los datos y vuelve a cotizar el transporte. No toma el precio enviado por el navegador como autoridad. En varias cajas conserva el valor total como referencia sin inventar su distribución.
- Elegir un origen distinto del domicilio habitual obliga a completar el remitente correcto. El formulario señala el primer campo inválido.
- Formularios más legibles en tema claro, botones que caben en móvil, enlaces de seguimiento visibles y modal de embalajes centrado.
- Cuenta corriente: saldo anterior a favor expresado correctamente, cargos aún no facturados explicados, alcance del cupo visible y pagos históricos diferenciados del período mostrado.
- Informar un pago comienza a cuenta; vincular documentos es opcional, con buscador y selección que permanece visible. La presentación distingue deuda actual de importes documentales históricos. El pago sigue pendiente hasta su aprobación: no cambia la acreditación ni la imputación contable.
- Retenciones visibles al comienzo del detalle con mensaje del courier y acceso a ayuda. Si el courier termina las actualizaciones de seguimiento, se informa esa limitación sin inventar una entrega.
- El Excel identifica el precio inicial y remite a las hojas de cuenta para consultar ajustes y pagos; conserva los importes y la conciliación del export.

## Qué cambia para el administrador

- Control financiero organizado en Resumen, Facturas recibidas, Control de envíos y Diferencias; indicadores enlazados con su filtro correspondiente.
- Veinticinco filas por página y totales completos. Los pendientes antiguos dejan de desaparecer por el límite de mil envíos. El expediente se obtiene por identificador exacto.
- Filtros por courier, cliente, estado y búsqueda según la vista. Al volver de un expediente se conserva la consulta, mediante rutas internas permitidas. Esto no modifica los redireccionamientos de las acciones financieras POST.
- Navegación sin recarga completa, aviso de actualización y recuperación ante errores. Un fallo de red conserva los datos anteriores; Atrás/Adelante conserva la coherencia entre URL y resultados.
- Guías identificadas y vinculaciones propuestas tienen etiquetas distintas. Una NC no aparece como deuda pagable. El historial sin verificar no se presenta como saldo auditado.
- El indicador de pagos cuenta únicamente pendientes y coincide con la bandeja de revisión.

## Contenido público corregido

Se retiraron promesas de precio final invariable y se diferenciaron transporte, tributos y ajustes posteriores. Se corrigieron las condiciones de Brasil y la referencia desactualizada de pequeños envíos a España. Esto no constituye una revisión normativa completa de todas las guías por país.

La información de Brasil distingue el programa Remessa Conforme de otros regímenes y aclara la base y las condiciones. Fuente oficial consultada el 16/09: [Receita Federal](https://www.gov.br/receitafederal/pt-br/assuntos/aduana-e-comercio-exterior/manuais/remessas-postal-e-expressa/regras-futuras).

La información de España incorpora la medida europea vigente desde julio de 2026, con IVA y condiciones tratados por separado. Fuente oficial consultada el 16/09: [Consejo de la Unión Europea](https://www.consilium.europa.eu/en/policies/eu-action-influx-of-small-parcels/).

## Evidencia de validación

- Suite completa: **2.202 pruebas aprobadas, 5 subpruebas aprobadas, 0 fallos y 0 omitidas**; 31 advertencias. PostgreSQL local descartable, sin credenciales de producción y con conexiones externas bloqueadas. Duración: 44,44 s.
- Después de la suite se ajustaron únicamente presentación CSS, etiquetas/notas del Excel y un espacio de indentación. Se repitieron las cinco pruebas de exportación: aprobadas.
- Sintaxis compatible con Python 3.11 en los 22 archivos Python modificados/nuevos, 11 plantillas Jinja válidas, ambos JS válidos y `git diff --check` limpio.
- Los 12 fallos de la línea base se resolvieron actualizando expectativas obsoletas de plantillas/fixtures y la distinción entre descargar una guía y que el envío haya comenzado a circular. No se omitieron pruebas ni se alteraron saldos para hacerlas pasar.
- Navegador local con datos sintéticos: escritorio y móvil, temas claro/oscuro; inicio a 320 px, cuenta y control a 390 px sin desborde horizontal. Modal de embalaje a 390 × 844: ancho 343 px, alto 614 px y centrado; escritorio a 1440 × 1000: 560 × 608 px y centrado.
- Recorrido cotización→solicitud: 2,5 kg, 30 × 20 × 10 cm y USD 100,50 conservados; origen CN no precarga domicilio AR; los datos inválidos detienen el avance.
- Pago: búsqueda, selección documental persistente, quitar selección y preservación del borrador al filtrar movimientos. No se presentó ningún comprobante real.
- Admin: 608 registros sintéticos, páginas de 25 filas, búsqueda exacta, filtros independientes y fallo de red provocado seguido de recuperación. Los tests de PostgreSQL también cubren pendientes más allá del registro mil.
- Retención y límite de seguimiento: mensajes visibles y estado original conservado. No se emitieron guías ni se contactó a clientes/couriers.

Archivos locales de evidencia: `/Users/leanrmollo/Documents/TAURO/qa_portal_readiness_20260916/evidence/`, incluidos `full-suite-release.log`, `pytest.xml`, `export-final.xml` y `release-validation.json`.

## Límites y trabajo posterior

Esta versión mejora el portal operativo existente; no certifica automáticamente todas las integraciones ni todos los procesos del negocio.

1. **Prueba de producción después del despliegue:** ingresar como cliente y admin; contrastar saldo, filtros, documentos y exportación con el estado previo. La preview local usa datos simulados y bloquea escrituras; no es un ambiente de emisión.
2. **Shopify y Tienda Nube:** falta un piloto real de punta a punta en las tiendas autorizadas y confirmar elegibilidad/configuración de tarifas en checkout. No se mezclaron las ramas independientes de SOL. No anunciar ambas integraciones como plenamente listas con esta entrega.
3. **Recolecciones:** las reservas antiguas requieren contrastar retiro efectivo con courier; esta entrega no cambia su estado ni da una reserva por retirada.
4. **Operación nacional:** conserva las habilitaciones actuales. No se activaron couriers ni credenciales adicionales.
5. **Administradores:** completar MFA antes de ampliar accesos al equipo.
6. **Sitio público:** quedan por revisar todas las guías restantes y la interpretación del indicador histórico de disponibilidad. No son comprobaciones resueltas por el rediseño del portal.

## Controles de publicación

No se modificaron pagos reales, cargos, márgenes, NC, estados históricos ni archivos de clientes. El trabajo se realiza en un worktree independiente y no incorpora modificaciones ajenas.

El usuario autorizó implementar. La política raíz exige autorización explícita para producción; la autorización del 15/09 documentada en RELEVO corresponde a aquella entrega. Esta rama permanece local hasta autorizar su publicación. Antes de integrar: comprobar si `origin/main` avanzó, resolver y validar cualquier conflicto. Tras publicar: verificar recursos, salud y sesiones reales; no emitir ni pagar para probar sin autorización específica.

Modelo ejecutor: GPT-6 Astra, elegido expresamente por el usuario. Se consultó el enrutador y se mantuvo el nivel de revisión exigido por la política de arquitectura/finanzas; no se delegó a modelos de menor nivel ni se crearon subagentes.
