"""
Cruza FC TAURO 2025/2026 contra hojas mensuales de Mendez y escribe las marcas.

FC TAURO 2025 (col J = año, sólo procesar año != 2024):
  - Col K (idx 10) = ESTADO (CARGADA / FALTA CARGAR)
  - Col L (idx 11) = DONDE

FC TAURO 2026:
  - Col L (idx 11) = ESTADO
  - Col M (idx 12) = DONDE

Además crea hoja FALTAN CARGAR 2025 con las 273 que faltan.
"""
import re
import gspread
from google.oauth2.service_account import Credentials
from collections import defaultdict

creds = Credentials.from_service_account_file('credenciales.json', scopes=['https://www.googleapis.com/auth/spreadsheets'])
gc = gspread.authorize(creds)

MENDEZ_2025 = '1QAcp9IB2AeDHXJ4Z7Q1uujDputcRXh3qRTlwn3FDaq0'
MENDEZ_2026 = '1cI2uBWvcqAw5zHwNsV7-6uzQJPz1nxx9nBgWPPgh1CU'

MESES_2025 = ['ENERO', 'FEBRERO', 'MARZO', 'ABRIL', 'MAYO', 'JUNIO', 'JULIO',
              'AGOSTO', 'SEPTIEMBRE', 'OCTUBRE', 'NOVIEMBRE', 'DICIEMBRE']
HOJAS_2026 = ['pendientes 2025', 'ENERO', 'FEBRERO', 'MARZO', 'ABRIL', 'MAYO',
              'JUNIO', 'JULIO', 'AGOSTO', 'SEPTIEMBRE', 'OCTUBRE', 'NOVIEMBRE', 'DICIEMBRE']


def normalizar(s):
    if not s:
        return ''
    return re.sub(r'[^0-9]', '', str(s).strip())


print('=== Indexando NRO FC en hojas Mendez ===')

# Mendez 2025: FC en col N=13
sh25 = gc.open_by_key(MENDEZ_2025)
fc_en_mendez_2025 = {}
for hoja in MESES_2025:
    ws = sh25.worksheet(hoja)
    v = ws.get_all_values()
    for i, fila in enumerate(v[5:], start=6):
        if len(fila) > 13:
            fc = normalizar(fila[13])
            if fc and len(fc) >= 6:
                if fc not in fc_en_mendez_2025:
                    fc_en_mendez_2025[fc] = f'2025/{hoja[:3]}#{i}'
print(f'  Mendez 2025: {len(fc_en_mendez_2025)} FCs únicas')

# Mendez 2026: FC en col J=9
sh26 = gc.open_by_key(MENDEZ_2026)
fc_en_mendez_2026 = {}
fc_en_mendez_2026_2025_relacionadas = {}
for hoja in HOJAS_2026:
    try:
        ws = sh26.worksheet(hoja)
    except gspread.WorksheetNotFound:
        continue
    v = ws.get_all_values()
    for i, fila in enumerate(v[5:], start=6):
        if len(fila) > 9:
            fc = normalizar(fila[9])
            if fc and len(fc) >= 6:
                etiq = f'2026/{hoja[:6] if hoja.startswith("pend") else hoja[:3]}#{i}'
                if fc not in fc_en_mendez_2026:
                    fc_en_mendez_2026[fc] = etiq
                if hoja in ('pendientes 2025', 'ENERO') and fc not in fc_en_mendez_2026_2025_relacionadas:
                    fc_en_mendez_2026_2025_relacionadas[fc] = etiq
print(f'  Mendez 2026 (todas las hojas): {len(fc_en_mendez_2026)} FCs únicas')
print(f'  Mendez 2026 (pend 2025 + ENERO): {len(fc_en_mendez_2026_2025_relacionadas)} FCs')


print('\n=== Procesando FC TAURO 2025 ===')

ws_fctau25 = sh25.worksheet('FC TAURO 2025')
v_fc25 = ws_fctau25.get_all_values()

cambios_25 = []  # (fila, K_val, L_val)
faltan_lista_25 = []
contador_2024 = 0

for i, fila in enumerate(v_fc25[1:], start=2):
    if len(fila) <= 7:
        continue
    fc = normalizar(fila[7])
    if not fc or len(fc) < 6:
        continue

    # Filtro año (col J = idx 9)
    anio = str(fila[9] if len(fila) > 9 else '').strip()
    if anio == '2024':
        contador_2024 += 1
        continue  # NO procesar

    encontrada = (fc_en_mendez_2025.get(fc) or
                  fc_en_mendez_2026_2025_relacionadas.get(fc))
    if encontrada:
        cambios_25.append((i, 'CARGADA', encontrada))
    else:
        cambios_25.append((i, 'FALTA CARGAR', ''))
        faltan_lista_25.append(fila)

cargadas_25 = sum(1 for c in cambios_25 if c[1] == 'CARGADA')
faltan_25 = sum(1 for c in cambios_25 if c[1] == 'FALTA CARGAR')
print(f'  Excluidas (año 2024): {contador_2024}')
print(f'  CARGADAS:    {cargadas_25}')
print(f'  FALTAN:      {faltan_25}')

# Aplicar cambios en batch
print(f'\nEscribiendo {len(cambios_25) * 2} celdas en FC TAURO 2025...')
batch = []
for fila, k_val, l_val in cambios_25:
    batch.append({'range': f'K{fila}', 'values': [[k_val]]})
    batch.append({'range': f'L{fila}', 'values': [[l_val]]})
# Header
batch.append({'range': 'K1', 'values': [['ESTADO']]})
batch.append({'range': 'L1', 'values': [['DONDE']]})

# gspread tiene límite de batch. Lo hago en chunks de 200.
CHUNK = 200
for j in range(0, len(batch), CHUNK):
    ws_fctau25.batch_update(batch[j:j+CHUNK], value_input_option='USER_ENTERED')
print(f'  ✓ Aplicado ({len(batch)} celdas)')


print('\n=== Procesando FC TAURO 2026 ===')

ws_fctau26 = sh26.worksheet('FC TAURO 2026')
v_fc26 = ws_fctau26.get_all_values()

cambios_26 = []
faltan_lista_26 = []
for i, fila in enumerate(v_fc26[1:], start=2):
    if len(fila) <= 9:
        continue
    fc = normalizar(fila[9])
    if not fc or len(fc) < 6:
        continue
    encontrada = fc_en_mendez_2026.get(fc)
    if encontrada:
        cambios_26.append((i, 'CARGADA', encontrada))
    else:
        cambios_26.append((i, 'FALTA CARGAR', ''))
        faltan_lista_26.append(fila)

cargadas_26 = sum(1 for c in cambios_26 if c[1] == 'CARGADA')
faltan_26 = sum(1 for c in cambios_26 if c[1] == 'FALTA CARGAR')
print(f'  CARGADAS: {cargadas_26}')
print(f'  FALTAN:   {faltan_26}')

batch = []
for fila, l_val, m_val in cambios_26:
    batch.append({'range': f'L{fila}', 'values': [[l_val]]})
    batch.append({'range': f'M{fila}', 'values': [[m_val]]})
batch.append({'range': 'L1', 'values': [['ESTADO']]})
batch.append({'range': 'M1', 'values': [['DONDE']]})

print(f'\nEscribiendo {len(batch)} celdas en FC TAURO 2026...')
for j in range(0, len(batch), CHUNK):
    ws_fctau26.batch_update(batch[j:j+CHUNK], value_input_option='USER_ENTERED')
print(f'  ✓ Aplicado')


# === Crear hoja FALTAN CARGAR 2025 en sheet 2025 ===
print('\n=== Creando hoja FALTAN CARGAR 2025 ===')
NOMBRE = 'FALTAN CARGAR 2025'
try:
    sh25.del_worksheet(sh25.worksheet(NOMBRE))
    print(f'  (existía, borrada y recreada)')
except gspread.WorksheetNotFound:
    pass

n_filas = len(faltan_lista_25) + 5
ws_new = sh25.add_worksheet(title=NOMBRE, rows=str(n_filas), cols='12')

header = ['FECHA', 'MES', 'REMITENTE', 'DESTINATARIO', 'PESO',
          'FLETE O TAX', 'TRACKING', 'NRO FC', 'SALDO ARS', 'AÑO']
ws_new.update(values=[header] + faltan_lista_25, range_name='A1', value_input_option='USER_ENTERED')
ws_new.format('A1:J1', {'textFormat': {'bold': True, 'foregroundColor': {'red': 0.83, 'green': 0.69, 'blue': 0.22}},
                       'backgroundColor': {'red': 0.1, 'green': 0.1, 'blue': 0.1},
                       'horizontalAlignment': 'CENTER'})
ws_new.freeze(rows=1)
print(f'  ✓ Hoja {NOMBRE!r} creada con {len(faltan_lista_25)} filas')

print('\n══ DONE ══')
print(f'FC TAURO 2025: {cargadas_25} cargadas / {faltan_25} faltan / {contador_2024} excluidas (2024)')
print(f'FC TAURO 2026: {cargadas_26} cargadas / {faltan_26} faltan')
