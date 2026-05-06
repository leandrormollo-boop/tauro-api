import smtplib
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from io import BytesIO
from dotenv import load_dotenv

load_dotenv()


# ─────────────────────────────────────────────
# GENERADOR DE PDF
# ─────────────────────────────────────────────

def generar_pdf_pedido(datos: dict) -> bytes:
    """
    Genera el PDF de armado de guía con ReportLab.
    Incluye 4 bloques: Remitente, Destinatario, Aduana, Financiero.
    """
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, HRFlowable
        from reportlab.lib.enums import TA_LEFT, TA_CENTER
    except ImportError:
        raise RuntimeError(
            "ReportLab no instalado. Ejecutá: pip install reportlab --break-system-packages"
        )

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=2 * cm,
        leftMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )

    styles = getSampleStyleSheet()
    COLOR_PRIMARIO = colors.HexColor("#534AB7")
    COLOR_FILA_PAR = colors.HexColor("#f3f2fc")
    COLOR_BORDE = colors.HexColor("#cccccc")

    estilo_titulo = ParagraphStyle(
        "titulo", parent=styles["Heading1"], fontSize=16,
        textColor=colors.HexColor("#1a1a2e"), spaceAfter=4
    )
    estilo_subtitulo = ParagraphStyle(
        "subtitulo", parent=styles["Normal"], fontSize=10,
        textColor=colors.HexColor("#666666"), spaceAfter=12
    )
    estilo_seccion = ParagraphStyle(
        "seccion", parent=styles["Heading2"], fontSize=11,
        textColor=COLOR_PRIMARIO, spaceBefore=14, spaceAfter=4
    )

    def tabla_datos(filas: list[tuple]) -> Table:
        data = [[Paragraph(f"<b>{k}</b>", styles["Normal"]), Paragraph(str(v), styles["Normal"])] for k, v in filas]
        t = Table(data, colWidths=[5.5 * cm, 11 * cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.white),
            ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, COLOR_FILA_PAR]),
            ("GRID", (0, 0), (-1, -1), 0.5, COLOR_BORDE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        return t

    story = []

    # Encabezado
    story.append(Paragraph("📦 Orden de Armado de Guía", estilo_titulo))
    story.append(Paragraph(f"Referencia: {datos.get('referencia', 'N/A')}", estilo_subtitulo))
    story.append(HRFlowable(width="100%", thickness=1, color=COLOR_PRIMARIO))
    story.append(Spacer(1, 0.3 * cm))

    # Bloque 1 — Remitente
    story.append(Paragraph("1. Datos del Remitente", estilo_seccion))
    story.append(tabla_datos([
        ("Nombre / Empresa", datos.get("remitente_nombre", "")),
        ("CUIT", datos.get("remitente_cuit", "")),
        ("Dirección", datos.get("remitente_direccion", "")),
        ("Código Postal", datos.get("remitente_cp", "")),
        ("Ciudad", datos.get("remitente_ciudad", "")),
        ("País", datos.get("remitente_pais", "AR")),
        ("Teléfono", datos.get("remitente_telefono", "")),
        ("Email", datos.get("remitente_email", "")),
    ]))

    story.append(Spacer(1, 0.3 * cm))

    # Bloque 2 — Destinatario
    story.append(Paragraph("2. Datos del Destinatario", estilo_seccion))
    story.append(tabla_datos([
        ("Nombre completo", datos.get("dest_nombre", "")),
        ("Dirección exacta", datos.get("dest_direccion", "")),
        ("Ciudad", datos.get("dest_ciudad", "")),
        ("Estado / Provincia", datos.get("dest_estado", "")),
        ("ZIP / Código Postal", datos.get("dest_zip", "")),
        ("País", datos.get("dest_pais", "")),
        ("Teléfono", datos.get("dest_telefono", "")),
        ("Email", datos.get("dest_email", "")),
    ]))

    story.append(Spacer(1, 0.3 * cm))

    # Bloque 3 — Aduana
    story.append(Paragraph("3. Datos Aduanales", estilo_seccion))
    story.append(tabla_datos([
        ("Descripción (ES)", datos.get("producto_nombre_es", "")),
        ("Descripción (EN)", datos.get("producto_nombre_en", "")),
        ("Código HS", datos.get("producto_hs_code", "")),
        ("Valor declarado USD", f"USD {datos.get('producto_valor_usd', 0):.2f}"),
        ("Unidades", datos.get("producto_unidades", 1)),
        ("Peso (kg)", f"{datos.get('producto_peso_kg', 0)} kg"),
        ("Dimensiones (cm)", f"{datos.get('producto_largo', 0)} x {datos.get('producto_ancho', 0)} x {datos.get('producto_alto', 0)}"),
    ]))

    story.append(Spacer(1, 0.3 * cm))

    # Bloque 4 — Financiero (solo interno, no visible al cliente)
    story.append(Paragraph("4. Datos Financieros (Interno Tauro)", estilo_seccion))
    story.append(tabla_datos([
        ("Precio cobrado ARS", f"$ {datos.get('precio_cobrado_ars', 0):,.2f}"),
        ("Precio cobrado USD", f"USD {datos.get('precio_cobrado_usd', 0):.2f}"),
        ("Tipo de cambio usado", f"$ {datos.get('tipo_cambio', 0):,.0f}"),
        ("Costo FedEx ARS", f"$ {datos.get('costo_fedex_ars', 0):,.2f}"),
        ("Margen ARS", f"$ {datos.get('margen_ars', 0):,.2f}"),
    ]))

    doc.build(story)
    return buffer.getvalue()


# ─────────────────────────────────────────────
# EMAIL HELPER
# ─────────────────────────────────────────────

def _enviar_mail(asunto: str, cuerpo_html: str, pdf_bytes: bytes = None, nombre_pdf: str = None) -> bool:
    remitente = os.getenv("EMAIL_REMITENTE")
    password = os.getenv("EMAIL_PASSWORD")
    destinatario = os.getenv("EMAIL_DESTINO")

    if not remitente or not password or not destinatario:
        print("[email] Variables EMAIL_REMITENTE / EMAIL_PASSWORD / EMAIL_DESTINO no configuradas.")
        return False

    msg = MIMEMultipart("mixed")
    msg["From"] = remitente
    msg["To"] = destinatario
    msg["Subject"] = asunto

    msg.attach(MIMEText(cuerpo_html, "html", "utf-8"))

    if pdf_bytes and nombre_pdf:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(pdf_bytes)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{nombre_pdf}"')
        msg.attach(part)

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(remitente, password)
        server.sendmail(remitente, destinatario, msg.as_string())
        server.quit()
        print(f"[email] Enviado: {asunto}")
        return True
    except Exception as e:
        print(f"[email] Error al enviar: {e}")
        return False


# ─────────────────────────────────────────────
# EMAIL PEDIDO — con PDF adjunto
# ─────────────────────────────────────────────

def enviar_email_pedido(datos: dict) -> bool:
    """
    Genera el PDF del pedido y lo envía a logística como adjunto.
    """
    try:
        pdf_bytes = generar_pdf_pedido(datos)
    except Exception as e:
        print(f"[email] Error al generar PDF: {e}")
        return False

    referencia = datos.get("referencia", "pedido")
    nombre_pdf = f"guia_{referencia}.pdf"

    asunto = f"📦 NUEVO PEDIDO — {datos.get('remitente_nombre', '')} | {referencia}"
    cuerpo = f"""
    <html><body style="font-family: Arial, sans-serif; color: #333;">
    <h2 style="color:#534AB7;">Nuevo pedido recibido</h2>
    <p><b>Referencia:</b> {referencia}</p>
    <p><b>Cliente:</b> {datos.get('remitente_nombre', '')}</p>
    <p><b>Destinatario:</b> {datos.get('dest_nombre', '')} — {datos.get('dest_ciudad', '')}, {datos.get('dest_pais', '')}</p>
    <p><b>Producto:</b> {datos.get('producto_nombre_es', '')} ({datos.get('producto_nombre_en', '')})</p>
    <p style="color:#888; font-size:12px;">El PDF adjunto contiene todos los datos para armar la guía en el portal FedEx.</p>
    </body></html>
    """

    return _enviar_mail(asunto, cuerpo, pdf_bytes=pdf_bytes, nombre_pdf=nombre_pdf)


# ─────────────────────────────────────────────
# EMAIL ALERTA DE MARGEN
# ─────────────────────────────────────────────

def enviar_alerta_margen(alertas: list[dict]) -> bool:
    """
    Envía alerta cuando el margen cae por debajo del mínimo configurado.
    Llamado por el job semanal.
    """
    if not alertas:
        return True

    filas_html = ""
    for a in alertas:
        color = "#A32D2D" if a["margen_ars"] < 0 else "#C67A00"
        filas_html += f"""
        <tr>
          <td>{a['cliente_id']}</td>
          <td>{a['producto_id']}</td>
          <td>{a['destino_pais']}</td>
          <td>$ {a['precio_ars']:,.0f}</td>
          <td>$ {a['costo_fedex_ars']:,.0f}</td>
          <td style="color:{color}; font-weight:bold;">$ {a['margen_ars']:,.0f}</td>
        </tr>
        """

    asunto = f"⚠️ ALERTA MARGEN — {len(alertas)} combinación(es) bajo mínimo"
    cuerpo = f"""
    <html><body style="font-family: Arial, sans-serif; color: #333;">
    <h2 style="color:#A32D2D;">⚠️ Alerta de margen bajo</h2>
    <p>El job semanal detectó {len(alertas)} combinación(es) con margen por debajo del mínimo configurado.
    Revisá los precios en la hoja COTI y actualizá si es necesario.</p>
    <table cellpadding="8" cellspacing="0" border="1" style="border-collapse:collapse; width:100%;">
      <tr style="background:#534AB7; color:white;">
        <th>Cliente</th><th>Producto</th><th>Destino</th>
        <th>Precio ARS</th><th>Costo FedEx ARS</th><th>Margen</th>
      </tr>
      {filas_html}
    </table>
    <p style="margin-top:16px; color:#666;">Actualización automática — Tauro Solutions</p>
    </body></html>
    """

    return _enviar_mail(asunto, cuerpo)


# ─────────────────────────────────────────────
# EMAIL LINK MÁGICO — login del portal
# ─────────────────────────────────────────────

def enviar_link_magico(email_destino: str, link: str, cliente: str) -> bool:
    """
    Envía el link mágico de login al cliente.
    A diferencia de _enviar_mail, esta usa el email del cliente como destinatario
    (no el EMAIL_DESTINO global, que es para alertas internas).
    """
    asunto = "Acceso al portal Tauro Solutions"
    cuerpo = f"""<html><body style="font-family: Arial, sans-serif; color: #333; max-width: 600px; margin: 0 auto;">
<div style="background:#1a1a1a; padding: 20px; text-align: center;">
  <h1 style="color:#d4af37; margin: 0;">TAURO SOLUTIONS</h1>
</div>
<div style="padding: 30px 20px;">
  <h2 style="color:#1a1a1a;">Hola {cliente}</h2>
  <p>Pediste acceso al portal de Tauro Solutions. Para entrar, hacé click acá:</p>
  <p style="text-align: center; margin: 30px 0;">
    <a href="{link}" style="background:#d4af37; color:#1a1a1a; padding: 14px 28px; text-decoration: none; font-weight: bold; border-radius: 4px; display: inline-block;">Entrar al portal</a>
  </p>
  <p style="color:#666; font-size: 13px;">Si no fuiste vos, ignorá este mail. El link expira en 7 días.</p>
  <p style="color:#888; font-size: 11px; word-break: break-all;">{link}</p>
</div>
<div style="background:#f5f5f5; padding: 12px; text-align: center; font-size: 11px; color: #999;">
  Tauro Solutions — Envíos internacionales vía FedEx
</div>
</body></html>"""

    remitente = os.getenv("EMAIL_REMITENTE")
    password = os.getenv("EMAIL_PASSWORD")

    if not remitente or not password:
        print("[email] SMTP no configurado, no se envía link mágico.")
        return False

    msg = MIMEMultipart("mixed")
    msg["From"] = remitente
    msg["To"] = email_destino
    msg["Subject"] = asunto
    msg.attach(MIMEText(cuerpo, "html", "utf-8"))

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(remitente, password)
        server.sendmail(remitente, email_destino, msg.as_string())
        server.quit()
        print(f"[email] Link mágico enviado a {email_destino}")
        return True
    except Exception as e:
        print(f"[email] Error enviando link mágico: {e}")
        return False
