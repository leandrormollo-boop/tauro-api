# Embalajes guardados y tarifas de tienda

Implementación del pedido del 11/09/2026. Rama `codex/paquetes-preguardados`,
basada en `da36e53`. Pendiente de publicación y prueba de checkout real.
No se modificaron cuentas ni ventas reales de Pesca Jacks.

## Qué puede hacer el cliente

En `/portal/paquetes`:

1. Guardar embalajes con medidas externas, peso de caja vacía, protección y
   peso máximo de caja llena; editar los que todavía no estén asociados o
   archivar los que ya no use.
2. Asociar variantes del catálogo a una caja, indicando peso de una unidad
   y cuántas unidades caben. La confirmación física es obligatoria.
3. Guardar combinaciones exactas: por ejemplo, 1 reel + 2 señuelos en una
   caja mediana. Cada combinación tiene prioridad y confirmación física.
4. Configurar, por tienda y ámbito, tarifa TAURO, porcentaje adicional,
   precio fijo o envío gratis; también un mínimo de compra para bonificar.
5. Probar un carrito con país, ciudad y código postal reales: ve cajas,
   contenido, peso, tarifa de su cuenta y precio para su comprador.

Los datos de stock e imágenes existentes no se modifican. Las imágenes del
catálogo se muestran junto a las asociaciones. El catálogo sigue teniendo
su flujo de sincronización y de revisión de datos aduaneros.

## Criterio de embalaje

Se aplican combinaciones verificadas por prioridad descendente; ante empate,
la de más unidades y luego su ID. Se repiten mientras haya contenido
suficiente. Lo restante usa la capacidad confirmada de cada variante.
Una caja parcialmente llena conserva las medidas externas de esa caja y
pesa sólo su contenido efectivo más caja y protección.

No se suman volúmenes para inventar dimensiones. Tampoco se afirma que el
resultado sea el mínimo geométrico o el envío más barato posible. Para
cambiar medidas de una caja asociada, el cliente crea otra y vuelve a
confirmar su contenido. Los límites son 100 embalajes activos, 200
combinaciones, 100 unidades y 20 cajas por cotización; hasta 70 kg por caja,
sin superar los límites propios del servicio. Divisor volumétrico: 5000.

Productos sin peso/asociación, variantes ajenas o ambiguas, cajas archivadas
y exceso de peso bloquean la cotización con un mensaje. Una variante
Shopify se identifica por su GID dentro de su tienda y cuenta; los IDs
numéricos del callback se normalizan a GID. Un SKU no reemplaza una variante
desconocida. Para un artículo sin ID de variante, sólo se permite SKU único
de esa tienda o un producto manual del mismo cliente.

## Integraciones

**Shopify.** El endpoint histórico `/shopify/tarifas` sigue vacío para no
revivir CarrierServices retirados. El nuevo callback usa una URL con secreto
aleatorio, hash en PostgreSQL y vínculo a cliente, tienda y generación OAuth.
Comprueba instalación, webhooks y permiso `write_shipping` en cada pedido
de tarifa. No confía en el header de dominio ni llama a Admin API al cotizar.

La activación es por tienda. El consentimiento adicional se inicia desde
una tienda ya vinculada, usando el state/cookie existente. Los scopes base
permanecen iguales para quienes no usan tarifas. Es necesario declarar el
permiso adicional en la configuración de la app Shopify antes de su uso.
La conexión registra o reconcilia el CarrierService por GraphQL y evita
duplicarlo en un reintento. Luego el comerciante agrega TAURO en sus zonas
de envío y configura tarifas de respaldo en Shopify.

CCS requiere Advanced, Plus o Grow con esa función habilitada. Se devuelven
importes ARS en centavos y códigos estables por servicio. Un error de
configuración, moneda no soportada o caída devuelve 503 para permitir el
respaldo de Shopify; un ámbito deshabilitado devuelve una lista vacía.
Cuatro workers como máximo y respuesta limitada a 2,4 segundos. Si un
courier tarda más, su worker sigue ocupado hasta finalizar, sin crear una
cola ilimitada. Se debe medir latencia real antes de activar una tienda.

**Tiendanube.** Se mantiene su callback autenticado, registro de carrier,
límites contractuales y separación `price`/`price_merchant`. Si la tienda
opta por embalajes, usa el mismo plan, también al recotizar la parte paga
de un carrito con envío gratis por artículo. Su integración actual es
nacional; no se permite activar internacional en la tienda. El portal sí
puede cotizar rutas internacionales mediante sus couriers habilitados.

**API para otras integraciones.** `GET /api/paquetes` devuelve embalajes e
IDs de productos propios. `POST /api/paquetes/cotizar`, con `X-API-Key`, usa
el mismo simulador. Nunca se debe colocar esa clave en código público del
navegador del comprador; la integración se hace desde el servidor del comercio.

Ejemplo de cuerpo (IDs de producto obtenidos del GET):

```json
{
  "items": [{"producto_id": 123, "cantidad": 2}],
  "valor_ars": "150000.00",
  "origen": {"pais": "AR", "cp": "1425", "ciudad": "CABA"},
  "destino": {"pais": "US", "cp": "33101", "ciudad": "Miami", "estado": "FL"}
}
```

`tienda_id` opcional aplica la política de una tienda propia. La respuesta
incluye `plan`, `opciones`, `encontrado` y `motivo`. Las opciones sólo contienen
precios comerciales; nunca costo interno, margen TAURO ni credenciales.

## Operadores y ventas

- Nacional: usa los adapters registrados OCA/Andreani. Hoy OCA tiene código
  de cotización, apagado salvo credenciales, operativa y homologación completas.
  Se conserva el pricing nacional estricto por cliente. No se inventa una
  tarifa, ni siquiera cuando el cliente ofrece envío gratis.
- Internacional: reutiliza los couriers autorizados y el pricing de la
  cuenta, con origen/destino reales y una pieza física por caja. La primera
  versión devuelve ARS para todos los destinos. No incluye impuestos de
  importación. Los carritos con varios artículos por caja necesitan DHL
  para poder conservar la declaración completa.
- La venta con embalajes habilitados conserva un plan por pedido. En
  internacional prepara una solicitud con cajas e invoice completas cuando
  sus datos están revisados; no sustituye el courier elegido en checkout.
  La factura aduanera usa valores USD del catálogo para revisión por el
  comerciante; no sustituye la factura comercial real ni inventa datos faltantes.
- Si faltan datos, la venta queda pendiente con motivo. Al abrir Preparar
  envío se recuperan las cajas del plan para completar los datos aduaneros.
- Los planes pendientes pueden actualizarse al reintentar un pedido editado;
  una solicitud ya creada conserva sus bultos. El INSERT de solicitud usa la
  clave determinística y el linaje del circuito previo para evitar duplicados.
- La emisión nacional sigue pendiente de habilitarse. No se emite ninguna
  guía automáticamente ni se descuenta saldo al guardar cajas o cotizar.
- El importe cobrado al comprador y la tarifa de la cuenta TAURO siguen
  separados. Cambiar la política hoy no recalcula retrospectivamente el flete
  cobrado en una venta ya recibida.

## Despliegue y piloto

`sql/paquetes.sql` está replicado al final de `sql/schema.sql` para que el
arranque y las migraciones existentes creen las tablas de forma idempotente.
No requiere paquetes Python adicionales. El alta no activa automáticamente
tiendas anteriores. No modifica cuentas corrientes, tracking ni inventario.

Después de autorizar la publicación: comprobar `/salud`, `/portal/paquetes`,
guardar/editar/asociar con la cuenta real, habilitar los permisos y revisar
CCS de Pesca Jacks. Cargar únicamente medidas, pesos y combinaciones
confirmados por el comercio. Probar 1 reel, varias unidades, combinación
mixta, varios bultos, envío gratis, destino internacional, servicio caído y
venta recibida con su correspondiente plan. La tarifa del navegador local
es simulada y no reemplaza esta prueba con cuentas reales.

Fuentes oficiales consultadas el 11/09/2026:

- [Shopify CarrierService y contrato de tarifas](https://shopify.dev/docs/api/admin-graphql/latest/queries/carrierService)
- [Creación de CarrierService](https://shopify.dev/docs/api/admin-graphql/latest/mutations/carrierServiceCreate)
- [Disponibilidad de envío calculado por terceros](https://help.shopify.com/en/manual/fulfillment/setup/shipping-rates/third-party-carrier-calculated-shipping)
