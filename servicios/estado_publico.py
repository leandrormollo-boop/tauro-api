# ============================================================
# Página de estado pública — /estado
# ============================================================
# "No le tenemos miedo a que nos midan": el lenguaje corporal de una
# empresa seria. Muestra en vivo si la plataforma, la base y la recepción
# de ventas están operativas, más el historial de los últimos 90 días que
# alimenta el centinela (cada 15 minutos).
#
# HONESTIDAD ANTE TODO: si un chequeo falla, la página lo muestra en rojo.
# Una página de estado que siempre está en verde aunque el sistema esté
# caído es peor que no tenerla — el día que un cliente la mire durante un
# incidente real, la credibilidad no se recupera.
# ============================================================
from __future__ import annotations

from datetime import date, timedelta

DIAS_HISTORIAL = 90


def _chequeos_en_vivo() -> list[dict]:
    """Cada servicio con su estado REAL al momento de cargar la página."""
    checks = []

    # Base de datos: el cimiento de todo.
    db_ok = False
    try:
        from core.database import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        db_ok = True
    except Exception:
        pass
    checks.append({"nombre": "Base de datos", "ok": db_ok,
                   "detalle": "Donde viven tus envíos, saldos y guías"})

    # Recepción de ventas: que un webhook de tienda pueda procesarse ahora.
    ventas_ok = False
    if db_ok:
        try:
            from servicios.integraciones_tienda import parsear_pedido_shopify
            p = parsear_pedido_shopify({
                "id": 0, "name": "#STATUS", "total_price": "1", "currency": "ARS",
                "shipping_address": {"name": "s", "address1": "a", "city": "c",
                                     "province_code": "x", "zip": "1", "country_code": "US"},
                "line_items": [], "shipping_lines": [],
            })
            ventas_ok = bool(p)
        except Exception:
            pass
    checks.append({"nombre": "Recepción de ventas (Shopify / Tiendanube)",
                   "ok": ventas_ok,
                   "detalle": "Tus ventas entran solas como envíos pendientes"})

    # Cotizador: que haya tarifas frescas para responder al instante.
    cotizador_ok = False
    if db_ok:
        try:
            from servicios.tarifas_cache import buscar_tarifas
            cotizador_ok = bool(buscar_tarifas("US", 1.0))
        except Exception:
            pass
    checks.append({"nombre": "Cotizador", "ok": cotizador_ok,
                   "detalle": "Precios internacionales al instante"})

    return checks


def _historial() -> list[dict]:
    """Un punto por día, últimos 90: verde / con incidentes / sin datos."""
    filas = {}
    try:
        from core.database import get_conn
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT dia, checks, fallos FROM salud_historial
                    WHERE dia > CURRENT_DATE - INTERVAL '%s days'
                """ % DIAS_HISTORIAL)
                for r in cur.fetchall():
                    filas[r["dia"]] = (int(r["checks"]), int(r["fallos"]))
    except Exception:
        pass

    hoy = date.today()
    out = []
    for i in range(DIAS_HISTORIAL - 1, -1, -1):
        d = hoy - timedelta(days=i)
        if d in filas:
            checks, fallos = filas[d]
            estado = "ok" if fallos == 0 else ("parcial" if fallos < max(checks, 1) else "caido")
        else:
            estado = "sin_datos"
        out.append({"dia": d.strftime("%d/%m"), "estado": estado})
    return out


def pagina_estado() -> str:
    checks = _chequeos_en_vivo()
    historial = _historial()
    todo_ok = all(c["ok"] for c in checks)

    con_datos = [h for h in historial if h["estado"] != "sin_datos"]
    dias_ok = sum(1 for h in con_datos if h["estado"] == "ok")
    uptime = f"{(dias_ok / len(con_datos) * 100):.1f}%" if con_datos else "—"

    filas_checks = "".join(
        f'<div class="check"><span class="luz {"ok" if c["ok"] else "mal"}"></span>'
        f'<div><b>{c["nombre"]}</b><small>{c["detalle"]}</small></div>'
        f'<span class="tag {"ok" if c["ok"] else "mal"}">'
        f'{"Operativo" if c["ok"] else "Con problemas"}</span></div>'
        for c in checks
    )
    barras = "".join(
        f'<span class="barra {h["estado"]}" title="{h["dia"]}"></span>'
        for h in historial
    )

    return f"""<!DOCTYPE html>
<html lang="es"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Estado del sistema · Tauro Solutions</title>
<meta name="description" content="Estado en vivo de la plataforma de Tauro Solutions: cotizador, recepción de ventas y base de datos, con el historial de los últimos 90 días.">
<link rel="icon" type="image/png" href="/static/img/favicon.png">
<meta http-equiv="refresh" content="120">
<style>
  body {{ margin:0; background:#0c0a14; color:#d7d3e4;
         font-family:'Helvetica Neue',Helvetica,Arial,sans-serif; line-height:1.6; }}
  .wrap {{ max-width:680px; margin:0 auto; padding:56px 24px 80px; }}
  .marca {{ font-size:19px; font-weight:700; letter-spacing:.08em; color:#fff;
           text-decoration:none; display:inline-block; margin-bottom:40px; }}
  .marca span {{ color:#a78bfa; }}
  h1 {{ color:#fff; font-size:28px; margin:0 0 6px; }}
  .resumen {{ display:flex; align-items:center; gap:10px; font-size:17px;
             margin:18px 0 34px; color:{"#3ddc97" if todo_ok else "#ffb454"}; }}
  .punto {{ width:12px; height:12px; border-radius:50%;
           background:{"#3ddc97" if todo_ok else "#ffb454"};
           box-shadow:0 0 12px {"#3ddc9766" if todo_ok else "#ffb45466"}; }}
  .check {{ display:flex; align-items:center; gap:14px; padding:16px;
           background:#131020; border:1px solid #241f38; border-radius:12px;
           margin-bottom:10px; }}
  .check b {{ color:#fff; display:block; }}
  .check small {{ color:#6f6a85; }}
  .check > div {{ flex:1; }}
  .luz {{ width:10px; height:10px; border-radius:50%; flex:none; }}
  .luz.ok {{ background:#3ddc97; box-shadow:0 0 10px #3ddc9766; }}
  .luz.mal {{ background:#ff5470; box-shadow:0 0 10px #ff547066; }}
  .tag {{ font-size:12px; font-weight:600; padding:4px 12px; border-radius:999px; }}
  .tag.ok {{ color:#3ddc97; background:#3ddc9714; }}
  .tag.mal {{ color:#ff5470; background:#ff547014; }}
  h2 {{ color:#fff; font-size:16px; margin:36px 0 4px; }}
  .sub {{ color:#6f6a85; font-size:13px; margin:0 0 14px; }}
  .barras {{ display:flex; gap:2px; }}
  .barra {{ flex:1; height:34px; border-radius:2px; background:#241f38; }}
  .barra.ok {{ background:#3ddc97; opacity:.85; }}
  .barra.parcial {{ background:#ffb454; }}
  .barra.caido {{ background:#ff5470; }}
  .pie {{ margin-top:52px; padding-top:22px; border-top:1px solid #241f38;
         color:#6f6a85; font-size:13px; }}
  a {{ color:#a78bfa; }}
</style></head>
<body><div class="wrap">
  <a class="marca" href="/web">TAURO <span>SOLUTIONS</span></a>
  <h1>Estado del sistema</h1>
  <div class="resumen"><span class="punto"></span>
    {"Todos los sistemas operativos" if todo_ok else "Estamos revisando un problema"}</div>
  {filas_checks}
  <h2>Últimos {DIAS_HISTORIAL} días</h2>
  <p class="sub">Uptime: <b style="color:#fff">{uptime}</b> · un chequeo automático cada 15 minutos</p>
  <div class="barras">{barras}</div>
  <div class="pie">
    Esta página se actualiza sola cada 2 minutos ·
    <a href="/web">taurosolutions.ar</a>
  </div>
</div></body></html>"""
