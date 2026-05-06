/* global React */

/* ============================================================
   SERVICES SECTION
   ============================================================ */
const SERVICES = [
  {
    id: "aereo",
    Icon: IconPlane,
    name: "Carga Aérea",
    tagline: "Cuando el tiempo importa.",
    desc: "Vuelos directos y consolidados a más de 40 países. Tránsito de 3 a 5 días.",
    bullets: ["Carga general y perecederos", "Vuelos charter on-demand", "Express y courier internacional"],
    span: "Tránsito 3–5 días",
  },
  {
    id: "maritimo",
    Icon: IconShip,
    name: "Carga Marítima",
    tagline: "Volumen sin límite.",
    desc: "FCL y LCL desde y hacia los principales puertos del mundo. Tarifas competitivas para grandes volúmenes.",
    bullets: ["Contenedores 20' / 40' / HC", "Carga consolidada (LCL)", "Reefer y carga proyecto"],
    span: "Tránsito 22–35 días",
  },
  {
    id: "terrestre",
    Icon: IconTruck,
    name: "Carga Terrestre",
    tagline: "Mercosur sin escalas.",
    desc: "Camiones propios y aliados para Argentina, Brasil, Chile, Uruguay, Paraguay y Bolivia.",
    bullets: ["FTL y LTL", "Cross-docking en frontera", "Cadena de frío"],
    span: "Tránsito 2–10 días",
  },
  {
    id: "aduana",
    Icon: IconShield,
    name: "Despacho Aduanero",
    tagline: "Sin sorpresas en frontera.",
    desc: "Despachantes propios habilitados para gestionar exportaciones e importaciones sin demoras.",
    bullets: ["Clasificación arancelaria", "Régimen general y simplificado", "Asesoría en tratados de libre comercio"],
    span: "On-site en EZE, BUE, ROS",
  },
  {
    id: "almacen",
    Icon: IconWarehouse,
    name: "Almacenaje & Fulfillment",
    tagline: "Tu stock listo para mover.",
    desc: "Depósitos fiscales y de uso público en zonas estratégicas. Picking, packing y distribución última milla.",
    bullets: ["Depósito fiscal", "Pick & pack", "Distribución última milla"],
    span: "12.000 m² de capacidad",
  },
];

function Services() {
  const [active, setActive] = React.useState("aereo");
  const current = SERVICES.find((s) => s.id === active);

  return (
    <section id="servicios" data-screen-label="Servicios">
      <div className="container">
        <div className="section-head">
          <div className="eyebrow">01 — Servicios</div>
          <h2>Una sola plataforma<br/>para toda tu logística internacional.</h2>
          <p>Desde la cotización hasta la entrega final, gestionamos cada modo y cada eslabón con la misma rigurosidad.</p>
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
            <div style={{ display: "flex", gap: 12, marginTop: 32 }}>
              <button className="btn btn-primary">Cotizar {current.name.toLowerCase()} <ArrowRight size={14}/></button>
              <button className="btn btn-ghost">Ver casos de éxito</button>
            </div>
          </div>
        </div>
      </div>
      <style>{`
        @media (max-width: 880px) {
          .services-grid { grid-template-columns: 1fr !important; }
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

function Tracking() {
  const [activeIdx, setActiveIdx] = React.useState(3);

  return (
    <section id="tracking" style={{ background: "var(--bg-elev)", borderTop: "1px solid var(--line-soft)", borderBottom: "1px solid var(--line-soft)" }} data-screen-label="Tracking">
      <div className="container">
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1.3fr", gap: 64, alignItems: "center" }} className="tracking-grid">
          <div>
            <div className="eyebrow" style={{ marginBottom: 16 }}>02 — Tracking</div>
            <h2 style={{ fontSize: "clamp(36px, 4.5vw, 52px)", marginBottom: 20 }}>
              Sabés exactamente<br/>dónde está tu carga.
            </h2>
            <p style={{ color: "var(--fg-2)", fontSize: 17, lineHeight: 1.6, marginBottom: 28 }}>
              Conectamos directamente con navieras, aerolíneas y aduanas. Sin
              "esperá y te aviso", sin llamadas — todo el estado en tu pantalla,
              actualizado al minuto.
            </p>
            <div style={{ display: "flex", gap: 28, marginBottom: 32 }}>
              <Stat n="< 90s" l="Latencia de update"/>
              <Stat n="API" l="Webhooks + REST"/>
            </div>
            <button className="btn btn-ghost">Ver documentación API <ArrowRight size={14}/></button>
          </div>

          <div className="card" style={{ padding: 0, overflow: "hidden", background: "var(--bg)" }}>
            {/* terminal-style header */}
            <div style={{ padding: "14px 20px", borderBottom: "1px solid var(--line-soft)", display: "flex", alignItems: "center", gap: 12, fontFamily: "var(--font-mono)", fontSize: 12 }}>
              <div style={{ display: "flex", gap: 6 }}>
                <span style={{ width: 10, height: 10, borderRadius: "50%", background: "#ff5f57" }}/>
                <span style={{ width: 10, height: 10, borderRadius: "50%", background: "#febc2e" }}/>
                <span style={{ width: 10, height: 10, borderRadius: "50%", background: "#28c840" }}/>
              </div>
              <div style={{ marginLeft: 12, color: "var(--fg-3)" }}>tauro://tracking/TRO-2026-04812</div>
              <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 6, color: "var(--ok)" }}>
                <span style={{ width: 6, height: 6, background: "var(--ok)", borderRadius: "50%" }} className="pulse"/>
                LIVE
              </div>
            </div>

            <div style={{ padding: 28 }}>
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
            </div>
          </div>
        </div>
      </div>
      <style>{`
        @media (max-width: 880px) {
          .tracking-grid { grid-template-columns: 1fr !important; }
        }
      `}</style>
    </section>
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
  { n: "01", t: "Cotizá online", d: "Origen, destino, peso. Tarifa real en menos de 60 segundos, sin formularios eternos." },
  { n: "02", t: "Reservá y documentá", d: "Subís la documentación una vez. Generamos BL, AWB y factura proforma automáticamente." },
  { n: "03", t: "Nosotros movemos", d: "Recogida, consolidación, transporte internacional, despacho aduanero. Vos seguís en tu negocio." },
  { n: "04", t: "Trackeás en vivo", d: "Status del envío, alertas en cada hito y entrega confirmada con prueba digital." },
];

function Process() {
  return (
    <section id="proceso" data-screen-label="Proceso">
      <div className="container">
        <div className="section-head">
          <div className="eyebrow">03 — Cómo funciona</div>
          <h2>Cuatro pasos.<br/>Sin sorpresas.</h2>
          <p>Diseñamos el proceso para que dediques minutos, no días, a coordinar tu logística.</p>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 0, border: "1px solid var(--line-soft)", borderRadius: 16, overflow: "hidden" }} className="process-grid">
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
                  STEP / {s.n}
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
          .process-grid { grid-template-columns: 1fr 1fr !important; }
          .process-grid > div:nth-child(2) { border-right: none !important; }
          .process-grid > div:nth-child(odd) { border-right: 1px solid var(--line-soft) !important; }
          .process-grid > div:nth-child(1), .process-grid > div:nth-child(2) { border-bottom: 1px solid var(--line-soft); }
        }
      `}</style>
    </section>
  );
}

window.Services = Services;
window.Tracking = Tracking;
window.Process = Process;
