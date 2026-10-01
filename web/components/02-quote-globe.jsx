/* Same globe as the portal. React owns quote values; the map only reads this local bridge. */
function PublicQuoteGlobe({ origin, destination, countries, compact = false, busy = false }) {
  const rootRef = React.useRef(null);
  const previous = React.useRef({ origin, destination, countries });
  React.useEffect(() => {
    const root = rootRef.current;
    try { window.TauroQuoteMap?.attach(root); } catch (_) { /* Quoting remains available. */ }
    return () => window.TauroQuoteMap?.dispose(root);
  }, []);
  React.useEffect(() => {
    const root = rootRef.current;
    const changed = [];
    if (previous.current.origin !== origin) changed.push("origen_pais");
    if (previous.current.destination !== destination) changed.push("destino_pais");
    if (!changed.length && previous.current.countries !== countries) changed.push("destino_pais");
    changed.forEach(name => root.querySelector(`[name="${name}"]`).dispatchEvent(new Event("change", { bubbles: true })));
    previous.current = { origin, destination, countries };
  }, [origin, destination, countries]);
  return (
    <div ref={rootRef} className={`quote-screen unified-quote public-quote-map${compact ? " is-result" : ""}${busy ? " is-loading" : ""}`} data-quote-active="internacional" data-map-countries-only>
      {/* Hidden presentation values never submit or change the quote. */}
      <form hidden aria-hidden="true" data-unified-form="internacional" onSubmit={event => event.preventDefault()}>
        <select name="origen_pais" value={origin} onChange={() => {}} tabIndex={-1}>
          {countries.map(country => <option key={country.value} value={country.value}>{country.label}</option>)}
        </select>
        <select name="destino_pais" value={destination} onChange={() => {}} tabIndex={-1}>
          {countries.map(country => <option key={country.value} value={country.value}>{country.label}</option>)}
        </select>
        <input name="origen_ciudad" type="hidden" value="" readOnly />
        <input name="destino_ciudad_internacional" type="hidden" value="" readOnly />
      </form>
<aside className="quote-route-map" data-quote-map aria-label="Mapa de origen y destino">
  <div className="quote-map-heading"><span className="quote-map-live-dot" aria-hidden="true"></span></div>
  <div className="quote-map-art">

    <canvas data-map-canvas aria-hidden={compact} tabIndex={compact ? -1 : 0} role="img" aria-roledescription="globo interactivo" aria-label="Mapa del mundo. Arrastrá para girar. Con teclado, usá las flechas para girar, más y menos para acercar y alejar, e Inicio para volver a la ruta."></canvas>
    <svg className="quote-map-glint" data-map-glint aria-hidden="true" focusable="false" preserveAspectRatio="none">
      <defs>
        <linearGradient data-map-glint-gradient x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stopColor="#8b6bf7" stopOpacity="0"/>
          <stop offset=".37" stopColor="#7c4dff" stopOpacity=".25"/>
          <stop offset=".465" stopColor="#aa86ff" stopOpacity=".65"/>
          <stop offset=".495" stopColor="#f6f0ff"/>
          <stop offset=".51" stopColor="#e9ddff" stopOpacity=".85"/>
          <stop offset=".54" stopColor="#8b6bf7" stopOpacity=".65"/>
          <stop offset=".66" stopColor="#8b6bf7" stopOpacity=".08"/>
          <stop offset="1" stopColor="#8b6bf7" stopOpacity="0"/>
        </linearGradient>
        <mask data-map-glint-mask maskUnits="userSpaceOnUse" x="0" y="0" width="100%" height="100%">
          <path data-map-glint-land fill="white"/>
          <path data-map-glint-route fill="none" stroke="white" strokeWidth="2" strokeLinecap="round"/>
        </mask>
      </defs>
      <g className="quote-map-glint-layer" data-map-glint-layer>
        <rect className="quote-map-glint-band" data-map-glint-band x="-55%" y="-30%" width="55%" height="160%"/>
      </g>
      <g className="quote-map-current-glow">
        <path className="quote-map-current is-trail" data-map-current pathLength="100"/>
        <path className="quote-map-current is-core" data-map-current pathLength="100"/>
      </g>
    </svg>
    <div className="quote-map-explore-bar">
      <span className="quote-map-drag-hint" data-map-drag-hint>Arrastrá para girar</span>
      <div className="quote-map-controls" role="group" aria-label="Vista del mapa">
        <button type="button" data-map-zoom="out" aria-label="Alejar mapa" title="Alejar">−</button>
        <button type="button" data-map-zoom="in" aria-label="Acercar mapa" title="Acercar">+</button>
        <button type="button" data-map-reset aria-label="Volver a la ruta" title="Volver a la ruta">↺</button>
      </div>
    </div>
  </div>
  <div className="quote-map-itinerary" aria-live="polite" aria-atomic="true">
    <div className="quote-map-endpoint"><span className="quote-map-pin is-origin" aria-hidden="true"></span><div><small>ORIGEN</small><strong data-map-origin>Elegí origen</strong><span data-map-origin-country></span></div></div>
    <span className="quote-map-connector" aria-hidden="true">↗</span>
    <div className="quote-map-endpoint"><span className="quote-map-pin" aria-hidden="true"></span><div><small>DESTINO</small><strong data-map-destination>Elegí destino</strong><span data-map-destination-country></span></div></div>
  </div>
  <div className="quote-map-footer"><span data-map-caption>Ruta orientativa entre países.</span><button type="button" className="quote-map-toggle" aria-pressed="false" data-map-toggle>Ocultar mapa</button></div>
  <span className="quote-map-credit">Cartografía · Natural Earth</span>
</aside>

    </div>
  );
}
