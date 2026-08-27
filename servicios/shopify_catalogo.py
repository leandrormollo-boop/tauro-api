"""Espejo local de catálogo e inventario Shopify para el portal TAURO.

Shopify es la fuente de verdad. El portal nunca consulta Shopify mientras se
renderiza: lee PostgreSQL y por eso no se vuelve más lento. La carga inicial y
la reconciliación usan GraphQL; los webhooks se encolan de forma durable y se
procesan fuera del request.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from core.database import get_conn
from servicios.catalogo import (
    desactivar_ausentes_shopify,
    desactivar_producto_shopify,
    upsert_producto_importado,
)
from servicios.shopify_app import _graphql, instalacion


# Diez variantes × hasta cincuenta ubicaciones mantiene el costo solicitado
# por debajo del máximo de 1.000 puntos de Shopify. Los catálogos grandes se
# recorren por cursor en segundo plano; nunca se sacrifica stock por velocidad.
_PAGINA_VARIANTES = 10
_MAX_PAGINAS = 1000
_worker_lock = threading.Lock()
_sync_executor = ThreadPoolExecutor(
    max_workers=3,
    thread_name_prefix="shopify-catalog-sync",
)
_sync_en_curso: set[tuple[str, str]] = set()
_sync_en_curso_lock = threading.Lock()


class ShopifyCatalogError(RuntimeError):
    def __init__(self, codigo: str, mensaje: str):
        super().__init__(mensaje)
        self.codigo = codigo


@contextmanager
def _bloqueo_generacion_operativa(
    dominio: str,
    cliente: str,
    install_generation: str,
):
    """Impide que catálogo/stock sobrevivan al purge de otra generación.

    La descarga GraphQL ocurre sin lock. Justo antes de escribir, este lock
    transaccional vuelve a comprobar generación, token, owner y binding. Si
    uninstall/reinstall ganó mientras Shopify respondía, no se persiste nada.
    """
    dominio = (dominio or "").strip().lower()
    cliente = (cliente or "").strip().upper()
    install_generation = str(install_generation or "").strip()
    if not (dominio and cliente and install_generation):
        raise ShopifyCatalogError(
            "GENERACION_OBSOLETA",
            "El evento pertenece a una instalación Shopify anterior.",
        )

    from servicios.integraciones_tienda import (
        OAUTH_SECRET_MARKER,
        _bloquear_dominio_shopify,
    )

    with get_conn() as lock_conn:
        with lock_conn.cursor() as cur:
            _bloquear_dominio_shopify(cur, dominio)
            cur.execute(
                """
                SELECT 1
                  FROM shopify_instalaciones i
                  JOIN tiendas_conectadas t ON LOWER(t.dominio) = LOWER(i.dominio)
                 WHERE LOWER(i.dominio) = %s
                   AND i.install_generation = %s
                   AND NULLIF(BTRIM(i.access_token), '') IS NOT NULL
                   AND UPPER(COALESCE(i.cliente_id, '')) = %s
                   AND t.plataforma = 'shopify'
                   AND t.secreto = %s
                   AND t.activa = TRUE
                   AND UPPER(t.cliente_id) = %s
                 LIMIT 1
                """,
                (
                    dominio,
                    install_generation,
                    cliente,
                    OAUTH_SECRET_MARKER,
                    cliente,
                ),
            )
            if cur.fetchone() is None:
                raise ShopifyCatalogError(
                    "GENERACION_OBSOLETA",
                    "El evento pertenece a una instalación Shopify anterior.",
                )
            yield


def _actualizar_estado_si_actual(
    dominio: str,
    cliente: str,
    install_generation: str,
    estado: str,
    **valores,
) -> bool:
    try:
        with _bloqueo_generacion_operativa(
            dominio, cliente, install_generation,
        ):
            _actualizar_estado(dominio, cliente, estado, **valores)
        return True
    except ShopifyCatalogError as exc:
        if exc.codigo != "GENERACION_OBSOLETA":
            raise
        return False


_VARIANT_FIELDS = """
    id
    title
    sku
    price
    updatedAt
    image { url }
    product {
      id
      title
      status
      updatedAt
      featuredMedia { preview { image { url } } }
    }
"""

_INVENTORY_FIELDS = """
    id
    updatedAt
    tracked
    harmonizedSystemCode
    countryCodeOfOrigin
    measurement { weight { value unit } }
    inventoryLevels(first: 50) {
      nodes {
        updatedAt
        location { id name }
        quantities(names: ["available", "committed", "on_hand", "incoming"]) {
          name
          quantity
        }
      }
      pageInfo { hasNextPage }
    }
"""

_QUERY_VARIANTES = f"""
    query TauroCatalogo($first: Int!, $after: String, $query: String) {{
      shop {{ currencyCode }}
      productVariants(first: $first, after: $after, query: $query) {{
        nodes {{
          {_VARIANT_FIELDS}
          inventoryItem {{ {_INVENTORY_FIELDS} }}
        }}
        pageInfo {{ hasNextPage endCursor }}
      }}
    }}
"""

_QUERY_INVENTORY_ITEM = f"""
    query TauroInventoryItem($id: ID!) {{
      shop {{ currencyCode }}
      inventoryItem(id: $id) {{
        {_INVENTORY_FIELDS}
        variant {{ {_VARIANT_FIELDS} }}
      }}
    }}
"""


def _gid(tipo: str, valor) -> str:
    crudo = str(valor or "").strip()
    if not crudo:
        return ""
    if crudo.startswith("gid://shopify/"):
        return crudo
    if not crudo.isdigit():
        return ""
    return f"gid://shopify/{tipo}/{crudo}"


def _peso_kg(weight: Optional[dict]) -> float:
    if not isinstance(weight, dict):
        return 0.0
    try:
        valor = float(weight.get("value") or 0)
    except (TypeError, ValueError):
        return 0.0
    unidad = str(weight.get("unit") or "KILOGRAMS").upper()
    factores = {
        "KILOGRAMS": 1.0,
        "GRAMS": 0.001,
        "POUNDS": 0.45359237,
        "OUNCES": 0.028349523125,
    }
    return round(valor * factores.get(unidad, 1.0), 6)


def _timestamp_fuente_mas_nuevo(*valores) -> Optional[str]:
    """Normaliza el reloj de Shopify usado para impedir stock regresivo."""
    candidatos: list[datetime] = []
    for valor in valores:
        if not valor:
            continue
        try:
            fecha = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
            if fecha.tzinfo is None:
                fecha = fecha.replace(tzinfo=timezone.utc)
            candidatos.append(fecha.astimezone(timezone.utc))
        except (TypeError, ValueError):
            continue
    return max(candidatos).isoformat() if candidatos else None


def _cantidades(niveles: Optional[dict]) -> tuple[list[dict], dict[str, Optional[int]]]:
    if (((niveles or {}).get("pageInfo") or {}).get("hasNextPage")):
        raise ShopifyCatalogError(
            "DEMASIADAS_UBICACIONES",
            "La tienda tiene más de 50 ubicaciones; no se publicará un stock parcial.",
        )
    ubicaciones: list[dict] = []
    totales: dict[str, int] = {
        "available": 0,
        "committed": 0,
        "on_hand": 0,
        "incoming": 0,
    }
    for nivel in ((niveles or {}).get("nodes") or []):
        if not isinstance(nivel, dict):
            continue
        cantidades = {
            str(item.get("name") or ""): int(item.get("quantity") or 0)
            for item in (nivel.get("quantities") or []) if isinstance(item, dict)
        }
        for nombre in totales:
            totales[nombre] += cantidades.get(nombre, 0)
        location = nivel.get("location") or {}
        ubicaciones.append({
            "external_location_id": str(location.get("id") or ""),
            "ubicacion_nombre": str(location.get("name") or "Ubicación Shopify"),
            "disponible": cantidades.get("available", 0),
            "comprometido": cantidades.get("committed", 0),
            "fisico": cantidades.get("on_hand", 0),
            "entrante": cantidades.get("incoming", 0),
            "source_updated_at": nivel.get("updatedAt"),
        })
    return ubicaciones, {
        "stock_disponible": totales["available"],
        "stock_comprometido": totales["committed"],
        "stock_fisico": totales["on_hand"],
        "stock_entrante": totales["incoming"],
    }


def _mapear_variante(node: dict, moneda: str, inventory_item: Optional[dict] = None) -> dict:
    producto = node.get("product") or {}
    inventario = inventory_item or node.get("inventoryItem") or {}
    tracked = bool(inventario.get("tracked"))
    ubicaciones, totales = _cantidades(inventario.get("inventoryLevels"))
    if not tracked:
        totales = {clave: None for clave in totales}

    imagen = ((node.get("image") or {}).get("url") or
              ((((producto.get("featuredMedia") or {}).get("preview") or {})
                .get("image") or {}).get("url")))
    titulo = str(producto.get("title") or "").strip()
    variante = str(node.get("title") or "").strip()
    nombre = titulo
    if variante and variante.lower() != "default title":
        nombre = f"{titulo} · {variante}".strip(" ·")
    medicion = (inventario.get("measurement") or {}).get("weight")
    source_updated_at = _timestamp_fuente_mas_nuevo(
        node.get("updatedAt"),
        producto.get("updatedAt"),
        inventario.get("updatedAt"),
        *(
            nivel.get("updatedAt")
            for nivel in ((inventario.get("inventoryLevels") or {}).get("nodes") or [])
            if isinstance(nivel, dict)
        ),
    )

    return {
        "sku": str(node.get("sku") or "").strip(),
        "nombre": nombre or "Producto Shopify",
        "titulo_tienda": titulo,
        "variante_tienda": "" if variante.lower() == "default title" else variante,
        "peso_kg": _peso_kg(medicion),
        "imagen_src": str(imagen or ""),
        "external_product_id": str(producto.get("id") or ""),
        "external_variant_id": str(node.get("id") or ""),
        "external_inventory_item_id": str(inventario.get("id") or ""),
        "precio_tienda": node.get("price"),
        "moneda_tienda": moneda,
        "hs_code_tienda": str(inventario.get("harmonizedSystemCode") or ""),
        "pais_origen_tienda": str(inventario.get("countryCodeOfOrigin") or ""),
        "stock_controlado": tracked,
        "source_updated_at": source_updated_at,
        "ubicaciones": ubicaciones,
        **totales,
    }


def traer_variantes(dominio: str, token: str, query: Optional[str] = None) -> list[dict]:
    """Descarga todas las variantes; un fallo nunca se confunde con catálogo vacío."""
    filas: list[dict] = []
    cursor = None
    for _pagina in range(_MAX_PAGINAS):
        data = _graphql(dominio, token, _QUERY_VARIANTES, {
            "first": _PAGINA_VARIANTES,
            "after": cursor,
            "query": query,
        })
        if data is None:
            raise ShopifyCatalogError("SHOPIFY_NO_RESPONDE", "Shopify no respondió al leer el catálogo.")
        conexion = data.get("productVariants") or {}
        moneda = str((data.get("shop") or {}).get("currencyCode") or "")
        for node in (conexion.get("nodes") or []):
            if isinstance(node, dict) and node.get("id"):
                filas.append(_mapear_variante(node, moneda))
        page_info = conexion.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            return filas
        cursor = page_info.get("endCursor")
        if not cursor:
            raise ShopifyCatalogError("PAGINACION_INVALIDA", "Shopify no devolvió el cursor siguiente.")
    raise ShopifyCatalogError(
        "CATALOGO_DEMASIADO_GRANDE",
        f"El catálogo superó {_PAGINA_VARIANTES * _MAX_PAGINAS} variantes.",
    )


# Alias conservado para scripts existentes.
traer_productos = traer_variantes


def _actualizar_estado(dominio: str, cliente: str, estado: str, **valores) -> None:
    permitidos = {
        "ultimo_error_codigo", "ultimo_error", "productos_total", "variantes_total",
        "creados", "actualizados", "desactivados",
    }
    datos = {k: v for k, v in valores.items() if k in permitidos}
    columnas = ["dominio", "cliente_id", "estado", "ultimo_intento_at", *datos]
    params = [dominio, cliente, estado, datetime.now(timezone.utc), *datos.values()]
    updates = ["cliente_id=EXCLUDED.cliente_id", "estado=EXCLUDED.estado",
               "ultimo_intento_at=EXCLUDED.ultimo_intento_at", "updated_at=NOW()"]
    updates.extend(f"{campo}=EXCLUDED.{campo}" for campo in datos)
    if estado == "COMPLETADO":
        columnas.append("ultima_sincronizacion_at")
        params.append(datetime.now(timezone.utc))
        updates.append("ultima_sincronizacion_at=EXCLUDED.ultima_sincronizacion_at")
    marcadores = ",".join(["%s"] * len(columnas))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO shopify_sync_estado ({','.join(columnas)})
                VALUES ({marcadores})
                ON CONFLICT (dominio) DO UPDATE SET {','.join(updates)}
                """,
                params,
            )


def _guardar_variantes(
    dominio: str,
    cliente: str,
    filas: list[dict],
    run_id: str,
    source_observed_at=None,
) -> tuple[int, int]:
    creados = actualizados = 0
    for fila in filas:
        estado = upsert_producto_importado(
            cliente,
            fila.get("sku") or "",
            fila.get("nombre") or "Producto Shopify",
            fila.get("peso_kg") or 0,
            fila.get("imagen_src") or None,
            tienda_dominio=dominio,
            external_product_id=fila.get("external_product_id") or "",
            external_variant_id=fila.get("external_variant_id") or "",
            external_inventory_item_id=fila.get("external_inventory_item_id") or "",
            variante_tienda=fila.get("variante_tienda") or "",
            precio_tienda=fila.get("precio_tienda"),
            moneda_tienda=fila.get("moneda_tienda") or "",
            hs_code_tienda=fila.get("hs_code_tienda") or "",
            pais_origen_tienda=fila.get("pais_origen_tienda") or "",
            stock_controlado=bool(fila.get("stock_controlado")),
            stock_disponible=fila.get("stock_disponible"),
            stock_comprometido=fila.get("stock_comprometido"),
            stock_fisico=fila.get("stock_fisico"),
            stock_entrante=fila.get("stock_entrante"),
            source_updated_at=fila.get("source_updated_at"),
            source_observed_at=source_observed_at,
            sync_run_id=run_id,
            ubicaciones=fila.get("ubicaciones") or [],
            inventario_completo=True,
        )
        if estado == "creado":
            creados += 1
        else:
            actualizados += 1
    return creados, actualizados


def importar_catalogo(dominio: str, cliente_id: str) -> dict:
    dominio = (dominio or "").strip().lower()
    cliente = (cliente_id or "").strip().upper()
    if not dominio or not cliente:
        return {"ok": False, "codigo": "DATOS_INCOMPLETOS", "error": "Faltan dominio o cliente."}

    inst = instalacion(dominio)
    token = (inst or {}).get("access_token")
    generation = str((inst or {}).get("install_generation") or "").strip()
    owner = str((inst or {}).get("cliente_id") or "").strip().upper()
    scopes = {s.strip() for s in str((inst or {}).get("scopes") or "").split(",") if s.strip()}
    faltantes = {"read_products", "read_inventory"} - scopes
    if not token or not generation or owner != cliente or faltantes:
        if generation and owner == cliente:
            _actualizar_estado_si_actual(
                dominio, cliente, generation, "REAUTORIZAR",
                ultimo_error_codigo="REAUTORIZACION_REQUERIDA",
                ultimo_error="La tienda debe aprobar catálogo e inventario.",
            )
        return {
            "ok": False,
            "codigo": "REAUTORIZACION_REQUERIDA",
            "error": "Reconectá Shopify una vez para autorizar productos e inventario.",
            "reautorizar_url": f"/shopify/install?shop={dominio}&reautorizar=1",
        }

    run_id = uuid.uuid4().hex
    sincronizacion_iniciada_at = datetime.now(timezone.utc)
    try:
        with _bloqueo_generacion_operativa(dominio, cliente, generation):
            _actualizar_estado(
                dominio, cliente, "SINCRONIZANDO",
                ultimo_error_codigo=None, ultimo_error=None,
            )
        filas = traer_variantes(dominio, token)
        with _bloqueo_generacion_operativa(dominio, cliente, generation):
            creados, actualizados = _guardar_variantes(
                dominio,
                cliente,
                filas,
                run_id,
                source_observed_at=sincronizacion_iniciada_at,
            )
            desactivados = desactivar_ausentes_shopify(
                cliente, dominio, run_id, sincronizacion_iniciada_at,
            )
            productos_total = len({f.get("external_product_id") for f in filas})
            _actualizar_estado(
                dominio, cliente, "COMPLETADO",
                ultimo_error_codigo=None, ultimo_error=None,
                productos_total=productos_total, variantes_total=len(filas),
                creados=creados, actualizados=actualizados, desactivados=desactivados,
            )
        print(f"[shopify_sync] {dominio} → {cliente}: {len(filas)} variantes, "
              f"{creados} nuevas, {actualizados} actualizadas, {desactivados} archivadas")
        return {
            "ok": True, "creados": creados, "actualizados": actualizados,
            "desactivados": desactivados, "total": len(filas),
        }
    except ShopifyCatalogError as exc:
        if exc.codigo != "GENERACION_OBSOLETA":
            _actualizar_estado_si_actual(
                dominio, cliente, generation, "ERROR",
                ultimo_error_codigo=exc.codigo,
                ultimo_error="No pudimos completar la sincronización.",
            )
        return {"ok": False, "codigo": exc.codigo, "error": str(exc)}
    except Exception as exc:
        print(f"[shopify_sync] {dominio}: {type(exc).__name__}")
        _actualizar_estado_si_actual(
            dominio, cliente, generation, "ERROR",
            ultimo_error_codigo="ERROR_INTERNO",
            ultimo_error="No pudimos completar la sincronización.",
        )
        return {"ok": False, "codigo": "ERROR_INTERNO",
                "error": "No pudimos completar la sincronización."}


def _dominio_instalado_de(cliente_id: str) -> Optional[str]:
    cliente = (cliente_id or "").strip().upper()
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT dominio FROM shopify_instalaciones
                    WHERE cliente_id=%s AND access_token IS NOT NULL AND access_token<>''
                    ORDER BY instalada_en DESC LIMIT 1
                    """,
                    (cliente,),
                )
                row = cur.fetchone()
        return str((row or {}).get("dominio") or "") or None
    except Exception as exc:
        print(f"[shopify_sync] no pude buscar instalación de {cliente}: {type(exc).__name__}")
        return None


def sincronizar_para_cliente(cliente_id: str) -> dict:
    dominio = _dominio_instalado_de(cliente_id)
    if not dominio:
        return {"ok": False, "codigo": "SIN_TIENDA",
                "error": "Todavía no tenés una tienda Shopify conectada por la app oficial."}
    return importar_catalogo(dominio, cliente_id)


def solicitar_sincronizacion_cliente(cliente_id: str) -> dict:
    """Botón del portal: responde enseguida y deja el trabajo en segundo plano."""
    dominio = _dominio_instalado_de(cliente_id)
    if not dominio:
        return {"ok": False, "codigo": "SIN_TIENDA",
                "error": "Todavía no tenés una tienda Shopify conectada por la app oficial."}
    inst = instalacion(dominio) or {}
    scopes = {s.strip() for s in str(inst.get("scopes") or "").split(",") if s.strip()}
    if {"read_products", "read_inventory"} - scopes:
        return {
            "ok": False, "codigo": "REAUTORIZACION_REQUERIDA",
            "error": "Autorizá una vez el catálogo y el inventario de Shopify.",
            "reautorizar_url": f"/shopify/install?shop={dominio}&reautorizar=1",
        }
    iniciada = lanzar_sincronizacion(dominio, cliente_id)
    return {
        "ok": True,
        "iniciada": iniciada,
        "en_curso": not iniciada,
        "dominio": dominio,
    }


def lanzar_sincronizacion(dominio: str, cliente_id: str) -> bool:
    """Encola una sola sincronización por tienda y limita el uso del pool SQL."""
    clave = (
        (dominio or "").strip().lower(),
        (cliente_id or "").strip().upper(),
    )
    with _sync_en_curso_lock:
        if clave in _sync_en_curso:
            return False
        _sync_en_curso.add(clave)

    def _run():
        try:
            importar_catalogo(clave[0], clave[1])
        except Exception as exc:
            print(f"[shopify_sync] hilo inicial falló: {type(exc).__name__}")
        finally:
            with _sync_en_curso_lock:
                _sync_en_curso.discard(clave)

    try:
        _sync_executor.submit(_run)
    except Exception:
        with _sync_en_curso_lock:
            _sync_en_curso.discard(clave)
        raise
    return True


def sincronizar_producto(
    dominio: str,
    cliente: str,
    product_id,
    install_generation_verificada: str = "",
    triggered_at: Optional[str] = None,
) -> dict:
    dominio = (dominio or "").strip().lower()
    cliente = (cliente or "").strip().upper()
    generation = str(install_generation_verificada or "").strip()
    inst = instalacion(dominio) or {}
    token = inst.get("access_token")
    if (
        not generation
        or str(inst.get("install_generation") or "").strip() != generation
        or str(inst.get("cliente_id") or "").strip().upper() != cliente
    ):
        raise ShopifyCatalogError(
            "GENERACION_OBSOLETA",
            "El evento pertenece a una instalación Shopify anterior.",
        )
    gid = _gid("Product", product_id)
    if not token or not gid:
        raise ShopifyCatalogError("EVENTO_INVALIDO", "Producto Shopify inválido.")
    with _bloqueo_generacion_operativa(dominio, cliente, generation):
        pass
    legacy_id = gid.rsplit("/", 1)[-1]
    filas = traer_variantes(dominio, token, query=f"product_id:{legacy_id}")
    with _bloqueo_generacion_operativa(dominio, cliente, generation):
        if not filas:
            n = desactivar_producto_shopify(
                cliente, dominio, gid, triggered_at,
            )
            return {"ok": True, "desactivados": n}
        creados, actualizados = _guardar_variantes(
            dominio,
            cliente,
            filas,
            uuid.uuid4().hex,
            source_observed_at=(triggered_at or datetime.now(timezone.utc)),
        )
    return {"ok": True, "creados": creados, "actualizados": actualizados}


def sincronizar_inventory_item(
    dominio: str,
    cliente: str,
    inventory_item_id,
    install_generation_verificada: str = "",
    triggered_at: Optional[str] = None,
) -> dict:
    dominio = (dominio or "").strip().lower()
    cliente = (cliente or "").strip().upper()
    generation = str(install_generation_verificada or "").strip()
    inst = instalacion(dominio) or {}
    token = inst.get("access_token")
    if (
        not generation
        or str(inst.get("install_generation") or "").strip() != generation
        or str(inst.get("cliente_id") or "").strip().upper() != cliente
    ):
        raise ShopifyCatalogError(
            "GENERACION_OBSOLETA",
            "El evento pertenece a una instalación Shopify anterior.",
        )
    gid = _gid("InventoryItem", inventory_item_id)
    if not token or not gid:
        raise ShopifyCatalogError("EVENTO_INVALIDO", "Inventario Shopify inválido.")
    with _bloqueo_generacion_operativa(dominio, cliente, generation):
        pass
    data = _graphql(dominio, token, _QUERY_INVENTORY_ITEM, {"id": gid})
    if data is None:
        raise ShopifyCatalogError("SHOPIFY_NO_RESPONDE", "Shopify no respondió al leer inventario.")
    inventory = data.get("inventoryItem") or {}
    moneda = str((data.get("shop") or {}).get("currencyCode") or "")
    variante = inventory.get("variant") or {}
    filas = ([_mapear_variante(variante, moneda, inventory)]
             if isinstance(variante, dict) and variante.get("id") else [])
    with _bloqueo_generacion_operativa(dominio, cliente, generation):
        creados, actualizados = _guardar_variantes(
            dominio,
            cliente,
            filas,
            uuid.uuid4().hex,
            source_observed_at=(triggered_at or datetime.now(timezone.utc)),
        )
    return {"ok": True, "creados": creados, "actualizados": actualizados}


def encolar_evento(
    webhook_id: str,
    dominio: str,
    topic: str,
    payload: dict,
    triggered_at: Optional[str] = None,
    install_generation: str = "",
) -> bool:
    """Encola sólo para la generación activa; False significa duplicado exacto."""
    webhook_id = (webhook_id or "").strip()
    dominio = (dominio or "").strip().lower()
    topic = (topic or "").strip().lower()
    generation = str(install_generation or "").strip()
    if not webhook_id or not dominio or not topic or not generation:
        raise ShopifyCatalogError(
            "EVENTO_INVALIDO", "El webhook Shopify está incompleto.",
        )

    from servicios.integraciones_tienda import (
        OAUTH_SECRET_MARKER,
        _bloquear_dominio_shopify,
    )

    with get_conn() as conn:
        with conn.cursor() as cur:
            _bloquear_dominio_shopify(cur, dominio)
            cur.execute(
                """
                INSERT INTO shopify_webhook_eventos
                    (webhook_id, dominio, topic, triggered_at, payload,
                     install_generation)
                SELECT %s,%s,%s,%s,%s::jsonb,%s
                  FROM shopify_instalaciones i
                  JOIN tiendas_conectadas t
                    ON LOWER(t.dominio) = LOWER(i.dominio)
                 WHERE LOWER(i.dominio) = %s
                   AND i.install_generation = %s
                   AND NULLIF(BTRIM(i.access_token), '') IS NOT NULL
                   AND NULLIF(BTRIM(i.cliente_id), '') IS NOT NULL
                   AND t.plataforma = 'shopify'
                   AND t.secreto = %s
                   AND t.activa = TRUE
                   AND UPPER(t.cliente_id) = UPPER(i.cliente_id)
                ON CONFLICT (webhook_id) DO NOTHING
                RETURNING webhook_id
                """,
                (
                    webhook_id, dominio, topic, triggered_at,
                    json.dumps(payload, ensure_ascii=False), generation,
                    dominio, generation, OAUTH_SECRET_MARKER,
                ),
            )
            if cur.fetchone() is not None:
                return True
            cur.execute(
                """
                SELECT dominio, topic, install_generation
                  FROM shopify_webhook_eventos
                 WHERE webhook_id = %s
                """,
                (webhook_id,),
            )
            existente = cur.fetchone()
            if existente:
                coincide = (
                    str(existente.get("dominio") or "").lower() == dominio
                    and str(existente.get("topic") or "").lower() == topic
                    and str(existente.get("install_generation") or "") == generation
                )
                if coincide:
                    return False
                raise ShopifyCatalogError(
                    "WEBHOOK_ID_REUTILIZADO",
                    "El identificador del webhook no coincide con su entrega original.",
                )
    raise ShopifyCatalogError(
        "GENERACION_OBSOLETA",
        "El evento pertenece a una instalación Shopify anterior.",
    )


def _tomar_evento() -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH elegido AS (
                    SELECT webhook_id FROM shopify_webhook_eventos
                    WHERE estado='PENDIENTE'
                       OR (estado='PROCESANDO' AND started_at < NOW()-INTERVAL '10 minutes')
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE shopify_webhook_eventos e
                SET estado='PROCESANDO', started_at=NOW(), intentos=intentos+1
                FROM elegido
                WHERE e.webhook_id=elegido.webhook_id
                RETURNING e.*
                """
            )
            row = cur.fetchone()
    return dict(row) if row else None


def _resolver_cliente(dominio: str, install_generation: str) -> str:
    inst = instalacion(dominio) or {}
    if str(inst.get("install_generation") or "") != str(install_generation or ""):
        raise ShopifyCatalogError(
            "GENERACION_OBSOLETA",
            "El evento pertenece a una instalación Shopify anterior.",
        )
    return str(inst.get("cliente_id") or "").strip().upper()


def _procesar_evento(evento: dict) -> None:
    dominio = str(evento.get("dominio") or "").strip().lower()
    topic = str(evento.get("topic") or "").strip().lower()
    payload = evento.get("payload") or {}
    triggered_at = evento.get("triggered_at")
    generation = str(evento.get("install_generation") or "").strip()
    if not generation:
        raise ShopifyCatalogError(
            "GENERACION_OBSOLETA",
            "El evento no identifica una instalación Shopify activa.",
        )
    cliente = _resolver_cliente(dominio, generation)
    if not cliente:
        raise ShopifyCatalogError("TIENDA_SIN_VINCULAR", "La tienda todavía no está vinculada.")

    if topic in ("products/create", "products/update"):
        sincronizar_producto(dominio, cliente,
                             payload.get("admin_graphql_api_id") or payload.get("id"),
                             generation, triggered_at)
    elif topic == "products/delete":
        gid = _gid("Product", payload.get("admin_graphql_api_id") or payload.get("id"))
        if not gid:
            raise ShopifyCatalogError("EVENTO_INVALIDO", "Producto Shopify inválido.")
        with _bloqueo_generacion_operativa(dominio, cliente, generation):
            desactivar_producto_shopify(
                cliente, dominio, gid, triggered_at,
            )
    elif topic in ("inventory_levels/update", "inventory_items/update"):
        sincronizar_inventory_item(
            dominio, cliente,
            payload.get("inventory_item_id") or payload.get("admin_graphql_api_id") or payload.get("id"),
            generation,
            triggered_at,
        )


def procesar_cola_eventos(limite: int = 20) -> dict:
    procesados = errores = 0
    for _ in range(max(1, min(int(limite), 100))):
        evento = _tomar_evento()
        if not evento:
            break
        try:
            _procesar_evento(evento)
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE shopify_webhook_eventos
                        SET estado='COMPLETADO', processed_at=NOW(), ultimo_error=NULL
                        WHERE webhook_id=%s
                        """,
                        (evento["webhook_id"],),
                    )
            procesados += 1
        except Exception as exc:
            errores += 1
            codigo = (
                exc.codigo if isinstance(exc, ShopifyCatalogError)
                else type(exc).__name__
            )[:80]
            obsoleto = codigo == "GENERACION_OBSOLETA"
            reintentar = not obsoleto and int(evento.get("intentos") or 1) < 5
            estado = (
                "COMPLETADO" if obsoleto
                else ("PENDIENTE" if reintentar else "ERROR")
            )
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE shopify_webhook_eventos
                        SET estado=%s, ultimo_error=%s,
                            processed_at=CASE WHEN %s THEN NULL ELSE NOW() END
                        WHERE webhook_id=%s
                        """,
                        (estado, codigo, reintentar,
                         evento["webhook_id"]),
                    )
            if obsoleto:
                errores -= 1
                procesados += 1
                continue
            print(f"[shopify_sync] webhook falló: {codigo}")
            break
    return {"procesados": procesados, "errores": errores}


def lanzar_procesamiento_eventos() -> None:
    def _run():
        if not _worker_lock.acquire(blocking=False):
            return
        try:
            procesar_cola_eventos()
        except Exception as exc:
            print(f"[shopify_sync] worker falló: {type(exc).__name__}")
        finally:
            _worker_lock.release()

    threading.Thread(target=_run, daemon=True, name="shopify-webhook-worker").start()


def reconciliar_tiendas_pendientes(limite: int = 2) -> dict:
    """Red de seguridad: resincroniza tiendas aunque un webhook se haya perdido."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT i.dominio, i.cliente_id
                    FROM shopify_instalaciones i
                    LEFT JOIN shopify_sync_estado s ON s.dominio=i.dominio
                    WHERE i.cliente_id IS NOT NULL AND i.cliente_id<>''
                      AND (s.ultima_sincronizacion_at IS NULL
                           OR s.ultima_sincronizacion_at < NOW()-INTERVAL '30 minutes')
                      AND (s.estado IS NULL OR s.estado<>'SINCRONIZANDO'
                           OR s.ultimo_intento_at < NOW()-INTERVAL '20 minutes')
                    ORDER BY s.ultima_sincronizacion_at NULLS FIRST
                    LIMIT %s
                    """,
                    (max(1, min(int(limite), 10)),),
                )
                tiendas = [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        print(f"[shopify_sync] no pude listar reconciliaciones: {type(exc).__name__}")
        return {"procesadas": 0, "errores": 1}

    ok = errores = 0
    for tienda in tiendas:
        resultado = importar_catalogo(tienda["dominio"], tienda["cliente_id"])
        if resultado.get("ok"):
            ok += 1
        else:
            errores += 1
    return {"procesadas": ok, "errores": errores}


def limpiar_eventos() -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM shopify_webhook_eventos
                WHERE (estado='COMPLETADO' AND processed_at < NOW()-INTERVAL '7 days')
                   OR (estado='ERROR' AND processed_at < NOW()-INTERVAL '30 days')
                """
            )
            return cur.rowcount
