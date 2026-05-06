import pandas as pd
import os

pdf_dir = "/Users/leanrmollo/Documents/colab tauro/notas_credito/"
csv_file = os.path.join(pdf_dir, "Resumen_Notas_Credito.csv")
xlsx_file = os.path.join(pdf_dir, "Resumen_Notas_Credito.xlsx")

# Leer el CSV original
df = pd.read_csv(csv_file)

# Limpiar los valores de la columna "Monto" (por ejemplo, quitando saltos de línea y tratando de forzarlo a número)
if "Monto" in df.columns:
    df["Monto"] = df["Monto"].astype(str).str.replace(r'\n', '', regex=True).str.replace(r'\r', '', regex=True).str.strip()

# Guardar en formato Excel
df.to_excel(xlsx_file, index=False, engine='openpyxl')

print(f"✅ Convertido exitosamente a Excel: {xlsx_file}")
