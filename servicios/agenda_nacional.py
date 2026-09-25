"""Proyección de la agenda al formulario nacional, sin adivinar domicilios."""
from servicios.provincias import normalizar_provincia, normalizar_codigo_postal


def datos_guardados(*, nombre, apellido, calle, numero, piso, depto, pais, estado, cp):
    if pais != "AR" or not any((calle, numero, apellido, piso, depto)):
        return {}, None
    campos = {k: str(v or "").strip() for k, v in dict(
        nombre=nombre, apellido=apellido, calle=calle, numero=numero, piso=piso, depto=depto).items()}
    for key, limit in dict(nombre=30, apellido=30, calle=30, numero=6, piso=6, depto=4).items():
        if len(campos[key]) > limit:
            raise ValueError(f"{key.capitalize()}: máximo {limit} caracteres para envíos nacionales.")
    if not campos["calle"] or not campos["numero"]:
        raise ValueError("Completá la calle y el número por separado.")
    if not campos["numero"].isdigit():
        raise ValueError("El número de la calle debe contener sólo dígitos.")
    if not normalizar_provincia(estado):
        raise ValueError("Elegí una provincia argentina válida.")
    if not normalizar_codigo_postal(cp, estado):
        raise ValueError("Revisá el código postal y la provincia.")
    direccion = f'{campos["calle"]} {campos["numero"]}'
    if campos["piso"]: direccion += f' · Piso {campos["piso"]}'
    if campos["depto"]: direccion += f' · Depto. {campos["depto"]}'
    return campos, direccion


def proyectar(direccion):
    """Sólo AR; campos que no se guardaron separados quedan para completar."""
    if str(direccion.get("pais") or "").upper() != "AR":
        return None
    national = direccion.get("datos_nacionales") or {}
    fields = {key: national.get(key) or "" for key in ("nombre", "apellido", "calle", "numero", "piso", "depto")}
    fields["nombre"] = national.get("nombre") or direccion.get("nombre") or ""
    if direccion["tipo"] == "REMITENTE":
        fields["nombre"] = direccion.get("nombre") or fields["nombre"]
    fields.update(provincia=normalizar_provincia(direccion.get("estado")),
                  localidad=direccion.get("ciudad") or "", cp=direccion.get("cp") or "",
                  email=direccion.get("email") or "", telefono=direccion.get("telefono") or "")
    return {"id": str(direccion["id"]), "label": direccion.get("label") or direccion.get("nombre"),
            "tipo": direccion["tipo"], "direccion": direccion.get("direccion") or "", "fields": fields}
