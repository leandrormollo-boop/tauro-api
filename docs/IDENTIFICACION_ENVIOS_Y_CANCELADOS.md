# Identificación de envíos y cancelaciones — 17/09/2026

Preparado en `codex/shipment-identity-20260917`, sobre `d2f5887`.
Publicación autorizada por el usuario: «publicalo». Cambio `7414c12`.
Sin migraciones ni operaciones reales de prueba.

## Presentación

Mis envíos y Cuenta corriente usan filas separadas y tarjetas en celular.
Destinatario destacado, referencia del cliente, número TAURO existente (o ID
histórico, claramente rotulado), ruta completa, fecha y tracking. La ruta ya no
queda escondida en el desglose. Se incorporan origen/ciudad y número TAURO al
listado, sin cargar PDFs. Ambas búsquedas admiten el número TAURO.

CANCELADO y REEMPLAZADO tienen distintivo y borde ámbar. No muestran el precio
anterior ni el importe «a tu cliente». El detalle también muestra «Sin cargo».
Las etiquetas descartadas no se descargan ni se ofrecen para emitir.

## Cuenta e integridad

Se agregan registros informativos de solicitudes canceladas/reemplazadas
visibles, no de prueba, del cliente autenticado. Una fila por solicitud,
con cargos/créditos/importes en cero y valor del envío nulo. Incluye las que
nunca generaron cargo. Filtro «Cancelados / reemplazados» y exportación con los
mismos filtros. Quedan fuera de «Cargos y ajustes» y de sus gráficos.

No se modifica ningún saldo, cargo, pago, factura, importe ni estado persistido.
Si existe una inconsistencia (solicitud cancelada con cargo todavía activo),
no se agrega una fila «sin cargo» ni se oculta el débito real: se señala el estado
operativo y «Cargo aún registrado», con ayuda para solicitar conciliación.
Eso requiere una revisión contable separada; este cambio no aplica créditos.

Los registros informativos usan la fecha de creación de la solicitud y el corte
habitual de la cuenta. No reconstruyen cancelaciones anteriores al corte.

## Validación

- Suite completa: **2.324 pruebas y 5 subpruebas aprobadas**, PostgreSQL local.
- Casos reales de SQL en schema aislado: cancelado con/sin cargo previo,
  reemplazado, cargo activo inconsistente, cliente ajeno, oculto y de prueba.
- Verificados impacto cero, preservación del libro, filtros, búsqueda por TAURO,
  fechas, ámbito y exportación; se mantienen las pruebas de redondeos y pagos.
- Render de cancelados con importes históricos: no reaparecen los costos en el
  listado ni en el detalle, y no hay descarga de etiqueta inválida.
- Navegador local con datos ficticios: 1280 px, 390 px y 320 px; ambos temas;
  filtro mediante AJAX, búsqueda, ayuda por clic/Escape y ausencia de desborde.
- Revisión de producción: lectura de la cuenta de WAIMAO. No se emitieron guías,
  retiros, cargos, pagos ni cambios de datos.

Evidencia local: `qa_shipment_identity_20260917/` en el workspace TAURO.
Vista previa de sólo lectura en `http://127.0.0.1:8779/portal/cuenta#movimientos`.
