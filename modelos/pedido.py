# ============================================================
# Modelos Pydantic — Pedido (guía futura)
# ============================================================
from pydantic import BaseModel, Field, EmailStr, validator
from typing import List, Optional
from .producto import ItemPedido


class Direccion(BaseModel):
    """Datos de remitente o destinatario para la guía."""
    nombre_completo: str = Field(..., min_length=2, max_length=80)
    empresa: Optional[str] = Field(None, max_length=80)
    direccion_linea_1: str = Field(..., min_length=5, max_length=120)
    direccion_linea_2: Optional[str] = Field(None, max_length=120)
    ciudad: str = Field(..., min_length=2, max_length=60)
    estado_provincia: str = Field(..., min_length=2, max_length=60)
    zip: str = Field(..., min_length=3, max_length=12)
    pais: str = Field(..., min_length=2, max_length=2, description="ISO-2")
    telefono: str = Field(..., min_length=6)
    email: EmailStr


class PedidoInput(BaseModel):
    coti_id: str = Field(..., description="Cotización previa válida")
    remitente: Direccion
    destinatario: Direccion
    items: List[ItemPedido] = Field(..., min_items=1)
    notas: Optional[str] = None


class PedidoOutput(BaseModel):
    pedido_id: str
    estado: str  # PENDIENTE | EN_PROCESO | DESPACHADO | ENTREGADO | CANCELADO
    tracking_fedex: Optional[str] = None
    guia_pdf_url: Optional[str] = None
    invoice_pdf_url: Optional[str] = None
    creado: str  # ISO timestamp
