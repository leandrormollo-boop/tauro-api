# Checklist de homologación · TAURO Nacional

No marcar un control por intención. Cada `OK` necesita evidencia reproducible.

## Cuenta y acceso

- [ ] App creada como **Tienda de Aplicaciones**, categoría **Shipping**.
- [ ] `app_id` y `client_secret` productivos entregados a TAURO.
- [ ] Redirect URL exacta configurada.
- [ ] Scopes exactos: `write_shipping`, `write_orders`, `read_customers`.
- [ ] Shipping API habilitada por el Platform Team para cuenta y tienda demo.
- [ ] Tienda demo en Argentina disponible para el revisor.

## Producto nacional

- [ ] Adapter OCA o Andreani implementado contra QA oficial.
- [x] Cotización OCA implementada contra [`Tarifar_Envio_Corporativo`](https://developers.oca.com.ar/epak.html), apagada por defecto.
- [ ] UAT OCA con CUIT/cuenta/operativa propios y evidencia de costo/SLA.
- [ ] Contrato comercial y operativa/cuenta confirmados.
- [ ] Tarifas contractuales determinísticas; sin precios inventados.
- [ ] Cotización AR→AR con peso, volumen, CP y valor declarado.
- [ ] SLA del callback p95 menor a 4 segundos; timeout máximo 5 segundos.
- [ ] Carrito sin peso/medidas devuelve 422 y no una tarifa falsa.
- [ ] Carrito mixto distingue `price` de `price_merchant`.
- [ ] Emisión idempotente y nunca automática por el simple webhook de venta.
- [ ] Emisión, etiqueta, tracking y cancelación OCA implementados y probados; no se anuncian hasta cerrar su contrato neutral e idempotencia.

## Instalación y seguridad

- [ ] Instalación iniciada desde Tiendanube, no desde el panel TAURO.
- [ ] OAuth canjea el código una sola vez y valida el redirect configurado.
- [ ] El merchant nuevo puede crear/vincular su cuenta tras instalar.
- [ ] Reinstalación no duplica carrier, opciones ni webhooks.
- [ ] Tokens de acceso no aparecen en logs, frontend ni respuestas.
- [ ] Clave exclusiva `TIENDANUBE_TOKEN_ENCRYPTION_KEY` cargada y rotación ensayada.
- [ ] Callback de tarifas usa un token aleatorio distinto por tienda.
- [ ] Webhooks validan firma, contrato de payload e idempotencia durable.
- [ ] `app/suspended` corta cotizaciones y llamadas a la API.
- [ ] `app/uninstalled` revoca el vínculo operativo.
- [ ] Solicitudes de eliminación/privacidad quedan registradas y resueltas.
- [ ] Política de privacidad y términos públicos responden 200 por HTTPS.

## NubeSDK

- [x] Código sin `window`, `document`, jQuery ni manipulación del DOM.
- [x] Tests, typecheck y build local verdes.
- [ ] Bundle `main.min.js` cargado marcando **Uses NubeSDK**.
- [ ] Validado en tienda demo con NubeSDK DevTools.

## Artefactos de homologación

- [x] Diagrama de secuencia preparado.
- [x] Guion de demostración preparado.
- [ ] Video real grabado sobre tienda demo y producción candidata.
- [ ] Credenciales demo sin período de espera ni pago obligatorio.
- [ ] Evidencia de instalación, reautorización, suspensión y desinstalación.
- [ ] Evidencia de tarifa, pedido, guía y tracking de punta a punta.
- [ ] Icono 600×600 PNG/JPEG sin texto ni bordes tocados.
- [ ] Capturas 1600×800.
- [ ] Datos de publicación, soporte, precios y handle completados.

## Release

- [ ] Preflight determinístico sin bloqueadores.
- [ ] Backup y plan de rollback documentados.
- [ ] Aprobación humana explícita del release productivo.
- [ ] Deploy controlado con flags apagados.
- [ ] Smoke OAuth en demo.
- [ ] Activación gradual de flags.
- [ ] Primera venta y primer tracking monitoreados.
