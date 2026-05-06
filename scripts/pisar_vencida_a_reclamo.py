"""
Para las filas donde M='VENCIDA' y la FC está en reclamos, pisar M='RECLAMO'.
N ya quedó escrito en la corrida anterior, no se toca.
"""
import re
import gspread
from google.oauth2.service_account import Credentials
from collections import defaultdict

creds = Credentials.from_service_account_file('credenciales.json', scopes=['https://www.googleapis.com/auth/spreadsheets'])
gc = gspread.authorize(creds)

RECLAMOS = '1Wnfkx-uzcNEsUoOCSsxDRlHySHsYZT3_IfWiwWB-Up8'
ESTADO = '16KbPY-SsgrlLHmk-tTxx81pVckyDIzJw8GLqogtqXyU'

HOJAS_MOTIVOS = {
    'DESCUENTO NO APLICADO': 6,
    'RETORNO ERRONEO': 6,
    'SOBRECARGO MANEJO ADIC': 6,
    'DIF MEDIDAS RECLAMAR': 9,
}


def normalizar_fc(s):
    if not s:
        return ''
    return re.sub(r'[^0-9]', '', str(s).strip().upper())


# FC reclamadas
sh_reclamos = gc.open_by_key(RECLAMOS)
fc_reclamadas = set()
for hoja_name, col_idx in HOJAS_MOTIVOS.items():
    ws = sh_reclamos.worksheet(hoja_name)
    for fila in ws.get_all_values()[1:]:
        if len(fila) > col_idx:
            fc = normalizar_fc(fila[col_idx])
            if fc and len(fc) >= 6:
                fc_reclamadas.add(fc)

# Estado de cuenta
sh_estado = gc.open_by_key(ESTADO)
ws_estado = sh_estado.worksheets()[0]
valores = ws_estado.get_all_values()

IDX_REF = 6
IDX_M = 12

cambios = []
for i, fila in enumerate(valores[1:], start=2):
    inv = normalizar_fc(fila[IDX_REF]) if len(fila) > IDX_REF else ''
    m_actual = (fila[IDX_M] if len(fila) > IDX_M else '').strip().upper()
    if inv in fc_reclamadas and m_actual == 'VENCIDA':
        cambios.append({'range': f'M{i}', 'values': [['RECLAMO']]})

print(f'Filas a cambiar de VENCIDA a RECLAMO: {len(cambios)}')
if cambios:
    ws_estado.batch_update(cambios, value_input_option='USER_ENTERED')
    print('OK aplicado.')
