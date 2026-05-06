"""
Dry-run: muestra las 13 filas SIN FC + CANCELADO y qué valor tiene la col H (FACTURADO).
NO ESCRIBE NADA.
"""
import gspread
from google.oauth2.service_account import Credentials

creds = Credentials.from_service_account_file('credenciales.json', scopes=['https://www.googleapis.com/auth/spreadsheets'])
gc = gspread.authorize(creds)
sh = gc.open_by_key('1QAcp9IB2AeDHXJ4Z7Q1uujDputcRXh3qRTlwn3FDaq0')

ws_sinfc = sh.worksheet('SIN FC')
sinfc = ws_sinfc.get_all_values()
header = sinfc[0]
idx_estado = header.index('ESTADO')
idx_mes = header.index('MES')
idx_fila = header.index('FILA')
idx_tracking = header.index('TRACKING')

candidatas = []
for f in sinfc[1:]:
    if len(f) <= idx_estado:
        continue
    if str(f[idx_estado]).strip().upper() == 'SIN FC + CANCELADO':
        candidatas.append({
            'mes': f[idx_mes],
            'fila': int(f[idx_fila]),
            'tracking': f[idx_tracking],
        })

print(f'Total a tocar: {len(candidatas)} filas\n')
print(f'{"MES":<12} {"FILA":<6} {"TRACKING":<14} {"FACTURADO actual":<20}')
print('-' * 60)
# Cache hojas
hojas = {}
for c in candidatas:
    if c['mes'] not in hojas:
        hojas[c['mes']] = sh.worksheet(c['mes']).get_all_values()
    fila_data = hojas[c['mes']][c['fila'] - 1] if len(hojas[c['mes']]) >= c['fila'] else []
    facturado = fila_data[7] if len(fila_data) > 7 else ''
    print(f'{c["mes"]:<12} {c["fila"]:<6} {c["tracking"]:<14} {facturado:<20}')
