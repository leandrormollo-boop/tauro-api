"""
Aplica 5 correcciones de prueba en AGOSTO 2025.
Para cada fila:
  - Cambia col M (FLETE O TAX) si la etiqueta está mal
  - Cambia col N (NRO FC) si el número está mal
"""
import re
import gspread
from google.oauth2.service_account import Credentials

creds = Credentials.from_service_account_file('credenciales.json', scopes=['https://www.googleapis.com/auth/spreadsheets'])
gc = gspread.authorize(creds)
sh = gc.open_by_key('1QAcp9IB2AeDHXJ4Z7Q1uujDputcRXh3qRTlwn3FDaq0')
ws = sh.worksheet('AGOSTO')

# Las 5 correcciones (basadas en el dry-run anterior)
# (fila, etiqueta_correcta, nro_fc_correcta, etiqueta_actual, nro_fc_actual)
CORRECCIONES = [
    (117, 'FLETE', '0106-00873714', 'TAX',   '0106-00875080'),
    (118, 'FLETE', '0106-00873723', 'TAX',   '0106-00877602'),
    (119, 'FLETE', '0106-00874373', 'FLETE', '0106-00877975'),  # solo NRO FC
    (120, 'FLETE', '0106-00874996', 'FLETE', '0106-00875081'),  # solo NRO FC
    (123, 'FLETE', '0106-00873720', 'TAX',   '0106-00875078'),
]

# Verificar valor actual antes de aplicar (sanity check)
print('═══ Verificando valores actuales antes de aplicar ═══')
batch = []
for fila, etiq_new, fc_new, etiq_old, fc_old in CORRECCIONES:
    valor_m = ws.cell(fila, 13).value or ''  # col M = FLETE O TAX
    valor_n = ws.cell(fila, 14).value or ''  # col N = NRO FC
    print(f'  fila {fila}: actual M={valor_m!r} N={valor_n!r}')
    if etiq_new != etiq_old:
        batch.append({'range': f'M{fila}', 'values': [[etiq_new]]})
    if fc_new != fc_old:
        batch.append({'range': f'N{fila}', 'values': [[fc_new]]})

print(f'\n═══ Aplicando {len(batch)} cambios ═══')
ws.batch_update(batch, value_input_option='USER_ENTERED')
print('✓ Aplicado')

print('\n═══ Verificando después ═══')
for fila, etiq_new, fc_new, _, _ in CORRECCIONES:
    valor_m = ws.cell(fila, 13).value or ''
    valor_n = ws.cell(fila, 14).value or ''
    ok = valor_m == etiq_new and valor_n == fc_new
    print(f'  fila {fila}: M={valor_m!r} N={valor_n!r} {"✓" if ok else "✗"}')
