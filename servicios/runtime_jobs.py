"""Controles de entorno para automatizaciones que corren fuera del request.

Staging se usa para validar el borde HTTP y datos aislados. No debe heredar
por accidente automatizaciones capaces de consultar couriers, correo o APIs de
tiendas. La ejecución manual y acotada de un job sigue siendo posible desde un
proceso one-shot; este módulo sólo bloquea los arranques automáticos.
"""

from collections.abc import Mapping
import os


def automatic_background_jobs_enabled(
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Devuelve ``False`` exclusivamente para el entorno STAGING.

    No existe un override global deliberadamente: una variable copiada desde
    producción no puede reactivar de golpe todos los workers en staging.
    Producción y desarrollo conservan el comportamiento histórico.
    """

    source = os.environ if environ is None else environ
    environment = str(source.get("ENV", "")).strip().upper()
    return environment != "STAGING"
