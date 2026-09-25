# Autoevaluación Shopify App Store — TAURO Solutions

Fecha de corte: 25/09/2026.

Fuente: requisitos canónicos obtenidos con `shopify doc fetch` desde
`app-store-ai-self-review-requirements` y guardados durante la revisión en
`/tmp/tauro-shopify-review-requirements-20260925.md`.

## Resultado local

| Estado | Cantidad |
|---|---:|
| ✅ Evidencia favorable | 27 |
| ❌ Falla local detectada | 0 |
| ⚠️ Requiere decisión/verificación externa | 4 |
| ⏭️ No aplicable, grupos omitidos | 69 |

Los 100 requisitos del archivo quedaron clasificados. Este control revisa lo
observable en el repositorio; no reemplaza la revisión de Shopify ni confirma
el estado del Dev Dashboard, el certificado del deploy o la política comercial.

## Evidencia favorable

| Grupo | PASS | Evidencia resumida |
|---|---:|---|
| 1.1 Política de plataforma | 13 | No existe checkout alternativo, procesamiento de pagos/refunds, fee opcional, POS, lending, marketplace ni modificación de themes. La app opera sobre pedidos reales de cada tienda y no fabrica actividad. |
| 2.2 APIs y herramientas Shopify | 5 | App Bridge CDN se carga primero; Admin API usa GraphQL 2026-07. No hay admin extensions promocionales ni Max modal automático. |
| 2.3 Instalación segura | 4 | La instalación parte de Shopify, autentica inmediatamente por OAuth, valida HMAC + `state`, vuelve al App Home y admite reinstalación creando una generación nueva. |
| 3.2 Scopes mínimos | 5 | El manifiesto sólo declara `read_orders`, `read_products`, `read_inventory`, `read_locations` y `write_merchant_managed_fulfillment_orders`; no solicita ninguno de los scopes sensibles enumerados por el grupo. |

Referencias principales del repositorio:

- `shopify_app/shopify.app.toml`: superficie embebida, URLs, versión, scopes y
  webhooks obligatorios.
- `servicios/shopify_embedded.py` y `endpoints/shopify.py`: App Bridge,
  ID/session token, CSP, instalación y OAuth inmediato.
- `servicios/shopify_app.py`: GraphQL, token rotativo cifrado, ownership y
  generaciones de instalación.
- `endpoints/integraciones.py`: HMAC, clasificación, validación remota y
  procesamiento durable de webhooks.
- `shopify_app/tests/app-config.test.mjs`: contrato estático del manifiesto.

## ⚠️ Requisitos que requieren revisión

### 1.2.1 — Usar Shopify App Pricing o Shopify Billing API

**Por qué queda abierto:** TAURO pretende publicar la app como *Free to
install* y facturar por fuera el transporte físico prestado al comerciante. El
repositorio no contiene Billing API, lo cual es correcto sólo si Shopify
confirma que ese flete no constituye un cargo de la app.

**Cierre requerido:** obtener confirmación escrita de Shopify. Si lo considera
un app charge, implementar App Pricing/Billing antes de presentar el listing.

### 1.2.2 — Implementar correctamente App Pricing/Billing

**Por qué queda abierto:** hoy no existe un flujo de cobro de app, por lo que
no hay aceptación/rechazo o reautorización de cargos que evaluar. La
aplicabilidad depende de la decisión del punto 1.2.1.

**Cierre requerido:** si Shopify exige Billing, probar aprobación, rechazo,
cancelación y nuevo consentimiento después de reinstalar. Si confirma que la
app es gratuita y el flete físico queda fuera, documentar esa respuesta para el
revisor.

### 1.2.3 — Permitir cambios de plan

**Por qué queda abierto:** TAURO no ofrece planes de app en este candidato. Si
el flete externo queda permitido, el requisito no agrega implementación; si se
incorpora App Pricing, el comerciante deberá poder cambiar de plan sin soporte
ni reinstalación.

**Cierre requerido:** cerrar primero la clasificación comercial y reflejar la
decisión final tanto en producto como en el listing.

### 3.1.1 — Certificado TLS/SSL válido

**Por qué queda abierto:** manifiesto, callbacks y `BASE_URL` usan HTTPS, pero
el repositorio no puede demostrar el certificado, la cadena ni la respuesta
del dominio una vez desplegado.

**Cierre requerido:** después del deploy UAT verificar desde una red externa
`https://taurosolutions.ar`, App Home, callback OAuth y los tres endpoints de
compliance sin error de certificado ni fallback HTTP.

## Grupos no aplicables

| Grupo | Requisitos N/A | Motivo |
|---|---:|---|
| 5.1 Online store | 3 | No hay theme app extension ni modificación de theme. |
| 5.2 Payment | 8 | No hay payment extension ni scope de payment gateway. |
| 5.3 Payment facilitator | 1 | Grupo opt-in no solicitado; TAURO no es facilitador de pagos. |
| 5.4 Purchase option | 16 | No hay subscriptions, selling plans ni payment mandates. |
| 5.5 Product sourcing | 3 | Grupo opt-in no solicitado; la app no es dropshipping/sourcing. |
| 5.6 Checkout customization | 6 | No hay checkout UI extension. |
| 5.7 Sales channel | 14 | No hay `channel_config`; TAURO es una app de shipping/fulfillment. |
| 5.8 Post purchase | 10 | No hay post-purchase extension. |
| 5.9 Mobile app builders | 2 | Grupo opt-in no solicitado; la app no construye apps móviles. |
| 5.10 Donation | 6 | Grupo opt-in no solicitado; la app no procesa donaciones. |

Total omitido: **69 requisitos en 10 grupos**.

## Controles externos adicionales antes de enviar

Estos puntos no cambian el conteo anterior, pero bloquean la presentación:

- obtener aprobación Level 2 para los protected customer data estrictamente
  necesarios para despachar;
- completar UAT en una development store activa, incluida reinstalación,
  aislamiento entre tiendas, replay, timeout y fulfillment/tracking;
- confirmar en Dev Dashboard URLs, scopes y los tres compliance webhooks;
- preparar screencast, instrucciones y acceso de revisión sin depender de una
  explicación oral.

## Endurecimiento aplicado en este candidato

- OAuth siempre deja la instalación ownerless. El claim compara el email del
  cliente TAURO con `shop.email`/`shop.contactEmail` y revalida bajo lock la
  generación exacta antes de vincular.
- Los webhooks atan el body firmado al recurso remoto visible para esa tienda
  antes del dedupe. El estado remoto impide que un evento atrasado reviva una
  orden cancelada/anulada/reembolsada.
- El predeploy cifra access/refresh tokens históricos en texto plano y el
  runtime falla cerrado si encuentra un token legacy.

## Recursos

- [App Store requirements](https://shopify.dev/docs/apps/launch/shopify-app-store/app-store-requirements)
- [Best practices for apps](https://shopify.dev/docs/apps/launch/shopify-app-store/best-practices)
- [About billing for your app](https://shopify.dev/docs/apps/launch/billing)
- [Submitting your app for review](https://shopify.dev/docs/apps/launch/app-store-review/submit-app-for-review)
