/* global React */
const { useState, useEffect, useRef } = React;

/* ============================================================
   LOGO — Marca OFICIAL de Tauro Solutions
   Extraída de la papelería real (guía aérea / proforma).
   PNG blanco con transparencia: static/img/logo-mark-white.png
   ============================================================ */
function TauroLogo({ size = 32 }) {
  return (
    <img
      src="/static/img/logo-mark-white.png"
      alt="Tauro Solutions"
      style={{ height: size, width: "auto", display: "block" }}
    />
  );
}

/* ============================================================
   PARTNERS — los couriers habilitados HOY
   La lista sale de /partners, que la calcula de las credenciales
   cargadas. A propósito no está escrita a mano: el día que se
   encienda UPS aparece solo, y si a un courier se le caen las
   credenciales deja de figurar en vez de quedar prometido en la
   web. Mientras carga, muestra los que ya sabemos que operan
   para que no haya un salto visual.
   ============================================================ */
function PartnersMeta() {
  const [partners, setPartners] = useState(null);

  useEffect(() => {
    let vivo = true;
    fetch(`${window.TAURO_API_URL ?? ""}/partners`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (vivo && d?.partners?.length) setPartners(d.partners); })
      .catch(() => {});   // si falla, queda el fallback: nunca una barra vacía
    return () => { vivo = false; };
  }, []);

  const nombres = partners
    ? partners.map((p) => p.nombre.replace(" Express", ""))
    : ["DHL"];

  return (
    <div className="hero-meta-item">
      <div className="num tweb-partners">{nombres.join(" · ")}</div>
      <div className="lbl">
        {nombres.length === 1 ? "Partner de envíos" : "Partners de envío"}
      </div>
    </div>
  );
}

/* ============================================================
   NAV
   ============================================================ */
function Nav() {
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 12);
    window.addEventListener("scroll", onScroll);
    return () => window.removeEventListener("scroll", onScroll);
  }, []);
  return (
    <nav className={`nav ${scrolled ? "scrolled" : ""}`}>
      <div className="container nav-inner">
        <a href="/web" className="logo">
          <span className="logo-mark"><TauroLogo size={28} color="#fff" /></span>
          {/* "solutions" en violeta metálico — eco sutil del chip y los precios */}
          <span>Tauro<span className="tweb-price-metal" style={{ fontWeight: 400, marginLeft: 2 }}>solutions</span></span>
        </a>
        <ul className="nav-links">
          <li><a href="#servicios">Servicios</a></li>
          <li><a href="#tracking">Tracking</a></li>
          <li><a href="#proceso">Cómo funciona</a></li>
          <li><a href="#nosotros">Nosotros</a></li>
          <li><a href="#contacto">Contacto</a></li>
        </ul>
        <div className="nav-cta">
          <a href="/portal/login" className="btn btn-ghost" style={{ fontSize: 13, padding: "8px 16px" }}>
            {/* En celular la etiqueta se acorta para que la marca entre entera */}
            <span className="tweb-txt-largo">Iniciar sesión</span>
            <span className="tweb-txt-corto">Ingresar</span>
          </a>
          <a href="/portal/login" className="btn btn-primary" style={{ fontSize: 13, padding: "10px 18px" }}>
            <span className="tweb-txt-largo">Conectá tu tienda</span>
            <span className="tweb-txt-corto">Conectar</span>
            <ArrowRight size={14} />
          </a>
        </div>
      </div>
    </nav>
  );
}

/* ============================================================
   ICONS — minimal line icons
   ============================================================ */
function ArrowRight({ size = 16 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <path d="M3 8 H13 M9 4 L13 8 L9 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
function ArrowDown({ size = 16 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none">
      <path d="M8 3 V13 M4 9 L8 13 L12 9" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
function IconShip({ size = 28 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" fill="none">
      <path d="M4 22 L28 22 L26 28 L6 28 Z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"/>
      <path d="M8 22 V14 L24 14 V22" stroke="currentColor" strokeWidth="1.5"/>
      <path d="M16 6 V14 M12 10 H20" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
      <rect x="11" y="17" width="3" height="3" stroke="currentColor" strokeWidth="1.2"/>
      <rect x="18" y="17" width="3" height="3" stroke="currentColor" strokeWidth="1.2"/>
    </svg>
  );
}
function IconPlane({ size = 28 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" fill="none">
      <path d="M16 4 L18 14 L28 18 L28 21 L18 19 L17 26 L20 28 L20 30 L16 29 L12 30 L12 28 L15 26 L14 19 L4 21 L4 18 L14 14 Z"
            stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"/>
    </svg>
  );
}
function IconTruck({ size = 28 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" fill="none">
      <rect x="3" y="10" width="16" height="12" stroke="currentColor" strokeWidth="1.5"/>
      <path d="M19 14 L25 14 L29 18 L29 22 L19 22" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"/>
      <circle cx="9" cy="24" r="2.5" stroke="currentColor" strokeWidth="1.5"/>
      <circle cx="23" cy="24" r="2.5" stroke="currentColor" strokeWidth="1.5"/>
    </svg>
  );
}
function IconWarehouse({ size = 28 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" fill="none">
      <path d="M4 14 L16 6 L28 14 L28 28 L4 28 Z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"/>
      <rect x="10" y="18" width="12" height="10" stroke="currentColor" strokeWidth="1.5"/>
      <path d="M10 23 L22 23" stroke="currentColor" strokeWidth="1.2"/>
    </svg>
  );
}
function IconShield({ size = 28 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" fill="none">
      <path d="M16 4 L26 8 V16 C26 22 22 26 16 28 C10 26 6 22 6 16 V8 Z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"/>
      <path d="M11 16 L15 20 L21 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  );
}

/* ============================================================
   HERO
   ============================================================ */
function Hero({ variant = "split", onCotizarClick, t }) {
  if (variant === "centered") return <HeroCentered onCotizarClick={onCotizarClick} />;
  if (variant === "minimal") return <HeroMinimal onCotizarClick={onCotizarClick} />;
  return <HeroSplit onCotizarClick={onCotizarClick} />;
}

function HeroSplit({ onCotizarClick }) {
  return (
    <section className="hero" data-screen-label="Hero">
      <div className="hero-bg">
        <div className="grid-lines"></div>
        <div className="glow"></div>
      </div>
      <div className="container">
        <div className="hero-grid">
          <div>
            <div className="chip fade-up" style={{ marginBottom: 28 }}>
              <span className="chip-dot pulse"></span>
              Integraciones + logística
            </div>
            <h1 className="fade-up d1">
              Conectá.<br/>
              <span className="accent">Centralizá.</span><br/>
              Expandí.
            </h1>
            <p className="hero-promise fade-up d2">
              Tu logística en un solo portal.
            </p>
            <p className="lead fade-up d2">
              Logística nacional e internacional conectada directamente a tu tienda.
              <span className="hero-manual-note"> ¿No tenés tienda? Cargá tus envíos manualmente.</span>
            </p>
            <div className="hero-actions fade-up d3">
              <a href="/portal/login" className="btn btn-primary btn-lg">
                Conectá tu tienda
                <ArrowRight size={16} />
              </a>
              <button className="btn btn-ghost btn-lg" onClick={onCotizarClick}>
                Cotizá un envío
              </button>
            </div>
            <div className="hero-meta fade-up d4">
              <PartnersMeta />
              <div className="hero-meta-item">
                <div className="num">Manual</div>
                <div className="lbl">También sin tienda</div>
              </div>
              <div className="hero-meta-item">
                <div className="num">Portal</div>
                <div className="lbl">Una sola operación</div>
              </div>
            </div>
          </div>
          <div className="fade-up d2">
            <QuoteWidget />
          </div>
        </div>
      </div>
    </section>
  );
}

function HeroCentered({ onCotizarClick }) {
  return (
    <section className="hero" data-screen-label="Hero" style={{ textAlign: "center" }}>
      <div className="hero-bg"><div className="grid-lines"></div><div className="glow" style={{ left: "50%", marginLeft: -300, right: "auto" }}></div></div>
      <div className="container" style={{ maxWidth: 980 }}>
        <div className="chip fade-up" style={{ marginBottom: 28 }}>
          <span className="chip-dot pulse"></span> Integraciones + logística
        </div>
        <h1 className="fade-up d1" style={{ fontSize: "clamp(56px, 9vw, 120px)" }}>
          Conectá.<br/>
          <span className="accent">Centralizá.</span><br/>
          Expandí.
        </h1>
        <p className="hero-promise fade-up d2" style={{ margin: "32px auto 12px" }}>
          Tu logística en un solo portal.
        </p>
        <p className="lead fade-up d2" style={{ margin: "0 auto 40px", fontSize: 19 }}>
          Logística nacional e internacional conectada directamente a tu tienda.
          <span className="hero-manual-note"> También podés operar manualmente.</span>
        </p>
        <div className="hero-actions fade-up d3" style={{ justifyContent: "center" }}>
          <a href="/portal/login" className="btn btn-primary btn-lg">
            Conectá tu tienda <ArrowRight size={16} />
          </a>
          <button className="btn btn-ghost btn-lg" onClick={onCotizarClick}>Cotizá un envío</button>
        </div>
        <div className="fade-up d4" style={{ marginTop: 80, maxWidth: 720, margin: "80px auto 0" }}>
          <QuoteWidget compact />
        </div>
      </div>
    </section>
  );
}

function HeroMinimal({ onCotizarClick }) {
  return (
    <section className="hero" data-screen-label="Hero">
      <div className="hero-bg"><div className="grid-lines"></div></div>
      <div className="container">
        <div style={{ maxWidth: 900 }}>
          <div className="eyebrow fade-up" style={{ marginBottom: 32 }}>Tauro Solutions / 2026</div>
          <h1 className="fade-up d1" style={{ fontSize: "clamp(48px, 8vw, 112px)" }}>
            Conectá.<br/>
            <em style={{ color: "var(--accent)", fontWeight: 500 }}>Centralizá.</em><br/>
            Expandí.
          </h1>
          <div className="fade-up d2" style={{ display: "flex", gap: 64, marginTop: 80, alignItems: "flex-end", flexWrap: "wrap" }}>
            <p style={{ maxWidth: 420, color: "var(--fg-2)", fontSize: 17, lineHeight: 1.6, margin: 0 }}>
              Tu logística en un solo portal. Logística nacional e internacional
              conectada directamente a tu tienda, con carga manual disponible.
            </p>
            <a href="/portal/login" className="btn btn-primary btn-lg">
              Conectá tu tienda <ArrowRight size={16} />
            </a>
            <button className="btn btn-ghost btn-lg" onClick={onCotizarClick}>Cotizá un envío</button>
          </div>
        </div>
      </div>
    </section>
  );
}

window.TauroLogo = TauroLogo;
window.Nav = Nav;
window.Hero = Hero;
window.ArrowRight = ArrowRight;
window.ArrowDown = ArrowDown;
window.IconShip = IconShip;
window.IconPlane = IconPlane;
window.IconTruck = IconTruck;
window.IconWarehouse = IconWarehouse;
window.IconShield = IconShield;
