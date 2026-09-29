"""Confirmación del domicilio cuando la ubicación viene del cotizador."""


def validar_ubicacion_cotizada(form, lado):
    if (form.get(lado + "_referencia") == "1"
            and form.get(lado + "_ubicacion_confirmada") != "1"):
        raise ValueError(
            f"Confirmá la ciudad y el código postal del domicilio de {lado}."
        )
