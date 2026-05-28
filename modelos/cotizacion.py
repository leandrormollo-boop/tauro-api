# ============================================================
# Modelos Pydantic — Cotización
# ============================================================
from pydantic import BaseModel, Field, validator
from typing import Optional


class CotizacionInput(BaseModel):
    """Input simplificado del cliente — usa ruta predefinida."""
    ruta_id: str = Field(..., description="ID de RUTAS_DEFAULT, ej 'AR-US'")
    peso_kg: float = Field(..., gt=0, le=70, description="Peso real (max 70kg FedEx IP)")
    largo_cm: float = Field(..., gt=0, description="Largo del paquete")
    ancho_cm: float = Field(..., gt=0, description="Ancho del paquete")
    alto_cm: float = Field(..., gt=0, description="Alto del paquete")

    @validator("alto_cm")
    def suma_dimensiones(cls, v, values):
        if all(k in values for k in ["largo_cm", "ancho_cm"]):
            total = values["largo_cm"] + values["ancho_cm"] + v
            if total > 330:
                raise ValueError(f"Suma de dimensiones {total}cm supera el límite FedEx (330cm)")
        return v


class CotizacionAvanzada(BaseModel):
    """Input avanzado — para B2B que controla origen/destino."""
    origen_pais: str = Field(..., min_length=2, max_length=2, description="ISO-2: AR, US")
    origen_ciudad: str = Field(..., min_length=2)
    origen_zip: str = Field(..., min_length=3)
    destino_pais: str = Field(..., min_length=2, max_length=2)
    destino_ciudad: str = Field(..., min_length=2)
    destino_zip: str = Field(..., min_length=3)
    peso_kg: float = Field(..., gt=0, le=70)
    largo_cm: float = Field(..., gt=0)
    ancho_cm: float = Field(..., gt=0)
    alto_cm: float = Field(..., gt=0)


class CotizacionOutput(BaseModel):
    """Respuesta al cliente."""
    coti_id: str = Field(..., description="UUID — referencia para crear pedido")
    ruta: str
    peso_real_kg: float
    peso_volumetrico_kg: float
    peso_usado_kg: float = Field(..., description="El mayor entre real y volumétrico")
    costo_fedex_usd: float = Field(..., description="Costo crudo FedEx, sin markup")
    markup_pct: float
    markup_tipo: str = "PCT"
    markup_valor: Optional[float] = None
    precio_final_usd: float
    precio_final_ars: float
    dias_estimados: int
    valida_hasta: str = Field(..., description="ISO timestamp")


def calcular_peso_volumetrico(largo: float, ancho: float, alto: float) -> float:
    """Fórmula FedEx: (L × A × H) / 5000."""
    return round((largo * ancho * alto) / 5000, 2)
