"""
Dry-run: compara FC TAURO 2025 y FC TAURO 2026 contra todas las hojas mensuales de Mendez.
NO ESCRIBE NADA — solo cuenta.

Match: NRO FC normalizada (sin guiones, sin espacios, solo dígitos).
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


# Mendez 2025: indexar NRO FC por hoja (col N=13). Datos desde fila 6.
sh25 = gc.open_by_key(MENDEZ_2025)
fc_en_mendez_2025 = {}  # fc_norm → "etiq#fila"
for hoja in MESES_2025:
    ws = sh25.worksheet(hoja)
    v = ws.get_all_values()
    for i, fila in enumerate(v[5:], start=6):
        if len(fila) > 13:
            fc = normalizar(fila[13])
            if fc and len(fc) >= 6:
                fc_en_mendez_2025[fc] = f'2025/{hoja[:3]}#{i}'

# Mendez 2026: NRO FC en col J=9. Header en fila 5, datos desde fila 6.
sh26 = gc.open_by_key(MENDEZ_2026)
fc_en_mendez_2026 = {}
fc_en_mendez_2026_solo_2025_relacionadas = {}  # solo pendientes 2025 y ENERO
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
                fc_en_mendez_2026[fc] = etiq
                if hoja in ('pendientes 2025', 'ENERO'):
                    fc_en_mendez_2026_solo_2025_relacionadas[fc] = etiq

# FC TAURO 2025 (en sheet 2025): NRO FC en col H=7.
ws_fctau25 = sh25.worksheet('FC TAURO 2025')
v_fc25 = ws_fctau25.get_all_values()
print(f'FC TAURO 2025: {len(v_fc25) - 1} filas de datos')

cargada25 = 0
faltan25 = 0
faltan_lista_25 = []
for i, fila in enumerate(v_fc25[1:], start=2):
    if len(fila) <= 7:
        continue
    fc = normalizar(fila[7])
    if not fc or len(fc) < 6:
        continue
    # Buscar en Mendez 2025 OR en pendientes 2025 / ENERO de Mendez 2026
    encontrada_en = (fc_en_mendez_2025.get(fc) or
                     fc_en_mendez_2026_solo_2025_relacionadas.get(fc))
    if encontrada_en:
        cargada25 += 1
    else:
        faltan25 += 1
        faltan_lista_25.append((i, fila[7], fila[3] if len(fila) > 3 else '', fila[5] if len(fila) > 5 else '', fila[8] if len(fila) > 8 else ''))

print(f'  CARGADAS: {cargada25}')
print(f'  FALTAN:   {faltan25}')

# FC TAURO 2026 (en sheet 2026): NRO FC en col J=9.
ws_fctau26 = sh26.worksheet('FC TAURO 2026')
v_fc26 = ws_fctau26.get_all_values()
print(f'\nFC TAURO 2026: {len(v_fc26) - 1} filas de datos')

cargada26 = 0
faltan26 = 0
faltan_lista_26 = []
for i, fila in enumerate(v_fc26[1:], start=2):
    if len(fila) <= 9:
        continue
    fc = normalizar(fila[9])
    if not fc or len(fc) < 6:
        continue
    encontrada_en = fc_en_mendez_2026.get(fc)
    if encontrada_en:
        cargada26 += 1
    else:
        faltan26 += 1
        faltan_lista_26.append((i, fila[9], fila[3] if len(fila) > 3 else '', fila[7] if len(fila) > 7 else '', fila[10] if len(fila) > 10 else ''))

print(f'  CARGADAS: {cargada26}')
print(f'  FALTAN:   {faltan26}')

# Mostrar primeras 10 que faltan de cada
print(f'\n══ Primeras 10 FCs FALTAN cargar de FC TAURO 2025 ══')
for f in faltan_lista_25[:10]:
    print(f'  fila {f[0]}: NRO FC={f[1]!r}, dest={f[2]!r}, FLETE/TAX={f[3]}, monto={f[4]}')

print(f'\n══ Primeras 10 FCs FALTAN cargar de FC TAURO 2026 ══')
for f in faltan_lista_26[:10]:
    print(f'  fila {f[0]}: NRO FC={f[1]!r}, dest={f[2]!r}, FLETE/TAX={f[3]}, monto={f[4]}')
