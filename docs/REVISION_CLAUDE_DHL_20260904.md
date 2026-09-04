# Revisión de Claude — entrada de facturas DHL

Fecha: 04/09/2026. Código revisado: `7d3a117fd3d6009d9fd4b2b97b1fe6ebafe38174`.
Base indicada al revisor: `5f5908e`.

## Resultado ejecutivo

Claude considera la base **apta para un piloto manual supervisado**, condicionado
a no aplicar diferencias al cliente sin verificar el tipo de cambio. No la aprueba
para correo automático, que aún no está implementado.

Mi recomendación después de reproducir los hallazgos: **corregir H1 antes de
publicar esta etapa**, y resolver H2/H3 en el mismo corte de robustez. Un aviso de
revisión pendiente no sustituye un bloqueo del servidor. La importación por sí
sola sigue sin crear cargos: H1 aparece al continuar con acciones posteriores de
confirmación/cálculo/aplicación de diferencias.

No se modificó código de la aplicación, no se desplegó y no se escribieron datos
productivos. Las reproducciones se ejecutaron exclusivamente en schemas de
PostgreSQL sintéticos descartables.

## Hallazgos contrastados

### H1 — P1 — Falta exigir revisión financiera del tipo de cambio

- `servicios/bandeja_facturas_dhl.py:149`: la importación marca
  `revision_financiera_pendiente=True`.
- `servicios/lector_pdf_dhl.py:166`: cada línea recibe el cambio impositivo del PDF.
- `servicios/conciliacion_couriers.py:1362`: la conciliación toma como costo real
  el ARS calculado con ese cambio.
- `servicios/conciliacion_couriers.py:2126`: la aplicación del ajuste no consulta
  esa bandera. Tampoco la consulta la confirmación de coincidencias.

**Confirmado.** En un envío sintético se importó la evidencia, se confirmaron
coincidencias, se calculó y se pudo aplicar una diferencia conservando todavía
`revision_financiera_pendiente=True`. No ocurrió con clientes reales.

Debe existir una decisión financiera explícita y auditada antes de permitir
trasladar diferencias. Si la política usa otro tipo de cambio, hay que conservar
separadamente la conversión documental y la conversión aprobada para conciliación;
no alcanza con apagar una bandera sobre valores que ya quedaron congelados.
La política cambiaria debe definirla el responsable de TAURO, no el lector.

### H2 — P2 — Las extracciones de versiones anteriores siguen importables

- `servicios/bandeja_facturas_dhl.py:106`: una extracción en revisión no se relee.
- `servicios/bandeja_facturas_dhl.py:133`: importar valida la huella guardada,
  pero no la versión guardada contra la versión vigente del lector.
- `sql/schema.sql:3246`: tampoco hay un estado terminal de descarte de la entrada.

**Confirmado.** Después de generar una extracción y simular un incremento de
`LECTOR_VERSION`, la extracción anterior pudo importarse. Debe poder invalidarse
una versión problemática sin perder evidencia y generarse una revisión nueva.
Conviene agregar descarte con motivo, auditoría y preservación del original.

### H3 — P2 operativo — Saturación y error documental comparten estado

- `servicios/ejecucion_lector_dhl.py:21`: el lector ocupado produce
  `ExtraccionDHLInvalida`.
- `servicios/bandeja_facturas_dhl.py:125`: el mismo manejo registra estado
  `REVISION_MANUAL` tanto para saturación como para un documento no reconocido.

**Confirmado.** Simulando falta temporal de capacidad, una entrada válida se
etiqueta como revisión manual y aumenta el contador de intentos. No altera
importes, pero confunde la operación. Separar errores transitorios de rechazo
documental y mostrar un reintento claro.

### H4 — P3 — Dependencias innecesarias del lector

Para importar una excepción, el lector importa transitivamente el módulo de
conciliación, `core.database` y `psycopg2`. Se comprobó la dependencia; el módulo
actual no carga dotenv ni abre la base al importarse. **No se confirmó una fuga
de credenciales.** Separar la excepción en un módulo mínimo mejora el aislamiento
y reduce dependencias; es una mejora preventiva, no una vulnerabilidad demostrada.

### H5 — P3 / pendiente de evidencia — Redondeo de conversión documental

Claude señaló que `total * fx == ars` puede rechazar formatos con otros decimales
o redondeos. Hoy eso deriva a revisión manual: no genera un importe incorrecto.
No se aportó una factura real adicional que demuestre ese formato.

Además, el ejemplo numérico del informe original no cierra: el producto exacto
de 148,36 por 1350,485 es 200357,95460, no 200330,35. Se comprobó con `Decimal`.
**No adoptar su tolerancia ni elegir otra fuente de TC sólo por esa sugerencia.**
Ampliar cobertura únicamente contra originales y una política explícita.

## Controles que Claude consideró bien resueltos

- Importar no confirma coincidencias ni modifica saldos, precios o cargos.
- Coincidencias sólo por courier y tracking único; impuestos generales sin asignar.
- Importes con Decimal y controles de subtotal, impuestos, total y renglones.
- Evidencia y extracción congeladas, huella de revisión y auditoría.
- Dedupe por PDF y documento; concurrencia y rollback atómico de la importación.
- Admin obligatorio, formularios con HMAC/alcance/vencimiento y PDFs privados.
- Límites de tiempo, memoria y concurrencia del proceso de lectura.

No encontró un caso demostrado de asignación cruzada de pesos/cargos que pase
todos los controles del formato admitido. Eso no prueba ausencia de otros bugs.

## Verificación local posterior al informe

- 146 pruebas específicas del módulo: aprobadas, 22 advertencias existentes.
- Tres pruebas diagnósticas independientes: reproducen H1, H2 y H3.
  Que estas pruebas pasen confirma la reproducción, **no que el bug esté corregido**.
- El lector de uploads ya limita a 8 MB y rechaza entradas sin interfaz de archivo;
  se resolvió esa incertidumbre que Claude había dejado por falta del extracto.
- El código de la aplicación conserva exactamente el commit revisado.

## Modelo, alcance y consumo

- Solicitud explícita del usuario: Claude Fable 5.1.
- Claude Code `2.1.259`, sesión existente autenticada con modalidad `claude.ai` / Max.
- Modelo solicitado: `claude-fable-5-1`, esfuerzo `high`, sin fallback configurado.
- 23 archivos o extractos numerados, 231947 caracteres: sólo código y pruebas
  sintéticas. No se enviaron originales, credenciales, correo ni datos productivos.
- Herramientas deshabilitadas, modo seguro, sin persistencia de sesión del CLI.
  El revisor no ejecutó tests; las pruebas descritas arriba son locales.
- La respuesta principal corresponde a Fable 5.1 según el modelo solicitado y
  la coincidencia exacta entre uso de salida principal y `modelUsage` de Fable.
  **No fue uso exclusivo:** el CLI también registró `claude-haiku-4-5-20251001`,
  con 19 tokens de salida. Su función exacta no está expuesta en el resultado;
  no se atribuye a ese modelo la revisión principal ni se ocultó su consumo.
- Fable: 2 tokens de entrada, 107255 de creación de caché y 33599 de salida
  reportada, de los cuales 27079 son contados como razonamiento.
- El CLI informa `total_cost_usd=3.905603`, con `costBasis=list`. Es una
  equivalencia de tarifa informada por la herramienta, **no un comprobante de
  cobro adicional a la suscripción Max**. No se compraron créditos ni planes.
- Router TAURO: revisión crítica por arquitectura, seguridad e impacto financiero.
  Revisor externo elegido por pedido explícito; validación determinística y
  recomendación final conservadas en el agente principal.

La revisión no introduce IA en el importador: su ejecución por factura sigue
siendo determinística y sin llamadas a modelos.
