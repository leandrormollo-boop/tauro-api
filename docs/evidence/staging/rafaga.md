# TAURO staging webhook burst

Result: **PASS**

La prueba se ejecutó dentro de la instancia Railway de staging para medir la
API sin sumar la latencia geográfica de una conexión externa. No se usó una
tienda real: `tauro-qa.myshopify.com` fue una instalación sintética y aislada,
creada sólo para validar HMAC y eliminada al finalizar.

La ejecución corresponde al deployment Railway
`8a71e3de-64b8-40d7-bccc-d60d34117fba` (ref `6682061`), con Python `3.11.16`.
Una verificación posterior de sólo lectura registró que había una sola
instalación sintética antes de la ráfaga y cero después de limpiarla. La ráfaga
no tocó recursos Shopify de producción.

| Metric | Value |
| --- | ---: |
| Webhooks attempted | 200 |
| Webhook HTTP 2xx | 200 |
| Burst dispatch (ms) | 6668.469 |
| Burst duration (ms) | 6840.658 |
| Webhook p95 (ms) | 446.079 |
| Health samples | 101 |
| Health HTTP 2xx | 101 |
| Health p95 (ms) | **226.818** |

Webhook statuses: `{"200":200}`

Webhook errors: `{}`

Health statuses: `{"200":101}`

Health errors: `{}`

## Checks

- PASS — `all_requested_webhooks_attempted`
- PASS — `at_least_200_webhook_2xx`
- PASS — `burst_dispatched_within_window`
- PASS — `health_all_2xx`
- PASS — `health_p95_below_300_ms`
- PASS — `health_sample_count_met`
- PASS — `no_redirect_responses`

## Privacy and isolation

- El secreto HMAC no fue impreso ni persistido.
- No se guardaron cuerpos ni headers de requests o responses.
- Los identificadores firmados fueron exclusivos de staging y no representan
  recursos Shopify válidos, por lo que no hubo lecturas ni escrituras en una
  tienda externa.
- `webhook-burst.json` separa las métricas emitidas por el script de la
  verificación posterior de creación/limpieza de la instalación sintética.
