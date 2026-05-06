/* global React, useTweaks, TweaksPanel, TweakSection, TweakSlider, TweakToggle, TweakRadio, TweakSelect, TweakColor */

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "palette": ["#a78bfa", "#0c0a14", "#f4f5f7"],
  "fontDisplay": "Space Grotesk",
  "fontBody": "Inter",
  "density": "regular",
  "heroVariant": "split",
  "headline": "Logística sin fronteras, decisiones en tiempo real.",
  "showWhatsapp": true
}/*EDITMODE-END*/;

const PALETTES = [
  ["#ff2d6b", "#0a0e12", "#f4f5f7"], // magenta original
  ["#3b82f6", "#0a1018", "#f4f5f7"], // azul corporativo
  ["#22c55e", "#0a0f0c", "#f4f5f7"], // verde logística
  ["#f59e0b", "#0e0a05", "#f5f3ee"], // amber cargo
  ["#a78bfa", "#0c0a14", "#f4f5f7"], // violeta tech
];

const FONT_PAIRS = {
  "Space Grotesk": "Space Grotesk",
  "Bricolage Grotesque": "Bricolage Grotesque",
  "Instrument Serif": "Instrument Serif",
  "DM Sans": "DM Sans",
};
const BODY_FONTS = {
  "Inter": "Inter",
  "DM Sans": "DM Sans",
  "Geist": "Geist",
};

function App() {
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);
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
      <Testimonial />
      <ContactCTA onCotizarClick={scrollToQuote} />
      <Footer />

      {t.showWhatsapp && <WhatsappFab />}

      <TweaksPanel>
        <TweakSection label="Tema" />
        <TweakColor
          label="Paleta"
          value={t.palette}
          options={PALETTES}
          onChange={(v) => setTweak("palette", v)}
        />

        <TweakSection label="Tipografía" />
        <TweakSelect
          label="Display"
          value={t.fontDisplay}
          options={Object.keys(FONT_PAIRS)}
          onChange={(v) => setTweak("fontDisplay", v)}
        />
        <TweakSelect
          label="Body"
          value={t.fontBody}
          options={Object.keys(BODY_FONTS)}
          onChange={(v) => setTweak("fontBody", v)}
        />

        <TweakSection label="Layout" />
        <TweakRadio
          label="Densidad"
          value={t.density}
          options={["compact", "regular", "spacious"]}
          onChange={(v) => setTweak("density", v)}
        />
        <TweakSelect
          label="Hero variant"
          value={t.heroVariant}
          options={["split", "centered", "minimal"]}
          onChange={(v) => setTweak("heroVariant", v)}
        />

        <TweakSection label="Componentes" />
        <TweakToggle
          label="Botón de WhatsApp"
          value={t.showWhatsapp}
          onChange={(v) => setTweak("showWhatsapp", v)}
        />
      </TweaksPanel>
    </>
  );
}

function WhatsappFab() {
  return (
    <a href="#"
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
      <svg width="26" height="26" viewBox="0 0 24 24" fill="currentColor">
        <path d="M17.5 14.4c-.3-.1-1.7-.8-2-.9-.3-.1-.5-.1-.7.1-.2.3-.7.9-.9 1.1-.2.2-.3.2-.6.1-.3-.1-1.2-.4-2.3-1.4-.8-.8-1.4-1.7-1.6-2-.2-.3 0-.5.1-.6.1-.1.3-.3.4-.5.1-.2.2-.3.3-.5.1-.2 0-.4 0-.5-.1-.1-.7-1.6-.9-2.2-.2-.6-.5-.5-.7-.5h-.6c-.2 0-.5.1-.8.4-.3.3-1 1-1 2.4 0 1.4 1 2.8 1.2 3 .1.2 2 3.1 4.9 4.4 1.8.7 2.4.8 3.3.6.5-.1 1.7-.7 1.9-1.3.2-.7.2-1.2.1-1.3-.1-.1-.3-.2-.6-.3z"/>
        <path d="M12 2C6.5 2 2 6.5 2 12c0 1.7.4 3.4 1.3 4.9L2 22l5.2-1.3c1.5.8 3.1 1.2 4.8 1.2 5.5 0 10-4.5 10-10S17.5 2 12 2zm0 18c-1.5 0-3-.4-4.3-1.2l-.3-.2-3.1.8.8-3-.2-.3C4.1 14.9 3.7 13.4 3.7 12 3.7 7.4 7.4 3.7 12 3.7s8.3 3.7 8.3 8.3-3.7 8-8.3 8z"/>
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
