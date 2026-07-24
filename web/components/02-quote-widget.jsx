/* global React */
const { useState: useStateQ, useRef: useRefQ, useEffect: useEffectQ } = React;

const API_URL = window.TAURO_API_URL ?? "http://localhost:8000";

const DESTINOS = [
  { value: "US", label: "Estados Unidos" },
  { value: "BR", label: "Brasil" },
  { value: "CL", label: "Chile" },
  { value: "UY", label: "Uruguay" },
  { value: "MX", label: "México" },
  { value: "ES", label: "España" },
];

function QuoteWidget({ compact = false }) {
  const [destino, setDestino] = useStateQ("US");
  const [peso, setPeso] = useStateQ(5);
  const [largo, setLargo] = useStateQ(30);
  const [ancho, setAncho] = useStateQ(20);
  const [alto, setAlto] = useStateQ(10);
  const [valor, setValor] = useStateQ(100);
  const [step, setStep] = useStateQ("form");
  const [result, setResult] = useStateQ(null);
  const [error, setError] = useStateQ(null);

  const calculate = async () => {
    setStep("calculating");
    setError(null);
    try {
      const resp = await fetch(`${API_URL}/cotizar-web`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          destino_pais: destino,
          peso_kg: parseFloat(peso) || 1,
          largo_cm: parseFloat(largo) || 30,
          ancho_cm: parseFloat(ancho) || 20,
          alto_cm: parseFloat(alto) || 10,
          valor_declarado_usd: parseFloat(valor) || 100,
        }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "Error al cotizar");
      setResult(data);
      setStep("result");
    } catch (e) {
      setError(e.message);
      setStep("form");
    }
  };

  const reset = () => { setStep("form"); setResult(null); setError(null); };

  return (
    <div style={{
      background: "var(--bg-elev)",
      border: "1px solid var(--line)",
      borderRadius: "var(--radius-lg)",
      padding: compact ? 24 : 28,
      position: "relative",
      overflow: "hidden",
    }}>
      <div style={{
        position: "absolute", top: 0, right: 0,
        width: 160, height: 160,
        background: "radial-gradient(circle at top right, var(--accent-glow), transparent 70%)",
        pointerEvents: "none",
      }}/>

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20 }}>
        <div>
          <div className="eyebrow" style={{ marginBottom: 6 }}>Cotizador instantáneo</div>
          <div style={{ fontFamily: "var(--font-display)", fontSize: 20, fontWeight: 600 }}>
            Calculá tu envío
          </div>
        </div>
        {step === "result" && (
          <button onClick={reset} className="btn-link" style={{ fontSize: 12, fontFamily: "var(--font-mono)" }}>
            ← Nueva cotización
          </button>
        )}
      </div>

      {step !== "result" && (
        <>
          <div style={{ marginBottom: 12 }}>
            <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--fg-3)", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 6 }}>
              Origen
            </div>
            <div style={{ padding: "11px 12px", background: "var(--bg)", border: "1px solid var(--line-soft)", borderRadius: 8, color: "var(--fg-2)", fontSize: 14 }}>
              Buenos Aires, Argentina
            </div>
          </div>

          <div style={{ marginBottom: 12 }}>
            <SelectField label="Destino" value={destino} onChange={setDestino} options={DESTINOS} />
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: 12 }}>
            <Field label="Peso (kg)" value={peso} onChange={setPeso} type="number" />
            <Field label="Valor declarado (USD)" value={valor} onChange={setValor} type="number" />
          </div>

          <div style={{ marginBottom: 6, fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--fg-3)", textTransform: "uppercase", letterSpacing: "0.08em" }}>
            Dimensiones (cm)
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8, marginBottom: 20 }}>
            <Field label="Largo" value={largo} onChange={setLargo} type="number" />
            <Field label="Ancho" value={ancho} onChange={setAncho} type="number" />
            <Field label="Alto" value={alto} onChange={setAlto} type="number" />
          </div>

          {error && (
            <div style={{ marginBottom: 12, padding: "10px 12px", background: "rgba(255,80,80,0.1)", border: "1px solid rgba(255,80,80,0.3)", borderRadius: 8, fontSize: 13, color: "#ff6b6b" }}>
              {error}
            </div>
          )}

          <button
            onClick={calculate}
            disabled={step === "calculating"}
            className={`btn btn-primary ${step === "calculating" ? "btn-loading" : ""}`}
            style={{ width: "100%", padding: 14, fontSize: 14 }}
          >
            {step === "calculating" ? (
              <TauroQuoteLoader />
            ) : (
              <>Obtener cotización <ArrowRight size={14} /></>
            )}
          </button>

          <div style={{ marginTop: 14, fontSize: 11, color: "var(--fg-3)", fontFamily: "var(--font-mono)", textAlign: "center" }}>
            Precio real FedEx · sin compromiso · respuesta en segundos
          </div>
        </>
      )}

      {step === "result" && result && (
        <div className="fade-up">
          <div style={{ display: "flex", alignItems: "baseline", gap: 12, marginBottom: 4 }}>
            <div style={{ fontFamily: "var(--font-display)", fontSize: 48, fontWeight: 600, letterSpacing: "-0.03em", lineHeight: 1 }}>
              ${result.precio_usd.toLocaleString("es-AR")}
            </div>
            <div style={{ fontSize: 14, color: "var(--fg-3)" }}>USD</div>
          </div>
          <div style={{ color: "var(--fg-2)", marginBottom: 6, fontSize: 14 }}>
            ARS ${result.precio_ars.toLocaleString("es-AR")} · {peso}kg
          </div>
          <div style={{ color: "var(--fg-3)", marginBottom: 24, fontSize: 12, fontFamily: "var(--font-mono)" }}>
            {result.servicio}
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 20 }}>
            <ResultStat label="Tránsito" value={`${result.dias_estimados} días`} />
            <ResultStat label="Destino" value={DESTINOS.find(d => d.value === destino)?.label || destino} />
          </div>

          <div style={{ background: "var(--bg)", border: "1px solid var(--line-soft)", borderRadius: 10, padding: 14, fontSize: 13, fontFamily: "var(--font-mono)", color: "var(--fg-2)", marginBottom: 16 }}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
              <span>Buenos Aires</span>
              <span style={{ color: "var(--accent)" }}>→</span>
              <span>{DESTINOS.find(d => d.value === destino)?.label || destino}</span>
            </div>
            <div style={{ height: 1, background: "linear-gradient(to right, var(--accent), transparent)" }}/>
          </div>

          <a className="btn btn-primary" style={{ width: "100%" }} href="/portal/login">
            Crear este envío en el portal <ArrowRight size={14}/>
          </a>
          <a className="btn btn-ghost" style={{ width: "100%", marginTop: 10 }}
             href="mailto:taurosolutionsar@gmail.com?subject=Quiero%20una%20cuenta%20en%20el%20portal%20Tauro">
            Todavía no tengo cuenta
          </a>
        </div>
      )}
    </div>
  );
}

function Field({ label, value, onChange, type = "text" }) {
  return (
    <label style={{ display: "block" }}>
      <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--fg-3)", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 6 }}>
        {label}
      </div>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        style={{
          width: "100%",
          padding: "11px 12px",
          background: "var(--bg)",
          border: "1px solid var(--line-soft)",
          borderRadius: 8,
          color: "var(--fg)",
          fontSize: 14,
          outline: "none",
          transition: "border-color .15s",
          boxSizing: "border-box",
        }}
        onFocus={(e) => (e.target.style.borderColor = "var(--accent)")}
        onBlur={(e) => (e.target.style.borderColor = "var(--line-soft)")}
      />
    </label>
  );
}

/* Desplegable propio de TAURO — mismo lenguaje visual que el portal:
   botón con caret animado + panel flotante oscuro con tilde violeta.
   Nada del picker nativo del navegador. */
function SelectField({ label, value, onChange, options }) {
  const [open, setOpen] = useStateQ(false);
  const boxRef = useRefQ(null);
  const seleccionada = options.find((o) => o.value === value) || options[0];

  useEffectQ(() => {
    if (!open) return;
    const cerrar = (e) => {
      if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", cerrar);
    return () => document.removeEventListener("mousedown", cerrar);
  }, [open]);

  const onKey = (e) => {
    const i = options.findIndex((o) => o.value === value);
    if (e.key === "ArrowDown") { e.preventDefault(); onChange(options[Math.min(i + 1, options.length - 1)].value); }
    else if (e.key === "ArrowUp") { e.preventDefault(); onChange(options[Math.max(i - 1, 0)].value); }
    else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setOpen((o) => !o); }
    else if (e.key === "Escape") setOpen(false);
  };

  return (
    <label style={{ display: "block" }}>
      <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--fg-3)", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 6 }}>
        {label}
      </div>
      <div ref={boxRef} style={{ position: "relative" }}>
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          onKeyDown={onKey}
          className="tweb-select-btn"
          style={{
            width: "100%",
            display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10,
            padding: "11px 12px",
            background: "var(--bg)",
            border: `1px solid ${open ? "var(--accent)" : "var(--line-soft)"}`,
            boxShadow: open ? "0 0 0 3px var(--accent-glow)" : "none",
            borderRadius: 8,
            color: "var(--fg)",
            fontSize: 14,
            cursor: "pointer",
            textAlign: "left",
            transition: "border-color .15s, box-shadow .15s",
          }}
        >
          <span>{seleccionada ? seleccionada.label : "Seleccionar"}</span>
          <svg width="11" height="7" viewBox="0 0 10 6" fill="none" aria-hidden="true"
               style={{ color: open ? "var(--accent-soft)" : "var(--fg-3)", transform: open ? "rotate(180deg)" : "none", transition: "transform .22s cubic-bezier(.2,.7,.3,1), color .15s", flexShrink: 0 }}>
            <path d="M1 1l4 4 4-4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
        </button>

        <div style={{
          position: "absolute", top: "calc(100% + 6px)", left: 0, right: 0, zIndex: 40,
          padding: 6,
          background: "var(--bg-elev-2)",
          border: "1px solid var(--line)",
          borderRadius: 12,
          boxShadow: "0 18px 50px rgba(0,0,0,.55)",
          opacity: open ? 1 : 0,
          transform: open ? "translateY(0) scale(1)" : "translateY(-6px) scale(.985)",
          pointerEvents: open ? "auto" : "none",
          transition: "opacity .16s ease, transform .18s cubic-bezier(.2,.7,.3,1)",
        }}>
          {options.map((o) => {
            const sel = o.value === value;
            return (
              <div
                key={o.value}
                onClick={() => { onChange(o.value); setOpen(false); }}
                className="tweb-select-opt"
                style={{
                  display: "flex", alignItems: "center", gap: 9,
                  padding: "9px 11px", borderRadius: 8, fontSize: 13.5, cursor: "pointer",
                  color: sel ? "var(--fg)" : "var(--fg-2)",
                  background: sel ? "var(--accent-glow)" : "transparent",
                }}
              >
                <span style={{ width: 14, flexShrink: 0, color: "var(--accent-soft)", fontSize: 11, opacity: sel ? 1 : 0 }}>✓</span>
                <span>{o.label}</span>
              </div>
            );
          })}
        </div>
      </div>
    </label>
  );
}

function ResultStat({ label, value }) {
  return (
    <div style={{ background: "var(--bg)", border: "1px solid var(--line-soft)", borderRadius: 10, padding: "12px 14px" }}>
      <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--fg-3)", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 4 }}>
        {label}
      </div>
      <div style={{ fontSize: 16, fontWeight: 500 }}>{value}</div>
    </div>
  );
}

/* Loader "Ultracode": la matriz de puntitos violeta que se va cargando
   (el formato del slider de esfuerzo de Claude). Sin animalitos. */
function TauroQuoteLoader() {
  return (
    <span className="tauro-quote-loader" role="status" aria-live="polite">
      <span className="tauro-loader-track" aria-hidden="true">
        <span className="tauro-loader-flame" />
      </span>
      <span className="tauro-loader-label">Consultando FedEx...</span>
    </span>
  );
}

window.QuoteWidget = QuoteWidget;
