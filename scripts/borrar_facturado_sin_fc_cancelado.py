"""
Borra el valor de col H (FACTURADO) en cada hoja mensual,
para las filas marcadas como 'SIN FC + CANCELADO' en la hoja SIN FC.
"""
import gspread
from google.oauth2.service_account import Credentials
from collections import defaultdict

creds = Credentials.from_service_account_file('credenciales.json', scopes=['https://www.googleapis.com/auth/spreadsheets'])
gc = gspread.authorize(creds)
sh = gc.open_by_key('1QAcp9IB2AeDHXJ4Z7Q1uujDputcRXh3qRTlwn3FDaq0')

# Leer hoja SIN FC
ws_sinfc = sh.worksheet('SIN FC')
sinfc = ws_sinfc.get_all_values()
header = sinfc[0]
idx_estado = header.index('ESTADO')
idx_mes = header.index('MES')
idx_fila = header.index('FILA')

# Agrupar filas a borrar por hoja
por_hoja = defaultdict(list)
for f in sinfc[1:]:
    if len(f) <= idx_estado:
        continue
    if str(f[idx_estado]).strip().upper() == 'SIN FC + CANCELADO':
        por_hoja[f[idx_mes]].append(int(f[idx_fila]))

print(f'Filas a borrar en col H por hoja:')
for mes, filas in por_hoja.items():
    print(f'  {mes}: {filas}')

# Por hoja, hacer batch update con celdas vacías en H{fila}
total = 0
for mes, filas in por_hoja.items():
    ws = sh.worksheet(mes)
    batch = [{'range': f'H{r}', 'values': [['']]} for r in filas]
    if batch:
        ws.batch_update(batch, value_input_option='USER_ENTERED')
        print(f'  {mes}: {len(batch)} celdas H borradas')
        total += len(batch)

print(f'\nTotal celdas H borradas: {total}')
