/* global React */

/* ============================================================
   SERVICES SECTION
   ============================================================ */
const SERVICES = [
  {
    id: "conecta",
    Icon: IconWarehouse,
    name: "Conectá",
    tagline: "Conectá tu tienda.",
    desc: "Vinculá Shopify con TAURO para recibir pedidos en el portal. Si vendés por otros canales, también podés cargar cada envío manualmente.",
    bullets: ["Pedidos de Shopify en el portal", "Carga manual para ventas externas", "Datos listos para preparar el envío"],
    span: "Tienda + manual",
    cta: "Conectá tu tienda",
    href: "/portal/login",
  },
  {
    id: "centraliza",
    Icon: IconShip,
    name: "Centralizá",
    tagline: "Centralizá tu operación.",
    desc: "Reuní clientes habituales, productos, solicitudes y documentación en una misma cuenta, con la información disponible para cada nuevo envío.",
    bullets: ["Base propia de clientes", "Productos opcionales", "Todos tus envíos organizados"],
    span: "Un solo portal",
    cta: "Ingresá al portal",
    href: "/portal/login",
  },
  {
    id: "cotiza",
    Icon: IconPlane,
    name: "Cotizá",
    tagline: "Cotizá con claridad.",
    desc: "Ingresá origen, destino, peso y medidas. El portal muestra las opciones habilitadas para tu cuenta antes de crear el envío.",
    bullets: ["Precio configurado por cliente", "Peso real y volumétrico", "Opciones disponibles por ruta"],
    span: "Datos claros",
    cta: "Cotizá un envío",
    action: "quote",
  },
  {
    id: "automatiza",
    Icon: IconTruck,
    name: "Automatizá",
    tagline: "Automatizá lo repetitivo.",
    desc: "Con los permisos habilitados para tu cuenta, prepará guías y coordiná recolecciones desde la misma operación.",
    bullets: ["Datos reutilizables", "Guías según habilitación", "Recolecciones vinculadas al envío"],
    span: "Según tu cuenta",
    cta: "Ingresá al portal",
    href: "/portal/login",
  },
  {
    id: "segui",
    Icon: IconShield,
    name: "Seguí",
    tagline: "Seguí cada movimiento.",
    desc: "Consultá solicitudes, guías, seguimiento y cuenta corriente desde tu portal, con acceso al detalle oficial del courier.",
    bullets: ["Historial de envíos", "Acceso a guías y tracking", "Pagos, facturas y saldo"],
    span: "Control de cuenta",
    cta: "Seguí un envío",
    href: "#tracking",
  },
  {
    id: "expandi",
    Icon: IconPlane,
    name: "Expandí",
    tagline: "Expandí con control.",
    desc: "Usá una misma operación para tus envíos nacionales e internacionales y sumá canales a medida que tu negocio crece.",
    bullets: ["Operación nacional e internacional", "Carga manual o desde tienda", "Configuración por cliente"],
    span: "Operación escalable",
    cta: "Cotizá un envío",
    action: "quote",
  },
];

function Services({ onCotizarClick }) {
  const [active, setActive] = React.useState("conecta");
  const current = SERVICES.find((s) => s.id === active);

  return (
    <section id="servicios" data-screen-label="Servicios">
      <div className="container">
        <div className="section-head">
          <div className="eyebrow">01 — Operación conectada</div>
          <h2>Seis acciones.<br/>Una sola operación.</h2>
          <p>Conectá, centralizá, cotizá, automatizá, seguí y expandí desde el portal de TAURO.</p>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "260px 1fr", gap: 32, alignItems: "stretch" }} className="services-grid">
          {/* sidebar tabs */}
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {SERVICES.map((s, i) => (
              <button
                key={s.id}
                onClick={() => setActive(s.id)}
                style={{
                  display: "flex", alignItems: "center", gap: 14,
                  padding: "16px 18px",
                  borderRadius: 12,
                  background: active === s.id ? "var(--bg-elev)" : "transparent",
                  border: `1px solid ${active === s.id ? "var(--line)" : "transparent"}`,
                  color: active === s.id ? "var(--fg)" : "var(--fg-2)",
                  textAlign: "left",
                  transition: "all .2s",
                }}
              >
                <span style={{
                  fontFamily: "var(--font-mono)", fontSize: 11,
                  color: active === s.id ? "var(--accent)" : "var(--fg-4)",
                  width: 22,
                }}>0{i + 1}</span>
                <s.Icon size={20} />
                <span style={{ fontSize: 14, fontWeight: 500 }}>{s.name}</span>
                {active === s.id && <span style={{ marginLeft: "auto", color: "var(--accent)" }}><ArrowRight size={14}/></span>}
              </button>
            ))}
          </div>

          {/* detail panel */}
          <div className="card" key={current.id} style={{ padding: 40, display: "flex", flexDirection: "column", justifyContent: "space-between", minHeight: 440 }}>
            <div className="fade-up" style={{ animationDuration: "0.4s" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 32 }}>
                <div style={{
                  width: 64, height: 64, borderRadius: 16,
                  background: "var(--bg-elev-2)", border: "1px solid var(--line)",
                  display: "grid", placeItems: "center",
                  color: "var(--accent)",
                }}>
                  <current.Icon size={32} />
                </div>
                <div className="chip"><span className="chip-dot"></span>{current.span}</div>
              </div>
              <div style={{ fontFamily: "var(--font-display)", fontSize: 36, fontWeight: 600, letterSpacing: "-0.02em", lineHeight: 1.05, marginBottom: 12 }}>
                {current.tagline}
              </div>
              <p style={{ color: "var(--fg-2)", fontSize: 17, lineHeight: 1.6, maxWidth: 520, margin: 0 }}>
                {current.desc}
              </p>
              <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 32, paddingTop: 24, borderTop: "1px solid var(--line-soft)" }}>
                {current.bullets.map((b) => (
                  <div key={b} style={{ display: "flex", alignItems: "center", gap: 12, fontSize: 14, color: "var(--fg-2)" }}>
                    <span style={{ width: 4, height: 4, background: "var(--accent)", borderRadius: "50%" }}/>
                    {b}
                  </div>
                ))}
              </div>
            </div>
            <div style={{ display: "flex", gap: 12, marginTop: 32, flexWrap: "wrap" }}>
              {current.action === "quote" ? (
                <button className="btn btn-primary" onClick={onCotizarClick}>
                  {current.cta} <ArrowRight size={14}/>
                </button>
              ) : (
                <a className="btn btn-primary" href={current.href}>
                  {current.cta} <ArrowRight size={14}/>
                </a>
              )}
              <a className="btn btn-ghost" href="#proceso">Cómo funciona</a>
            </div>
          </div>
        </div>
      </div>
      <style>{`
        @media (max-width: 880px) {
          .services-grid { grid-template-columns: minmax(0, 1fr) !important; }
        }
      `}</style>
    </section>
  );
}

/* ============================================================
   TRACKING — interactive shipment status
   ============================================================ */
const TRACK_STATES = [
  { id: "booked", label: "Reservado", time: "12 oct · 09:14", loc: "Buenos Aires, AR", desc: "Reserva confirmada con número TRO-2026-04812." },
  { id: "pickup", label: "Recogida", time: "13 oct · 14:32", loc: "Depósito CABA", desc: "Carga retirada del depósito y consolidada para vuelo." },
  { id: "transit", label: "En tránsito", time: "14 oct · 02:18", loc: "EZE → MIA", desc: "Embarcado en vuelo AA-908. ETA: 14 oct 11:45 EDT." },
  { id: "customs", label: "En aduana", time: "14 oct · 12:40", loc: "Miami, US", desc: "Despacho aduanero en proceso. Documentación completa." },
  { id: "delivery", label: "Entregado", time: "—", loc: "Doral, FL", desc: "Pendiente de entrega final al destinatario." },
];

// Las 5 etapas visibles de la barra (para el resultado real).
const ETAPAS = ["Reservado", "Recogida", "En tránsito", "En aduana", "Entregado"];

function Tracking() {
  const [activeIdx, setActiveIdx] = React.useState(3);   // sólo para el DEMO
  const [nro, setNro] = React.useState("");
  const [res, setRes] = React.useState(null);
  const [cargando, setCargando] = React.useState(false);
  const [error, setError] = React.useState("");

  async function rastrear(e) {
    if (e) e.preventDefault();
    const q = nro.trim();
    if (!q) return;
    setCargando(true); setError(""); setRes(null);
    try {
      const r = await fetch(
        `${window.TAURO_API_URL || ""}/api/rastrear?nro=${encodeURIComponent(q)}`
      );
      const data = await r.json();
      if (!data.ok) {
        setError(data.error || "No pudimos rastrear. Probá de nuevo.");
      } else if (data.encontrado === false && !data.courier) {
        setError(data.mensaje || "No pudimos identificar el courier por el número.");
      } else {
        setRes(data);
      }
    } catch (_) {
      setError("No pudimos conectar. Probá de nuevo en un momento.");
    } finally {
      setCargando(false);
    }
  }

  const real = res && res.encontrado;          // envío nuestro, con estado
  const soloCourier = res && res.encontrado === false && res.courier;  // detectado por formato

  return (
    <section id="tracking" style={{ background: "var(--bg-elev)", borderTop: "1px solid var(--line-soft)", borderBottom: "1px solid var(--line-soft)" }} data-screen-label="Tracking">
      <div className="container">
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1.3fr", gap: 64, alignItems: "center" }} className="tracking-grid">
          <div>
            <div className="eyebrow" style={{ marginBottom: 16 }}>02 — Tracking</div>
            <h2 style={{ fontSize: "clamp(36px, 4.5vw, 52px)", marginBottom: 20 }}>
              Seguí cada envío<br/>desde un mismo lugar.
            </h2>
            <p style={{ color: "var(--fg-2)", fontSize: 17, lineHeight: 1.6, marginBottom: 24 }}>
              Ingresá tu número de seguimiento para consultar el estado disponible
              y acceder al detalle oficial del courier desde un mismo lugar.
            </p>

            <form onSubmit={rastrear} style={{ display: "flex", gap: 10, marginBottom: 14 }}>
              <input
                value={nro}
                onChange={(e) => setNro(e.target.value)}
                placeholder="Ej: 771234567890"
                aria-label="Número de seguimiento"
                style={{
                  flex: 1, minWidth: 0, padding: "13px 16px",
                  background: "var(--bg)", border: "1px solid var(--line)",
                  borderRadius: 10, color: "var(--fg)", fontSize: 15,
                  fontFamily: "var(--font-mono)",
                }}
              />
              <button type="submit" className="btn btn-primary" disabled={cargando}
                style={{ whiteSpace: "nowrap" }}>
                {cargando ? "Buscando…" : "Rastrear"}
              </button>
            </form>
            {error && (
              <div style={{
                color: "var(--warn, #ffb454)", fontSize: 14, marginBottom: 18,
                padding: "10px 14px", background: "var(--bg)",
                border: "1px solid var(--line-soft)", borderRadius: 8,
              }}>{error}</div>
            )}

            <div style={{ display: "flex", gap: 28, marginTop: 18 }}>
              <Stat n="En un lugar" l="Seguimiento de envíos"/>
              <Stat n="Portal" l="Acceso para clientes"/>
            </div>
          </div>

          <div className="card tracking-demo" style={{ padding: 0, overflow: "hidden" }}>
            {/* terminal-style header */}
            <div style={{ padding: "14px 20px", borderBottom: "1px solid var(--line-soft)", display: "flex", alignItems: "center", gap: 12, fontFamily: "var(--font-mono)", fontSize: 12 }}>
              <div style={{ display: "flex", gap: 6 }}>
                <span style={{ width: 10, height: 10, borderRadius: "50%", background: "#ff5f57" }}/>
                <span style={{ width: 10, height: 10, borderRadius: "50%", background: "#febc2e" }}/>
                <span style={{ width: 10, height: 10, borderRadius: "50%", background: "#28c840" }}/>
              </div>
              <div style={{ marginLeft: 12, color: "var(--fg-3)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                tauro://tracking/{res ? nro.trim().toUpperCase() : "TRO-2026-04812"}
              </div>
              <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 6, color: res ? "var(--accent)" : "var(--ok)" }}>
                <span style={{ width: 6, height: 6, background: res ? "var(--accent)" : "var(--ok)", borderRadius: "50%" }} className="pulse"/>
                {res ? "RESULTADO" : "DEMO"}
              </div>
            </div>

            <div style={{ padding: 28 }}>
              {real ? (
                <ResultadoReal res={res} nro={nro}/>
              ) : soloCourier ? (
                <ResultadoCourier res={res}/>
              ) : (
              <React.Fragment>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 24 }}>
                <div>
                  <div style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--fg-3)", textTransform: "uppercase", letterSpacing: "0.08em" }}>
                    Tracking · TRO-2026-04812
                  </div>
                  <div style={{ fontFamily: "var(--font-display)", fontSize: 24, fontWeight: 600, marginTop: 4 }}>
                    Buenos Aires → Miami
                  </div>
                </div>
                <div className="chip"><IconPlane size={12}/> Aéreo</div>
              </div>

              {/* progress bar */}
              <div style={{ marginBottom: 28, position: "relative" }}>
                <div style={{ height: 3, background: "var(--bg-elev-2)", borderRadius: 99, overflow: "hidden" }}>
                  <div style={{
                    height: "100%",
                    width: `${(activeIdx / (TRACK_STATES.length - 1)) * 100}%`,
                    background: "linear-gradient(to right, var(--accent), var(--accent-soft))",
                    transition: "width .5s cubic-bezier(.2,.7,.3,1)",
                  }}/>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", marginTop: 10 }}>
                  {TRACK_STATES.map((s, i) => (
                    <button
                      key={s.id}
                      onClick={() => setActiveIdx(i)}
                      style={{
                        padding: 0,
                        fontSize: 10,
                        fontFamily: "var(--font-mono)",
                        textTransform: "uppercase",
                        letterSpacing: "0.05em",
                        color: i <= activeIdx ? (i === activeIdx ? "var(--accent)" : "var(--fg-2)") : "var(--fg-4)",
                      }}
                    >
                      {s.label}
                    </button>
                  ))}
                </div>
              </div>

              {/* events list */}
              <div style={{ display: "flex", flexDirection: "column", gap: 14, maxHeight: 260, overflow: "hidden" }}>
                {TRACK_STATES.slice().reverse().map((s, ri) => {
                  const i = TRACK_STATES.length - 1 - ri;
                  const done = i < activeIdx;
                  const current = i === activeIdx;
                  return (
                    <div key={s.id} style={{
                      display: "flex", gap: 14,
                      padding: "12px 14px",
                      background: current ? "var(--bg-elev)" : "transparent",
                      border: `1px solid ${current ? "var(--line)" : "transparent"}`,
                      borderRadius: 10,
                      opacity: i > activeIdx ? 0.4 : 1,
                    }}>
                      <div style={{
                        width: 8, height: 8, marginTop: 6, borderRadius: "50%",
                        background: current ? "var(--accent)" : done ? "var(--ok)" : "var(--fg-4)",
                        boxShadow: current ? "0 0 0 4px var(--accent-glow)" : "none",
                        flexShrink: 0,
                      }}/>
                      <div style={{ flex: 1 }}>
                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
                          <div style={{ fontSize: 14, fontWeight: 500 }}>{s.label} <span style={{ color: "var(--fg-3)", fontWeight: 400, fontSize: 13 }}>· {s.loc}</span></div>
                          <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--fg-3)" }}>{s.time}</div>
                        </div>
                        <div style={{ fontSize: 13, color: "var(--fg-2)", marginTop: 4 }}>{s.desc}</div>
                      </div>
                    </div>
                  );
                })}
              </div>
              </React.Fragment>
              )}
            </div>
          </div>
        </div>
      </div>
      <style>{`
        @media (max-width: 880px) {
          .tracking-grid { grid-template-columns: minmax(0, 1fr) !important; }
        }
        /* Foto del avión con paquetes DETRÁS de la tarjeta de tracking, con un
           overlay oscuro degradado para que el texto claro siga legible: se
           percibe arriba (header + título) y se cierra sobre los eventos. */
        .tracking-demo {
          background-image:
            linear-gradient(180deg, rgba(10,14,18,0.66) 0%, rgba(10,14,18,0.86) 52%, rgba(10,14,18,0.95) 100%),
            url('/static/img/escenas/avion-hero.jpg');
          background-size: cover;
          background-position: center;
          background-repeat: no-repeat;
        }
        @media (max-width: 880px) {
          .tracking-demo {
            background-image:
              linear-gradient(180deg, rgba(10,14,18,0.66) 0%, rgba(10,14,18,0.86) 52%, rgba(10,14,18,0.95) 100%),
              url('/static/img/escenas/avion-hero-mob.jpg');
          }
        }
      `}</style>
    </section>
  );
}

// Resultado real: envío nuestro. Mostramos origen→destino, courier, la barra
// en la etapa que sabemos, el estado, y un botón al detalle en vivo del courier.
function ResultadoReal({ res, nro }) {
  const etapa = typeof res.etapa === "number" ? res.etapa : 0;
  return (
    <React.Fragment>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 24 }}>
        <div>
          <div style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--fg-3)", textTransform: "uppercase", letterSpacing: "0.08em" }}>
            Tracking · {nro.trim().toUpperCase()}
          </div>
          <div style={{ fontFamily: "var(--font-display)", fontSize: 24, fontWeight: 600, marginTop: 4 }}>
            {res.origen} → {res.destino}
          </div>
        </div>
        <div className="chip">{res.courier_nombre}</div>
      </div>

      {/* progress bar en la etapa real */}
      <div style={{ marginBottom: 28, position: "relative" }}>
        <div style={{ height: 3, background: "var(--bg-elev-2)", borderRadius: 99, overflow: "hidden" }}>
          <div style={{
            height: "100%",
            width: `${(etapa / (ETAPAS.length - 1)) * 100}%`,
            background: "linear-gradient(to right, var(--accent), var(--accent-soft))",
            transition: "width .5s cubic-bezier(.2,.7,.3,1)",
          }}/>
        </div>
        <div style={{ display: "flex", justifyContent: "space-between", marginTop: 10 }}>
          {ETAPAS.map((label, i) => (
            <span key={label} style={{
              fontSize: 10, fontFamily: "var(--font-mono)", textTransform: "uppercase",
              letterSpacing: "0.05em",
              color: i <= etapa ? (i === etapa ? "var(--accent)" : "var(--fg-2)") : "var(--fg-4)",
            }}>{label}</span>
          ))}
        </div>
      </div>

      {/* estado actual */}
      <div style={{
        display: "flex", gap: 14, padding: "14px 16px",
        background: "var(--bg-elev)", border: "1px solid var(--line)", borderRadius: 10,
        marginBottom: 18,
      }}>
        <div style={{ width: 8, height: 8, marginTop: 6, borderRadius: "50%", background: "var(--accent)", boxShadow: "0 0 0 4px var(--accent-glow)", flexShrink: 0 }}/>
        <div style={{ flex: 1 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
            <div style={{ fontSize: 15, fontWeight: 600 }}>{res.estado}</div>
            {res.fecha && <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--fg-3)" }}>{res.fecha}</div>}
          </div>
          <div style={{ fontSize: 13, color: "var(--fg-2)", marginTop: 4 }}>
            Para los hitos de tránsito, aduana y entrega, consultá el detalle actualizado del courier.
          </div>
        </div>
      </div>

      <a href={res.url_courier} target="_blank" rel="noopener noreferrer"
         className="btn btn-primary" style={{ width: "100%", textAlign: "center", boxSizing: "border-box" }}>
        Ver detalle en {res.courier_nombre} →
      </a>
    </React.Fragment>
  );
}

// El número no es de un envío nuestro pero reconocimos el courier por su
// formato: lo mandamos al seguimiento oficial de ese courier.
function ResultadoCourier({ res }) {
  return (
    <div style={{ textAlign: "center", padding: "18px 8px" }}>
      <div style={{ fontFamily: "var(--font-display)", fontSize: 22, fontWeight: 600, marginBottom: 8 }}>
        Es un envío de {res.courier_nombre}
      </div>
      <p style={{ color: "var(--fg-2)", fontSize: 15, lineHeight: 1.6, marginBottom: 24 }}>
        Reconocimos el courier por el número. Seguí tu paquete en el
        rastreo oficial de {res.courier_nombre}.
      </p>
      <a href={res.url_courier} target="_blank" rel="noopener noreferrer"
         className="btn btn-primary" style={{ width: "100%", textAlign: "center", boxSizing: "border-box" }}>
        Ver seguimiento en {res.courier_nombre} →
      </a>
    </div>
  );
}

function Stat({ n, l }) {
  return (
    <div>
      <div style={{ fontFamily: "var(--font-display)", fontSize: 28, fontWeight: 600, letterSpacing: "-0.02em" }}>{n}</div>
      <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--fg-3)", textTransform: "uppercase", letterSpacing: "0.08em", marginTop: 2 }}>{l}</div>
    </div>
  );
}

/* ============================================================
   PROCESS — How it works
   ============================================================ */
const STEPS = [
  { n: "01", t: "Conectá o cargá", d: "Vinculá Shopify o ingresá el envío manualmente. Elegí un cliente guardado para completar sus datos." },
  { n: "02", t: "Cotizá y generá", d: "Cargá peso y medidas, revisá la opción habilitada para tu cuenta y prepará la solicitud de guía." },
  { n: "03", t: "Seguí y controlá", d: "Consultá el envío, accedé al tracking y mantené organizada tu cuenta corriente desde el portal." },
];

function Process() {
  return (
    <section id="proceso" data-screen-label="Proceso">
      <div className="container">
        <div className="section-head">
          <div className="eyebrow">03 — Cómo funciona</div>
          <h2>Tres pasos.<br/>Una operación clara.</h2>
          <p>Elegí cómo cargar tus datos y avanzá desde el mismo portal.</p>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 0, border: "1px solid var(--line-soft)", borderRadius: 16, overflow: "hidden" }} className="process-grid">
          {STEPS.map((s, i) => (
            <div key={s.n} style={{
              padding: 32,
              borderRight: i < STEPS.length - 1 ? "1px solid var(--line-soft)" : "none",
              position: "relative",
              minHeight: 280,
              display: "flex", flexDirection: "column", justifyContent: "space-between",
              background: "var(--bg-elev)",
            }}>
              <div>
                <div style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--accent)", marginBottom: 24, letterSpacing: "0.08em" }}>
                  PASO / {s.n}
                </div>
                <div style={{ fontFamily: "var(--font-display)", fontSize: 22, fontWeight: 600, lineHeight: 1.2, marginBottom: 14 }}>
                  {s.t}
                </div>
                <p style={{ color: "var(--fg-2)", fontSize: 14, lineHeight: 1.55, margin: 0 }}>{s.d}</p>
              </div>
              <div style={{
                fontFamily: "var(--font-display)",
                fontSize: 88,
                fontWeight: 700,
                color: "var(--bg-elev-2)",
                lineHeight: 1,
                marginTop: 24,
                letterSpacing: "-0.04em",
              }}>{s.n}</div>
            </div>
          ))}
        </div>
      </div>
      <style>{`
        @media (max-width: 880px) {
          .process-grid { grid-template-columns: minmax(0, 1fr) !important; }
          .process-grid > div { border-right: none !important; }
          .process-grid > div:not(:last-child) { border-bottom: 1px solid var(--line-soft); }
        }
      `}</style>
    </section>
  );
}

window.Services = Services;
window.Tracking = Tracking;
window.Process = Process;
