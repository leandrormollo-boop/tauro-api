# ============================================================
# Modelos Pydantic — Ruta predefinida
# ============================================================
from pydantic import BaseModel, Field


class Ruta(BaseModel):
    """Ruta predefinida de RUTAS_DEFAULT."""
    ruta_id: str = Field(..., description="ID único, ej AR-US")
    origen_pais: str
    origen_ciudad: str
    origen_zip: str
    destino_pais: str
    destino_ciudad: str
    destino_zip: str
    dias_estimados: int = Field(..., ge=1)
    activa: bool = True

    @property
    def label(self) -> str:
        """Para mostrar en el dropdown del portal."""
        return f"{self.origen_pais} → {self.destino_pais}"

    @property
    def descripcion_completa(self) -> str:
        return (f"{self.origen_ciudad} ({self.origen_zip}) → "
                f"{self.destino_ciudad} ({self.destino_zip}) · "
                f"{self.dias_estimados} días")
