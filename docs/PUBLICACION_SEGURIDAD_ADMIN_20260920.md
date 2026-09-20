# Publicación acotada de seguridad del Admin — 20/09/2026

Estado: preparada y probada, pendiente de aprobación explícita de publicación.
Rama: `codex/admin-mfa-release-20260920`, sobre producción `1f1bb90`.

## Problema y resultado

El dueño vinculó y verificó su autenticador. Railway aplicó la configuración
en el despliegue `100ea66c-9d6d-4717-9969-efd7a0d58d8e`, con el mismo código
productivo `1f1bb9026b2f1396a63494ef45bdb8ab361e2ad5`. El login por contraseña
ya pide TOTP, pero la recuperación por correo de esa versión permite ingresar
sin segundo factor.

Esta actualización exige un código vigente del autenticador al canjear el
enlace de correo. Los códigos sólo pueden utilizarse una vez entre ambos
métodos de ingreso, incluso con procesos concurrentes o reinicios. El enlace
mantiene su vencimiento y uso único. La verificación por correo espera una
confirmación explícita en el formulario, en lugar de enviarse automáticamente.

El Admin recibe sesiones individuales de ocho horas, revocables al cerrar
sesión. Cambiar contraseña o clave TOTP invalida las anteriores. El login y
el canje tienen límites persistentes; el límite de cuenta no depende de las
cabeceras de IP. La pantalla Seguridad muestra si el segundo factor está activo.

## Alcance exacto

- Autenticación, sesiones y recuperación de Admin; dos formularios y sus pruebas.
- Tres tablas nuevas: `admin_sesiones`, `admin_totp_uso`, `auth_intentos`, más índices.
- Sin modificaciones a envíos, pagos, saldos, facturas, clientes, integraciones,
  configuración de proxy o Docker. Se comprobó igualdad de las funciones
  existentes de IP y límites para los demás módulos.
- La rama general `codex/launch-hardening-20260918` conserva las demás mejoras
  sin publicar. Antes de publicarla deberá incorporar esta rama y sus pruebas.

## Validación

- 29 pruebas enfocadas aprobadas.
- Suite completa de esta rama: **2.367 pruebas y 5 subpruebas aprobadas**.
- Casos HTTP con PostgreSQL aislado: correo sin código rechazado; canje válido;
  código y enlace reutilizados rechazados; límite independiente de cabeceras IP;
  vencimiento del enlace; logout que revoca la cookie y compatibilidad sin MFA.
- Consumo TOTP concurrente y límites durables comprobados en PostgreSQL.
- Sintaxis Python 3.11 y `git diff --check` correctos.
- Runner con entorno limpio y red externa bloqueada. No se enviaron correos,
  no se crearon guías ni se modificaron datos financieros de producción.

Evidencias locales fuera del repositorio: `qa_admin_mfa_release_20260920/`.
Router: architecture/security-sensitive, mínimo Sol/high, ejecución Astra
según preferencia del dueño; sin subagentes. La comparación de la clave cargada
con la verificada se hizo dentro de un proceso privado, sin registrarla.

## Publicación y comprobación

1. Obtener aprobación específica; actualizar la lectura de `origin/main`.
2. Publicar esta rama acotada. Railway despliega al actualizar `main`.
3. Verificar migración aditiva, despliegue SUCCESS, `/health`, login y recuperación.
4. El dueño vuelve a ingresar con contraseña y código del teléfono. Comprobar
   el estado del segundo factor y cierre de sesión. No pedir códigos por chat.
5. Una prueba completa por correo requiere autorización para enviar el mensaje.

El despliegue invalida la cookie anterior y exige iniciar sesión de nuevo.
El respaldo cifrado ya restaurado y las copias de Railway están disponibles.
Un rollback de código a `1f1bb90` conserva las tablas aditivas, pero devuelve
la limitación conocida de la recuperación sin TOTP; no se considera cierre
del problema de seguridad.

Siguen separados los pendientes de DNS/certificado de www, configuración de
proxy y habilitación comercial de integraciones. Esta publicación no declara
esas tareas completadas.
