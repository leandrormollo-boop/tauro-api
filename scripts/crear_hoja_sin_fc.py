"""
Crea la hoja 'SIN FC' en el sheet de Mendez 2025.
Recorre las 12 hojas mensuales y junta las filas donde:
  - Col N (FC) está vacía, O
  - Aparece 'CANCELADO' en alguna columna

Si la hoja 'SIN FC' ya existe, la borra y la recrea.
"""
import gspread
from google.oauth2.service_account import Credentials

creds = Credentials.from_service_account_file('credenciales.json', scopes=['https://www.googleapis.com/auth/spreadsheets'])
gc = gspread.authorize(creds)
sh = gc.open_by_key('1QAcp9IB2AeDHXJ4Z7Q1uujDputcRXh3qRTlwn3FDaq0')

MESES = ['ENERO', 'FEBRERO', 'MARZO', 'ABRIL', 'MAYO', 'JUNIO', 'JULIO',
         'AGOSTO', 'SEPTIEMBRE', 'OCTUBRE', 'NOVIEMBRE', 'DICIEMBRE']

# Indices de columnas (basado en header en fila 5):
# A=FECHA, B=REMITENTE, C=DESTINATARIO, D=PAIS, E=PESO, F=MEDIDAS,
# G=TRACKING, H=FACTURADO, I=DIFERENCIA, ..., M=FLETE O TAX, N=FC
IDX = {
    'FECHA': 0, 'REMITENTE': 1, 'DESTINATARIO': 2, 'PAIS': 3,
    'PESO': 4, 'MEDIDAS': 5, 'TRACKING': 6, 'FACTURADO': 7,
    'FC': 13,
}

resultados = []

for mes in MESES:
    ws = sh.worksheet(mes)
    valores = ws.get_all_values()
    # Datos arrancan en fila 6 (índice 5). Header en fila 5 (índice 4).
    for i, fila in enumerate(valores[5:], start=6):
        # Solo considerar filas que tengan datos reales (al menos tracking o fecha)
        tracking = fila[IDX['TRACKING']] if len(fila) > IDX['TRACKING'] else ''
        fecha = fila[IDX['FECHA']] if len(fila) > IDX['FECHA'] else ''
        if not str(tracking).strip() and not str(fecha).strip():
            continue

        fc_val = str(fila[IDX['FC']]).strip() if len(fila) > IDX['FC'] else ''
        # ¿Hay "CANCELADO" en alguna columna?
        es_cancelado = any('CANCEL' in str(v).upper() for v in fila)
        es_sin_fc = (fc_val == '')

        if not es_sin_fc and not es_cancelado:
            continue

        if es_sin_fc and es_cancelado:
            estado = 'SIN FC + CANCELADO'
        elif es_sin_fc:
            estado = 'SIN FC'
        else:
            estado = 'CANCELADO'

        resultados.append([
            mes, i,
            fila[IDX['FECHA']] if len(fila) > IDX['FECHA'] else '',
            fila[IDX['REMITENTE']] if len(fila) > IDX['REMITENTE'] else '',
            fila[IDX['DESTINATARIO']] if len(fila) > IDX['DESTINATARIO'] else '',
            fila[IDX['PAIS']] if len(fila) > IDX['PAIS'] else '',
            fila[IDX['PESO']] if len(fila) > IDX['PESO'] else '',
            fila[IDX['MEDIDAS']] if len(fila) > IDX['MEDIDAS'] else '',
            fila[IDX['TRACKING']] if len(fila) > IDX['TRACKING'] else '',
            fila[IDX['FACTURADO']] if len(fila) > IDX['FACTURADO'] else '',
            estado,
        ])

print(f'Total filas a volcar: {len(resultados)}')
print(f'\nDistribución por mes:')
from collections import Counter
por_mes = Counter(r[0] for r in resultados)
for mes in MESES:
    if por_mes[mes]:
        print(f'  {mes}: {por_mes[mes]}')

print(f'\nDistribución por estado:')
por_estado = Counter(r[10] for r in resultados)
for estado, n in por_estado.most_common():
    print(f'  {estado}: {n}')

# Crear / recrear hoja
NOMBRE_HOJA = 'SIN FC'
try:
    ws_existente = sh.worksheet(NOMBRE_HOJA)
    print(f'\nLa hoja {NOMBRE_HOJA!r} ya existe. La borro y la recreo.')
    sh.del_worksheet(ws_existente)
except gspread.WorksheetNotFound:
    print(f'\nLa hoja {NOMBRE_HOJA!r} no existe. La creo.')

ws_nueva = sh.add_worksheet(title=NOMBRE_HOJA, rows=str(len(resultados) + 10), cols='12')

# Escribir header + datos en una sola llamada
header = ['MES', 'FILA', 'FECHA', 'REMITENTE', 'DESTINATARIO', 'PAIS',
          'PESO', 'MEDIDAS', 'TRACKING', 'FACTURADO', 'ESTADO']
ws_nueva.update(values=[header] + resultados, range_name='A1', value_input_option='USER_ENTERED')

# Formateo: header en negrita
ws_nueva.format('A1:K1', {'textFormat': {'bold': True}, 'backgroundColor': {'red': 0.1, 'green': 0.1, 'blue': 0.1}, 'horizontalAlignment': 'CENTER'})
ws_nueva.format('A1:K1', {'textFormat': {'bold': True, 'foregroundColor': {'red': 0.83, 'green': 0.69, 'blue': 0.22}}})
# Freeze header
ws_nueva.freeze(rows=1)

print(f'\n✓ Hoja {NOMBRE_HOJA!r} creada con {len(resultados)} filas + header.')
print(f'  URL: https://docs.google.com/spreadsheets/d/1QAcp9IB2AeDHXJ4Z7Q1uujDputcRXh3qRTlwn3FDaq0/edit?gid={ws_nueva.id}')
