import smtplib


def _from_visible(remitente: str) -> str:
    """
    El remitente que VE el destinatario. EMAIL_FROM permite mandar como un
    alias del dominio (ej: "TAURO Solutions <cotizaciones@taurosolutions.ar>")
    mientras el login SMTP sigue siendo la cuenta real (EMAIL_REMITENTE).
    En Google Workspace el alias tiene que estar dado de alta en el usuario
    ("Enviar como") o Gmail lo pisa con la cuenta real.
    """
    import os
    return os.getenv("EMAIL_FROM", "").strip() or remitente


import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from io import BytesIO
from dotenv import load_dotenv
from core.email_transport import EmailDeliveryResult

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
    msg["From"] = _from_visible(remitente)
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

def _enviar_mail_a(email_destino: str, asunto: str, cuerpo_html: str) -> bool:
    """
    Manda un mail HTML a un destinatario puntual. A diferencia de
    _enviar_mail (que va al EMAIL_DESTINO global de alertas), acá el
    destino se elige — lo usa el centinela del checkout.
    """
    import html
    import re

    from core.email_transport import send_transactional_email

    texto = re.sub(r"<\s*br\s*/?>", "\n", cuerpo_html or "", flags=re.I)
    texto = re.sub(r"</\s*(?:p|div|tr|h[1-6])\s*>", "\n", texto, flags=re.I)
    texto = html.unescape(re.sub(r"<[^>]+>", "", texto))
    texto = "\n".join(line.strip() for line in texto.splitlines() if line.strip())
    resultado = send_transactional_email(
        recipient=email_destino,
        subject=asunto,
        text_body=texto or "Notificación de TAURO Solutions",
        html_body=cuerpo_html,
    )
    print(f"[email] aviso: {resultado.code}")
    return resultado.accepted


def enviar_link_magico(email_destino: str, link: str, cliente: str,
                       vence_en: str = "7 días") -> bool:
    """
    Envía el link mágico de login. Sirve para el portal del cliente y para
    recuperar el acceso al admin (de ahí el `vence_en` parametrizable).

    A diferencia de _enviar_mail, usa el email del destinatario y no el
    EMAIL_DESTINO global, que es para alertas internas.
    """
    from html import escape

    from core.email_transport import send_transactional_email

    asunto = "Acceso al portal Tauro Solutions"
    cliente_seguro = escape(str(cliente or "Cliente"))
    link_seguro = escape(link, quote=True)
    # Identidad TAURO: violeta #a78bfa sobre negro violáceo. Los clientes de
    # mail no soportan CSS moderno, así que todo va en estilos inline.
    cuerpo = f"""<html><body style="margin:0;padding:0;background:#f4f4f6;">
<div style="font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;color:#2a2a33;max-width:560px;margin:0 auto;background:#ffffff;">

  <div style="background:#0c0a14;padding:34px 24px;text-align:center;">
    <div style="font-size:24px;font-weight:700;letter-spacing:0.08em;color:#ffffff;">
      TAURO <span style="color:#a78bfa;">SOLUTIONS</span>
    </div>
    <div style="margin-top:6px;font-size:11px;letter-spacing:0.14em;color:#8b86a0;text-transform:uppercase;">
      Logística internacional
    </div>
  </div>

  <div style="padding:34px 30px;">
    <h2 style="color:#0c0a14;margin:0 0 14px;font-size:21px;">Hola {cliente_seguro}</h2>
    <p style="line-height:1.7;color:#4a4a58;margin:0 0 26px;">
      Pediste acceso al portal de Tauro Solutions. Entrá con este botón:
    </p>
    <p style="text-align:center;margin:0 0 26px;">
      <a href="{link_seguro}" style="background:#7c5cf6;color:#ffffff;padding:15px 34px;
         text-decoration:none;font-weight:600;border-radius:999px;display:inline-block;
         font-size:15px;">Entrar al portal</a>
    </p>
    <p style="color:#7a7a88;font-size:13px;line-height:1.6;margin:0 0 18px;">
      Si no fuiste vos, ignorá este mail. El link vence en {vence_en} y se usa una sola vez.
    </p>
    <p style="color:#a0a0ad;font-size:11px;word-break:break-all;margin:0;">{link_seguro}</p>
  </div>

  <div style="background:#0c0a14;padding:18px;text-align:center;font-size:11px;color:#8b86a0;">
    Tauro Solutions · taurosolutions.ar<br>
    <span style="color:#5f5b70;">Envíos internacionales puerta a puerta</span>
  </div>
</div>
</body></html>"""

    texto = (
        f"Hola {cliente}.\n\nEntrá al portal TAURO desde este link: {link}\n\n"
        f"El link vence en {vence_en} y se usa una sola vez. Si no fuiste vos, ignoralo."
    )
    resultado = send_transactional_email(
        recipient=email_destino,
        subject=asunto,
        text_body=texto,
        html_body=cuerpo,
        dedupe_key=f"magic:{link}",
    )
    print(f"[email] link mágico: {resultado.code}")
    return resultado.accepted


def enviar_restablecimiento_password(
    email_destino: str,
    link: str,
) -> EmailDeliveryResult:
    """Envía el link de un solo uso para crear una contraseña nueva.

    Devuelve el ``EmailDeliveryResult`` tipado del transporte. El worker usa
    ``accepted``, ``retryable`` y ``code`` para decidir activación/reintentos;
    por eso un fallo de configuración o red nunca deja un link canjeable.
    No se imprimen destinatario ni URL: ambos contienen datos sensibles.
    """
    from html import escape
    from core.email_transport import send_transactional_email

    link_seguro = escape(link, quote=True)
    cuerpo = f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="x-apple-disable-message-reformatting"><title>Restablecer contraseña · TAURO</title></head>
<body style="margin:0;padding:0;background:#f4f4f6;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" bgcolor="#f4f4f6" style="border-collapse:collapse;background:#f4f4f6;">
  <tr><td align="center" style="padding:22px 10px;">
    <table role="presentation" width="560" cellspacing="0" cellpadding="0" bgcolor="#ffffff" style="width:100%;max-width:560px;border-collapse:collapse;background:#ffffff;font-family:Arial,Helvetica,sans-serif;color:#2a2a33;">
      <tr><td align="center" bgcolor="#0c0a14" style="background:#0c0a14;padding:34px 24px;">
        <div style="font-size:24px;font-weight:700;letter-spacing:0.08em;color:#ffffff;">TAURO <span style="color:#a78bfa;">SOLUTIONS</span></div>
        <div style="margin-top:6px;font-size:11px;letter-spacing:0.14em;color:#b1aabd;text-transform:uppercase;">Tu operación logística, en un solo lugar</div>
      </td></tr>
      <tr><td style="padding:34px 30px;">
        <div style="font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#6746d9;margin-bottom:10px;">Seguridad de la cuenta</div>
        <h1 style="color:#0c0a14;margin:0 0 14px;font-size:22px;line-height:1.3;">Creá una nueva contraseña</h1>
        <p style="line-height:1.7;color:#4a4a58;margin:0 0 26px;">Recibimos un pedido para restablecer la contraseña de tu portal TAURO.</p>
        <table role="presentation" cellspacing="0" cellpadding="0" align="center" style="margin:0 auto 26px;">
          <tr><td align="center" bgcolor="#6746d9" style="border-radius:999px;">
            <a href="{link_seguro}" style="background:#6746d9;border:1px solid #6746d9;color:#ffffff;padding:15px 30px;text-decoration:none;font-weight:700;border-radius:999px;display:inline-block;font-size:15px;">Crear nueva contraseña</a>
          </td></tr>
        </table>
        <div style="border:1px solid #e8e6ef;border-radius:12px;padding:16px 18px;background:#faf9ff;color:#514b60;font-size:13px;line-height:1.6;">El link vence en <strong>30 minutos</strong> y se puede usar una sola vez. Al cambiarla, se cerrarán las sesiones abiertas de tu cuenta.</div>
        <p style="color:#676270;font-size:13px;line-height:1.6;margin:22px 0 10px;">Si no pediste este cambio, ignorá este correo. Tu contraseña actual seguirá funcionando.</p>
        <p style="color:#777181;font-size:11px;word-break:break-all;margin:0;">{link_seguro}</p>
      </td></tr>
      <tr><td align="center" bgcolor="#0c0a14" style="background:#0c0a14;padding:18px;color:#b1aabd;font-size:11px;">Tauro Solutions · taurosolutions.ar</td></tr>
    </table>
  </td></tr>
</table>
</body></html>"""
    texto = (
        "TAURO Solutions\n\n"
        "Recibimos un pedido para restablecer la contraseña de tu portal.\n"
        f"Creá una nueva contraseña desde este link: {link}\n\n"
        "El link vence en 30 minutos y se puede usar una sola vez. "
        "Al cambiarla, se cerrarán las sesiones abiertas de tu cuenta.\n\n"
        "Si no pediste este cambio, ignorá este correo."
    )
    resultado = send_transactional_email(
        recipient=email_destino,
        subject="Restablecé tu contraseña · TAURO Solutions",
        text_body=texto,
        html_body=cuerpo,
        dedupe_key=f"password-reset:{link}",
    )
    print(f"[email] recupero: {resultado.code}")
    return resultado


# ─────────────────────────────────────────────
# EMAIL NOTIFICACIÓN DE ESTADO — al cliente
# ─────────────────────────────────────────────

_ESTADO_COPY = {
    "EN_PROCESO": (
        "Tu envío está en proceso",
        "Ya estamos trabajando en tu solicitud. Te avisamos apenas la guía esté lista.",
    ),
    "GUIA_LISTA": (
        "¡Tu guía está lista! 🎉",
        "Ya podés descargar la guía en PDF desde tu portal, en la sección <b>Mis envíos</b>.",
    ),
    "DESPACHADO": (
        "Tu envío fue despachado 🚀",
        "El paquete ya está en manos del courier. Podés seguirlo con el número de tracking.",
    ),
    "CANCELADO": (
        "Tu solicitud fue cancelada",
        "Si no lo esperabas o querés más detalles, escribinos y lo revisamos juntos.",
    ),
}


def enviar_notificacion_estado(
    email_destino: str,
    cliente: str,
    solicitud_id: int,
    estado: str,
    tracking: str = "",
    portal_url: str = "https://tauro-api-production.up.railway.app/portal/envios",
) -> bool:
    """
    Avisa al cliente que su solicitud cambió de estado. Best-effort:
    nunca lanza excepción (el flujo operativo no debe romperse por el mail).
    """
    copy = _ESTADO_COPY.get((estado or "").strip().upper())
    if not copy:
        return False  # estados sin comunicación (ej. SOLICITADO)
    titulo, detalle = copy

    tracking_html = ""
    if tracking:
        tracking_html = f"""
  <p style="margin:18px 0 6px; color:#555; font-size:13px;">Número de seguimiento</p>
  <p style="font-family: monospace; font-size: 18px; margin:0; color:#111;"><b>{tracking}</b></p>"""

    asunto = f"Tauro · Envío #{solicitud_id}: {titulo}"
    cuerpo = f"""<html><body style="font-family: Arial, sans-serif; color: #333; max-width: 600px; margin: 0 auto;">
<div style="background:#0a0e12; padding: 22px; text-align: center; border-radius: 0 0 12px 12px;">
  <h1 style="color:#ff2d6b; margin: 0; letter-spacing: 1px;">TAURO</h1>
  <p style="color:#7a828c; margin: 4px 0 0; font-size: 12px; text-transform: uppercase; letter-spacing: 2px;">Solutions</p>
</div>
<div style="padding: 30px 20px;">
  <h2 style="color:#111; margin-top:0;">{titulo}</h2>
  <p>Hola {cliente},</p>
  <p>{detalle}</p>{tracking_html}
  <p style="text-align: center; margin: 30px 0;">
    <a href="{portal_url}" style="background:#ff2d6b; color:#fff; padding: 13px 26px; text-decoration: none; font-weight: bold; border-radius: 8px; display: inline-block;">Ver mi envío en el portal</a>
  </p>
</div>
<div style="background:#f5f5f5; padding: 12px; text-align: center; font-size: 11px; color: #999; border-radius: 12px;">
  Tauro Solutions — Logística internacional
</div>
</body></html>"""

    remitente = os.getenv("EMAIL_REMITENTE")
    password = os.getenv("EMAIL_PASSWORD")
    if not remitente or not password or not email_destino:
        print(f"[email] Notificación de estado no enviada (SMTP o destino faltante) — solicitud {solicitud_id}")
        return False

    msg = MIMEMultipart("mixed")
    msg["From"] = _from_visible(remitente)
    msg["To"] = email_destino
    msg["Subject"] = asunto
    msg.attach(MIMEText(cuerpo, "html", "utf-8"))

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587, timeout=15)
        server.starttls()
        server.login(remitente, password)
        server.sendmail(remitente, email_destino, msg.as_string())
        server.quit()
        print(f"[email] Notificación '{estado}' enviada a {email_destino} (solicitud {solicitud_id})")
        return True
    except Exception as e:
        print(f"[email] Error enviando notificación de estado: {e}")
        return False
