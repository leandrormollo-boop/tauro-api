/* global React */
const { useState, useEffect, useRef } = React;

/* ============================================================
   LOGO — Original mark for Tauro Solutions (bull horns / arrow)
   ============================================================ */
function TauroLogo({ size = 32, color = "currentColor" }) {
  return (
    <svg width={size} height={size} viewBox="0 0 40 40" fill="none" aria-label="Tauro Solutions">
      {/* Stylized "T" with horn-like arms — original interpretation */}
      <path
        d="M6 8 L20 8 L20 32 L16 32 L16 12 L10 12 L10 18 L6 18 Z"
        fill={color}
      />
      <path
        d="M22 8 L36 8 L36 18 L32 18 L32 12 L26 12 L26 16 L30 16 L30 20 L22 20 Z"
        fill={color}
      />
      <circle cx="33" cy="32" r="2.5" fill={color} />
    </svg>
  );
}

/* ============================================================
   NAV
   ============================================================ */
function Nav({ onCotizarClick }) {
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
          <span>Tauro<span style={{ color: "var(--fg-3)", fontWeight: 400, marginLeft: 4 }}>solutions</span></span>
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
            Iniciar sesión
          </a>
          <button className="btn btn-primary" onClick={onCotizarClick} style={{ fontSize: 13, padding: "10px 18px" }}>
            Cotizar envío
            <ArrowRight size={14} />
          </button>
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
              Envíos a todo el mundo vía FedEx
            </div>
            <h1 className="fade-up d1">
              Logística sin <span className="accent">fronteras</span>,<br/>
              decisiones <span className="accent">en tiempo real</span>.
            </h1>
            <p className="lead fade-up d2">
              Movemos tu carga por aire, mar y tierra con visibilidad total.
              Cotizá, despachá y trackeá todo desde un solo lugar — pensado para
              empresas argentinas que exportan e importan.
            </p>
            <div className="hero-actions fade-up d3">
              <button className="btn btn-primary btn-lg" onClick={onCotizarClick}>
                Cotizar mi envío
                <ArrowRight size={16} />
              </button>
              <a href="#proceso" className="btn btn-ghost btn-lg">
                Cómo funciona
              </a>
            </div>
            <div className="hero-meta fade-up d4">
              <div className="hero-meta-item">
                <div className="num">FedEx</div>
                <div className="lbl">Partner de envíos</div>
              </div>
              <div className="hero-meta-item">
                <div className="num">200+</div>
                <div className="lbl">Destinos</div>
              </div>
              <div className="hero-meta-item">
                <div className="num">24hs</div>
                <div className="lbl">Respuesta</div>
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
          <span className="chip-dot pulse"></span> Envíos a todo el mundo vía FedEx
        </div>
        <h1 className="fade-up d1" style={{ fontSize: "clamp(56px, 9vw, 120px)" }}>
          Tu carga,<br/>
          <span className="accent">a cualquier puerto</span>
          <br/>del mundo.
        </h1>
        <p className="lead fade-up d2" style={{ margin: "32px auto 40px", fontSize: 19 }}>
          Soluciones logísticas internacionales para empresas y emprendedores argentinos.
          Aire, mar y tierra — con seguimiento en tiempo real.
        </p>
        <div className="hero-actions fade-up d3" style={{ justifyContent: "center" }}>
          <button className="btn btn-primary btn-lg" onClick={onCotizarClick}>
            Cotizar mi envío <ArrowRight size={16} />
          </button>
          <a href="#proceso" className="btn btn-ghost btn-lg">Ver demo</a>
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
            Movemos lo que tu<br/>
            negocio necesita,<br/>
            <em style={{ color: "var(--accent)", fontWeight: 500 }}>donde sea.</em>
          </h1>
          <div className="fade-up d2" style={{ display: "flex", gap: 64, marginTop: 80, alignItems: "flex-end", flexWrap: "wrap" }}>
            <p style={{ maxWidth: 420, color: "var(--fg-2)", fontSize: 17, lineHeight: 1.6, margin: 0 }}>
              Logística internacional para empresas argentinas. Cotización instantánea,
              despacho aduanero, tracking 24/7.
            </p>
            <button className="btn btn-primary btn-lg" onClick={onCotizarClick}>
              Empezar ahora <ArrowRight size={16} />
            </button>
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
