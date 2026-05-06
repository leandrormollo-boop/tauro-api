============================================================
  03_modelos — Schemas Pydantic
============================================================

QUÉ HAY ACÁ
-----------
Modelos de datos (Pydantic) que validan inputs y outputs del API.

  cotizacion.py    - CotizacionInput, CotizacionOutput
  pedido.py        - PedidoInput, PedidoOutput, ItemPedido
  producto.py      - Producto (catálogo), ProductoNuevo
  ruta.py          - Ruta predefinida (origen/destino con ZIP)
  cliente.py       - Perfil de cliente (PERFILES)

LÓGICA Y DECISIONES
-------------------

POR QUÉ PYDANTIC
- Validación automática de tipos en cada request.
- Errores 422 con mensaje claro si falta un campo.
- Documentación automática en /docs (Swagger UI).
- Convertimos Pydantic → dict para escribir a Sheets.

REGLAS DE VALIDACIÓN
- Pesos: > 0 y < 70 (límite FedEx International Priority).
- Dimensiones: cada lado > 0 y suma < 330cm.
- ZIP: regex por país (^\d{5}$ US, [A-Z]\d{4}\w{3} AR, etc.).
- HS Code: ^\d{4}\.\d{2}\.\d{2}$ (formato Mercosur 8 dígitos).
- Email: validador estándar Pydantic.
- Tracking FedEx: 12-15 dígitos.
- NRO FC: regex 0106-XXXXXXXX.

EJEMPLO
-------
class CotizacionInput(BaseModel):
    ruta_id: str = Field(..., description="ID de RUTAS_DEFAULT, ej AR-US")
    peso_kg: float = Field(..., gt=0, le=70)
    largo_cm: float = Field(..., gt=0)
    ancho_cm: float = Field(..., gt=0)
    alto_cm: float = Field(..., gt=0)

    @validator("largo_cm", "ancho_cm", "alto_cm")
    def suma_dimensiones(cls, v, values):
        if all(k in values for k in ["largo_cm", "ancho_cm", "alto_cm"]):
            if sum(values.values()) > 330:
                raise ValueError("Suma de dimensiones supera 330cm")
        return v

REGLAS
------
- Un archivo por dominio (cotizacion, pedido, producto, etc.).
- Todos los modelos heredan de BaseModel.
- Inputs y outputs separados (Input para POST, Output para respuesta).
- Documentar cada campo con Field(..., description=).
