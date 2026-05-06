"""Agrega columna MARKUP_PCT al sheet PERFILES si no existe, con default 25."""
import gspread, os
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

load_dotenv()

creds = Credentials.from_service_account_file('credenciales.json', scopes=['https://www.googleapis.com/auth/spreadsheets'])
gc = gspread.authorize(creds)
sh = gc.open_by_url(os.getenv('GOOGLE_SHEET_URL'))
hoja = sh.worksheet('PERFILES')

header = hoja.row_values(1)
print('HEADER ACTUAL:', header)

if 'MARKUP_PCT' not in header:
    nueva_col = len(header) + 1
    hoja.update_cell(1, nueva_col, 'MARKUP_PCT')
    n_rows = len(hoja.get_all_values())
    if n_rows > 1:
        for r in range(2, n_rows + 1):
            hoja.update_cell(r, nueva_col, 25)
    print(f'OK: agregada MARKUP_PCT en col {nueva_col}, default=25 para {n_rows-1} filas')
else:
    print('Ya existe MARKUP_PCT, no toco nada.')

print('HEADER FINAL:', hoja.row_values(1))
