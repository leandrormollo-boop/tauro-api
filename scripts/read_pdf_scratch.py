import os
import fitz
import re

pdf_dir = "/Users/leanrmollo/Documents/colab tauro/notas_credito/"
files = [f for f in os.listdir(pdf_dir) if f.endswith(".pdf")]

for i, f in enumerate(files[:3]):
    doc = fitz.open(os.path.join(pdf_dir, f))
    text = ""
    for page in doc:
        text += page.get_text()
    
    print(f"--- File: {f} ---")
    
    nc_match = re.search(r"Nota de Crédito:\s*([\d-]+)", text)
    factura_match = re.search(r"Ajustes en Factura\s*([\d-]+)", text)
    fecha_match = re.search(r"Fecha Emisión:\s*(.+)", text)
    monto_match = re.search(r"Total de Crédito\s*([\d\s,]+)", text)
    
    # We might have tracking number at the end, often above "AHS-SOBRECARGO" or similar
    # The last page usually has "Detalle de Nota de Crédito"
    detalle_idx = text.find("Detalle de Nota de Crédito")
    guia, cod = None, None
    if detalle_idx != -1:
        detalle_text = text[detalle_idx:]
        guia_match = re.search(r"(\d{12})", detalle_text) # 12 digit tracking number
        if guia_match:
            guia = guia_match.group(1)
            # Find description (next lines usually)
            lines = detalle_text[guia_match.end():].split('\n')
            for line in lines:
                line = line.strip()
                if line and not line.replace(',', '').replace('.','').isdigit() and "Página" not in line and "Nota" not in line:
                    cod = line
                    break

    print(f"NC: {nc_match.group(1) if nc_match else 'None'}")
    print(f"Factura: {factura_match.group(1) if factura_match else 'None'}")
    print(f"Fecha: {fecha_match.group(1) if fecha_match else 'None'}")
    print(f"Monto: {monto_match.group(1) if monto_match else 'None'}")
    print(f"Guia: {guia}")
    print(f"Cod: {cod}")
