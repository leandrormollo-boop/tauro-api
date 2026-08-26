/* global React */
const { useState: useStateQ, useRef: useRefQ, useEffect: useEffectQ } = React;
let selectFieldSeq = 0;

// Same-origin: la web SIEMPRE se sirve desde la propia API. El viejo fallback
// a http://localhost:8000 quedaba cross-origin en producción y la CSP
// (connect-src 'self') lo bloquearía en silencio. "" = mismo dominio.
const API_URL = window.TAURO_API_URL || "";

// Mismo contrato que `servicios/numeros_humanos.py`: la coma y el punto
// pueden ser decimales; para importes, una agrupación de tres cifras también
// puede ser miles. El servidor vuelve a validar: esto sólo evita que el
// navegador transforme `5,5` en `5` antes de enviar.
function parseHumanNumber(value, { money = false } = {}) {
  let text = String(value ?? "").trim();
  if (!text || !/^[+-]?[0-9.,]+$/.test(text)) return NaN;
  let sign = "";
  if (text[0] === "+" || text[0] === "-") {
    sign = text[0];
    text = text.slice(1);
  }
  const validGroups = (groups) => (
    groups.length >= 2 && /^[0-9]{1,3}$/.test(groups[0]) &&
    groups.slice(1).every((group) => /^[0-9]{3}$/.test(group))
  );
  const dots = (text.match(/\./g) || []).length;
  const commas = (text.match(/,/g) || []).length;
  let canonical;

  if (dots && commas) {
    const decimal = text.lastIndexOf(".") > text.lastIndexOf(",") ? "." : ",";
    const thousands = decimal === "." ? "," : ".";
    if (text.split(decimal).length !== 2) return NaN;
    const [integer, fraction] = text.split(decimal);
    const groups = integer.split(thousands);
    if (!validGroups(groups) || !/^[0-9]+$/.test(fraction)) return NaN;
    canonical = `${groups.join("")}.${fraction}`;
  } else if (dots || commas) {
    const separator = dots ? "." : ",";
    const groups = text.split(separator);
    if (groups.length > 2) {
      if (!validGroups(groups)) return NaN;
      canonical = groups.join("");
    } else {
      const [integer, fraction] = groups;
      if (!/^[0-9]+$/.test(integer) || !/^[0-9]+$/.test(fraction)) return NaN;
      if (fraction.length === 3 && Number(integer) !== 0) {
        if (!money || integer.length > 3) return NaN;
        canonical = integer + fraction;
      } else {
        canonical = `${integer}.${fraction}`;
      }
    }
  } else {
    canonical = text;
  }
  const parsed = Number(sign + canonical);
  return Number.isFinite(parsed) ? parsed : NaN;
}

function normalizeSearchText(value) {
  const text = String(value ?? "").toLocaleLowerCase("es");
  return text.normalize
    ? text.normalize("NFD").replace(/[\u0300-\u036f]/g, "")
    : text;
}

function rankedSelectOptions(options, query) {
  const needle = normalizeSearchText(query).trim();
  return options
    .map((option, index) => {
      const label = normalizeSearchText(option.label).trim();
      const value = normalizeSearchText(option.value).trim();
      let score = 0;
      if (needle) {
        if (label === needle || value === needle) score = 0;
        else if (label.startsWith(needle)) score = 1;
        else if (value.startsWith(needle)) score = 2;
        else if (label.split(/\s+/).some((word) => word.startsWith(needle))) score = 3;
        else if (label.includes(needle)) score = 4;
        else if (value.includes(needle)) score = 5;
        else score = 99;
      }
      return { option, index, score };
    })
    .filter((item) => item.score < 99)
    .sort((a, b) => a.score - b.score || a.index - b.index)
    .map((item) => item.option);
}

// Fallback mientras carga /paises: nunca un desplegable vacío.
const PAISES_FALLBACK = [
  { value: "AR", label: "Argentina" },
  { value: "US", label: "Estados Unidos" },
  { value: "CN", label: "China" },
  { value: "BR", label: "Brasil" },
  { value: "ES", label: "España" },
];

const MENSAJE_COTIZACION_NACIONAL =
  "Los envíos dentro de Argentina se habilitarán con OCA y Andreani. " +
  "Todavía no se pueden cotizar desde este formulario.";

function normalizeCountry(value) {
  return String(value ?? "").trim().toUpperCase();
}

function OperatorStatus() {
  const [catalog, setCatalog] = useStateQ(null);

  useEffectQ(() => {
    let alive = true;
    fetch(`${API_URL}/operadores`)
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => { if (alive && data) setCatalog(data); })
      .catch(() => {});
    return () => { alive = false; };
  }, []);

  if (!catalog) return null;
  const groups = [
    ["Internacional", catalog.internacionales || []],
    ["Nacional", catalog.nacionales || []],
  ];
  return (
    <div className="tweb-operator-status" aria-label="Estado de integraciones logísticas" style={{
      display: "grid", gap: 6, marginBottom: 14, padding: "9px 10px",
      border: "1px solid var(--line-soft)", borderRadius: 10,
      background: "rgba(255,255,255,.015)",
    }}>
      {groups.map(([label, operators]) => (
        <div key={label} style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
          <span style={{ minWidth: 74, color: "var(--fg-4)", fontFamily: "var(--font-mono)", fontSize: 9, textTransform: "uppercase", letterSpacing: ".06em" }}>{label}</span>
          {operators.map((operator) => {
            const ready = operator.estado === "disponible_segun_cuenta";
            const prepared = operator.estado === "integracion_preparada";
            return (
              <span key={operator.id} title={operator.estado_label} style={{
                display: "inline-flex", alignItems: "center", gap: 5,
                padding: "3px 7px", border: "1px solid var(--line-soft)",
                borderRadius: 99, color: "var(--fg-3)", fontSize: 10,
              }}>
                <i aria-hidden="true" style={{
                  width: 6, height: 6, borderRadius: 99,
                  background: ready ? "#2ec27e" : prepared ? "#a78bfa" : "var(--fg-4)",
                  boxShadow: ready ? "0 0 7px rgba(46,194,126,.55)" : "none",
                }}/>
                {operator.nombre}
                <small style={{
                  color: "var(--fg-4)", fontFamily: "var(--font-mono)",
                  fontSize: 7, letterSpacing: ".03em", textTransform: "uppercase",
                }}>
                  {operator.estado_corto || operator.estado_label}
                </small>
              </span>
            );
          })}
        </div>
      ))}
    </div>
  );
}

function QuoteWidget({ compact = false }) {
  // Cotizador internacional en ambos sentidos y entre terceros países:
  // AR→CN, CN→AR y CN→IN. AR→AR va por el circuito nacional OCA/Andreani
  // y se bloquea también acá, antes del request. Los botones son atajos;
  // los dos combos siguen siendo la fuente de verdad.
  const [origen, setOrigen] = useStateQ("AR");
  const [destino, setDestino] = useStateQ("US");
  const [paises, setPaises] = useStateQ(PAISES_FALLBACK);

  useEffectQ(() => {
    let vivo = true;
    fetch(`${API_URL}/paises`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (vivo && d?.paises?.length) {
          setPaises(d.paises.map((p) => ({ value: p.iso, label: p.nombre })));
        }
      })
      .catch(() => {});
    return () => { vivo = false; };
  }, []);
  const [peso, setPeso] = useStateQ(5);
  const [largo, setLargo] = useStateQ(30);
  const [ancho, setAncho] = useStateQ(20);
  const [alto, setAlto] = useStateQ(10);
  const [valor, setValor] = useStateQ(100);
  const [step, setStep] = useStateQ("form");
  const [result, setResult] = useStateQ(null);
  const [error, setError] = useStateQ(null);

  const calculate = async () => {
    setError(null);
    try {
      const origenIso = normalizeCountry(origen);
      const destinoIso = normalizeCountry(destino);
      const paisesValidos = new Set(paises.map((pais) => normalizeCountry(pais.value)));
      if (!paisesValidos.has(origenIso) || !paisesValidos.has(destinoIso)) {
        throw new Error("Elegí países válidos para origen y destino.");
      }
      if (origenIso === "AR" && destinoIso === "AR") {
        throw new Error(MENSAJE_COTIZACION_NACIONAL);
      }

      const parsed = {
        peso: parseHumanNumber(peso),
        largo: parseHumanNumber(largo),
        ancho: parseHumanNumber(ancho),
        alto: parseHumanNumber(alto),
        valor: parseHumanNumber(valor, { money: true }),
      };
      if (Object.values(parsed).some((n) => !Number.isFinite(n) || n <= 0)) {
        throw new Error("Revisá peso, medidas y valor. Podés usar coma o punto.");
      }
      if (parsed.peso > 70) throw new Error("El peso máximo por caja es 70 kg.");
      setStep("calculating");
      const resp = await fetch(`${API_URL}/cotizar-web`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          origen_pais: origenIso,
          destino_pais: destinoIso,
          peso_kg: parsed.peso,
          largo_cm: parsed.largo,
          ancho_cm: parsed.ancho,
          alto_cm: parsed.alto,
          valor_declarado_usd: parsed.valor,
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
    <div id="cotizador" style={{ position: "relative", scrollMarginTop: 88 }}>
      {/* Chip de marca 3D — flota sobre el borde superior derecho del cotizador,
          en el mismo lenguaje metálico+neón de los precios. */}
      <div className="tweb-brand-tag" aria-hidden="true">
        <img src="/static/img/logo-mark-white.png" alt=""
             style={{ height: 14, width: "auto", display: "block" }} />
        <span className="tweb-price-metal">Tauro Solutions</span>
      </div>
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

      <div className="tweb-cot-head" style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20, gap: 12, flexWrap: "wrap" }}>
        <div>
          <div className="eyebrow" style={{ marginBottom: 6 }}>Cotizador de envíos</div>
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

      <OperatorStatus />

      {step !== "result" && (
        <>
          {/* Atajos del cotizador internacional: setean ambos combos. Los
              combos siguen libres para rutas entre terceros países. */}
          <div className="tweb-sentido" role="group" aria-label="Atajos de sentido">
            <button type="button"
                    className={`btn ${origen === "AR" && destino !== "AR" ? "btn-primary" : "btn-ghost"}`}
                    onClick={() => { setOrigen("AR"); if (destino === "AR") setDestino("US"); }}>
              Exportar
            </button>
            <button type="button"
                    className={`btn ${destino === "AR" && origen !== "AR" ? "btn-primary" : "btn-ghost"}`}
                    onClick={() => { setDestino("AR"); if (origen === "AR") setOrigen("CN"); }}>
              Importar
            </button>
          </div>

          <div className="tweb-campos-2" style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(0,1fr)", gap: 10, marginBottom: 12 }}>
            <SelectField label="Origen" value={origen} onChange={setOrigen} options={paises} />
            <SelectField label="Destino" value={destino} onChange={setDestino} options={paises} />
          </div>

          <div className="tweb-campos-2" style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(0,1fr)", gap: 10, marginBottom: 12 }}>
            <Field label="Peso (kg)" value={peso} onChange={setPeso} inputMode="decimal" />
            <Field label="Valor declarado (USD)" value={valor} onChange={setValor} inputMode="decimal" money />
          </div>

          <div style={{ marginBottom: 6, fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--fg-3)", textTransform: "uppercase", letterSpacing: "0.08em" }}>
            Dimensiones (cm)
          </div>
          <div className="tweb-campos-3" style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0,1fr))", gap: 8, marginBottom: 20 }}>
            <Field label="Largo" value={largo} onChange={setLargo} inputMode="decimal" />
            <Field label="Ancho" value={ancho} onChange={setAncho} inputMode="decimal" />
            <Field label="Alto" value={alto} onChange={setAlto} inputMode="decimal" />
          </div>

          {error && (
            <div role="alert" style={{ marginBottom: 12, padding: "10px 12px", background: "rgba(255,80,80,0.1)", border: "1px solid rgba(255,80,80,0.3)", borderRadius: 8, fontSize: 13, color: "#ff6b6b" }}>
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
            Mostramos las opciones habilitadas para la ruta ingresada
          </div>
        </>
      )}

      {step === "result" && result && (
        <div className="fade-up" role="status" aria-live="polite">
          <div style={{ fontFamily: "var(--font-display)", fontSize: 20, fontWeight: 600, marginBottom: 2 }}>
            Tus opciones de envío
          </div>
          <div style={{ color: "var(--fg-3)", marginBottom: 18, fontSize: 12, fontFamily: "var(--font-mono)" }}>
            {peso}kg · {result.origen} → {result.destino} · opciones disponibles
          </div>

          <div style={{ display: "grid", gap: 10, marginBottom: 20 }}>
            {result.carriers.map((c) => (
              <CarrierCard key={c.id} carrier={c} recomendado={c.id === result.recomendado} />
            ))}
          </div>

          {/* Captura de contacto DESPUÉS del precio, en el momento de máximo
              interés. El cotizador sigue gratis y sin login: esto es una
              oferta, no un peaje. */}
          <EmailCapture
            quoteId={result.quote_id}
            referencia={result.referencia}
          />

          <a className="btn btn-primary" style={{ width: "100%" }}
             href={`/portal/login?quote_id=${encodeURIComponent(result.quote_id)}`}>
            Crear este envío en el portal <ArrowRight size={14}/>
          </a>
          <a className="btn btn-ghost" style={{ width: "100%", marginTop: 10 }}
             href="mailto:cotizaciones@taurosolutions.ar?subject=Quiero%20una%20cuenta%20en%20el%20portal%20Tauro">
            Todavía no tengo cuenta
          </a>
        </div>
      )}
    </div>
    </div>
  );
}

function EmailCapture({ quoteId, referencia }) {
  const [email, setEmail] = React.useState("");
  const [estado, setEstado] = React.useState("idle"); // idle | enviando | ok | pendiente | error
  const [msg, setMsg] = React.useState("");

  const enviar = async () => {
    if (!email.trim()) return;
    setEstado("enviando");
    try {
      const r = await fetch("/cotizacion-lead", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: email.trim(),
          quote_id: quoteId,
        }),
      });
      const d = await r.json();
      if (d.ok) {
        setEstado("ok");
      } else if (["pendiente", "procesando", "verificar", "limitado"].includes(d.estado)) {
        setEstado("pendiente");
        setMsg(d.error || "La cotización quedó guardada y estamos procesando el correo.");
      } else {
        setEstado("error");
        setMsg(d.error || "No pudimos mandarla. Probá de nuevo.");
      }
    } catch (e) {
      setEstado("error");
      setMsg("No pudimos mandarla. Probá de nuevo.");
    }
  };

  if (estado === "ok") {
    return (
      <div role="status" aria-live="polite" style={{
        marginBottom: 14, padding: "12px 14px", borderRadius: 10,
        background: "rgba(46,194,126,0.08)", border: "1px solid rgba(46,194,126,0.3)",
        fontSize: 13, color: "#2ec27e", textAlign: "center",
      }}>
        ✓ Enviamos el presupuesto {referencia} a {email}. Si no aparece en unos minutos, revisá Spam o Promociones.
      </div>
    );
  }

  if (estado === "pendiente") {
    return (
      <div role="status" aria-live="polite" style={{
        marginBottom: 14, padding: "12px 14px", borderRadius: 10,
        background: "rgba(224,165,79,0.09)", border: "1px solid rgba(224,165,79,0.35)",
        fontSize: 13, color: "#e0b66d", textAlign: "center",
      }}>
        {msg}
      </div>
    );
  }

  return (
    <div style={{
      marginBottom: 14, padding: "12px 14px", borderRadius: 10,
      background: "rgba(255,255,255,0.02)", border: "1px solid var(--line-soft)",
    }}>
      <div style={{ fontSize: 12, color: "var(--fg-3)", marginBottom: 8, fontFamily: "var(--font-mono)" }}>
        ¿Querés guardar el presupuesto {referencia}? Te lo mandamos por mail.
      </div>
      <label htmlFor="quote-email" style={{
        display: "block", fontSize: 12, color: "var(--fg-2)", marginBottom: 6,
      }}>
        Email donde querés recibirlo
      </label>
      <div style={{ display: "flex", gap: 8 }}>
        <input
          id="quote-email"
          type="email"
          value={email}
          placeholder="tu@email.com"
          autoComplete="email"
          aria-describedby={estado === "error" ? "quote-email-error" : undefined}
          onChange={(e) => { setEmail(e.target.value); if (estado === "error") setEstado("idle"); }}
          onKeyDown={(e) => { if (e.key === "Enter") enviar(); }}
          style={{
            flex: 1, minWidth: 0, padding: "10px 12px",
            background: "var(--bg)", border: "1px solid var(--line-soft)",
            borderRadius: 8, color: "var(--fg)", fontSize: 16,
          }}
        />
        <button className="btn btn-ghost" onClick={enviar}
                disabled={estado === "enviando"}
                style={{ whiteSpace: "nowrap", padding: "10px 16px", fontSize: 13 }}>
          {estado === "enviando" ? "Enviando…" : "Mandámela"}
        </button>
      </div>
      {estado === "error" && (
        <div id="quote-email-error" role="alert" style={{ marginTop: 8, fontSize: 12, color: "#ff6b6b" }}>{msg}</div>
      )}
    </div>
  );
}

function Field({ label, value, onChange, type = "text", inputMode, money = false }) {
  return (
    <label style={{ display: "block" }}>
      <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--fg-3)", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 6 }}>
        {label}
      </div>
      <input
        type={type}
        inputMode={inputMode}
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
        onBlur={(e) => {
          e.target.style.borderColor = "var(--line-soft)";
          const parsed = parseHumanNumber(e.target.value, { money });
          if (Number.isFinite(parsed)) onChange(String(parsed));
        }}
      />
    </label>
  );
}

/* Combobox inteligente TAURO. Busca por nombre o ISO, prioriza coincidencias
   al comienzo, permite recorrer sin confirmar y recién aplica con Enter/click. */
function SelectField({ label, value, onChange, options }) {
  const [open, setOpen] = useStateQ(false);
  const [query, setQuery] = useStateQ("");
  const [activeIndex, setActiveIndex] = useStateQ(0);
  const [openUp, setOpenUp] = useStateQ(false);
  const [panelMaxHeight, setPanelMaxHeight] = useStateQ(280);
  const boxRef = useRefQ(null);
  const buttonRef = useRefQ(null);
  const searchRef = useRefQ(null);
  const listIdRef = useRefQ(null);
  if (!listIdRef.current) listIdRef.current = `tweb-select-${++selectFieldSeq}`;
  const seleccionada = options.find((o) => o.value === value) || options[0];
  const filtered = rankedSelectOptions(options, query);

  const close = (focusButton = false) => {
    setOpen(false);
    setQuery("");
    if (focusButton) {
      window.setTimeout(() => buttonRef.current?.focus({ preventScroll: true }), 0);
    }
  };

  const openWith = (seed = "") => {
    setQuery(seed);
    setOpen(true);
  };

  const choose = (option) => {
    if (!option) return;
    onChange(option.value);
    close(true);
  };

  useEffectQ(() => {
    if (!open) return;
    const cerrar = (e) => {
      if (boxRef.current && !boxRef.current.contains(e.target)) close(false);
    };
    document.addEventListener("mousedown", cerrar);
    return () => document.removeEventListener("mousedown", cerrar);
  }, [open]);

  useEffectQ(() => {
    if (!open) return;
    if (query.trim()) {
      setActiveIndex(0);
      return;
    }
    const selectedIndex = filtered.findIndex((option) => option.value === value);
    setActiveIndex(selectedIndex >= 0 ? selectedIndex : 0);
  }, [open, query, options, value]);

  useEffectQ(() => {
    if (!open) return;
    const place = () => {
      const rect = boxRef.current?.getBoundingClientRect();
      if (!rect) return;
      const viewportTop = window.visualViewport?.offsetTop || 0;
      const viewportHeight = window.visualViewport?.height || window.innerHeight;
      const viewportBottom = viewportTop + viewportHeight;
      const below = Math.max(0, viewportBottom - rect.bottom - 7);
      const above = Math.max(0, rect.top - viewportTop - 7);
      const shouldOpenUp = below < 240 && above > below;
      setOpenUp(shouldOpenUp);
      setPanelMaxHeight(Math.max(140, Math.min(280, shouldOpenUp ? above : below)));
    };
    place();
    const viewport = window.visualViewport;
    window.addEventListener("resize", place, { passive: true });
    viewport?.addEventListener("resize", place, { passive: true });
    viewport?.addEventListener("scroll", place, { passive: true });
    const focusTimer = window.setTimeout(() => {
      searchRef.current?.focus({ preventScroll: true });
      searchRef.current?.select();
    }, 0);
    return () => {
      window.clearTimeout(focusTimer);
      window.removeEventListener("resize", place);
      viewport?.removeEventListener("resize", place);
      viewport?.removeEventListener("scroll", place);
    };
  }, [open]);

  useEffectQ(() => {
    if (!open) return;
    document.getElementById(`${listIdRef.current}-option-${activeIndex}`)
      ?.scrollIntoView({ block: "nearest" });
  }, [open, activeIndex, query]);

  const onButtonKey = (e) => {
    if (e.key === "Enter" || e.key === " " || e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      openWith("");
    } else if (e.key === "Escape") {
      close(false);
    } else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault();
      openWith(e.key);
    }
  };

  const onSearchKey = (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!filtered.length) return;
      const delta = e.key === "ArrowDown" ? 1 : -1;
      setActiveIndex((current) => (current + delta + filtered.length) % filtered.length);
    } else if (e.key === "Home" || e.key === "End") {
      e.preventDefault();
      if (filtered.length) setActiveIndex(e.key === "Home" ? 0 : filtered.length - 1);
    } else if (e.key === "Enter") {
      e.preventDefault();
      choose(filtered[activeIndex] || filtered[0]);
    } else if (e.key === "Escape") {
      e.preventDefault();
      close(true);
    } else if (e.key === "Tab") {
      close(false);
    }
  };

  return (
    <div style={{ display: "block" }}>
      <div id={`${listIdRef.current}-label`} style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--fg-3)", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 6 }}>
        {label}
      </div>
      <div ref={boxRef} style={{ position: "relative" }}>
        <button
          ref={buttonRef}
          type="button"
          onClick={() => (open ? close(false) : openWith(""))}
          onKeyDown={onButtonKey}
          className="tweb-select-btn"
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-controls={listIdRef.current}
          aria-labelledby={`${listIdRef.current}-label ${listIdRef.current}-value`}
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
          <span id={`${listIdRef.current}-value`}>{seleccionada ? seleccionada.label : "Seleccionar"}</span>
          <svg width="11" height="7" viewBox="0 0 10 6" fill="none" aria-hidden="true"
               style={{ color: open ? "var(--accent-soft)" : "var(--fg-3)", transform: open ? "rotate(180deg)" : "none", transition: "transform .22s cubic-bezier(.2,.7,.3,1), color .15s", flexShrink: 0 }}>
            <path d="M1 1l4 4 4-4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
        </button>

        {open && (
          <div
            className={`tweb-select-panel${openUp ? " open-up" : ""}`}
            style={{ maxHeight: panelMaxHeight }}
          >
            <div className="tweb-select-search-wrap">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="11" cy="11" r="7" stroke="currentColor" strokeWidth="1.8"/><path d="m20 20-4-4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"/></svg>
              <input
                ref={searchRef}
                type="search"
                className="tweb-select-search"
                value={query}
                placeholder="Buscar país o código"
                autoComplete="off"
                role="combobox"
                aria-autocomplete="list"
                aria-expanded="true"
                aria-controls={listIdRef.current}
                aria-activedescendant={filtered.length ? `${listIdRef.current}-option-${activeIndex}` : undefined}
                onChange={(e) => { setQuery(e.target.value); setActiveIndex(0); }}
                onKeyDown={onSearchKey}
              />
            </div>
            <div id={listIdRef.current} className="tweb-select-options" role="listbox" aria-labelledby={`${listIdRef.current}-label`}>
              {filtered.length ? filtered.map((o, index) => {
                const sel = o.value === value;
                const active = index === activeIndex;
                return (
                  <div
                    id={`${listIdRef.current}-option-${index}`}
                    key={o.value}
                    role="option"
                    aria-selected={sel}
                    onMouseDown={(event) => { event.preventDefault(); }}
                    onClick={(event) => { event.preventDefault(); choose(o); }}
                    className={`tweb-select-opt${active ? " active" : ""}${sel ? " selected" : ""}`}
                  >
                    <span className="tweb-select-check" aria-hidden="true">✓</span>
                    <span>{o.label}</span>
                    <small>{o.value}</small>
                  </div>
                );
              }) : (
                <div className="tweb-select-empty">No encontramos resultados</div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/* Tarjeta de courier del comparador: logo real de la empresa + precio.
   estados: "cotizado" (precio en vivo) · "proximamente" (acuerdo en cierre,
   se muestra igual con su logo) · "sin_tarifa" (sin cobertura en la ruta). */
function CarrierCard({ carrier, recomendado }) {
  const cotizado = carrier.estado === "cotizado";
  const logoSize = {
    fedex: { width: 66, height: 28 },
    ups: { width: 32, height: 34 },
    dhl: { width: 68, height: 34 },
  }[carrier.id] || { width: 66, height: 32 };

  return (
    <div className={`tweb-carrier${recomendado ? " tweb-neon-ring" : ""}`} style={{
      display: "flex", alignItems: "center", gap: 14,
      padding: "14px 16px",
      background: cotizado ? "var(--bg)" : "rgba(255,255,255,0.02)",
      border: `1px solid ${recomendado ? "var(--accent)" : "var(--line-soft)"}`,
      // el glow de la recomendada lo pone .tweb-neon-ring (neón respirando)
      borderRadius: 12,
      opacity: 1,
      position: "relative",
    }}>
      {recomendado && (
        <div style={{
          position: "absolute", top: -9, right: 12,
          background: "var(--accent)", color: "#fff",
          fontSize: 10, fontFamily: "var(--font-mono)", fontWeight: 600,
          letterSpacing: "0.06em", textTransform: "uppercase",
          padding: "2px 8px", borderRadius: 99,
        }}>
          Precio más bajo
        </div>
      )}

      <div style={{
        width: 82, height: 44, flexShrink: 0,
        background: "#fff", borderRadius: 8,
        display: "flex", alignItems: "center", justifyContent: "center",
        padding: "5px 7px", boxSizing: "border-box",
        boxShadow: "inset 0 0 0 1px rgba(0,0,0,.06)",
      }}>
        <img src={carrier.logo} alt={carrier.nombre}
             style={{
               width: logoSize.width,
               height: logoSize.height,
               objectFit: "contain",
               display: "block",
             }} />
      </div>

      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontWeight: 600, fontSize: 14, display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          {carrier.nombre}
          {/* "Operativo" + luz verde: solo cuando el sistema confirmó que el
              carrier devolvió tarifa en vivo (estado "cotizado"). */}
          {cotizado && (
            <span style={{
              display: "inline-flex", alignItems: "center", gap: 5,
              fontSize: 9.5, fontFamily: "var(--font-mono)", fontWeight: 600,
              letterSpacing: ".08em", textTransform: "uppercase", color: "#2ec27e",
            }}>
              <span className="pulse" style={{
                width: 6, height: 6, borderRadius: 99, background: "#2ec27e",
                boxShadow: "0 0 8px rgba(46,194,126,.8)", flexShrink: 0,
              }}/>
              Operativo
            </span>
          )}
        </div>
        <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--fg-3)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
          {cotizado ? `${carrier.servicio} · ${carrier.dias_estimados} días` : carrier.servicio}
        </div>
      </div>

      <div className="tweb-carrier-precio" style={{ textAlign: "right", flexShrink: 0 }}>
        {cotizado ? (
          <>
            {carrier.precio_lista_usd && (
              <div style={{ fontSize: 10.5, color: "var(--fg-4)", fontFamily: "var(--font-mono)", textDecoration: "line-through" }}>
                ${carrier.precio_lista_usd.toLocaleString("es-AR")} USD
              </div>
            )}
            <div style={{ fontFamily: "var(--font-display)", fontSize: 20, fontWeight: 700, lineHeight: 1.1 }}>
              {/* Violeta metálico con brillo en movimiento — paleta del ad de
                  referencia (95f1d64); animación en .tweb-price-metal (styles.css).
                  El brillo cubre el monto COMPLETO: número + moneda, USD y ARS. */}
              <span className="tweb-price-metal">
                ${carrier.precio_usd.toLocaleString("es-AR")}
                {" "}<span style={{ fontSize: 11, fontWeight: 400 }}>USD</span>
              </span>
            </div>
            <div style={{ fontSize: 11, fontFamily: "var(--font-mono)" }}>
              <span className="tweb-price-metal" style={{ filter: "drop-shadow(0 0 8px rgba(124, 92, 246, .35))" }}>
                ARS ${carrier.precio_ars.toLocaleString("es-AR")}
              </span>
            </div>
            {carrier.descuento_pct > 0 && (
              <div style={{ fontSize: 10, color: "var(--accent-soft)", fontFamily: "var(--font-mono)", marginTop: 2 }}>
                −{carrier.descuento_pct}% de mejora en tu precio
              </div>
            )}
          </>
        ) : (
          <div style={{
            fontSize: 10.5, fontFamily: "var(--font-mono)", fontWeight: 600,
            letterSpacing: "0.05em", textTransform: "uppercase",
            color: "var(--accent-soft)",
            border: "1px solid var(--line)", borderRadius: 99,
            padding: "4px 10px", whiteSpace: "nowrap",
          }}>
            {carrier.estado === "proximamente" ? "Próximamente" : "Sin tarifa"}
          </div>
        )}
      </div>
    </div>
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
      <span className="tauro-loader-label">Comparando couriers...</span>
    </span>
  );
}

window.QuoteWidget = QuoteWidget;
