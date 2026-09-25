# Runbook de durabilidad eCommerce

Estado de entrega: código local, sin despliegue ni llamadas reales. La
publicación automática de tracking queda cerrada por defecto con
`ECOMMERCE_FULFILLMENT_WORKER_ENABLED=false`.

## Antes del piloto

1. Aplicar `sql/schema.sql` dos veces sobre una copia reciente y comprobar que
   la segunda ejecución no produce drift ni errores.
2. Ejecutar la suite PostgreSQL con `TAURO_TEST_DATABASE_URL` apuntando a una
   base exclusiva. No reutilizar una base de desarrollo con datos humanos.
3. Verificar que los tres conteos manuales estén en cero o tengan responsable:

```sql
SELECT estado, count(*) FROM solicitud_automatica_outbox GROUP BY estado;
SELECT estado, count(*) FROM tienda_fulfillment_outbox GROUP BY estado;
SELECT estado, count(*) FROM tienda_cancelacion_obligaciones GROUP BY estado;
```

4. Hacer UAT con una tienda de desarrollo y un pedido Shopify que tenga
   exactamente una fulfillment order elegible. Cero o más de una pasan a
   `MANUAL_REVIEW` sin ejecutar `fulfillmentCreate`.
5. Sólo después del UAT y aprobación humana activar
   `ECOMMERCE_FULFILLMENT_WORKER_ENABLED=true` para el piloto.

## Semántica operativa

- `solicitud_automatica_outbox`: el pedido y su job se confirman en la misma
  transacción. Los updates generan una nueva huella y se reencolan. El worker
  usa `claim_id`, `SKIP LOCKED`, recuperación stale y backoff; datos
  incompletos quedan en `MANUAL_REVIEW` hasta que un update cree otra versión.
- `tienda_fulfillment_outbox`: se inserta en la transacción que guarda el
  tracking. `RECONCILIAR` significa que la escritura remota pudo haber sido
  aplicada; el ciclo siguiente es sólo lectura y no reintenta a ciegas.
- `tienda_cancelacion_obligaciones`: nunca llama al carrier. Una solicitud sin
  tracking, guía, referencia remota ni cargo puede cancelarse localmente
  conservando historia. Cualquier emisión o ambigüedad bloquea automatismos y
  queda en `MANUAL_REVIEW`; facturas, deuda y cargos no se modifican.

## Recuperación

- Un `PROCESANDO` de más de diez minutos es reclamable por otra instancia.
- Los reconciliadores de cinco minutos reponen pedidos o trackings que carezcan
  de outbox, por ejemplo después de una migración desde una versión anterior.
- Para replay manual no se debe editar el tracking ni borrar filas. Corregir la
  causa y mover el job a `REINTENTAR` con `proximo_intento_at=NOW()`, dejando
  evidencia del operador fuera de este procedimiento.

## Límites del piloto

- Shopify: una sola fulfillment order elegible. Split fulfillment es manual.
- La cancelación de una guía ya emitida es manual; este cambio no afirma ni
  simula cancelación OCA/DHL/FedEx/UPS.
- La habilitación del worker no sustituye scopes OAuth, homologación OCA,
  approvals de Tiendanube ni aprobación humana de release.
