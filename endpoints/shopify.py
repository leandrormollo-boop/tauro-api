# ============================================================
# Endpoints públicos de la app de Shopify
# ============================================================
#   GET  /shopify/app        → App Home embebida (shell sin PII)
#   GET  /shopify/app/data   → estado/pedidos autenticados por ID token
#   GET  /shopify/install    → arranca la instalación (OAuth)
#   GET  /shopify/callback   → Shopify vuelve acá con el permiso dado
#   POST /shopify/webhook/desinstalada → limpieza al desinstalar
# ============================================================
from __future__ import annotations

import html
import json
import secrets
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from servicios.shopify_app import (
    app_configurada, url_instalacion, validar_hmac_query, dominio_valido,
    canjear_token, guardar_instalacion, registrar_webhooks,
    desinstalar, ShopifyWebhookVerificationError,
    confirmar_shop_redact, confirmar_webhooks_verificados,
    webhooks_requeridos, api_key_publica,
)
from servicios.shopify_embedded import (
    APP_BRIDGE_CDN,
    POLARIS_CDN,
    ShopifyEmbeddedAuthError,
    bearer_token,
    crear_estado_oauth,
    host_embebido_para_shop,
    instalacion_para_session,
    shop_desde_host_embebido,
    url_admin_app,
    validar_session_token,
    verificar_estado_oauth,
)

router = APIRouter(prefix="/shopify", tags=["shopify"])


_PAGINA = """<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{titulo} · Tauro Solutions Ar</title>
<style>
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0c0a14;color:#f4f5f7;font-family:'Helvetica Neue',system-ui,sans-serif;text-align:center;padding:24px}}
.box{{max-width:520px}}
h1{{font-size:34px;margin:0 0 14px;letter-spacing:-.02em}}
p{{color:#b9bfc7;line-height:1.65;margin:0 0 26px;font-size:16px}}
a{{display:inline-block;padding:14px 30px;border-radius:999px;text-decoration:none;font-weight:700;
color:#efe9ff;border:1px solid rgba(167,139,250,.5);
background:linear-gradient(180deg,#35206b,#180c33);box-shadow:0 0 22px rgba(124,92,246,.4)}}
small{{display:block;margin-top:26px;color:#7a828c;font-size:13px}}
</style></head><body><div class="box">
<h1>{titulo}</h1><p>{texto}</p>{boton}
<small>Tauro Solutions · logística internacional</small>
</div></body></html>"""


def _pagina(titulo: str, texto: str, boton: str = "", status: int = 200) -> HTMLResponse:
    # Pantallas de error/compatibilidad fuera de App Home no son embebibles.
    return HTMLResponse(
        _PAGINA.format(titulo=titulo, texto=texto, boton=boton),
        status_code=status,
        headers={
            "Content-Security-Policy": "frame-ancestors 'none';",
            "X-Frame-Options": "DENY",
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
        },
    )


def _app_home_html(api_key: str, nonce: str, reconnect_url: str) -> str:
    """Shell sin datos privados; App Bridge autentica la lectura posterior."""
    api_key = html.escape(api_key, quote=True)
    reconnect_js = json.dumps(reconnect_url)
    return f"""<!doctype html>
<html lang="es"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="shopify-api-key" content="{api_key}">
<title>Tauro Solutions Ar</title>
<style nonce="{nonce}">
  :root {{
    color-scheme:dark;
    --bg:#0c0a14; --panel:#13101e; --panel-2:#1a1628;
    --line:#2a2440; --line-soft:#1f1a30;
    --fg:#f4f5f7; --fg-2:#b9bfc7; --fg-3:#858b95;
    --accent:#a78bfa; --accent-deep:#7c5cf6;
    --accent-glow:rgba(167,139,250,.16);
    --ok:#2ec27e; --ok-glow:rgba(46,194,126,.13);
    --warn:#f5b800; --warn-glow:rgba(245,184,0,.12);
    --error:#ff6868; --error-glow:rgba(255,77,77,.13);
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; min-height:100vh; background:var(--bg); color:var(--fg);
          font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
          -webkit-font-smoothing:antialiased; }}
  button,a {{ font:inherit; }}
  button:focus-visible,a:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}
  .appbar {{ display:flex; align-items:center; justify-content:space-between; gap:16px;
             min-height:66px; padding:12px 26px; border-bottom:1px solid var(--line);
             background:var(--panel); }}
  .brand {{ display:flex; align-items:center; gap:12px; min-width:0; }}
  .brand-mark {{ display:grid; place-items:center; width:38px; height:38px;
                 border:1px solid var(--line); border-radius:12px; background:var(--bg);
                 color:var(--fg); font-size:9px; font-weight:750; letter-spacing:.08em; }}
  .brand h1 {{ margin:0; font-size:18px; line-height:1.2; letter-spacing:-.015em; }}
  .brand p {{ margin:3px 0 0; color:var(--fg-3); font-size:12px; }}
  .connection {{ display:inline-flex; align-items:center; gap:8px; padding:7px 11px;
                 border:1px solid var(--line); border-radius:999px; color:var(--fg-2);
                 background:var(--panel-2); font-size:11px; font-weight:650;
                 white-space:nowrap; }}
  .connection.ok {{ color:var(--ok); border-color:rgba(46,194,126,.3);
                    background:var(--ok-glow); }}
  .connection.warn {{ color:var(--warn); border-color:rgba(245,184,0,.3);
                      background:var(--warn-glow); }}
  .dot {{ width:8px; height:8px; flex:0 0 8px; border-radius:50%; background:currentColor; }}
  .tabs {{ display:flex; gap:4px; padding:0 26px; border-bottom:1px solid var(--line);
           background:var(--panel); overflow-x:auto; }}
  .tab {{ position:relative; min-height:46px; padding:0 13px; border:0;
          background:transparent; color:var(--fg-3); font-weight:650; cursor:pointer; }}
  .tab[aria-selected="true"] {{ color:var(--fg); }}
  .tab[aria-selected="true"]::after {{ content:""; position:absolute; left:13px;
          right:13px; bottom:-1px; height:2px; border-radius:2px; background:var(--accent); }}
  main {{ max-width:1040px; margin:0 auto; padding:26px; }}
  [role="tabpanel"][hidden] {{ display:none; }}
  .page-head {{ display:flex; align-items:flex-end; justify-content:space-between;
                gap:18px; margin-bottom:20px; }}
  .eyebrow {{ margin-bottom:6px; color:var(--accent); font-size:10px; font-weight:750;
              letter-spacing:.12em; text-transform:uppercase; }}
  .page-head h2 {{ margin:0; font-size:27px; line-height:1.2; letter-spacing:-.03em; }}
  .page-head p {{ margin:6px 0 0; color:var(--fg-2); }}
  .button {{ display:inline-flex; align-items:center; justify-content:center; gap:7px;
             min-height:40px; padding:9px 15px; border:1px solid var(--line);
             border-radius:999px; background:var(--panel-2); color:var(--fg);
             font-weight:700; cursor:pointer; text-decoration:none; white-space:nowrap; }}
  .button.primary {{ border-color:#8b70ef; color:#fff; background:#6d45d8; }}
  .button.hidden,.hidden {{ display:none!important; }}
  .action-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr));
                  gap:14px; margin-bottom:18px; }}
  .action {{ display:grid; grid-template-columns:auto 1fr auto; gap:12px; align-items:center;
             min-width:0; padding:17px; border:1px solid var(--line); border-radius:16px;
             background:var(--panel); color:var(--fg); text-align:left; cursor:pointer; }}
  .action:not(button) {{ cursor:default; }}
  .action.primary {{ border-color:rgba(167,139,250,.38);
                     background:linear-gradient(135deg,var(--accent-glow),transparent 68%),var(--panel); }}
  .action[disabled] {{ cursor:default; opacity:1; }}
  .action-icon {{ display:grid; place-items:center; width:36px; height:36px;
                  border-radius:11px; background:var(--panel-2); color:var(--accent);
                  font-size:12px; font-weight:800; }}
  .action.ok .action-icon {{ color:var(--ok); background:var(--ok-glow); }}
  .action strong {{ display:block; margin-bottom:2px; font-size:13px; }}
  .action small {{ display:block; color:var(--fg-3); font-size:11px; }}
  .action-count {{ font-size:23px; font-weight:750; font-variant-numeric:tabular-nums; }}
  .home-grid {{ display:grid; grid-template-columns:minmax(0,1fr) minmax(250px,.48fr);
                gap:16px; align-items:start; }}
  .card {{ min-width:0; padding:20px; border:1px solid var(--line); border-radius:16px;
           background:var(--panel); box-shadow:0 10px 30px rgba(0,0,0,.08); }}
  .card-head {{ display:flex; align-items:flex-start; justify-content:space-between;
                gap:12px; margin-bottom:14px; }}
  .card h3 {{ margin:0 0 4px; font-size:16px; }}
  .card p {{ margin:0; color:var(--fg-2); }}
  .text-button {{ padding:4px 0; border:0; background:transparent; color:var(--accent);
                  font-size:12px; font-weight:700; cursor:pointer; white-space:nowrap; }}
  .health {{ display:flex; align-items:center; gap:11px; margin:14px 0;
             padding:12px; border:1px solid rgba(46,194,126,.25); border-radius:12px;
             background:var(--ok-glow); }}
  .health.warn {{ border-color:rgba(245,184,0,.25); background:var(--warn-glow); }}
  .health .health-dot {{ display:grid; place-items:center; width:32px; height:32px;
                         flex:0 0 32px; border-radius:10px; background:var(--panel);
                         color:var(--ok); font-weight:800; }}
  .health.warn .health-dot {{ color:var(--warn); }}
  .health strong {{ display:block; font-size:12px; }}
  .health small {{ display:block; margin-top:2px; color:var(--fg-2); font-size:11px; }}
  .flow {{ display:grid; grid-template-columns:24px 1fr; gap:9px; align-items:start;
           padding:10px 0; border-top:1px solid var(--line-soft); }}
  .step {{ display:grid; place-items:center; width:22px; height:22px; border:1px solid var(--line);
           border-radius:50%; background:var(--panel-2); color:var(--accent); font-size:10px; }}
  .flow strong {{ display:block; margin-bottom:2px; font-size:11px; }}
  .flow span:last-child {{ color:var(--fg-3); font-size:11px; }}
  .table-wrap {{ overflow-x:auto; }}
  table {{ width:100%; min-width:570px; border-collapse:collapse; }}
  th,td {{ padding:13px 9px; border-top:1px solid var(--line-soft); text-align:left;
           vertical-align:middle; }}
  th {{ color:var(--fg-3); font-size:10px; letter-spacing:.05em; text-transform:uppercase; }}
  td {{ font-size:12px; }}
  td:first-child {{ font-weight:750; }}
  .mono {{ font-variant-numeric:tabular-nums; }}
  .status-tag {{ display:inline-flex; align-items:center; gap:5px; padding:4px 8px;
                 border-radius:999px; font-size:10px; font-weight:700; white-space:nowrap; }}
  .status-tag.accent {{ color:var(--accent); background:var(--accent-glow); }}
  .status-tag.warn {{ color:var(--warn); background:var(--warn-glow); }}
  .status-tag.ok {{ color:var(--ok); background:var(--ok-glow); }}
  .status-tag.error {{ color:var(--error); background:var(--error-glow); }}
  .status-tag.muted {{ color:var(--fg-3); background:var(--panel-2); }}
  .empty {{ padding:30px 12px; color:var(--fg-3); text-align:center; }}
  .shipment-list {{ display:grid; gap:10px; }}
  .shipment {{ display:grid; grid-template-columns:44px minmax(0,1fr) auto; gap:13px;
               align-items:center; padding:15px; border:1px solid var(--line);
               border-radius:14px; background:var(--panel); }}
  .shipment-mark {{ display:grid; place-items:center; width:44px; height:44px;
                    border-radius:12px; background:var(--panel-2); color:var(--accent);
                    font-size:11px; font-weight:800; }}
  .shipment strong {{ display:block; font-size:12px; }}
  .shipment small {{ display:block; margin-top:4px; color:var(--fg-3); }}
  .notice {{ margin-top:14px; padding:13px 15px; border:1px solid var(--line);
             border-radius:12px; background:var(--panel-2); color:var(--fg-2); font-size:12px; }}
  #message {{ min-height:18px; }}
  @media (max-width:760px) {{
    .appbar {{ padding:11px 16px; }} .brand p {{ display:none; }}
    .connection {{ padding:7px 9px; font-size:10px; }}
    .tabs {{ padding:0 12px; }} main {{ padding:20px 14px; }}
    .page-head {{ align-items:flex-start; flex-direction:column; }}
    .page-head h2 {{ font-size:23px; }} .page-head .button {{ width:100%; }}
    .action-grid,.home-grid {{ grid-template-columns:1fr; }}
    .shipment {{ grid-template-columns:40px minmax(0,1fr); }}
    .shipment .button {{ grid-column:1/-1; width:100%; }}
  }}
</style>
<script nonce="{nonce}" src="{APP_BRIDGE_CDN}"></script>
<script nonce="{nonce}" src="{POLARIS_CDN}"></script>
<script nonce="{nonce}">
  const reconnectUrl = {reconnect_js};
  const byId = (id) => document.getElementById(id);
  const text = (id, value) => {{ byId(id).textContent = String(value ?? ""); }};

  const STATUS = {{
    PENDIENTE: ["Pendiente de preparar", "warn"],
    CONVERTIDO: ["Envío creado", "accent"],
    CANCELADO: ["Cancelado", "muted"],
    DESCARTADO: ["Descartado", "muted"],
  }};

  function normalizedStatus(value) {{
    return String(value || "").trim().toUpperCase();
  }}

  function statusPresentation(value) {{
    return STATUS[normalizedStatus(value)] || [String(value || "Por confirmar"), "muted"];
  }}

  function formattedTotal(order) {{
    if (!order.total) return "—";
    const amount = Number(order.total);
    const value = Number.isFinite(amount)
      ? amount.toLocaleString("es-AR", {{maximumFractionDigits:2}})
      : String(order.total);
    return `${{order.currency || ""}} ${{value}}`.trim();
  }}

  function formattedDate(value) {{
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("es-AR", {{day:"2-digit", month:"short"}}).format(date);
  }}

  function statusTag(value) {{
    const [label, tone] = statusPresentation(value);
    const tag = document.createElement("span");
    tag.className = `status-tag ${{tone}}`;
    tag.textContent = label;
    return tag;
  }}

  function tableRow(order) {{
    const row = document.createElement("tr");
    const number = document.createElement("td");
    number.textContent = order.number || "—";
    const status = document.createElement("td");
    status.appendChild(statusTag(order.status));
    const total = document.createElement("td");
    total.className = "mono";
    total.textContent = formattedTotal(order);
    const received = document.createElement("td");
    received.textContent = formattedDate(order.created_at);
    row.append(number, status, total, received);
    return row;
  }}

  function selectTab(tab) {{
    for (const candidate of document.querySelectorAll('[role="tab"]')) {{
      candidate.setAttribute("aria-selected", String(candidate === tab));
      candidate.tabIndex = candidate === tab ? 0 : -1;
    }}
    for (const panel of document.querySelectorAll('[role="tabpanel"]')) {{
      panel.hidden = panel.id !== tab.getAttribute("aria-controls");
    }}
    tab.focus({{preventScroll:true}});
  }}

  function showReconnect(message) {{
    selectTab(byId("home-tab"));
    text("message", message);
    byId("connection").className = "connection warn";
    text("connection-label", "Requiere conexión");
    byId("health").className = "health warn";
    text("health-symbol", "!");
    text("health-title", "No pudimos validar la conexión");
    text("health-detail", message);
    byId("reconnect").classList.remove("hidden");
  }}

  function render(data) {{
    byId("connection").className = data.linked ? "connection ok" : "connection warn";
    text("connection-label", data.linked ? "Shopify conectado" : "Falta vincular TAURO");
    text("state", data.linked ? "Instalada y vinculada" : "Instalada · falta vincular a TAURO");
    text("shop", data.shop);
    text("message", data.linked
      ? "Los pedidos se sincronizan con el portal TAURO."
      : "Iniciá sesión en el portal TAURO para completar el vínculo.");
    byId("health").className = data.linked ? "health" : "health warn";
    text("health-symbol", data.linked ? "✓" : "!");
    text("health-title", data.linked ? "Sincronización activa" : "Vínculo pendiente");
    text("health-detail", data.linked ? "La tienda está comunicándose con TAURO." : "Completá el vínculo desde el portal.");

    const orders = Array.isArray(data.orders) ? data.orders : [];
    const summary = data.summary && typeof data.summary === "object" ? data.summary : {{}};
    const pending = orders.filter((order) => normalizedStatus(order.status) === "PENDIENTE");
    const shipments = orders.filter((order) => normalizedStatus(order.status) === "CONVERTIDO");
    const totalOrders = Number.isFinite(Number(summary.orders)) ? Number(summary.orders) : orders.length;
    const totalPending = Number.isFinite(Number(summary.pending)) ? Number(summary.pending) : pending.length;
    const totalShipments = Number.isFinite(Number(summary.shipments)) ? Number(summary.shipments) : shipments.length;
    text("pending-count", totalPending);
    text("shipment-count", totalShipments);
    text("order-count", totalOrders);
    text("orders-scope", `Mostrando ${{orders.length}} de ${{totalOrders}}, del más reciente al más antiguo.`);

    const ordersBody = byId("orders");
    const recentBody = byId("recent-orders");
    ordersBody.replaceChildren(...orders.map(tableRow));
    recentBody.replaceChildren(...orders.slice(0, 3).map(tableRow));
    byId("orders-empty").classList.toggle("hidden", orders.length > 0);
    byId("recent-empty").classList.toggle("hidden", orders.length > 0);
    byId("orders-table").classList.toggle("hidden", orders.length === 0);
    byId("recent-table").classList.toggle("hidden", orders.length === 0);

    const shipmentList = byId("shipment-list");
    shipmentList.replaceChildren();
    for (const order of shipments) {{
      const item = document.createElement("article");
      item.className = "shipment";
      const mark = document.createElement("span");
      mark.className = "shipment-mark";
      mark.textContent = "TS";
      mark.setAttribute("aria-hidden", "true");
      const copy = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = `Pedido ${{order.number || "—"}}`;
      const detail = document.createElement("small");
      detail.textContent = `${{formattedTotal(order)}} · envío creado en TAURO`;
      copy.append(title, detail);
      const link = document.createElement("a");
      link.className = "button";
      link.href = "https://taurosolutions.ar/portal/envios";
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "Ver en portal";
      link.setAttribute("aria-label", `Ver pedido ${{order.number || "sin número"}} en el portal TAURO`);
      item.append(mark, copy, link);
      shipmentList.appendChild(item);
    }}
    byId("shipments-empty").classList.toggle("hidden", shipments.length > 0);
    text("shipments-empty-title", totalShipments > 0
      ? "Tus envíos anteriores están disponibles en TAURO."
      : "Todavía no hay envíos creados desde Shopify.");
    text("shipments-empty-detail", totalShipments > 0
      ? "Esta vista enumera solamente los envíos incluidos entre los últimos pedidos sincronizados."
      : "Cuando prepares uno en TAURO, aparecerá en este resumen.");
  }}

  async function load() {{
    try {{
      const token = await shopify.idToken();
      const response = await fetch("/shopify/app/data", {{
        cache: "no-store",
        headers: {{Authorization: `Bearer ${{token}}`}},
      }});
      const data = await response.json();
      if (response.status === 409 && data.code === "REAUTHORIZE") {{
        showReconnect("La instalación necesita autorización nuevamente.");
        window.open(reconnectUrl, "_top");
        return;
      }}
      if (!response.ok) throw new Error(data.detail || "No pudimos validar la sesión.");
      render(data);
    }} catch (_error) {{
      showReconnect("No pudimos validar esta sesión de Shopify. Volvé a abrir la app.");
    }}
  }}

  window.addEventListener("DOMContentLoaded", () => {{
    byId("reconnect").addEventListener("click", () => window.open(reconnectUrl, "_top"));
    for (const tab of document.querySelectorAll('[role="tab"]')) {{
      tab.addEventListener("click", () => selectTab(tab));
      tab.addEventListener("keydown", (event) => {{
        if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
        event.preventDefault();
        const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
        const direction = event.key === 'ArrowRight' ? 1 : -1;
        selectTab(tabs[(tabs.indexOf(tab) + direction + tabs.length) % tabs.length]);
      }});
    }}
    byId("open-orders").addEventListener("click", () => selectTab(byId("orders-tab")));
    byId("open-shipments").addEventListener("click", () => selectTab(byId("shipments-tab")));
    byId("recent-all").addEventListener("click", () => selectTab(byId("orders-tab")));
    load();
  }});
</script>
</head><body>
<ui-title-bar title="Tauro Solutions Ar"></ui-title-bar>
<header class="appbar">
  <div class="brand">
    <span class="brand-mark" aria-hidden="true">TAURO</span>
    <div><h1>Tauro Solutions Ar</h1><p>Logística conectada a tu tienda</p></div>
  </div>
  <div id="connection" class="connection" aria-live="polite">
    <span class="dot" aria-hidden="true"></span><span id="connection-label">Validando conexión…</span>
  </div>
</header>
<nav class="tabs" role="tablist" aria-label="Secciones de Tauro Solutions Ar">
  <button id="home-tab" class="tab" type="button" role="tab" aria-selected="true" aria-controls="home-panel">Inicio</button>
  <button id="orders-tab" class="tab" type="button" role="tab" tabindex="-1" aria-selected="false" aria-controls="orders-panel">Pedidos</button>
  <button id="shipments-tab" class="tab" type="button" role="tab" tabindex="-1" aria-selected="false" aria-controls="shipments-panel">Envíos</button>
</nav>
<main>
  <section id="home-panel" role="tabpanel" aria-labelledby="home-tab">
    <div class="page-head">
      <div><div class="eyebrow">Hoy</div><h2>Tu operación, de un vistazo</h2><p>Mostramos solamente lo que necesita atención.</p></div>
      <a class="button" href="https://taurosolutions.ar/portal/tienda" target="_blank" rel="noopener noreferrer">Abrir portal completo</a>
    </div>
    <div class="action-grid" aria-live="polite">
      <button id="open-orders" class="action primary" type="button">
        <span class="action-icon" aria-hidden="true">P</span>
        <span><strong>Pedidos por preparar</strong><small>Requieren elegir envío</small></span>
        <span id="pending-count" class="action-count">—</span>
      </button>
      <button id="open-shipments" class="action" type="button">
        <span class="action-icon" aria-hidden="true">E</span>
        <span><strong>Envíos creados</strong><small>Total sincronizado con TAURO</small></span>
        <span id="shipment-count" class="action-count">—</span>
      </button>
      <div class="action ok">
        <span class="action-icon" aria-hidden="true">✓</span>
        <span><strong>Pedidos sincronizados</strong><small>Total histórico de la tienda</small></span>
        <span id="order-count" class="action-count">—</span>
      </div>
    </div>
    <div class="home-grid">
      <section class="card" aria-labelledby="recent-title">
        <div class="card-head"><div><h3 id="recent-title">Últimos pedidos</h3><p>Sin datos personales del comprador.</p></div><button id="recent-all" class="text-button" type="button">Ver todos</button></div>
        <p id="recent-empty" class="empty">No hay pedidos sincronizados para mostrar.</p>
        <div id="recent-table" class="table-wrap hidden">
          <table aria-label="Últimos pedidos de Shopify"><thead><tr><th>Pedido</th><th>Estado</th><th>Total</th><th>Recibido</th></tr></thead><tbody id="recent-orders"></tbody></table>
        </div>
      </section>
      <aside class="card" aria-labelledby="state-title">
        <div class="card-head"><div><h3 id="state-title">Integración</h3><p id="message">Cargando estado seguro…</p></div></div>
        <div id="health" class="health" role="status" aria-live="polite">
          <span id="health-symbol" class="health-dot" aria-hidden="true">…</span>
          <span><strong id="health-title">Validando sesión…</strong><small id="health-detail">Un momento.</small></span>
        </div>
        <div class="flow"><span class="step">1</span><span><strong>Venta recibida</strong>El pedido entra automáticamente.</span></div>
        <div class="flow"><span class="step">2</span><span><strong>Envío preparado</strong>La operación continúa en el portal TAURO.</span></div>
        <div class="flow"><span class="step">3</span><span><strong>Tracking actualizado</strong>Shopify informa al comprador.</span></div>
        <p id="shop" class="mono"></p>
        <p id="state" class="hidden">Validando sesión…</p>
        <button id="reconnect" class="button primary hidden" type="button">Reconectar con Shopify</button>
      </aside>
    </div>
  </section>

  <section id="orders-panel" role="tabpanel" aria-labelledby="orders-tab" hidden>
    <div class="page-head">
      <div><div class="eyebrow">Pedidos</div><h2>Pedidos de Shopify</h2><p>Resumen operativo sin datos personales del comprador.</p></div>
      <a class="button" href="https://taurosolutions.ar/portal/tienda" target="_blank" rel="noopener noreferrer">Gestionar en TAURO</a>
    </div>
    <section class="card" aria-labelledby="orders-title">
      <div class="card-head"><div><h3 id="orders-title">Últimos pedidos sincronizados</h3><p id="orders-scope">Ordenados desde el más reciente.</p></div></div>
      <p id="orders-empty" class="empty">No hay pedidos sincronizados para mostrar.</p>
      <div id="orders-table" class="table-wrap hidden">
        <table aria-label="Pedidos Shopify"><thead><tr><th>Pedido</th><th>Estado</th><th>Total</th><th>Recibido</th></tr></thead><tbody id="orders"></tbody></table>
      </div>
    </section>
  </section>

  <section id="shipments-panel" role="tabpanel" aria-labelledby="shipments-tab" hidden>
    <div class="page-head">
      <div><div class="eyebrow">Envíos</div><h2>Envíos recientes iniciados desde Shopify</h2><p>Una vista breve; el historial completo permanece en TAURO.</p></div>
      <a class="button" href="https://taurosolutions.ar/portal/envios" target="_blank" rel="noopener noreferrer">Ver historial completo</a>
    </div>
    <div id="shipment-list" class="shipment-list"></div>
    <section id="shipments-empty" class="card">
      <div class="empty"><strong id="shipments-empty-title">Buscando envíos recientes…</strong><br><span id="shipments-empty-detail">Un momento.</span></div>
    </section>
    <div class="notice">La guía, el tracking detallado y las incidencias se administran en el portal TAURO. Shopify recibe las actualizaciones correspondientes sin exponer información interna.</div>
  </section>
</main>
</body></html>"""


@router.get("/app", response_class=HTMLResponse)
def app_home(request: Request, shop: str = "", host: str = ""):
    """App Home embebida: shell público, datos sólo detrás de ID token."""
    if not app_configurada():
        return _pagina(
            "App en preparación",
            "TAURO todavía no tiene configuradas sus credenciales Shopify.",
            status=503,
        )
    shop = str(shop or "").strip().lower()
    shop_host = shop_desde_host_embebido(host)
    if not dominio_valido(shop):
        shop = shop_host
    elif shop_host and shop_host != shop:
        return _pagina("Contexto inválido", "El host no corresponde a la tienda.", status=400)
    if not dominio_valido(shop):
        return _pagina("Contexto inválido", "Shopify no identificó una tienda válida.", status=400)
    try:
        host = host_embebido_para_shop(shop, host)
    except ValueError:
        return _pagina("Contexto inválido", "El host no corresponde a la tienda.", status=400)
    request_state = getattr(request, "state", None)
    nonce = str(getattr(request_state, "csp_nonce", "") or secrets.token_urlsafe(16))
    reconnect_url = f"/shopify/install?shop={quote(shop)}&host={quote(host)}"
    response = HTMLResponse(_app_home_html(api_key_publica(), nonce, reconnect_url))
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        f"script-src 'nonce-{nonce}' https://cdn.shopify.com; "
        f"style-src 'nonce-{nonce}'; "
        "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
        "base-uri 'none'; form-action 'none'; "
        f"frame-ancestors https://{shop} https://admin.shopify.com;"
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    return response


@router.get("/app/data")
def app_home_data(request: Request):
    """Estado y pedidos mínimos, seleccionados exclusivamente por el JWT."""
    payload = None
    try:
        payload = validar_session_token(
            bearer_token(request.headers.get("authorization", "")),
        )
        inst = instalacion_para_session(payload)
    except ShopifyEmbeddedAuthError as exc:
        if exc.code == "REAUTHORIZE" and payload:
            shop = str(payload["shop"])
            return JSONResponse(
                {"detail": "La instalación requiere autorización.", "code": exc.code},
                status_code=409,
            )
        return JSONResponse(
            {"detail": "Sesión Shopify inválida.", "code": exc.code},
            status_code=401,
            headers={"X-Shopify-Retry-Invalid-Session-Request": "1"},
        )

    shop = str(payload["shop"])
    cliente_id = str(inst.get("cliente_id") or "").strip().upper()
    pedidos = []
    resumen = {"orders": 0, "pending": 0, "shipments": 0}
    if cliente_id:
        try:
            from servicios.integraciones_tienda import listar_resumen_pedidos_shopify_embebido

            filas = listar_resumen_pedidos_shopify_embebido(shop, cliente_id)
            if filas:
                resumen = {
                    "orders": int(filas[0].get("total_count") or 0),
                    "pending": int(filas[0].get("pending_count") or 0),
                    "shipments": int(filas[0].get("shipment_count") or 0),
                }
            for fila in filas:
                creado = fila.get("created_at")
                pedidos.append({
                    "number": str(fila.get("numero") or fila.get("pedido_externo_id") or ""),
                    "status": str(fila.get("estado") or ""),
                    "total": str(fila["valor_total"]) if fila.get("valor_total") is not None else "",
                    "currency": str(fila.get("moneda") or ""),
                    "created_at": creado.isoformat() if hasattr(creado, "isoformat") else str(creado or ""),
                })
        except Exception as exc:
            print(f"[shopify] app home no pudo leer pedidos: {type(exc).__name__}")
            return JSONResponse(
                {"detail": "No pudimos cargar los pedidos."}, status_code=503,
            )
    return {
        "shop": shop,
        "installed": True,
        "linked": bool(cliente_id),
        "summary": resumen,
        "orders": pedidos,
    }


@router.get("/install", response_class=HTMLResponse)
def install(request: Request, shop: str = "", host: str = ""):
    """
    Inicia OAuth en navegación principal y devuelve a App Home embebida.
    """
    if not app_configurada():
        return _pagina(
            "App en preparación",
            "La app de TAURO para Shopify todavía no está disponible. "
            "Cuando quede habilitada, vas a poder instalarla únicamente desde Shopify Apps.",
            '<a href="https://admin.shopify.com">Volver a Shopify</a>',
        )
    shop = (shop or "").strip().lower()
    host = str(host or request.query_params.get("host", "") or "").strip()

    # `host` sólo puede seleccionar la misma tienda codificada por Shopify.
    if not dominio_valido(shop):
        shop = shop_desde_host_embebido(host) or shop

    if not dominio_valido(shop):
        return _pagina(
            "Abrí TAURO desde tu tienda",
            "Shopify no incluyó una tienda válida. Volvé al panel "
            "de Shopify y abrí Tauro Solutions Ar desde Apps.",
            '<a href="https://admin.shopify.com">Volver a Shopify</a>',
        )

    # Reintento explícito después de una autorización incompleta. No hacemos
    # llamadas al Admin API desde cada apertura del panel: este link inicia
    # nuevamente OAuth y sólo Shopify puede completar el callback firmado.
    if request.query_params.get("reautorizar") == "1":
        return _redirect_oauth(shop, host)

    # Si la tienda YA instaló la app, Shopify abre esta misma URL cada vez
    # que el comerciante hace click en TAURO desde su admin. Mandarlo de
    # nuevo al OAuth sería absurdo: le mostramos su panel.
    from servicios.shopify_app import instalacion
    try:
        inst = instalacion(shop)
    except Exception as e:
        print(f"[shopify] no pude leer la instalación: {type(e).__name__}")
        inst = None

    if inst and not inst.get("webhooks_ready"):
        print("[shopify] instalación pendiente de verificar webhooks → nuevo consentimiento")
        return _redirect_oauth(shop, host)

    if inst and inst.get("access_token"):
        # Una fila histórica puede tener todos los scopes correctos y aun así
        # pertenecer a la app vieja. Con la app pública activa, esa tienda debe
        # pasar una vez por SU OAuth; mostrar el panel legado acá impediría la
        # migración para siempre.
        app_instalada = str(inst.get("app_client_id") or "").strip()
        if app_instalada != api_key_publica():
            print("[shopify] instalación histórica → nuevo consentimiento")
            return _redirect_oauth(shop, host)
        if not inst.get("token_rotativo"):
            print("[shopify] instalación pública sin refresh → nuevo consentimiento")
            return _redirect_oauth(shop, host)
        # PERMISOS DESACTUALIZADOS: el token guardado sirve sólo para los
        # scopes con los que se autorizó. Si desde entonces la app pide más
        # (pasó al arreglar los de fulfillment orders), el token viejo sigue
        # funcionando para lo de antes y falla EN SILENCIO para lo nuevo —
        # exactamente lo que hacía que no se pudiera marcar "enviado".
        # La única salida es volver a pasar por el consentimiento.
        from servicios.shopify_app import SCOPES
        guardados = {s.strip() for s in (inst.get("scopes") or "").split(",") if s.strip()}
        pedidos = {s.strip() for s in SCOPES.split(",") if s.strip()}
        faltantes = pedidos - guardados
        if faltantes:
            print(f"[shopify] permisos desactualizados: {len(faltantes)} faltante(s)")
            return _redirect_oauth(shop, host)
        return RedirectResponse(
            url=url_admin_app(shop),
            status_code=303,
            headers={"Cache-Control": "private, no-store", "Pragma": "no-cache"},
        )

    return _redirect_oauth(shop, host)


def _redirect_oauth(shop: str, host: str = "") -> RedirectResponse:
    """
    Manda al consentimiento de Shopify guardando el `state` en una cookie
    corta. Hasta ahora el state se generaba, viajaba... y nadie lo comparaba
    a la vuelta — o sea, teatro. El callback ahora exige que coincida
    (anti-CSRF del flujo OAuth, y Shopify lo revisa para el App Store).
    """
    state = crear_estado_oauth(shop, host)
    url = url_instalacion(shop, state)
    resp = RedirectResponse(url=url, status_code=303)
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["Pragma"] = "no-cache"
    resp.set_cookie(
        key="shopify_state", value=state,
        max_age=600, httponly=True, secure=True,
        # Lax: la vuelta de Shopify es una navegación top-level GET, la
        # cookie viaja. Strict la dejaría afuera y rompería el flujo.
        samesite="lax",
    )
    return resp


@router.get("/callback", response_class=HTMLResponse)
def callback(request: Request):
    """Shopify vuelve con el permiso: canjeamos el token y dejamos todo listo."""
    params = dict(request.query_params)
    shop = (params.get("shop") or "").strip().lower()
    code = params.get("code") or ""

    if not app_configurada():
        return _pagina("App en preparación", "Todavía no está habilitada la instalación automática.", status=503)
    if not dominio_valido(shop) or not code:
        return _pagina("Instalación inválida", "Faltan datos de la tienda. Probá instalar de nuevo.", status=400)
    if not validar_hmac_query(params):
        # Firma inválida = alguien intentó hacerse pasar por Shopify.
        return _pagina("No pudimos verificar la instalación",
                       "La firma de Shopify no coincide. Por seguridad no continuamos.", status=401)

    # `state` vincula este callback al navegador que inició OAuth. El HMAC sólo
    # prueba que Shopify firmó la respuesta y no reemplaza este control CSRF.
    # Cookie o query ausentes, expirados o distintos se rechazan siempre.
    state_cookie = request.cookies.get("shopify_state") or ""
    state_query = str(params.get("state") or "")
    state_verificado = bool(
        state_cookie
        and state_query
        and secrets.compare_digest(state_query, state_cookie)
    )
    if state_verificado:
        try:
            verificar_estado_oauth(state_query, shop)
        except (ValueError, ShopifyEmbeddedAuthError):
            state_verificado = False
    if not state_verificado:
        respuesta = _pagina("La instalación expiró",
                            "Por seguridad, empezá de nuevo desde el link de instalación.",
                            f'<a href="/shopify/install?shop={shop}">Reintentar instalación</a>',
                            status=403)
        respuesta.delete_cookie("shopify_state")
        return respuesta

    data = canjear_token(shop, code)
    if not data or not data.get("access_token"):
        return _pagina("No pudimos conectar", "Shopify no nos dio el permiso. Probá de nuevo.", status=502)

    from servicios.shopify_app import SCOPES
    scopes_requeridos = {scope.strip() for scope in SCOPES.split(",") if scope.strip()}
    scopes_otorgados = {
        scope.strip() for scope in str(data.get("scope") or "").split(",")
        if scope.strip()
    }
    faltantes = scopes_requeridos - scopes_otorgados
    if faltantes:
        print(f"[shopify] OAuth incompleto: {len(faltantes)} scope(s) faltante(s)")
        respuesta = _pagina(
            "Faltan permisos de Shopify",
            "Shopify no confirmó todos los permisos necesarios para recibir ventas y "
            "devolver el tracking. Volvé a instalar la app desde tu tienda.",
            f'<a href="/shopify/install?shop={shop}">Reintentar instalación</a>',
            status=502,
        )
        respuesta.delete_cookie("shopify_state")
        return respuesta

    # Límite temporal de esta generación. Se persiste ANTES de tocar las
    # suscripciones: mientras Shopify registra webhooks ya no existe una
    # ventana en la que una entrega nueva pueda caer sobre el owner anterior.
    oauth_activada_desde = datetime.now(timezone.utc)

    try:
        generation = guardar_instalacion(
            shop,
            data["access_token"],
            data.get("scope", ""),
            oauth_activada_desde,
            refresh_token=data.get("refresh_token", ""),
            expires_in=data.get("expires_in"),
            refresh_token_expires_in=data.get("refresh_token_expires_in"),
        )
    except Exception as exc:
        print(f"[shopify] no pude persistir la generación: {type(exc).__name__}")
        respuesta = _pagina(
            "No pudimos terminar la vinculación",
            "Shopify autorizó la app, pero TAURO no pudo guardar la conexión "
            "de forma segura. Reintentá en unos minutos.",
            status=503,
        )
        respuesta.delete_cookie("shopify_state")
        return respuesta

    try:
        topics = registrar_webhooks(shop, data["access_token"])
    except ShopifyWebhookVerificationError:
        respuesta = _pagina(
            "No pudimos verificar Shopify",
            "La conexión no respondió a tiempo. La tienda quedó aislada de "
            "cualquier cuenta anterior; reintentá en unos minutos.",
            f'<a href="/shopify/install?shop={shop}&reautorizar=1">Reintentar conexión</a>',
            status=503,
        )
        respuesta.delete_cookie("shopify_state")
        return respuesta
    except Exception as exc:
        print(f"[shopify] error registrando webhooks: {type(exc).__name__}")
        respuesta = _pagina(
            "No pudimos verificar Shopify",
            "La conexión no respondió de forma segura. La tienda quedó pendiente "
            "y no puede operar; reintentá en unos minutos.",
            f'<a href="/shopify/install?shop={shop}&reautorizar=1">Reintentar conexión</a>',
            status=503,
        )
        respuesta.delete_cookie("shopify_state")
        return respuesta
    esperados = webhooks_requeridos()
    if set(topics) != esperados:
        respuesta = _pagina(
            "Conexión incompleta",
            "Shopify autorizó la app, pero no confirmó todos los avisos automáticos "
            "de ventas, productos e inventario. La tienda quedó pendiente y no puede "
            "operar. Reintentá en "
            "unos minutos.",
            f'<a href="/shopify/install?shop={shop}&reautorizar=1">Reintentar instalación</a>',
            status=503,
        )
        respuesta.delete_cookie("shopify_state")
        return respuesta

    try:
        lista = list(topics)
        if not confirmar_webhooks_verificados(shop, generation, lista):
            raise RuntimeError("generación OAuth reemplazada")
    except Exception as exc:
        print(f"[shopify] no pude habilitar webhooks: {type(exc).__name__}")
        respuesta = _pagina(
            "Conexión pendiente",
            "Los avisos de Shopify quedaron verificados, pero TAURO no pudo "
            "habilitar esta generación de forma segura. Reintentá en unos minutos.",
            f'<a href="/shopify/install?shop={shop}&reautorizar=1">Reintentar conexión</a>',
            status=503,
        )
        respuesta.delete_cookie("shopify_state")
        return respuesta

    # OAuth prueba control de la tienda, no identidad dentro de TAURO. Incluso
    # si el navegador trae una sesión TAURO, la instalación nace ownerless y
    # sólo se vincula después mediante la verificación del mail del comercio.
    print(
        f"[shopify] generación habilitada · {len(topics)} webhook(s) · ownerless"
    )

    # El destino se deriva del `shop` cubierto por el HMAC de Shopify y por el
    # state firmado; no se acepta una URL de retorno aportada por el navegador.
    respuesta = RedirectResponse(
        url=url_admin_app(shop),
        status_code=303,
        headers={
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
        },
    )
    respuesta.delete_cookie("shopify_state")
    return respuesta


@router.post("/tarifas")
async def tarifas(request: Request):
    """
    RETIRADO (28/07). La app ya no cotiza el envío dentro del checkout.

    El precio que ve el comprador lo define el comerciante con sus propias
    tarifas de Shopify; TAURO sólo toma la venta cuando entra y la carga como
    solicitud en el portal. El endpoint sigue vivo, y devolviendo lista vacía
    a propósito, por dos motivos:

      1. Si a alguna tienda le quedó un carrier service colgado, Shopify va a
         seguir llamando acá. Contestar 200 con [] hace que simplemente no
         aparezca nuestra opción; devolver un error o 404 le mete un timeout
         al checkout del comprador.
      2. Deja registro en el log si alguna tienda todavía nos está llamando,
         que es la señal de que hay un carrier service sin dar de baja.

    Toda la maquinaria de cotización (cascada de resiliencia, cache de
    tarifas, peso facturable) sigue en pie: la usa el portal y el cotizador
    público de la web, que son los que sí cotizan.
    """
    print("[shopify] endpoint de tarifas legado invocado; respuesta vacía")
    return {"rates": []}


# ── Webhooks de privacidad (obligatorios para el App Store) ─────
# Shopify exige estos tres endpoints a toda app publicada, y los prueba
# durante la revisión. Todos verifican la firma con el API secret de la
# app: sin eso, cualquiera podría pedir o borrar datos de un comercio.

def _firma_valida_app(cuerpo: bytes, firma: str) -> bool:
    from servicios.shopify_app import firma_valida_webhook_app
    return firma_valida_webhook_app(cuerpo, firma)


def _topic_exacto(request: Request, esperado: str) -> bool:
    return request.headers.get("x-shopify-topic", "").strip().lower() == esperado


def _payload_gdpr_y_dominio(
    cuerpo: bytes,
    dominio_header: str,
    contrato: str = "",
) -> tuple[dict, str]:
    """Parsea el cuerpo firmado y ata la operacion al dominio que contiene.

    El HMAC cubre el body, no los headers. Confiar solamente en
    ``X-Shopify-Shop-Domain`` permitiria cambiar ese header al reproducir un
    webhook valido y operar sobre otra tienda. Por eso ambos dominios deben
    coincidir antes de leer, anonimizar o borrar datos.
    """
    import json as _json

    datos = _json.loads(cuerpo.decode("utf-8"))
    if not isinstance(datos, dict):
        raise ValueError("payload GDPR invalido")
    dominio_body = str(datos.get("shop_domain") or "").strip().lower()
    dominio_header = str(dominio_header or "").strip().lower()
    if (
        not dominio_valido(dominio_body)
        or dominio_header != dominio_body
    ):
        raise ValueError("dominio GDPR inconsistente")
    if not str(datos.get("shop_id") or "").strip():
        raise ValueError("shop_id faltante")

    customer = datos.get("customer")
    tiene_customer = isinstance(customer, dict) and bool(
        str(customer.get("id") or "").strip()
    )
    if contrato == "customers/data_request":
        if (
            not tiene_customer
            or "orders_requested" not in datos
            or not isinstance(datos.get("orders_requested"), list)
            or "orders_to_redact" in datos
        ):
            raise ValueError("contrato customers/data_request inválido")
    elif contrato == "customers/redact":
        if (
            not tiene_customer
            or "orders_to_redact" not in datos
            or not isinstance(datos.get("orders_to_redact"), list)
            or "orders_requested" in datos
        ):
            raise ValueError("contrato customers/redact inválido")
    elif contrato == "shop/redact":
        if any(
            clave in datos
            for clave in ("customer", "orders_requested", "orders_to_redact")
        ):
            raise ValueError("contrato shop/redact inválido")
    return datos, dominio_body


@router.post("/webhook/customers/data_request")
async def gdpr_data_request(request: Request):
    """
    Un comprador pidió ver los datos que la tienda tiene sobre él.
    TAURO guarda datos de compradores sólo dentro del pedido que el
    comercio nos manda, así que dejamos constancia y el comercio responde
    con lo que le mostramos en su portal.
    """
    if not _topic_exacto(request, "customers/data_request"):
        return JSONResponse({"ok": False}, status_code=400)
    cuerpo = await request.body()
    if not _firma_valida_app(cuerpo, request.headers.get("x-shopify-hmac-sha256", "")):
        return JSONResponse({"ok": False}, status_code=401)

    try:
        datos, dominio = _payload_gdpr_y_dominio(
            cuerpo, request.headers.get("x-shopify-shop-domain", ""),
            "customers/data_request",
        )
        from servicios.shopify_gdpr import (
            SolicitudGDPRInvalida, encolar_data_request,
            normalizar_payload_data_request, resolver_order_ids_por_email,
        )
        referencia = normalizar_payload_data_request(datos)
        # Defensa en profundidad: el normalizador no decide ownership.
        if referencia["dominio"] != dominio:
            raise SolicitudGDPRInvalida("dominio inconsistente")
    except (ValueError, UnicodeError):
        return JSONResponse({"ok": False}, status_code=400)

    try:
        email_memoria = referencia.pop("customer_email_memoria", "")
        if not referencia["orders_requested"] and email_memoria:
            referencia["orders_requested"] = resolver_order_ids_por_email(
                dominio, email_memoria,
            )
        resultado = encolar_data_request(**referencia)
    except Exception as e:
        # Nunca incluir el body ni PII en el log. Un fallo de persistencia
        # devuelve 503 para que Shopify conserve y reintente la obligacion.
        print(f"[gdpr] no pude persistir data_request: {type(e).__name__}")
        return JSONResponse({"ok": False}, status_code=503)
    print("[gdpr] data_request persistido")
    return {"ok": True}


@router.post("/webhook/customers/redact")
async def gdpr_customer_redact(request: Request):
    """
    Un comprador pidió que borren sus datos. Anonimizamos su información
    personal en los pedidos que tengamos de esa tienda, conservando el
    registro comercial (montos y fechas) que hace falta por contabilidad.
    """
    if not _topic_exacto(request, "customers/redact"):
        return JSONResponse({"ok": False}, status_code=400)
    cuerpo = await request.body()
    if not _firma_valida_app(cuerpo, request.headers.get("x-shopify-hmac-sha256", "")):
        return JSONResponse({"ok": False}, status_code=401)

    try:
        datos, dominio = _payload_gdpr_y_dominio(
            cuerpo, request.headers.get("x-shopify-shop-domain", ""),
            "customers/redact",
        )
        from servicios.shopify_gdpr import normalizar_order_ids
        ids = normalizar_order_ids(
            datos.get("orders_to_redact"), campo="orders_to_redact",
        )
    except (ValueError, UnicodeError):
        return JSONResponse({"ok": False}, status_code=400)

    try:
        if not ids:
            from servicios.shopify_gdpr import resolver_order_ids_por_email
            customer = datos.get("customer") if isinstance(datos.get("customer"), dict) else {}
            # El email firmado vive solamente durante esta consulta. No se
            # persiste ni se incluye en logs, auditoria o respuesta.
            ids = resolver_order_ids_por_email(
                dominio, str(customer.get("email") or ""),
            )
        from servicios.integraciones_tienda import anonimizar_pedidos
        n = anonimizar_pedidos(dominio, ids)
        print(f"[gdpr] redact de comprador: {n} registro(s) sanitizados")
    except Exception as e:
        print(f"[gdpr] error procesando customers/redact: {type(e).__name__}")
        # Un 200 confirmaría un borrado que no ocurrió y Shopify dejaría de
        # reintentarlo. El 503 conserva la obligación pendiente sin filtrar el
        # detalle interno.
        return JSONResponse({"ok": False}, status_code=503)
    return {"ok": True}


@router.post("/webhook/shop/redact")
async def gdpr_shop_redact(request: Request):
    """
    Pasaron 48 hs desde que un comercio desinstaló la app: Shopify pide
    que borremos todo lo suyo.
    """
    if not _topic_exacto(request, "shop/redact"):
        return JSONResponse({"ok": False}, status_code=400)
    cuerpo = await request.body()
    firma = request.headers.get("x-shopify-hmac-sha256", "")
    from servicios.shopify_app import (
        cliente_app_instalada, cliente_app_para_webhook,
    )
    app_client_id = cliente_app_para_webhook(cuerpo, firma)
    if not app_client_id:
        return JSONResponse({"ok": False}, status_code=401)

    try:
        datos, dominio = _payload_gdpr_y_dominio(
            cuerpo, request.headers.get("x-shopify-shop-domain", ""),
            "shop/redact",
        )
        shop_id = str(datos.get("shop_id") or "").strip()
        if not shop_id:
            raise ValueError("shop_id faltante")
    except (ValueError, UnicodeError):
        return JSONResponse({"ok": False}, status_code=400)

    try:
        if confirmar_shop_redact(dominio, shop_id, app_client_id):
            # app/uninstalled ya purgó de forma atómica. El ACK se ata a esa
            # generación histórica y una reinstalación queda fuera del UPDATE.
            print("[gdpr] shop_redact confirmado sobre tombstone purgado")
            return {"ok": True}

        app_actual = cliente_app_instalada(dominio)
        if app_actual:
            # Sin tombstone no existe evidencia que permita atribuir este
            # evento a la instalación activa (menos aún si es la misma app).
            # Fail closed: no se toca token, mapping ni datos nuevos. El 200
            # sólo se entrega después de persistir la obligación operativa.
            from servicios.shopify_app import registrar_shop_redact_pendiente
            if not registrar_shop_redact_pendiente(
                dominio, shop_id, app_client_id,
            ):
                return JSONResponse({"ok": False}, status_code=503)
            print("[gdpr] shop_redact pendiente de verificar generación")
            return {"ok": True, "estado": "VERIFICAR_GENERACION"}

        # Compatibilidad con instalaciones históricas previas al tombstone:
        # sólo se purga por dominio cuando ya no existe NINGÚN token activo.
        from servicios.integraciones_tienda import borrar_datos_tienda
        n = borrar_datos_tienda(dominio)
        print(f"[gdpr] shop_redact legado: {n} registro(s) procesados")
    except Exception as e:
        # El error de DB puede incluir parámetros del payload; registrar sólo
        # su clase evita copiar PII del comercio o comprador a los logs.
        print(f"[gdpr] error procesando shop/redact: {type(e).__name__}")
        return JSONResponse({"ok": False}, status_code=503)
    return {"ok": True}


@router.post("/webhook/desinstalada")
async def desinstalada(request: Request):
    """Purga la generación que Shopify identificó en el body firmado."""
    from servicios.shopify_app import (
        cliente_app_para_webhook,
        verificar_uninstall_remoto,
    )

    if not _topic_exacto(request, "app/uninstalled"):
        return JSONResponse({"ok": False}, status_code=400)
    cuerpo = await request.body()
    shop_header = request.headers.get("x-shopify-shop-domain", "")
    firma = request.headers.get("x-shopify-hmac-sha256", "")
    app_client_id = cliente_app_para_webhook(cuerpo, firma)
    if not app_client_id:
        return JSONResponse({"ok": False}, status_code=401)

    try:
        import json as _json
        datos = _json.loads(cuerpo.decode("utf-8"))
        if not isinstance(datos, dict):
            raise ValueError("payload inválido")
        # Contrato exclusivo app/uninstalled. El HMAC no firma la URL ni el
        # topic: aceptar aliases GDPR permitiría relabelar shop/redact y usarlo
        # para purgar una instalación activa.
        if any(
            clave in datos
            for clave in (
                "shop_id", "shop_domain", "customer",
                "orders_requested", "orders_to_redact",
            )
        ):
            raise ValueError("contrato app/uninstalled inválido")
        shop = str(datos.get("myshopify_domain") or "").strip().lower()
        shop_id = str(datos.get("id") or "").strip()
        if (
            not dominio_valido(shop)
            or shop != str(shop_header or "").strip().lower()
            or not shop_id
        ):
            raise ValueError("identidad de tienda inconsistente")
    except (ValueError, TypeError, UnicodeError):
        return JSONResponse({"ok": False}, status_code=400)

    try:
        verificacion = verificar_uninstall_remoto(shop, app_client_id)
        estado = str(verificacion.get("estado") or "")
        if estado in {"VIGENTE", "IGNORAR", "YA_DESINSTALADA"}:
            print("[shopify] app/uninstalled ignorado · token actual vigente o ausente")
            return {"ok": True, "estado": estado}
        if estado != "REVOCADO":
            return JSONResponse({"ok": False}, status_code=503)
        borrada = desinstalar(
            shop,
            app_client_id,
            shop_id,
            str(verificacion.get("generation") or ""),
        )
    except Exception as exc:
        print(f"[shopify] error procesando uninstall: {type(exc).__name__}")
        return JSONResponse({"ok": False}, status_code=503)
    print("[shopify] app/uninstalled procesado · "
          f"{'generación purgada' if borrada else 'evento antiguo/duplicado preservado'}")
    return {"ok": True}
