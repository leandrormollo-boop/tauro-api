/* global React */

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "palette": ["#a78bfa", "#0c0a14", "#f4f5f7"],
  "fontDisplay": "Space Grotesk",
  "fontBody": "Inter",
  "density": "regular",
  "heroVariant": "split",
  "headline": "Logística sin fronteras, decisiones en tiempo real.",
  "showWhatsapp": true
}/*EDITMODE-END*/;

function App() {
  const t = TWEAK_DEFAULTS;
  const quoteRef = React.useRef(null);

  // apply palette + fonts to CSS vars
  React.useEffect(() => {
    const r = document.documentElement;
    const [accent, bg, fg] = t.palette;
    r.style.setProperty("--accent", accent);
    r.style.setProperty("--accent-soft", lightenHex(accent, 0.25));
    r.style.setProperty("--accent-deep", darkenHex(accent, 0.2));
    r.style.setProperty("--accent-glow", hexToRgba(accent, 0.18));
    r.style.setProperty("--bg", bg);
    r.style.setProperty("--bg-elev", lightenHex(bg, 0.04));
    r.style.setProperty("--bg-elev-2", lightenHex(bg, 0.08));
    r.style.setProperty("--line", lightenHex(bg, 0.12));
    r.style.setProperty("--line-soft", lightenHex(bg, 0.07));
    r.style.setProperty("--fg", fg);
    r.style.setProperty("--font-display", `"${t.fontDisplay}", system-ui, sans-serif`);
    r.style.setProperty("--font-body", `"${t.fontBody}", system-ui, sans-serif`);
  }, [t.palette, t.fontDisplay, t.fontBody]);

  React.useEffect(() => {
    document.body.classList.remove("density-compact", "density-spacious");
    if (t.density === "compact") document.body.classList.add("density-compact");
    else if (t.density === "spacious") document.body.classList.add("density-spacious");
  }, [t.density]);

  const scrollToQuote = () => {
    document.querySelector("#servicios")?.scrollIntoView({ behavior: "smooth", block: "start" });
    // actually scroll to top hero quote
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  return (
    <>
      <Nav onCotizarClick={scrollToQuote} />
      <Hero variant={t.heroVariant} onCotizarClick={scrollToQuote} t={t} />
      <Services />
      <Tracking />
      <Process />
      <WhyUs />
      <Industries />
      <ContactCTA onCotizarClick={scrollToQuote} />
      <Footer />

      {t.showWhatsapp && <ContactFab />}
    </>
  );
}

function ContactFab() {
  return (
    <a href="mailto:taurosolutionsar@gmail.com"
       aria-label="Escribinos"
       title="Escribinos"
       style={{
         position: "fixed", bottom: 24, left: 24,
         width: 56, height: 56, borderRadius: "50%",
         background: "var(--accent)", color: "#fff",
         display: "grid", placeItems: "center",
         boxShadow: "0 10px 30px var(--accent-glow), 0 4px 14px rgba(0,0,0,0.3)",
         zIndex: 40,
         transition: "transform .2s",
       }}
       onMouseEnter={(e) => e.currentTarget.style.transform = "scale(1.08)"}
       onMouseLeave={(e) => e.currentTarget.style.transform = "scale(1)"}
    >
      <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="5" width="18" height="14" rx="2"/>
        <path d="M3 7 L12 13 L21 7"/>
      </svg>
    </a>
  );
}

/* ---------- color utilities ---------- */
function hexToRgba(hex, a) {
  const h = hex.replace("#", "");
  const r = parseInt(h.substr(0, 2), 16);
  const g = parseInt(h.substr(2, 2), 16);
  const b = parseInt(h.substr(4, 2), 16);
  return `rgba(${r}, ${g}, ${b}, ${a})`;
}
function lightenHex(hex, amt) {
  const h = hex.replace("#", "");
  let r = parseInt(h.substr(0, 2), 16);
  let g = parseInt(h.substr(2, 2), 16);
  let b = parseInt(h.substr(4, 2), 16);
  r = Math.min(255, Math.round(r + (255 - r) * amt));
  g = Math.min(255, Math.round(g + (255 - g) * amt));
  b = Math.min(255, Math.round(b + (255 - b) * amt));
  return `#${r.toString(16).padStart(2,"0")}${g.toString(16).padStart(2,"0")}${b.toString(16).padStart(2,"0")}`;
}
function darkenHex(hex, amt) {
  const h = hex.replace("#", "");
  let r = parseInt(h.substr(0, 2), 16);
  let g = parseInt(h.substr(2, 2), 16);
  let b = parseInt(h.substr(4, 2), 16);
  r = Math.max(0, Math.round(r * (1 - amt)));
  g = Math.max(0, Math.round(g * (1 - amt)));
  b = Math.max(0, Math.round(b * (1 - amt)));
  return `#${r.toString(16).padStart(2,"0")}${g.toString(16).padStart(2,"0")}${b.toString(16).padStart(2,"0")}`;
}

  ReactDOM.createRoot(document.getElementById("root")).render(<App />);
  // Enable entry animations only after first paint
  requestAnimationFrame(() => {
    requestAnimationFrame(() => document.body.classList.add("js-anim"));
  });
