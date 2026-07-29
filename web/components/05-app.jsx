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

/* ============================================================
   CONTACTO FLOTANTE — mail, WhatsApp e Instagram.
   Sólo aparece el botón que tenga dato cargado: así nunca se
   publica un link roto. Para encender WhatsApp o Instagram,
   completá acá abajo y listo.
   ============================================================ */
const CONTACTO = {
  email: "taurosolutionsar@gmail.com",
  whatsapp: "",     // ej: "5491130092804" (país + área + número, sin + ni espacios)
  instagram: "",    // ej: "taurosolutions.ar" (sin la arroba)
};

function ContactFab() {
  const botones = [
    CONTACTO.email && {
      id: "mail",
      href: `mailto:${CONTACTO.email}`,
      titulo: "Escribinos por mail",
      icono: (
        <svg width="25" height="25" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <rect x="3" y="5" width="18" height="14" rx="2"/>
          <path d="M3 7 L12 13 L21 7"/>
        </svg>
      ),
    },
    CONTACTO.whatsapp && {
      id: "whatsapp",
      href: `https://wa.me/${CONTACTO.whatsapp}`,
      titulo: "Escribinos por WhatsApp",
      icono: (
        <svg width="25" height="25" viewBox="0 0 24 24" fill="currentColor">
          <path d="M17.47 14.38c-.3-.15-1.76-.87-2.03-.97-.27-.1-.47-.15-.67.15-.2.3-.77.97-.94 1.17-.17.2-.35.22-.65.07-.3-.15-1.25-.46-2.38-1.47-.88-.78-1.48-1.75-1.65-2.05-.17-.3-.02-.46.13-.6.13-.14.3-.35.45-.52.15-.17.2-.3.3-.5.1-.2.05-.37-.02-.52-.08-.15-.67-1.61-.92-2.2-.24-.58-.48-.5-.67-.51h-.57c-.2 0-.52.07-.79.37-.27.3-1.04 1.02-1.04 2.48s1.07 2.88 1.22 3.08c.15.2 2.1 3.2 5.08 4.49.71.3 1.26.49 1.69.63.71.22 1.36.19 1.87.12.57-.09 1.76-.72 2-1.41.25-.7.25-1.29.17-1.41-.07-.13-.27-.2-.57-.35z"/>
          <path d="M12.04 2C6.58 2 2.13 6.45 2.13 11.91c0 1.75.46 3.45 1.32 4.95L2 22l5.25-1.38a9.86 9.86 0 0 0 4.79 1.22h.01c5.46 0 9.91-4.45 9.91-9.91C21.96 6.45 17.5 2 12.04 2zm0 18.02h-.01a8.2 8.2 0 0 1-4.18-1.15l-.3-.18-3.12.82.83-3.04-.2-.31a8.18 8.18 0 0 1-1.25-4.35c0-4.54 3.7-8.23 8.24-8.23 2.2 0 4.27.86 5.82 2.42a8.18 8.18 0 0 1 2.41 5.82c0 4.54-3.69 8.2-8.24 8.2z"/>
        </svg>
      ),
    },
    CONTACTO.instagram && {
      id: "instagram",
      href: `https://instagram.com/${CONTACTO.instagram}`,
      titulo: "Seguinos en Instagram",
      icono: (
        <svg width="25" height="25" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <rect x="3" y="3" width="18" height="18" rx="5"/>
          <circle cx="12" cy="12" r="3.6"/>
          <circle cx="17.2" cy="6.8" r="1.1" fill="currentColor" stroke="none"/>
        </svg>
      ),
    },
  ].filter(Boolean);

  if (!botones.length) return null;

  return (
    <div className="tweb-fab-grupo">
      {botones.map((b) => (
        <a key={b.id}
           href={b.href}
           aria-label={b.titulo}
           title={b.titulo}
           target={b.id === "mail" ? undefined : "_blank"}
           rel={b.id === "mail" ? undefined : "noopener noreferrer"}
           className="tweb-fab"
        >
          {b.icono}
        </a>
      ))}
    </div>
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
