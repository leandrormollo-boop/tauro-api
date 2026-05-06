# ============================================================
# Modelos Pydantic — Cliente / Perfil
# ============================================================
from pydantic import BaseModel, Field, EmailStr
from typing import Optional


class Cliente(BaseModel):
    """Perfil de un cliente B2B (de hoja PERFILES)."""
    cliente: str = Field(..., description="UPPERCASE, único")
    email: EmailStr
    api_key: str = Field(..., min_length=20)
    markup_pct: float = Field(..., ge=0, le=200)
    activo: bool = True
    notas: Optional[str] = None


class ClientePublico(BaseModel):
    """Datos del cliente seguros para mostrar en el portal (sin API_KEY)."""
    cliente: str
    email: EmailStr
    markup_pct: float
    activo: bool
