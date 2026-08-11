/* global React */

/* ============================================================
   STATS / WHY US
   ============================================================ */
function WhyUs() {
  return (
    <section id="nosotros" style={{ background: "var(--bg-elev)", borderTop: "1px solid var(--line-soft)", borderBottom: "1px solid var(--line-soft)" }} data-screen-label="Nosotros">
      <div className="container">
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 80, alignItems: "center" }} className="why-grid">
          <div>
            <div className="eyebrow" style={{ marginBottom: 18 }}>04 — Por qué Tauro</div>
            <h2 style={{ fontSize: "clamp(36px, 4.5vw, 56px)", marginBottom: 24 }}>
              Tecnología argentina.<br/>
              Operación <span style={{ color: "var(--accent)", fontStyle: "italic", fontWeight: 500 }}>conectada</span>.
            </h2>
            <p style={{ color: "var(--fg-2)", fontSize: 17, lineHeight: 1.65, marginBottom: 32 }}>
              Somos un equipo argentino de logística y tecnología. Centralizamos
              pedidos, envíos y seguimiento en una plataforma propia. Las funciones
              de cotización, guías y recolecciones se habilitan por cliente y courier.
            </p>
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)", gap: 16 }}>
            <BigStat n="Portal" l="Una sola operación" />
            <BigStat n="B2B" l="Precios por cliente" accent />
            <BigStat n="1:1" l="Atención directa" />
            <BigStat n="2 vías" l="Tienda o carga manual" />
          </div>
        </div>
      </div>
      <style>{`
        @media (max-width: 880px) {
          .why-grid { grid-template-columns: minmax(0, 1fr) !important; gap: 48px !important; }
        }
      `}</style>
    </section>
  );
}

function BigStat({ n, l, accent }) {
  return (
    <div style={{
      padding: 28,
      background: accent ? "var(--accent)" : "var(--bg)",
      border: `1px solid ${accent ? "var(--accent)" : "var(--line-soft)"}`,
      borderRadius: 16,
      color: accent ? "#fff" : "var(--fg)",
      minHeight: 160,
      display: "flex", flexDirection: "column", justifyContent: "space-between",
    }}>
      <div style={{ fontFamily: "var(--font-mono)", fontSize: 11, textTransform: "uppercase", letterSpacing: "0.08em", opacity: 0.7 }}>
        {l}
      </div>
      <div style={{
        fontFamily: "var(--font-display)",
        fontSize: 56,
        fontWeight: 600,
        letterSpacing: "-0.03em",
        lineHeight: 1,
      }}>{n}</div>
    </div>
  );
}

/* ============================================================
   INDUSTRIES — marquee-style
   ============================================================ */
function Industries() {
  const items = [
    "E-commerce", "Agroindustria", "Vinos & Bebidas", "Indumentaria",
    "Pharma", "Automotriz", "Maquinaria", "Tecnología", "Alimentos",
    "Cosmética", "Pesca", "Energía"
  ];
  return (
    <section data-screen-label="Industrias" style={{ paddingTop: 80, paddingBottom: 80 }}>
      <div className="container">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end", marginBottom: 48, flexWrap: "wrap", gap: 24 }}>
          <div>
            <div className="eyebrow" style={{ marginBottom: 16 }}>05 — Industrias</div>
            <h2 style={{ fontSize: "clamp(32px, 4vw, 48px)" }}>Negocios que conectamos.</h2>
          </div>
          <p style={{ color: "var(--fg-2)", maxWidth: 380, margin: 0 }}>
            Cada empresa conserva su propia configuración, su base de clientes
            y las opciones logísticas habilitadas para su operación.
          </p>
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
          {items.map((i, idx) => (
            <div key={i} style={{
              padding: "16px 22px",
              background: idx === 2 || idx === 7 ? "var(--accent)" : "var(--bg-elev)",
              color: idx === 2 || idx === 7 ? "#fff" : "var(--fg)",
              border: `1px solid ${idx === 2 || idx === 7 ? "var(--accent)" : "var(--line-soft)"}`,
              borderRadius: 99,
              fontSize: 15,
              fontWeight: 500,
              transition: "transform .2s",
              cursor: "default",
            }}
            onMouseEnter={(e) => e.currentTarget.style.transform = "translateY(-2px)"}
            onMouseLeave={(e) => e.currentTarget.style.transform = "translateY(0)"}
            >
              {i}
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

/* ============================================================
   CTA / CONTACT
   ============================================================ */
function ContactCTA({ onCotizarClick }) {
  return (
    <section id="contacto" data-screen-label="Contacto" style={{ paddingBottom: 60 }}>
      <div className="container">
        <div style={{
          padding: "100px 64px",
          background: "linear-gradient(135deg, var(--bg-elev) 0%, var(--bg-elev-2) 100%)",
          border: "1px solid var(--line)",
          borderRadius: 24,
          position: "relative",
          overflow: "hidden",
          textAlign: "center",
        }}>
          {/* ambient glow */}
          <div style={{
            position: "absolute", inset: 0,
            background: "radial-gradient(circle at 50% 0%, var(--accent-glow), transparent 50%)",
            pointerEvents: "none",
          }}/>
          <div style={{ position: "relative" }}>
            <div className="eyebrow" style={{ marginBottom: 20, justifyContent: "center" }}>06 — Empezá hoy</div>
            <h2 style={{
              fontSize: "clamp(40px, 6vw, 84px)",
              maxWidth: 900,
              margin: "0 auto 28px",
              letterSpacing: "-0.03em",
            }}>
              Conectá tu tienda.<br/>
              Centralizá tu operación.
            </h2>
            <p style={{ color: "var(--fg-2)", fontSize: 18, maxWidth: 580, margin: "0 auto 40px" }}>
              Integrá Shopify o cargá tus envíos manualmente. Cotizá y gestioná
              cada operación desde el portal de TAURO.
            </p>
            <div style={{ display: "flex", gap: 14, justifyContent: "center", flexWrap: "wrap" }}>
              <a href="/portal/login" className="btn btn-primary btn-lg">
                Conectá tu tienda <ArrowRight size={16}/>
              </a>
              <button className="btn btn-ghost btn-lg" onClick={onCotizarClick}>Cotizá un envío</button>
            </div>
            <div style={{ marginTop: 56, display: "flex", justifyContent: "center", gap: 48, flexWrap: "wrap", fontFamily: "var(--font-mono)", fontSize: 13, color: "var(--fg-3)" }}>
              <span>cotizaciones@taurosolutions.ar</span>
              <span>Buenos Aires, Argentina</span>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

/* ============================================================
   FOOTER
   ============================================================ */
function Footer() {
  return (
    <footer className="footer">
      <div className="container">
        <div className="footer-grid">
          <div>
            <a href="/web" className="logo" style={{ marginBottom: 18 }}>
              <span className="logo-mark"><TauroLogo size={28} color="#fff"/></span>
              <span>Tauro<span style={{ color: "var(--fg-3)", fontWeight: 400, marginLeft: 4 }}>solutions</span></span>
            </a>
            <p style={{ color: "var(--fg-3)", fontSize: 14, maxWidth: 320, marginTop: 16 }}>
              Tu logística en un solo portal. Conectá tu tienda o cargá tus envíos manualmente.
            </p>
          </div>
          <div>
            <h4>Servicios</h4>
            <ul>
              <li><a href="/portal/login">Conectar tienda</a></li>
              <li><a href="#servicios">Centralizar operación</a></li>
              <li><a href="#servicios">Cotizar envíos</a></li>
              <li><a href="/portal/login">Crear envíos</a></li>
              <li><a href="#tracking">Seguir envíos</a></li>
            </ul>
          </div>
          <div>
            <h4>Empresa</h4>
            <ul>
              <li><a href="#nosotros">Nosotros</a></li>
            </ul>
          </div>
          <div>
            <h4>Soporte</h4>
            <ul>
              <li><a href="/portal/login">Portal de clientes</a></li>
              <li><a href="#tracking">Trackear envío</a></li>
              <li><a href="#contacto">Contacto</a></li>
            </ul>
          </div>
        </div>
        <div className="footer-bottom">
          <span>© 2026 Tauro Solutions</span>
          <span>Buenos Aires · Argentina</span>
        </div>
      </div>
    </footer>
  );
}

window.WhyUs = WhyUs;
window.Industries = Industries;
window.ContactCTA = ContactCTA;
window.Footer = Footer;
