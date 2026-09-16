# ============================================================
# Modelos Pydantic — Producto del catálogo
# ============================================================
from datetime import datetime
from pydantic import BaseModel, Field, validator
from typing import Optional

from servicios.numeros_humanos import parse_float_formulario
from servicios.hs_code import formato_hs


def _decimal_producto(valor):
    return parse_float_formulario(valor, "Peso o medida")


def _importe_producto(valor):
    return parse_float_formulario(valor, "Valor declarado", importe=True, minimo=0)


def _importe_producto_opcional(valor):
    return parse_float_formulario(
        valor, "Valor unitario", importe=True, requerido=False, minimo=0,
    )


class Producto(BaseModel):
    """Producto del catálogo de un cliente."""
    cliente: str = Field(..., description="UPPERCASE, match con PERFILES")
    alias_interno: str = Field(..., min_length=2, max_length=60,
                                description="Cómo lo llama el cliente, ej 'Mini bag'")
    nombre_invoice: str = Field(..., min_length=3, max_length=120,
                                 description="Cómo va en la commercial invoice (inglés)")
    hs_code: str = Field(..., description="HS internacional (6 dígitos) o extensión nacional (8/10)")
    largo_cm: float = Field(..., gt=0)
    ancho_cm: float = Field(..., gt=0)
    alto_cm: float = Field(..., gt=0)
    peso_kg: float = Field(..., gt=0)
    valor_usd_default: float = Field(..., ge=0)
    activo: bool = True
    # Miniatura del producto (data: URI). Se llena sola al importar de
    # Shopify; sirve para que el cliente lo identifique de un vistazo.
    imagen_url: Optional[str] = None
    plataforma: Optional[str] = None
    tienda_dominio: Optional[str] = None
    external_product_id: Optional[str] = None
    external_variant_id: Optional[str] = None
    external_inventory_item_id: Optional[str] = None
    sku_tienda: Optional[str] = None
    titulo_tienda: Optional[str] = None
    variante_tienda: Optional[str] = None
    precio_tienda: Optional[float] = None
    moneda_tienda: Optional[str] = None
    hs_code_tienda: Optional[str] = None
    pais_origen_tienda: Optional[str] = None
    stock_controlado: bool = False
    stock_disponible: Optional[int] = None
    stock_comprometido: Optional[int] = None
    stock_fisico: Optional[int] = None
    stock_entrante: Optional[int] = None
    stock_actualizado_at: Optional[datetime] = None
    source_updated_at: Optional[datetime] = None
    sync_activo: bool = True
    ubicaciones: list[dict] = Field(default_factory=list)

    _normalizar_decimales = validator(
        "largo_cm", "ancho_cm", "alto_cm", "peso_kg",
        pre=True, allow_reuse=True,
    )(_decimal_producto)
    _normalizar_importe = validator(
        "valor_usd_default", pre=True, allow_reuse=True,
    )(_importe_producto)

    @validator("hs_code")
    def validar_hs_code(cls, v):
        return formato_hs(v)

    @validator("cliente")
    def cliente_uppercase(cls, v):
        return v.strip().upper()

    @validator("nombre_invoice")
    def sin_caracteres_problematicos(cls, v):
        # Evitar caracteres que rompan el PDF de la invoice
        if any(c in v for c in ["\\", "<", ">", "|"]):
            raise ValueError("nombre_invoice no puede contener: \\ < > |")
        return v.strip()


class ProductoNuevo(BaseModel):
    """Lo que el cliente envía al agregar un producto desde el portal."""
    alias_interno: str = Field(..., min_length=2, max_length=60)
    nombre_invoice: str = Field(..., min_length=3, max_length=120)
    hs_code: str
    largo_cm: float = Field(..., gt=0)
    ancho_cm: float = Field(..., gt=0)
    alto_cm: float = Field(..., gt=0)
    peso_kg: float = Field(..., gt=0)
    valor_usd_default: float = Field(..., ge=0)

    _normalizar_hs = validator("hs_code", allow_reuse=True)(formato_hs)

    _normalizar_decimales = validator(
        "largo_cm", "ancho_cm", "alto_cm", "peso_kg",
        pre=True, allow_reuse=True,
    )(_decimal_producto)
    _normalizar_importe = validator(
        "valor_usd_default", pre=True, allow_reuse=True,
    )(_importe_producto)


class ItemPedido(BaseModel):
    """Un item dentro de un pedido (referencia un producto del catálogo)."""
    alias_interno: str = Field(..., description="Producto del catálogo del cliente")
    cantidad: int = Field(..., ge=1)
    valor_unitario_usd: Optional[float] = Field(None,
        description="Override del valor default (opcional)")

    _normalizar_importe = validator(
        "valor_unitario_usd", pre=True, allow_reuse=True,
    )(_importe_producto_opcional)
