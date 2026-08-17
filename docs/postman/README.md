# Pruebas de Shopify con Postman

Esta colección valida el flujo de TAURO sin guardar credenciales en Git ni
emitir guías. El alcance actual es:

1. Shopify envía una venta por webhook.
2. TAURO verifica la firma y guarda o actualiza el pedido sin duplicarlo.
3. TAURO intenta preparar una solicitud para que el cliente la revise.
4. La guía **no se emite automáticamente**.
5. Cuando una guía se emite desde TAURO, el tracking vuelve a Shopify.

La integración no calcula la tarifa del checkout. El comerciante conserva sus
tarifas y zonas de envío dentro de Shopify.

## Preparación

1. Crear o elegir una **development store** de Shopify. No usar una tienda de
   un cliente para estas pruebas.
2. Importar estos dos archivos en Postman:
   - `TAURO-Shopify.postman_collection.json`
   - `TAURO-Shopify.local.postman_environment.json`
3. Duplicar el entorno importado y completar localmente:
   - `shop_domain`: dominio exacto `*.myshopify.com`.
   - `shopify_access_token`: token de esa development store.
4. Crear una **app de desarrollo separada** en Shopify. No usar ni copiar el
   secreto de la app productiva de TAURO. Configurar la instancia local de
   TAURO con el secreto de esa app de desarrollo.
5. En Postman Vault, crear el secreto local
   `tauro-shopify-dev-app-secret` con ese valor. Habilitar el acceso a Vault
   para scripts cuando Postman lo solicite. Vault no sincroniza ese valor al
   workspace ni lo exporta con la colección.
6. No exportar ni compartir nuevamente el entorno con tokens cargados.

## Orden seguro de ejecución

Ejecutar primero toda la carpeta **00 · Diagnóstico seguro**. La consulta
GraphQL sólo lee el nombre y dominio interno de la tienda.

La carpeta **10 · Webhooks controlados** está deshabilitada por defecto y usa
`pm.vault.get`, por lo que se ejecuta manualmente en la app de Postman (no en
monitores, Newman ni Postman CLI). Para probarla:

1. Apuntar `base_url` a una instancia de TAURO con base de datos de prueba.
2. Verificar que `shop_domain` corresponde a la tienda vinculada en esa base.
3. Cambiar `allow_tauro_webhook_writes` a `true` sólo durante la ejecución.
4. Ejecutar los requests en orden y volver el valor a `false`.

El pedido usa un email reservado bajo `.invalid` y un SKU inexistente. Ese SKU
impide que el proceso automático encuentre un producto de catálogo y, por lo
tanto, evita que prepare una solicitud cotizable. Aun así, el request escribe
una fila sintética y no debe apuntarse a producción.

## Qué no hace Postman

- No reemplaza el consentimiento OAuth: la instalación se completa en un
  navegador donde el dueño de la development store inicia sesión.
- No crea pedidos reales en Shopify.
- No emite guías ni llama a DHL/FedEx.
- No prueba el mail de tracking al comprador. Eso requiere una orden de prueba,
  un tracking controlado y aprobación explícita antes de marcarla cumplida.

## Criterio de aprobación

- Salud TAURO: 200.
- PostgreSQL: `status=ok`, `db=ok`.
- GraphQL: el dominio devuelto coincide con `shop_domain`.
- Firma falsa: 401 y cero escritura.
- Primera entrega válida: `ok=true`.
- Segunda entrega con el mismo order id: `nuevo=false`.
- Topic no operativo: 200 con `ignorado`.
