============================================================
  05_jobs — Tareas programadas
============================================================

QUÉ HAY ACÁ
-----------
Tareas que corren automáticamente con APScheduler.

  reporte_semanal.py    - Resumen lunes 6am (ya existente)
  health_check.py       - Ping cada 5 min a FedEx/Sheets/SMTP
  resumen_diario.py     - Email diario 8am con stats del día
  limpiar_sessions.py   - Borra tokens expirados (diario 3am)

LÓGICA Y DECISIONES
-------------------

[reporte_semanal.py]
- Cron: lunes 6:00am.
- Lee COTI de la semana, agrupa por cliente.
- Genera PDF con ReportLab (cantidad, monto, top destinos).
- Manda email a cada cliente que tuvo actividad.
- Configurable en CONFIG: CRON_DIA, CRON_HORA.

[health_check.py]
- Cron: cada 5 minutos.
- Llama health.check_fedex() / check_sheets() / check_smtp().
- Si algo falla 3 veces seguidas → email de alerta a admin.
- Resultado en hoja LOG_HEALTH (timestamp, servicio, estado, ms).

[resumen_diario.py]
- Cron: todos los días 8:00am.
- Cantidad de cotizaciones, pedidos y errores del día anterior.
- Email a admin (vos).

[limpiar_sessions.py]
- Cron: todos los días 3:00am.
- Borra de SESSIONS los tokens con expiracion < hoy.

REGLAS
------
- Cada job loguea inicio/fin a LOG_JOBS (timestamp, job, estado, duracion).
- Si un job falla, NO matar el scheduler — solo loguear y seguir.
- Configurable: cualquier hora/cron en CONFIG, no hardcoded.
- Jobs son idempotentes: si corren dos veces, no rompen nada.
