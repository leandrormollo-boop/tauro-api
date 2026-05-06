"""
Aplica los cambios al sheet de ESTADO DE CUENTA según reglas:
  - Si M vacío → M='RECLAMO', N=motivo
  - Si M con cualquier valor y N vacío → solo N=motivo (no piso M)
  - Si M con valor y N con texto → no toco

Reporta al final qué se hizo.
"""
import re
import time
import gspread
from google.oauth2.service_account import Credentials
from collections import defaultdict

creds = Credentials.from_service_account_file('credenciales.json', scopes=['https://www.googleapis.com/auth/spreadsheets'])
gc = gspread.authorize(creds)

RECLAMOS = '1Wnfkx-uzcNEsUoOCSsxDRlHySHsYZT3_IfWiwWB-Up8'
ESTADO = '16KbPY-SsgrlLHmk-tTxx81pVckyDIzJw8GLqogtqXyU'

HOJAS_MOTIVOS = {
    'DESCUENTO NO APLICADO': ('descuento no aplicado', 6),
    'RETORNO ERRONEO': ('retorno erroneo', 6),
    'SOBRECARGO MANEJO ADIC': ('sobrecargo manejo adicional', 6),
    'DIF MEDIDAS RECLAMAR': ('dif de peso', 9),
}


def normalizar_fc(s):
    if not s:
        return ''
    s = str(s).strip().upper()
    s = re.sub(r'[^0-9]', '', s)
    return s


# Cargar FC reclamadas
sh_reclamos = gc.open_by_key(RECLAMOS)
fc_motivos = defaultdict(set)
for hoja_name, (motivo, col_idx) in HOJAS_MOTIVOS.items():
    ws = sh_reclamos.worksheet(hoja_name)
    valores = ws.get_all_values()
    for fila in valores[1:]:
        if len(fila) <= col_idx:
            continue
        fc_norm = normalizar_fc(fila[col_idx])
        if not fc_norm or len(fc_norm) < 6:
            continue
        fc_motivos[fc_norm].add(motivo)

print(f'FC reclamadas únicas: {len(fc_motivos)}')

# Leer estado de cuenta
sh_estado = gc.open_by_key(ESTADO)
ws_estado = sh_estado.worksheets()[0]
valores_estado = ws_estado.get_all_values()

IDX_REFERENCE = 6
IDX_M = 12
IDX_N = 13

cambios_m = []  # (row, valor) para M
cambios_n = []  # (row, valor) para N

stats = {
    'm_vacio_a_reclamo': 0,
    'solo_motivo_agregado': 0,
    'm_vencida_motivo_agregado': 0,
    'no_toco_n_con_texto': 0,
    'sin_match': 0,
}

for i, fila in enumerate(valores_estado[1:], start=2):
    inv = normalizar_fc(fila[IDX_REFERENCE]) if len(fila) > IDX_REFERENCE else ''
    if not inv or inv not in fc_motivos:
        stats['sin_match'] += 1
        continue

    motivo_str = ', '.join(sorted(fc_motivos[inv]))
    m_actual = (fila[IDX_M] if len(fila) > IDX_M else '').strip()
    n_actual = (fila[IDX_N] if len(fila) > IDX_N else '').strip()

    if m_actual == '':
        cambios_m.append((i, 'RECLAMO'))
        cambios_n.append((i, motivo_str))
        stats['m_vacio_a_reclamo'] += 1
    elif n_actual == '':
        cambios_n.append((i, motivo_str))
        if m_actual.upper() == 'VENCIDA':
            stats['m_vencida_motivo_agregado'] += 1
        else:
            stats['solo_motivo_agregado'] += 1
    else:
        stats['no_toco_n_con_texto'] += 1

print(f'\nEscribiendo cambios:')
print(f'  M (RECLAMO en vacíos): {len(cambios_m)} celdas')
print(f'  N (motivo): {len(cambios_n)} celdas')

# Hacer batch update para no rompernos contra rate limit
def col_letra(idx):
    return chr(65 + idx)

batch_data = []
for row, val in cambios_m:
    batch_data.append({'range': f'{col_letra(IDX_M)}{row}', 'values': [[val]]})
for row, val in cambios_n:
    batch_data.append({'range': f'{col_letra(IDX_N)}{row}', 'values': [[val]]})

if batch_data:
    ws_estado.batch_update(batch_data, value_input_option='USER_ENTERED')
    print(f'OK: aplicadas {len(batch_data)} celdas en batch.')
else:
    print('No hay nada para escribir.')

print('\n══ ESTADÍSTICAS FINALES ══')
for k, v in stats.items():
    print(f'  {k}: {v}')
