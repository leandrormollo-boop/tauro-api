# ============================================================
# Modelos Pydantic — Producto del catálogo
# ============================================================
import re
from pydantic import BaseModel, Field, validator
from typing import Optional


HS_CODE_REGEX = re.compile(r"^\d{4}\.\d{2}\.\d{2}$")


class Producto(BaseModel):
    """Producto del catálogo de un cliente."""
    cliente: str = Field(..., description="UPPERCASE, match con PERFILES")
    alias_interno: str = Field(..., min_length=2, max_length=60,
                                description="Cómo lo llama el cliente, ej 'Mini bag'")
    nombre_invoice: str = Field(..., min_length=3, max_length=120,
                                 description="Cómo va en la commercial invoice (inglés)")
    hs_code: str = Field(..., description="Código HS Mercosur, formato XXXX.XX.XX")
    largo_cm: float = Field(..., gt=0)
    ancho_cm: float = Field(..., gt=0)
    alto_cm: float = Field(..., gt=0)
    peso_kg: float = Field(..., gt=0)
    valor_usd_default: float = Field(..., ge=0)
    activo: bool = True

    @validator("hs_code")
    def validar_hs_code(cls, v):
        v = v.strip()
        solo_digitos = re.sub(r"\D", "", v)
        if len(solo_digitos) == 8:
            return f"{solo_digitos[:4]}.{solo_digitos[4:6]}.{solo_digitos[6:8]}"
        if not HS_CODE_REGEX.match(v):
            raise ValueError(f"HS Code inválido. Formato esperado: XXXX.XX.XX (ej 4202.21.00)")
        return v

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


class ItemPedido(BaseModel):
    """Un item dentro de un pedido (referencia un producto del catálogo)."""
    alias_interno: str = Field(..., description="Producto del catálogo del cliente")
    cantidad: int = Field(..., ge=1)
    valor_unitario_usd: Optional[float] = Field(None,
        description="Override del valor default (opcional)")
