# ============================================================
# Servicio de catálogo de productos por cliente
# ============================================================
# Lee/escribe PRODUCTOS_CATALOGO.
# Filtrado por cliente — Mendez no ve catálogo de Tenuta.
# Productos nuevos quedan ACTIVO=FALSE hasta que Tauro valide HS Code.
# ============================================================

from datetime import datetime
from typing import List, Optional

from core.sheets_client import _abrir_sheet
from modelos.producto import Producto, ProductoNuevo


def _row_a_producto(r: dict) -> Optional[Producto]:
    try:
        return Producto(
            cliente=str(r.get("CLIENTE", "")).strip().upper(),
            alias_interno=str(r.get("ALIAS_INTERNO", "")).strip(),
            nombre_invoice=str(r.get("NOMBRE_INVOICE", "")).strip(),
            hs_code=str(r.get("HS_CODE", "")).strip(),
            largo_cm=float(r.get("LARGO_CM", 0) or 0),
            ancho_cm=float(r.get("ANCHO_CM", 0) or 0),
            alto_cm=float(r.get("ALTO_CM", 0) or 0),
            peso_kg=float(r.get("PESO_KG", 0) or 0),
            valor_usd_default=float(r.get("VALOR_USD_DEFAULT", 0) or 0),
            activo=str(r.get("ACTIVO", "")).strip().upper() == "TRUE",
        )
    except Exception:
        return None


def get_productos(cliente: str, solo_activos: bool = True) -> List[Producto]:
    """Productos del catálogo de un cliente."""
    cliente = cliente.strip().upper()
    sh = _abrir_sheet()
    hoja = sh.worksheet("PRODUCTOS_CATALOGO")
    rows = hoja.get_all_records()
    productos = []
    for r in rows:
        p = _row_a_producto(r)
        if not p or p.cliente != cliente:
            continue
        if solo_activos and not p.activo:
            continue
        productos.append(p)
    return productos


def get_producto(cliente: str, alias_interno: str) -> Optional[Producto]:
    """Buscar un producto específico del cliente."""
    cliente = cliente.strip().upper()
    alias = alias_interno.strip()
    for p in get_productos(cliente, solo_activos=False):
        if p.alias_interno == alias:
            return p
    return None


def agregar_producto(cliente: str, nuevo: ProductoNuevo) -> Producto:
    """Agrega un producto al catálogo. Queda ACTIVO=FALSE hasta validación."""
    cliente = cliente.strip().upper()

    # Validar que no exista ya un alias igual para ese cliente
    if get_producto(cliente, nuevo.alias_interno):
        raise ValueError(f"Ya existe un producto con alias '{nuevo.alias_interno}'")

    producto = Producto(
        cliente=cliente,
        alias_interno=nuevo.alias_interno,
        nombre_invoice=nuevo.nombre_invoice,
        hs_code=nuevo.hs_code,
        largo_cm=nuevo.largo_cm,
        ancho_cm=nuevo.ancho_cm,
        alto_cm=nuevo.alto_cm,
        peso_kg=nuevo.peso_kg,
        valor_usd_default=nuevo.valor_usd_default,
        activo=False,  # pendiente de validación
    )

    sh = _abrir_sheet()
    hoja = sh.worksheet("PRODUCTOS_CATALOGO")
    hoja.append_row([
        producto.cliente, producto.alias_interno, producto.nombre_invoice,
        producto.hs_code, producto.largo_cm, producto.ancho_cm,
        producto.alto_cm, producto.peso_kg, producto.valor_usd_default,
        "FALSE",  # ACTIVO
        datetime.now().isoformat(timespec="seconds"),
    ], value_input_option="USER_ENTERED")

    return producto
