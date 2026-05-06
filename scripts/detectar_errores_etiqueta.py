"""
Dry-run: detecta filas en Mendez 2025/2026 donde
  - Etiqueta FLETE/TAX está mal (según el monto)
  - O NRO FC no corresponde al concepto real
NO ESCRIBE NADA.

Lógica:
  1. Indexar FC TAURO 2025/2026 por tracking → {FLETE: (NRO FC, monto), TAX: (NRO FC, monto)}
  2. Para cada fila en Mendez con tracking T:
     - Comparar monto Mendez con FLETE_monto y TAX_monto de FC TAURO
     - Concepto real = el que esté más cerca del monto
     - Si etiqueta o NRO FC en Mendez no coinciden → es error
"""
import re
import gspread
from google.oauth2.service_account import Credentials
from collections import defaultdict

creds = Credentials.from_service_account_file('credenciales.json', scopes=['https://www.googleapis.com/auth/spreadsheets'])
gc = gspread.authorize(creds)

MENDEZ_2025 = '1QAcp9IB2AeDHXJ4Z7Q1uujDputcRXh3qRTlwn3FDaq0'
MENDEZ_2026 = '1cI2uBWvcqAw5zHwNsV7-6uzQJPz1nxx9nBgWPPgh1CU'


def normalizar_fc(s):
    if not s:
        return ''
    return re.sub(r'[^0-9]', '', str(s).strip())


def parse_monto(s):
    if not s:
        return 0.0
    s = str(s).strip().replace('$', '').replace(' ', '').replace('\xa0', '')
    if ',' in s and '.' in s:
        # asumimos formato con coma como miles, punto decimal: 42,873.65
        s = s.replace(',', '')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return 0.0


def normalizar_concepto(s):
    s = str(s).strip().upper()
    if 'FLETE' in s:
        return 'FLETE'
    if 'TAX' in s:
        return 'TAX'
    return ''


# ── 1. Indexar FC TAURO 2025 y 2026 por tracking + concepto ──
sh25 = gc.open_by_key(MENDEZ_2025)
sh26 = gc.open_by_key(MENDEZ_2026)

# tracking → {FLETE: (nro_fc_norm, monto, nro_fc_orig), TAX: (...)}
fc_tauro = defaultdict(dict)

# FC TAURO 2025: A=FECHA, F=FLETE/TAX, G=TRACKING, H=NRO FC, I=SALDO ARS
v = sh25.worksheet('FC TAURO 2025').get_all_values()
for fila in v[1:]:
    if len(fila) < 9:
        continue
    tracking = str(fila[6]).strip()
    concepto = normalizar_concepto(fila[5])
    nro_fc = str(fila[7]).strip()
    monto = parse_monto(fila[8])
    if tracking and concepto and nro_fc:
        fc_tauro[tracking][concepto] = (normalizar_fc(nro_fc), monto, nro_fc)

# FC TAURO 2026: B=FECHA, H=FLETE/TAX, I=TRACKING, J=NRO FC, K=FACTURADO
v = sh26.worksheet('FC TAURO 2026').get_all_values()
for fila in v[1:]:
    if len(fila) < 11:
        continue
    tracking = str(fila[8]).strip()
    concepto = normalizar_concepto(fila[7])
    nro_fc = str(fila[9]).strip()
    monto = parse_monto(fila[10])
    if tracking and concepto and nro_fc:
        # Si ya existía en 2025, no pisamos (asumimos la del año correcto)
        if concepto not in fc_tauro[tracking]:
            fc_tauro[tracking][concepto] = (normalizar_fc(nro_fc), monto, nro_fc)

print(f'FC TAURO indexado: {len(fc_tauro)} trackings únicos')


# ── 2. Recorrer hojas Mendez y detectar errores ──
MESES_2025 = ['ENERO', 'FEBRERO', 'MARZO', 'ABRIL', 'MAYO', 'JUNIO', 'JULIO',
              'AGOSTO', 'SEPTIEMBRE', 'OCTUBRE', 'NOVIEMBRE', 'DICIEMBRE']
HOJAS_2026 = ['pendientes 2025', 'ENERO', 'FEBRERO', 'MARZO', 'ABRIL', 'MAYO',
              'JUNIO', 'JULIO', 'AGOSTO', 'SEPTIEMBRE', 'OCTUBRE', 'NOVIEMBRE', 'DICIEMBRE']

errores = []  # {sheet, hoja, fila, tracking, monto, etiqueta_actual, nro_fc_actual, concepto_real, nro_fc_correcta}

# Mendez 2025: TRACKING col G=6, FACTURADO col H=7, FLETE/TAX col M=12, NRO FC col N=13
for hoja in MESES_2025:
    ws = sh25.worksheet(hoja)
    valores = ws.get_all_values()
    for i, fila in enumerate(valores[5:], start=6):
        if len(fila) <= 13:
            continue
        tracking = str(fila[6]).strip()
        if not tracking or tracking not in fc_tauro:
            continue
        monto = parse_monto(fila[7])
        etiq = normalizar_concepto(fila[12])
        nro_fc_actual = str(fila[13]).strip()
        nro_fc_actual_norm = normalizar_fc(nro_fc_actual)
        info = fc_tauro[tracking]
        # Determinar concepto real por monto
        concepto_real = None
        if 'FLETE' in info and 'TAX' in info:
            d_flete = abs(monto - info['FLETE'][1])
            d_tax = abs(monto - info['TAX'][1])
            concepto_real = 'FLETE' if d_flete <= d_tax else 'TAX'
        elif 'FLETE' in info:
            concepto_real = 'FLETE'
        elif 'TAX' in info:
            concepto_real = 'TAX'
        if not concepto_real:
            continue
        nro_fc_correcta = info[concepto_real][2]
        nro_fc_correcta_norm = info[concepto_real][0]
        # Hay error si la etiqueta o el NRO FC no coinciden con el concepto real
        if etiq != concepto_real or nro_fc_actual_norm != nro_fc_correcta_norm:
            errores.append({
                'sheet': '2025', 'hoja': hoja, 'fila': i,
                'tracking': tracking, 'monto': monto,
                'etiq_actual': etiq or '?', 'fc_actual': nro_fc_actual,
                'concepto_real': concepto_real, 'fc_correcta': nro_fc_correcta,
            })

# Mendez 2026: TRACKING col I=8, FACTURADO col K=10, FLETE/TAX col H=7, NRO FC col J=9
for hoja in HOJAS_2026:
    try:
        ws = sh26.worksheet(hoja)
    except gspread.WorksheetNotFound:
        continue
    valores = ws.get_all_values()
    for i, fila in enumerate(valores[5:], start=6):
        if len(fila) <= 10:
            continue
        tracking = str(fila[8]).strip()
        if not tracking or tracking not in fc_tauro:
            continue
        monto = parse_monto(fila[10])
        etiq = normalizar_concepto(fila[7])
        nro_fc_actual = str(fila[9]).strip()
        nro_fc_actual_norm = normalizar_fc(nro_fc_actual)
        info = fc_tauro[tracking]
        concepto_real = None
        if 'FLETE' in info and 'TAX' in info:
            d_flete = abs(monto - info['FLETE'][1])
            d_tax = abs(monto - info['TAX'][1])
            concepto_real = 'FLETE' if d_flete <= d_tax else 'TAX'
        elif 'FLETE' in info:
            concepto_real = 'FLETE'
        elif 'TAX' in info:
            concepto_real = 'TAX'
        if not concepto_real:
            continue
        nro_fc_correcta = info[concepto_real][2]
        nro_fc_correcta_norm = info[concepto_real][0]
        if etiq != concepto_real or nro_fc_actual_norm != nro_fc_correcta_norm:
            errores.append({
                'sheet': '2026', 'hoja': hoja, 'fila': i,
                'tracking': tracking, 'monto': monto,
                'etiq_actual': etiq or '?', 'fc_actual': nro_fc_actual,
                'concepto_real': concepto_real, 'fc_correcta': nro_fc_correcta,
            })

print(f'\nTotal filas con error de etiqueta o NRO FC: {len(errores)}')

# Distribución por tipo de error
solo_etiq = sum(1 for e in errores if normalizar_fc(e['fc_actual']) == normalizar_fc(e['fc_correcta']) and e['etiq_actual'] != e['concepto_real'])
solo_fc = sum(1 for e in errores if normalizar_fc(e['fc_actual']) != normalizar_fc(e['fc_correcta']) and e['etiq_actual'] == e['concepto_real'])
ambos = sum(1 for e in errores if normalizar_fc(e['fc_actual']) != normalizar_fc(e['fc_correcta']) and e['etiq_actual'] != e['concepto_real'])
print(f'  Solo etiqueta mal:      {solo_etiq}')
print(f'  Solo NRO FC mal:        {solo_fc}')
print(f'  Etiqueta + NRO FC mal:  {ambos}')

# Distribución por sheet/hoja
por_sheet = defaultdict(int)
for e in errores:
    por_sheet[(e['sheet'], e['hoja'])] += 1
print(f'\nPor hoja:')
for (s, h), n in sorted(por_sheet.items()):
    print(f'  {s}/{h}: {n}')

print(f'\n══ Primeros 15 errores ══')
print(f'{"sheet/hoja":<22} {"fila":<6} {"tracking":<14} {"monto":<12} {"etiq":<6}→{"real":<6} {"fc_actual":<16}→{"fc_correcta"}')
for e in errores[:15]:
    print(f'  {e["sheet"]}/{e["hoja"][:14]:<14} {e["fila"]:<6} {e["tracking"]:<14} {e["monto"]:<12.2f} {e["etiq_actual"]:<6}→{e["concepto_real"]:<6} {e["fc_actual"]:<16}→{e["fc_correcta"]}')
