#!/usr/bin/env python3
# ============================================================
# Genera el secreto TOTP para el segundo factor del admin.
# ============================================================
# Uso:  python scripts/generar_totp_admin.py
#
# 1. Correr el script. Imprime el secreto y la URI otpauth.
# 2. En el teléfono: Google Authenticator (o 1Password) → agregar cuenta →
#    "ingresar clave de configuración" → pegar el SECRETO (o escanear la URI
#    convertida a QR en cualquier generador local).
# 3. En Railway → Variables: ADMIN_TOTP_SECRET=<el secreto>.
# 4. Redeploy. Desde ahí el login del admin pide contraseña + código de 6
#    dígitos. Si el teléfono se pierde: borrar la variable en Railway y el
#    login vuelve a ser sólo contraseña (Railway es la llave maestra).
# ============================================================
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from servicios.totp import generar_secreto, uri_otpauth, verificar_codigo, _codigo  # noqa: E402
import time  # noqa: E402

secreto = generar_secreto()
print()
print("=" * 60)
print("SECRETO (cargar como ADMIN_TOTP_SECRET en Railway,")
print("y como 'clave de configuración' en Google Authenticator):")
print()
print(f"    {secreto}")
print()
print("URI otpauth (alternativa: pegarla en un generador de QR local):")
print()
print(f"    {uri_otpauth(secreto)}")
print()
print(f"Código válido AHORA (para verificar que el teléfono coincide): "
      f"{_codigo(secreto, int(time.time()))}")
print("=" * 60)
