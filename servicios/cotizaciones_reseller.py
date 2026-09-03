"""Snapshots y PDF comerciales para clientes reseller.

El snapshot vive en PostgreSQL y se busca por un identificador aleatorio. Así
el navegador nunca firma ni transporta costos, márgenes o reglas de pricing.
"""

from __future__ import annotations

import io
import json
import secrets
from decimal import Decimal
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from core.database import get_conn
from servicios.numeros_humanos import parse_importe_humano


_ROOT = Path(__file__).resolve().parents[1]
_LOGO = _ROOT / "static" / "img" / "logo-lockup-dark.png"


def cliente_es_reseller(cliente_id: str) -> bool:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT es_reseller FROM clientes WHERE cliente_id=%s AND activo=TRUE",
                    ((cliente_id or "").strip().upper(),),
                )
                fila = cur.fetchone()
    except Exception as exc:
        print(f"[reseller] no pude verificar el permiso: {type(exc).__name__}")
        return False
    return bool(fila and fila.get("es_reseller"))


def guardar_opciones(
    cliente_id: str,
    *,
    ruta: str,
    bultos: list[dict],
    peso_facturable_kg,
    opciones: list[dict],
) -> list[dict]:
    """Persiste una copia segura por opción y devuelve opciones enriquecidas."""
    cliente_id = (cliente_id or "").strip().upper()
    if not opciones or not cliente_es_reseller(cliente_id):
        return opciones or []

    salida = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            for opcion in opciones:
                quote_id = "RQ-" + secrets.token_urlsafe(24)
                cur.execute(
                    """
                    INSERT INTO cotizaciones_reseller (
                        quote_id, cliente_id, ruta, bultos,
                        peso_facturable_kg, tiempo_estimado, precio_base_ars,
                        courier, servicio, vigente_hasta
                    ) VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s,
                              NOW() + INTERVAL '24 hours')
                    """,
                    (
                        quote_id, cliente_id, str(ruta or "")[:160],
                        json.dumps(bultos or [], ensure_ascii=False),
                        peso_facturable_kg,
                        str(opcion.get("dias_estimados") or "A confirmar")[:60],
                        opcion.get("precio_final_ars"),
                        str(opcion.get("carrier_nombre") or "")[:60],
                        str(opcion.get("servicio") or "")[:120],
                    ),
                )
                salida.append({**opcion, "reseller_quote_id": quote_id})
    return salida


def _obtener(cliente_id: str, quote_id: str) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT q.*
                FROM cotizaciones_reseller q
                JOIN clientes c ON c.cliente_id=q.cliente_id
                WHERE q.quote_id=%s AND q.cliente_id=%s
                  AND q.vigente_hasta >= NOW()
                  AND c.activo=TRUE AND c.es_reseller=TRUE
                """,
                (str(quote_id or "").strip(), (cliente_id or "").strip().upper()),
            )
            fila = cur.fetchone()
    return dict(fila) if fila else None


def _dinero(valor: Decimal) -> str:
    texto = f"{valor:,.2f}"
    return "$ " + texto.replace(",", "_").replace(".", ",").replace("_", ".")


def generar_pdf(cliente_id: str, quote_id: str, precio_mostrado) -> tuple[bytes, str]:
    cotizacion = _obtener(cliente_id, quote_id)
    if not cotizacion:
        raise ValueError("La cotización no existe, venció o no pertenece a tu cuenta.")
    precio = parse_importe_humano(precio_mostrado)
    if precio is None or precio <= 0 or precio > Decimal("999999999999.99"):
        raise ValueError("Ingresá un precio válido para mostrar.")
    base = Decimal(str(cotizacion["precio_base_ars"]))
    if precio < base:
        raise ValueError("El precio reseller puede mantenerse o subir, pero no bajar.")

    bultos = cotizacion.get("bultos") or []
    if isinstance(bultos, str):
        bultos = json.loads(bultos)
    buffer = io.BytesIO()
    documento = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=22 * mm, leftMargin=22 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title=f"Cotización {cotizacion['quote_id']} · TAURO Solutions",
        author="TAURO Solutions",
    )
    estilos = getSampleStyleSheet()
    elementos = []
    if _LOGO.exists():
        elementos.append(Image(str(_LOGO), width=24 * mm, height=24 * mm))
    elementos.extend([
        Paragraph("<b>TAURO SOLUTIONS</b>", estilos["Title"]),
        Paragraph("Cotización comercial de envío", estilos["Heading2"]),
        Spacer(1, 5 * mm),
    ])
    datos = [
        ["Referencia", cotizacion["quote_id"]],
        ["Ruta", cotizacion["ruta"]],
        ["Courier / servicio", f"{cotizacion['courier']} · {cotizacion['servicio']}"],
        ["Peso facturable", f"{cotizacion['peso_facturable_kg']} kg"],
        ["Tiempo estimado", str(cotizacion["tiempo_estimado"])],
        ["PRECIO", f"{_dinero(precio)} ARS"],
    ]
    tabla = Table(datos, colWidths=[48 * mm, 102 * mm])
    tabla.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#241c35")),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.white),
        ("GRID", (0, 0), (-1, -1), .5, colors.HexColor("#cfc4df")),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("PADDING", (0, 0), (-1, -1), 8),
    ]))
    elementos.extend([tabla, Spacer(1, 7 * mm), Paragraph("<b>Cajas</b>", estilos["Heading3"])])
    filas = [["Cant.", "Peso real", "Medidas"]]
    for bulto in bultos:
        filas.append([
            str(bulto.get("cantidad") or 1),
            f"{bulto.get('peso_kg') or '—'} kg",
            f"{bulto.get('largo_cm') or '—'} × {bulto.get('ancho_cm') or '—'} × {bulto.get('alto_cm') or '—'} cm",
        ])
    cajas = Table(filas, colWidths=[25 * mm, 42 * mm, 83 * mm])
    cajas.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#7c5cf6")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), .5, colors.HexColor("#cfc4df")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("PADDING", (0, 0), (-1, -1), 7),
    ]))
    elementos.extend([
        cajas,
        Spacer(1, 7 * mm),
        Paragraph(
            "Estimación sujeta a validación final de dirección, medidas, disponibilidad, impuestos y recargos extraordinarios.",
            estilos["BodyText"],
        ),
    ])
    documento.build(elementos)
    return buffer.getvalue(), f"cotizacion-{cotizacion['quote_id']}.pdf"
