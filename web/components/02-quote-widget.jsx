/* global React */
const { useState: useStateQ } = React;

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
            className="btn btn-primary"
            style={{ width: "100%", padding: 14, fontSize: 14 }}
          >
            {step === "calculating" ? (
              <><Spinner /> Consultando FedEx…</>
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

          <button className="btn btn-primary" style={{ width: "100%" }}
            onClick={() => window.location.href = "mailto:info@taurosolutions.ar?subject=Cotización de envío"}>
            Contactar para reservar <ArrowRight size={14}/>
          </button>
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

function SelectField({ label, value, onChange, options }) {
  return (
    <label style={{ display: "block" }}>
      <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--fg-3)", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 6 }}>
        {label}
      </div>
      <select
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
          appearance: "none",
          backgroundImage: `url("data:image/svg+xml,%3Csvg width='10' height='6' viewBox='0 0 10 6' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%237a828c' stroke-width='1.5' fill='none' stroke-linecap='round'/%3E%3C/svg%3E")`,
          backgroundRepeat: "no-repeat",
          backgroundPosition: "right 12px center",
          paddingRight: 32,
          boxSizing: "border-box",
        }}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
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

function Spinner() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" style={{ animation: "spin 1s linear infinite" }}>
      <circle cx="7" cy="7" r="5.5" stroke="currentColor" strokeWidth="1.5" fill="none" opacity="0.3"/>
      <path d="M12.5 7 A5.5 5.5 0 0 0 7 1.5" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round"/>
    </svg>
  );
}

window.QuoteWidget = QuoteWidget;
