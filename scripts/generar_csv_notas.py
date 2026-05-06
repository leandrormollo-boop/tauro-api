import os
import fitz
import re
import csv

pdf_dir = "/Users/leanrmollo/Documents/colab tauro/notas_credito/"
output_csv = os.path.join(pdf_dir, "Resumen_Notas_Credito.csv")

files = [f for f in os.listdir(pdf_dir) if f.endswith(".pdf")]

registros = []

for f in files:
    try:
        doc = fitz.open(os.path.join(pdf_dir, f))
        text = ""
        for page in doc:
            text += page.get_text()
        
        nc_match = re.search(r"Nota de Crédito:\s*([\d-]+)", text)
        factura_match = re.search(r"Ajustes en Factura\s*([\d-]+)", text)
        
        # Mejorar match de fecha
        fecha_match = re.search(r"Fecha Emisión:\s*(\d{1,2}\s+[a-zA-Z-]+\s+\d{4})", text, re.IGNORECASE)
        
        monto_match = re.search(r"Total de Crédito\s*([\d\s,]+)", text)
        
        guia, cod = "", ""
        detalle_idx = text.find("Detalle de Nota de Crédito")
        if detalle_idx != -1:
            detalle_text = text[detalle_idx:]
            guia_match = re.search(r"(\d{12})", detalle_text) # 12 digit tracking number
            if guia_match:
                guia = guia_match.group(1)
                lines = detalle_text[guia_match.end():].split('\n')
                for line in lines:
                    line = line.strip()
                    if line and not line.replace(',', '').replace('.','').isdigit() and "Página" not in line and "Nota" not in line:
                        cod = line
                        break

        nc = nc_match.group(1) if nc_match else ""
        factura = factura_match.group(1) if factura_match else ""
        fecha = fecha_match.group(1) if fecha_match else ""
        monto = monto_match.group(1).replace(" ", "") if monto_match else ""
        
        registros.append({
            "Archivo": f,
            "Nota de Credito": nc,
            "Factura Afectada": factura,
            "Fecha": fecha,
            "Guia (Tracking)": guia,
            "Monto": monto,
            "Motivo Ajuste": cod
        })
    except Exception as e:
        print(f"Error procesando {f}: {e}")

# Escribir a CSV
with open(output_csv, mode="w", newline="", encoding="utf-8-sig") as csv_file:
    writer = csv.DictWriter(csv_file, fieldnames=["Archivo", "Nota de Credito", "Factura Afectada", "Fecha", "Guia (Tracking)", "Monto", "Motivo Ajuste"])
    writer.writeheader()
    writer.writerows(registros)

print(f"✅ Se han procesado {len(registros)} notas de crédito.")
print(f"📁 El archivo se ha guardado en: {output_csv}")
