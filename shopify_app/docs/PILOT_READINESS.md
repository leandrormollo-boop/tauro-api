# Readiness del piloto Shopify

Fecha de preparación local: 2026-09-25.

## Arquitectura cerrada para v1

- Aplicación pública externa y no embebida (`embedded = false`).
- Backend y UI operativa: `https://taurosolutions.ar`.
- Sin runtime web ni extensiones dentro de esta carpeta.
- Sin tarifas TAURO en checkout ni CarrierService.
- Flujo: pedido Shopify → portal TAURO → guía → fulfillment con tracking.
- Un solo fulfillment order elegible por pedido en el piloto; cualquier caso
  multiubicación queda en revisión manual, sin mutar Shopify.

## Gate local repetible

```sh
npm ci
npm test
npm run typecheck
npm run build
shopify app config validate --json
```

La CLI debe ejecutarse con telemetría desactivada y la atribución exigida por el
entorno. Ninguno de estos comandos despliega o cambia la aplicación remota.

## Bloqueos externos antes del piloto real

- Configurar credenciales públicas y clave de cifrado en un entorno controlado.
- Confirmar en Dev Dashboard URLs, scopes y webhooks del manifiesto.
- Instalar en una development store limpia y activa.
- Completar OAuth, crear un pedido de prueba, comprobar su ingreso al portal,
  emitir una guía de prueba y confirmar fulfillment + tracking.
- Registrar evidencia del recorrido y del consentimiento de scopes.

No desplegar ni presentar al App Store hasta que esos puntos tengan evidencia.
