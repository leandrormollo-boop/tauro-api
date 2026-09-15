"""Filtros compartidos por la pantalla y su exportación, sin datos de cuenta."""

from datetime import date


def normalizar_filtros_cuenta(q="", desde="", hasta="", *, inicio: date | None = None) -> dict[str, str]:
    texto = str(q or "").strip()
    if len(texto) > 120:
        raise ValueError("La búsqueda admite hasta 120 caracteres.")
    valores = {"q": texto, "desde": "", "hasta": ""}
    for campo, etiqueta, valor in (
        ("desde", "Desde", desde), ("hasta", "Hasta", hasta)
    ):
        valor = str(valor or "").strip()
        if not valor:
            continue
        try:
            fecha = date.fromisoformat(valor)
            if fecha.isoformat() != valor:
                raise ValueError
        except ValueError:
            raise ValueError(f"{etiqueta}: elegí una fecha válida.") from None
        valores[campo] = fecha.isoformat()
    if inicio:
        corte = inicio.isoformat()
        if valores["hasta"] and valores["hasta"] < corte:
            raise ValueError(f"La cuenta muestra movimientos desde el {inicio.strftime('%d/%m/%Y')}.")
        valores["desde"] = max(valores["desde"], corte)
    if valores["desde"] and valores["hasta"] and valores["desde"] > valores["hasta"]:
        raise ValueError("La fecha Desde debe ser anterior o igual a Hasta.")
    return valores


def patron_busqueda_cuenta(texto: str) -> str:
    """ILIKE literal: %, _ y barras ingresadas no amplían la búsqueda."""
    return "%" + texto.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
