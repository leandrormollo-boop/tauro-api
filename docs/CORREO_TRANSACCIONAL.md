# Correo transaccional TAURO

## Qué cubre

- Presupuestos del cotizador público: el servidor guarda la tarifa exacta, genera una referencia `TW-...` y el navegador sólo solicita el correo usando un `quote_id` opaco.
- Restablecimiento del portal: pedido en cola durable, token hasheado, 30 minutos, un uso y revocación de sesiones al cambiar la contraseña. El secreto viaja en el fragmento `#token=`: no entra en access logs ni en la query del navegador.
- Recupero de emergencia del admin: el bearer también se guarda hasheado y viaja por fragmento, con canje por POST de un solo uso.
- Todos estos correos se envían como `multipart/alternative` (texto + HTML), con TLS, timeout, `Date`, `Message-ID` y `Reply-To`.

`ENVIADO` significa que el servidor SMTP aceptó el mensaje. No garantiza que el proveedor del destinatario lo coloque en Inbox; también intervienen SPF, DKIM, DMARC y sus filtros.

## Variables obligatorias en Railway

```text
BASE_URL=https://taurosolutions.ar
EMAIL_REMITENTE=operaciones@taurosolutions.ar
EMAIL_PASSWORD=app-password-de-google
EMAIL_FROM=TAURO Operaciones <operaciones@taurosolutions.ar>
EMAIL_REPLY_TO=operaciones@taurosolutions.ar
```

Opcionales:

```text
EMAIL_SMTP_HOST=smtp.gmail.com
EMAIL_SMTP_PORT=587
EMAIL_SMTP_TIMEOUT=15
EMAIL_COTIZACIONES_MAX_HORA=100
EMAIL_ADMIN_RECOVERY_MAX_HORA=6
COTIZACION_WEB_VIGENCIA_HORAS=24
```

`EMAIL_FROM` es deliberadamente estricto: la aplicación rechaza el envío si no resuelve exactamente a `operaciones@taurosolutions.ar`. Esa identidad debe estar autorizada en Gmail/Workspace como **Enviar como** para el usuario de `EMAIL_REMITENTE`; Gmail no debe reescribirla con la cuenta personal. Nunca guardar la contraseña en la tabla `config`, el repositorio, Postman, logs o capturas.

## Diagnóstico

En `/admin/config` aparece **Correo transaccional** con:

- presencia o ausencia de variables SMTP (no es un health check);
- dominio visible y host, sin secretos;
- aceptados, fallidos y en proceso durante las últimas 24 horas.

Sólo los rechazos transitorios confirmados por SMTP (`4xx`) se reintentan hasta cuatro veces mientras la estimación siga vigente. Un timeout, corte de red o caída del proceso puede haber ocurrido después de la aceptación: queda en `VERIFICAR_EMAIL` y no se reenvía a ciegas. Errores de autenticación/configuración tampoco se reintentan automáticamente.

Para evitar abuso, el límite durable es una cotización enviada o incierta por destinatario cada 24 horas, además del rate-limit por IP. También existe un tope global durable por hora (`EMAIL_COTIZACIONES_MAX_HORA`, 100 por defecto), independiente de headers de proxy. El recupero del admin tiene su propio cupo durable (`EMAIL_ADMIN_RECOVERY_MAX_HORA`, 6 por defecto), además del límite por IP, para proteger la casilla aunque el origen directo reciba headers falsificados. La deduplicación de la misma referencia funciona entre procesos. Los leads se conservan 365 días; las cotizaciones vencidas sin referencias, 30 días; los metadatos de recupero, 30 días, y sus hashes vencidos, 7 días.

## Gate de producción

1. Confirmar `BASE_URL=https://taurosolutions.ar` y todas las variables anteriores.
2. Verificar en Google Workspace que el alias del remitente esté autorizado.
3. Verificar SPF, DKIM y DMARC para `taurosolutions.ar` con el proveedor DNS.
4. Desplegar primero en staging o en una ventana controlada.
5. Enviar una cotización y un restablecimiento únicamente a una casilla propia autorizada. Confirmar que el reset abre `/portal/password/reset` sin query string y limpia el fragmento antes de mostrar el formulario.
6. Confirmar HTML, texto plano, enlace, Spam/Promociones y métricas de `/admin/config`.
7. Recién entonces habilitar el flujo a clientes.

Las pruebas automáticas siempre usan un SMTP falso y nunca envían correos reales.
