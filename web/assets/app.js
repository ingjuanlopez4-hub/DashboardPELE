"use strict";

const PAGE_SIZE = 10;
const { expectedReturn, simpleAverage, formatChange, classifyLifecycle, freshness } = globalThis.PeleMarketMath;

const storedAlerts = (() => {
  try {
    const parsed = JSON.parse(localStorage.getItem("pele-alerts-v2") || localStorage.getItem("pele-alerts") || "[]");
    if (Array.isArray(parsed)) return new Map(parsed.map(item => {
      const record = typeof item === "string" ? { id: item } : item;
      return [String(record.id), record];
    }));
  } catch {}
  return new Map();
})();
const state = {
  markets: [], search: "", category: "all", closing: "all", minLiquidity: 0,
  opportunities: false, sort: "activity", nextOffset: 0, hasMore: false, loading: false,
  alerts: storedAlerts, dataState: "loading", marketUi: new Map(), comparison: new Set(),
  responseGeneratedAt: null, responseReceivedAt: null
};
const elements = {
  grid: document.querySelector("#market-grid"), loading: document.querySelector("#loading"),
  empty: document.querySelector("#empty"), notice: document.querySelector("#notice"),
  search: document.querySelector("#search"), category: document.querySelector("#category"),
  sort: document.querySelector("#sort"), count: document.querySelector("#result-count"),
  closing: document.querySelector("#closing"), minLiquidity: document.querySelector("#min-liquidity"),
  opportunities: document.querySelector("#opportunities"), loadMore: document.querySelector("#load-more"),
  dataStatus: document.querySelector("#data-status"), dataStatusLabel: document.querySelector("#data-status-label"),
  alertCount: document.querySelector("#alert-count"), alertDeskCount: document.querySelector("#alert-desk-count"),
  alertLayerCount: document.querySelector("#alert-layer-count"),
  filters: document.querySelector("#filters"), filterToggle: document.querySelector("#filter-toggle"),
  comparisonPanel: document.querySelector("#comparison-panel"), comparisonItems: document.querySelector("#comparison-items"),
  comparisonStatus: document.querySelector("#comparison-status"), emptyTitle: document.querySelector("#empty-title"),
  comparisonSummary: document.querySelector("#comparison-summary"), comparisonTray: document.querySelector("#comparison-tray"),
  comparisonTrayCount: document.querySelector("#comparison-tray-count"), comparisonTrayAction: document.querySelector("#comparison-tray-action"),
  emptyCopy: document.querySelector("#empty-copy"), reset: document.querySelector("#reset")
};
const projectionElements = {
  form: document.querySelector("#projection-form"), symbol: document.querySelector("#projection-symbol"),
  horizon: document.querySelector("#projection-horizon"), target: document.querySelector("#projection-target"),
  status: document.querySelector("#projection-status"), results: document.querySelector("#projection-results"),
  chart: document.querySelector("#projection-chart"), current: document.querySelector("#projection-current"),
  median: document.querySelector("#projection-median"), gain: document.querySelector("#projection-gain"),
  above: document.querySelector("#projection-above"), range: document.querySelector("#projection-range"),
  meta: document.querySelector("#projection-meta")
};
let projectionLoading = false;
const money = new Intl.NumberFormat("es-ES", { style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1 });
const compactNumber = new Intl.NumberFormat("es-ES", { notation: "compact", maximumFractionDigits: 1 });
const percent = new Intl.NumberFormat("es-ES", { style: "percent", maximumFractionDigits: 0 });
const date = new Intl.DateTimeFormat("es-ES", { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" });
const validSorts = new Set(["activity", "volume", "liquidity", "probability", "confidence", "date"]);
const validClosings = new Set(["all", "7", "30", "90"]);
const validLiquidity = new Set([0, 50000, 250000, 1000000]);

function safeDate(value) {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function clamp(value, minimum = 0, maximum = 1) {
  return Math.min(maximum, Math.max(minimum, value));
}

function finite(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function openInterestLabel(metric) {
  const value = metric?.status === "available" ? finite(metric.value) : null;
  return value === null ? "No publicado" : `${compactNumber.format(value)} colateral`;
}

function concentrationLabel(metric) {
  const share = metric?.status === "available" ? finite(metric.marketTop5Share) : null;
  return share === null ? metric?.status === "unsupported_market_type" ? "No compatible" : "No publicada" : `${percent.format(share)} top 5`;
}

function marketKey(market) {
  return String(market.id || `${market.question}:${market.endDate}`);
}

function sourceClockNow() {
  const generated = safeDate(state.responseGeneratedAt)?.getTime();
  return generated && state.responseReceivedAt
    ? generated + Math.max(0, Date.now() - state.responseReceivedAt)
    : Date.now();
}

function applyUrlState() {
  const params = new URLSearchParams(location.search);
  state.search = params.get("q") || "";
  state.category = params.get("category") || "all";
  state.closing = validClosings.has(params.get("closing")) ? params.get("closing") : "all";
  const liquidity = Number(params.get("liquidity") || 0);
  state.minLiquidity = validLiquidity.has(liquidity) ? liquidity : 0;
  state.opportunities = params.get("activity") === "high";
  state.sort = validSorts.has(params.get("sort")) ? params.get("sort") : "activity";
  state.comparison = new Set((params.get("compare") || "").split(",").filter(Boolean).slice(0, 3));
  elements.search.value = state.search;
  elements.closing.value = state.closing;
  elements.minLiquidity.value = String(state.minLiquidity);
  elements.opportunities.checked = state.opportunities;
  elements.sort.value = state.sort;
}

function syncUrl() {
  const params = new URLSearchParams();
  if (state.search.trim()) params.set("q", state.search.trim());
  if (state.category !== "all") params.set("category", state.category);
  if (state.closing !== "all") params.set("closing", state.closing);
  if (state.minLiquidity) params.set("liquidity", String(state.minLiquidity));
  if (state.opportunities) params.set("activity", "high");
  if (state.sort !== "activity") params.set("sort", state.sort);
  if (state.comparison.size) params.set("compare", [...state.comparison].join(","));
  const query = params.toString();
  history.replaceState(null, "", `${location.pathname}${query ? `?${query}` : ""}${location.hash}`);
}

function alertSnapshot(market) {
  return {
    probability: finite(market.probability),
    change24h: finite(market.intelligence?.change24h),
    activityScore: finite(market.intelligence?.activityScore),
    spread: finite(market.spread),
    sourceUpdatedAt: market.updatedAt || null
  };
}

function evaluateWatchedMarket(market) {
  const key = marketKey(market);
  const record = state.alerts.get(key);
  if (!record) return;
  const current = alertSnapshot(market);
  const previous = record.snapshot || {};
  const events = [];
  if (current.probability !== null && finite(previous.probability) !== null
      && Math.abs(current.probability - previous.probability) >= 0.03) {
    events.push({ code: "probability_delta", label: `Precio cambió ${formatChange(current.probability - previous.probability)}` });
  }
  if (current.activityScore !== null && current.activityScore >= 70 && !(finite(previous.activityScore) >= 70)) {
    events.push({ code: "activity_pressure", label: `Presión de actividad ${current.activityScore}/100` });
  }
  if (current.spread !== null && current.spread >= 0.05 && !(finite(previous.spread) >= 0.05)) {
    events.push({ code: "wide_spread", label: `Spread ${percent.format(current.spread)}` });
  }
  const checkedAt = new Date().toISOString();
  const knownEvents = Array.isArray(record.events) ? record.events : [];
  for (const event of events) {
    const fingerprint = `${event.code}:${current.sourceUpdatedAt || checkedAt}`;
    if (!knownEvents.some(item => item.fingerprint === fingerprint)) knownEvents.unshift({ ...event, fingerprint, at: checkedAt });
  }
  state.alerts.set(key, {
    ...record, id: key, question: market.question, endDate: market.endDate, url: market.url,
    snapshot: current, checkedAt, events: knownEvents.slice(0, 20)
  });
}

function persistAlerts() {
  try {
    localStorage.setItem("pele-alerts-v2", JSON.stringify([...state.alerts.values()]));
    localStorage.removeItem("pele-alerts");
  } catch {}
  elements.alertCount.textContent = state.alerts.size;
  elements.alertDeskCount.textContent = state.alerts.size
    ? `${state.alerts.size} ${state.alerts.size === 1 ? "mercado vigilado" : "mercados vigilados"}`
    : "Sin mercados vigilados";
  elements.alertLayerCount.textContent = `${state.alerts.size} ${state.alerts.size === 1 ? "mercado vigilado" : "mercados vigilados"}`;
  const eventDesk = document.querySelector("#alert-events");
  const events = [...state.alerts.values()].map(record => {
    const latest = Array.isArray(record.events) ? record.events[0] : null;
    const item = document.createElement("span");
    item.dataset.triggered = String(Boolean(latest));
    const question = record.question || "Mercado guardado";
    const checked = safeDate(record.checkedAt);
    item.textContent = `${latest ? "Evento" : "Vigilando"}: ${question.slice(0, 48)}${question.length > 48 ? "…" : ""}${latest ? ` · ${latest.label}` : checked ? ` · revisado ${checked.toLocaleString("es-ES")}` : " · pendiente de primera revisión"}`;
    return item;
  });
  const empty = document.createElement("span");
  empty.textContent = state.alerts.size ? "Esperando la siguiente evaluación de mercado." : "Sin eventos evaluados.";
  eventDesk.replaceChildren(...(events.length ? events : [empty]));
}

function updateKpis() {
  const values = key => state.markets.map(market => finite(market[key])).filter(value => value !== null);
  const total = key => values(key).reduce((sum, value) => sum + value, 0);
  document.querySelector("#kpi-count").textContent = state.markets.length.toLocaleString("es-ES");
  document.querySelector("#kpi-volume").textContent = values("volume").length ? money.format(total("volume")) : "Sin dato";
  document.querySelector("#kpi-liquidity").textContent = values("liquidity").length ? money.format(total("liquidity")) : "Sin dato";
  const probabilities = values("probability");
  const average = simpleAverage(probabilities);
  document.querySelector("#kpi-probability").textContent = average === null ? "Sin dato" : percent.format(average);
  const covered = state.markets.filter(market => market.intelligence?.change24h !== null).length;
  const coverage = state.markets.length ? Math.round(covered / state.markets.length * 100) : 0;
  document.querySelector("#coverage-rate").innerHTML = `${coverage}<span>%</span>`;
  document.querySelector("#coverage-copy").textContent = `${covered} de ${state.markets.length} contratos incluyen variación de precio de 24h publicada por Gamma.`;
  for (const level of ["high", "medium", "low"]) {
    const count = state.markets.filter(market => market.confidence?.level === level).length;
    const share = state.markets.length ? count / state.markets.length * 100 : 0;
    document.querySelector(`#confidence-${level}`).textContent = count.toLocaleString("es-ES");
    document.querySelector(`#confidence-${level}-bar`).style.width = `${share}%`;
  }
  const attention = state.markets.filter(market => market.signalDossier?.status === "attention").length;
  const latest = state.markets.map(market => freshness(market.updatedAt, sourceClockNow())).filter(item => item.ageMinutes !== null).sort((a, b) => a.ageMinutes - b.ageMinutes)[0];
  document.querySelector("#sample-attention").textContent = state.markets.length ? `${attention} de ${state.markets.length}` : "--";
  document.querySelector("#sample-coverage").textContent = state.markets.length ? `${coverage}%` : "--";
  document.querySelector("#sample-freshness").textContent = latest?.label || "No publicada";
  document.querySelector("#sample-status").textContent = state.dataState === "empty" ? "Sin contratos" : state.dataState === "error" ? "Sin conexión" : state.dataState === "partial" ? "Datos parciales" : "Muestra recibida";
}

function setDataStatus(status, label) {
  elements.dataStatus.dataset.state = status;
  elements.dataStatusLabel.textContent = label;
}

function assetMoney(value, currency) {
  try { return new Intl.NumberFormat("es-ES", { style: "currency", currency: currency || "USD", maximumFractionDigits: 2 }).format(value); }
  catch { return Number(value).toFixed(2); }
}

async function loadProjection() {
  if (projectionLoading || !projectionElements.form) return;
  if (!projectionElements.form.reportValidity()) return;
  projectionLoading = true;
  const button = projectionElements.form.querySelector("button");
  button.setAttribute("aria-busy", "true");
  button.disabled = true;
  projectionElements.status.textContent = "Actualizando histórico y simulación…";
  const params = new URLSearchParams({
    symbol: projectionElements.symbol.value.trim(), horizonDays: projectionElements.horizon.value, paths: "5000"
  });
  if (projectionElements.target.value) params.set("targetPrice", projectionElements.target.value);
  try {
    const response = await fetch(`/api/projection?${params}`, { headers: { Accept: "application/json" } });
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json") ? await response.json() : null;
    if (!response.ok) throw new Error(payload?.error?.message || `API ${response.status}`);
    const currency = payload.currency || "USD";
    projectionElements.current.textContent = assetMoney(payload.currentPrice, currency);
    projectionElements.median.textContent = assetMoney(payload.distribution.median, currency);
    projectionElements.gain.textContent = percent.format(payload.probabilities.gain);
    projectionElements.above.textContent = percent.format(payload.probabilities.aboveTarget);
    const bins = payload.distribution.histogram;
    const maximum = Math.max(...bins.map(bin => bin.probability), Number.EPSILON);
    projectionElements.chart.replaceChildren(...bins.map(bin => {
      const bar = document.createElement("i");
      bar.style.height = `${Math.max(2, bin.probability / maximum * 100)}%`;
      bar.title = `${assetMoney(bin.from, currency)}–${assetMoney(bin.to, currency)}: ${percent.format(bin.probability)}`;
      return bar;
    }));
    projectionElements.chart.setAttribute("aria-label", `Distribución de ${payload.parameters.paths} precios simulados para ${payload.symbol}; mediana ${assetMoney(payload.distribution.median, currency)}`);
    projectionElements.range.textContent = `P5 ${assetMoney(payload.distribution.p05, currency)} · P95 ${assetMoney(payload.distribution.p95, currency)}`;
    const updated = safeDate(payload.dataAsOf);
    projectionElements.meta.textContent = `${payload.symbol} · ${payload.calibration.observations} cierres · σ anual ${percent.format(payload.calibration.annualVolatility)} · Yahoo Finance${updated ? ` · ${updated.toLocaleString("es-ES")}` : ""}. Modelo probabilístico, no asesoramiento financiero.`;
    projectionElements.status.textContent = `Simulación actualizada para ${payload.symbol}.`;
    projectionElements.results.hidden = false;
  } catch (error) {
    projectionElements.status.textContent = `Proyección no disponible. ${error.message}${projectionElements.results.hidden ? "" : " Se conserva el último resultado válido."}`;
  } finally {
    projectionLoading = false;
    button.removeAttribute("aria-busy");
    button.disabled = false;
  }
}

function card(market) {
  const article = document.createElement("article");
  article.className = "market-card";
  const end = safeDate(market.endDate);
  const key = marketKey(market);
  const savedUi = state.marketUi.get(key) || {};
  const rawProbability = finite(market.probability);
  const probability = rawProbability === null ? null : clamp(rawProbability);
  const confidence = market.confidence || { score: null, level: "unknown", coverage: 0, factors: [] };
  const intelligence = market.intelligence || { points: [], change24h: null, activityScore: null, activityRatio: null, activityLevel: "unknown", explanation: "Datos no disponibles." };
  const displayedLevel = confidence.coverage < 50 || confidence.score === null ? "unknown" : confidence.level;
  const confidenceLabels = { high: "Alta", medium: "Media", low: "Baja", unknown: "Sin datos" };
  article.innerHTML = `
    <div class="probability-rail" aria-hidden="true"><i></i><span></span></div>
    <div class="card-content">
       <div class="card-top"><span class="category"></span><span class="date"></span><span class="lifecycle-badge"></span><span class="freshness"></span></div>
      <h3></h3>
       <div class="scan-strip" aria-label="Resumen comparable">
         <div><span>Prob. Sí</span><strong class="scan-probability"></strong></div>
         <div><span>Cambio 24h</span><strong class="scan-change"></strong></div>
         <div><span>Intensidad</span><strong class="scan-activity"></strong></div>
         <div><span>Solidez</span><strong class="scan-confidence"></strong></div>
      </div>
      <p class="scan-reason"><span>Qué merece atención</span><strong></strong></p>
       <div class="card-actions"><button class="compare-button" type="button" aria-pressed="false"></button><button class="alert-button quick-watch" type="button" aria-pressed="false"></button></div>
      <details class="signal-details">
        <summary>Ver lectura completa</summary>
        <div class="model-readout" aria-label="Lecturas derivadas de datos de mercado">
        <div class="readout-label"><span>Lectura PELE</span><small>Datos Gamma + cálculo</small></div>
        <div class="trend-block">
          <div class="trend-head"><span>Precio de Sí · cambio en 24 h</span><strong class="trend-change"></strong></div>
          <svg class="trend-chart" viewBox="0 0 240 54" preserveAspectRatio="none" role="img"><title></title><line class="baseline" x1="0" y1="27" x2="240" y2="27"></line><polyline class="trend-line"></polyline><circle class="trend-dot" r="3.5"></circle></svg>
        </div>
        <div class="confidence-row"><span>Solidez de la señal</span><strong class="confidence"></strong></div>
        <div class="signal-grid">
          <div><span>Rotación 24h</span><strong class="activity-ratio"></strong></div>
          <div><span>Presión actividad</span><strong class="activity-pressure"></strong></div>
          <div><span>Spread</span><strong class="spread"></strong></div>
        </div>
        </div>
      </details>
      <details class="confidence-details"><summary>Ver cálculo de solidez</summary><div class="factor-list"></div><small></small></details>
      <details class="market-intelligence">
        <summary>Abrir expediente</summary>
        <div class="intelligence-detail">
          <div class="dossier-status"><span>Estado del expediente</span><strong></strong><p></p></div>
          <div class="comparison-row">
            <div><span>Polymarket</span><strong class="market-comparison"></strong></div>
            <div><span>Bid / ask</span><strong class="book-comparison"></strong></div>
          </div>
          <div class="gbm-projection">
            <div class="gbm-heading"><span>Movimiento Browniano Geométrico</span><small class="gbm-meta"></small></div>
            <div class="gbm-values">
              <div><span>Mediana simulada al cierre</span><strong class="gbm-median"></strong></div>
              <div><span>Rango simulado P5–P95</span><strong class="gbm-range"></strong></div>
            </div>
            <p class="gbm-note"></p>
          </div>
           <div class="why-change"><span>Qué explica el cambio</span><p></p></div>
           <div class="structure-context">
             <span>Estructura y contexto</span>
             <dl>
               <div><dt>Interés abierto</dt><dd class="dossier-oi"></dd></div>
               <div><dt>Concentración</dt><dd class="dossier-concentration"></dd></div>
             </dl>
             <p class="concentration-method"></p>
             <div class="regional-context"><strong>Fuentes regionales</strong><div></div><small></small></div>
           </div>
           <div class="dossier-proof"><span>Pruebas y procedencia</span><div></div><small></small></div>
          <label class="personal-belief"><span>Tu probabilidad estimada de Sí (%)</span><input type="number" min="1" max="99" step="1"><strong class="personal-edge" role="status" aria-live="polite"></strong></label>
          <small class="ev-formula">Retorno relativo estimado = (probabilidad propia − spread) ÷ precio − 1. No es una cantidad en dólares.</small>
           <button class="alert-button dossier-watch" type="button" aria-pressed="false"></button>
        </div>
      </details>
      <div class="card-footer">
         <div class="metric"><span>Volumen</span><strong></strong></div>
         <div class="metric"><span>Liquidez</span><strong></strong></div>
         <div class="metric"><span>Interés abierto</span><strong></strong></div>
          <a class="market-link" target="_blank" rel="noopener noreferrer"><span></span><small>Elegibilidad no verificada</small><b aria-hidden="true">&#8599;</b></a>
      </div>
    </div>`;
  article.querySelector(".category").textContent = market.category || "Other";
  article.querySelector(".date").textContent = end ? date.format(end) : "Sin fecha";
  const lifecycle = classifyLifecycle(market, sourceClockNow());
  const marketFreshness = freshness(market.updatedAt, sourceClockNow());
  article.dataset.lifecycle = lifecycle.code;
  article.querySelector(".lifecycle-badge").textContent = lifecycle.label;
  article.querySelector(".freshness").textContent = marketFreshness.label;
  article.querySelector(".freshness").dataset.state = marketFreshness.code;
  article.querySelector("h3").textContent = market.question;
  article.classList.toggle("market-card-unavailable", probability === null);
  article.querySelector(".probability-rail i").style.height = probability === null ? "0" : `${probability * 100}%`;
  article.querySelector(".probability-rail span").hidden = probability === null;
  if (probability !== null) article.querySelector(".probability-rail span").style.bottom = `calc(${probability * 100}% - 5px)`;
  article.querySelector(".scan-probability").textContent = probability === null ? "Sin dato" : percent.format(probability);
  const trendChange = article.querySelector(".trend-change");
  const hasDailyChange = intelligence.change24h !== null;
  trendChange.textContent = formatChange(intelligence.change24h);
  trendChange.classList.toggle("down", hasDailyChange && intelligence.change24h < 0);
  article.querySelector(".scan-change").textContent = trendChange.textContent;
  article.querySelector(".scan-change").classList.toggle("down", hasDailyChange && intelligence.change24h < 0);
  article.querySelector(".scan-activity").textContent = intelligence.activityScore === null ? "Sin dato" : `${intelligence.activityScore}/100`;
  const chart = article.querySelector(".trend-chart");
  const observedPoints = Array.isArray(intelligence.points) ? intelligence.points.filter(point => finite(point.probability) !== null) : [];
  const chartOrigin = observedPoints[0]?.probability ?? 0.5;
  const chartPoints = observedPoints.map((point, index) => {
    const y = Math.max(4, Math.min(50, 27 - (point.probability - chartOrigin) / .2 * 42));
    const x = observedPoints.length === 1 ? 0 : index / (observedPoints.length - 1) * 240;
    return `${x},${y}`;
  });
  if (chartPoints.length === 1) chartPoints.push(`240,${chartPoints[0].split(",")[1]}`);
  chart.querySelector("polyline").setAttribute("points", chartPoints.join(" "));
  const [lastX, lastY] = chartPoints.length ? chartPoints[chartPoints.length - 1].split(",") : [0, 27];
  chart.querySelector("circle").setAttribute("cx", lastX);
  chart.querySelector("circle").setAttribute("cy", lastY);
  chart.querySelector("circle").hidden = chartPoints.length === 0;
  chart.querySelector("title").textContent = hasDailyChange
    ? `El precio cambió ${percent.format(intelligence.change24h)} en 24 horas`
    : "Gamma no publicó una variación de 24 horas para este mercado";
  const confidenceElement = article.querySelector(".confidence");
  confidenceElement.classList.add(`confidence-${displayedLevel}`);
  confidenceElement.textContent = confidence.score === null ? "Sin datos" : displayedLevel === "unknown" ? `Datos insuficientes · ${confidence.coverage}% cobertura` : `${confidenceLabels[displayedLevel]} · ${confidence.score}/100`;
  confidenceElement.setAttribute("aria-label", confidence.score === null ? "Calidad de señal no disponible" : `Calidad de señal ${confidence.score} de 100, cobertura ${confidence.coverage}%`);
  article.querySelector(".scan-confidence").textContent = confidence.score === null ? "Sin dato" : displayedLevel === "unknown" ? `Cobertura ${confidence.coverage}%` : `${confidence.score}/100`;
  const factorList = article.querySelector(".factor-list");
  for (const factor of confidence.factors || []) {
    const item = document.createElement("span");
    item.textContent = `${factor.label} ${factor.score}/100`;
    factorList.append(item);
  }
  if (!factorList.children.length) factorList.textContent = "Gamma no publicó factores suficientes.";
  article.querySelector(".confidence-details small").textContent = `Puntaje parcial ${confidence.score ?? "sin dato"}/100 · cobertura de datos ${confidence.coverage}%`;
  article.querySelector(".activity-ratio").textContent = intelligence.activityRatio === null ? "Sin dato" : `${intelligence.activityRatio.toFixed(1)}×`;
  const activityLabels = { high: "Alta", medium: "Media", low: "Baja", unknown: "Sin dato" };
  const activity = article.querySelector(".activity-pressure");
  activity.textContent = intelligence.activityScore === null ? "Sin dato" : `${activityLabels[intelligence.activityLevel]} · ${intelligence.activityScore}`;
  activity.classList.add(`risk-${intelligence.activityLevel}`);
  article.querySelector(".spread").textContent = market.spread === null ? "Sin dato" : percent.format(market.spread);
  const signalReason = market.signalDossier?.status === "attention"
    ? (market.signalDossier.triggers?.[0]?.label || intelligence.explanation)
    : intelligence.activityScore >= 70
      ? `Actividad alta · ${intelligence.activityScore}/100`
      : hasDailyChange && trendChange.textContent !== "Sin cambio material"
        ? `Movimiento de ${trendChange.textContent} en 24h`
        : hasDailyChange ? "Sin cambio material en 24h" : "Sin anomalías observables";
  article.querySelector(".scan-reason strong").textContent = signalReason;
  article.querySelector(".market-comparison").textContent = probability === null ? "Sin dato" : percent.format(probability);
  article.querySelector(".book-comparison").textContent = market.bestBid === null || market.bestAsk === null
    ? "Sin dato"
    : `${percent.format(market.bestBid)} / ${percent.format(market.bestAsk)}`;
  const gbm = market.gbm;
  const gbmMeta = article.querySelector(".gbm-meta");
  const gbmMedian = article.querySelector(".gbm-median");
  const gbmRange = article.querySelector(".gbm-range");
  const gbmNote = article.querySelector(".gbm-note");
  if (gbm?.status === "available") {
    gbmMeta.textContent = `${gbm.paths.toLocaleString("es-ES")} trayectorias · ${Math.round(gbm.horizonDays)} días`;
    gbmMedian.textContent = percent.format(gbm.median);
    gbmRange.textContent = `${percent.format(gbm.p05)}–${percent.format(gbm.p95)}`;
    const calibration = gbm.calibration === "gamma" ? "calibrada con Gamma" : "volatilidad asumida";
    gbmNote.textContent = `σ anualizada ${gbm.volatility.toFixed(2)} · ${calibration}. Proyección probabilística, no pronóstico garantizado.`;
  } else {
    const reasons = {
      short_term_market: "No se ejecuta en mercados con 7 días o menos hasta el cierre.",
      market_expired: "El mercado ya alcanzó su fecha de cierre.",
      missing_expiry: "Gamma no publicó una fecha de cierre válida.",
      price_boundary: "El precio está en un límite incompatible con el modelo.",
      missing_price: "Gamma no publicó un precio de Sí utilizable."
    };
    gbmMeta.textContent = "No disponible";
    gbmMedian.textContent = "--";
    gbmRange.textContent = "--";
    gbmNote.textContent = reasons[gbm?.reason] || "No hay datos suficientes para ejecutar el modelo.";
  }
  article.querySelector(".why-change p").textContent = intelligence.explanation;
  const structure = market.marketStructure || {};
  const openInterest = structure.openInterest || { status: "unavailable" };
  const concentration = structure.walletConcentration || { status: "unavailable" };
  article.querySelector(".dossier-oi").textContent = openInterestLabel(openInterest);
  article.querySelector(".dossier-concentration").textContent = concentrationLabel(concentration);
  const concentrationMethod = article.querySelector(".concentration-method");
  concentrationMethod.textContent = concentration.status === "available"
    ? `Máximo entre outcomes: posiciones de las 5 wallets mayores ÷ OI. Muestra limitada a ${concentration.sampleLimitPerOutcome || 20} holders por outcome.`
    : concentration.status === "unsupported_market_type"
      ? "No se calcula en mercados de riesgo negativo porque la conversión entre outcomes altera el denominador."
      : "Polymarket Data API no publicó OI y holders suficientes para calcular esta lectura.";
  const regional = market.regionalSources || { status: "unavailable", items: [] };
  const regionalList = article.querySelector(".regional-context div");
  for (const source of regional.items || []) {
    const link = document.createElement("a");
    link.href = source.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = `${source.publisher} · ${source.country}`;
    regionalList.append(link);
  }
  article.querySelector(".regional-context small").textContent = regionalList.children.length
    ? "Fuente oficial de resolución identificada por Gamma; no implica elegibilidad regional."
    : "No hay una fuente latinoamericana verificable publicada para este contrato.";
  const dossier = market.signalDossier || { status: "insufficient_data", summary: "Expediente no disponible.", evidence: [], provenance: {} };
  const dossierLabels = { attention: "Requiere atención", monitoring: "Sin anomalías", insufficient_data: "Datos insuficientes" };
  const dossierStatus = article.querySelector(".dossier-status");
  dossierStatus.dataset.state = dossier.status;
  dossierStatus.querySelector("strong").textContent = dossierLabels[dossier.status] || "No evaluado";
  dossierStatus.querySelector("p").textContent = dossier.summary;
  const proof = article.querySelector(".dossier-proof div");
  for (const evidence of dossier.evidence || []) {
    const item = document.createElement("span");
    const formattedValue = evidence.value === null ? "sin dato"
      : evidence.key === "openInterest" ? `${compactNumber.format(evidence.value)} colateral`
        : evidence.kind === "model" || evidence.key === "probability" || evidence.key === "change24h" || evidence.key === "walletConcentration" ? percent.format(evidence.value)
          : evidence.value;
    item.textContent = `${evidence.label}: ${formattedValue}`;
    item.dataset.kind = evidence.kind;
    proof.append(item);
  }
  const observedAt = safeDate(dossier.provenance?.observedAt);
  article.querySelector(".dossier-proof small").textContent = `ID ${dossier.provenance?.marketId || "no publicado"} · Fuente ${dossier.provenance?.source || "no disponible"}${observedAt ? ` · observada ${observedAt.toLocaleString("es-ES")}` : " · fecha de observación no publicada"}`;
  const personalInput = article.querySelector(".personal-belief input");
  const personalEdge = article.querySelector(".personal-edge");
  personalInput.value = savedUi.belief || "";
  personalInput.placeholder = "1–99";
  const updatePersonalEdge = () => {
    if (personalInput.value === "") {
      personalEdge.textContent = "Introduce tu estimación";
      personalEdge.className = "personal-edge";
      return;
    }
    const cost = finite(market.spread);
    if (probability === null || cost === null) {
      personalEdge.textContent = "No calculable: falta precio o spread";
      personalEdge.className = "personal-edge";
      return;
    }
    const belief = clamp(Number(personalInput.value) / 100);
    const value = expectedReturn(belief, probability, cost);
    if (value === null) {
      personalEdge.textContent = "No calculable en este límite de precio";
      personalEdge.className = "personal-edge";
      return;
    }
    personalEdge.textContent = `${value >= 0 ? "+" : "−"}${percent.format(Math.abs(value))} retorno relativo`;
    personalEdge.className = value >= 0 ? "personal-edge edge-positive" : "personal-edge edge-negative";
  };
  personalInput.addEventListener("input", () => {
    state.marketUi.set(key, { ...(state.marketUi.get(key) || {}), belief: personalInput.value });
    updatePersonalEdge();
  });
  updatePersonalEdge();
  const alertButtons = article.querySelectorAll(".alert-button");
  const shortQuestion = market.question.length > 90 ? `${market.question.slice(0, 87)}…` : market.question;
  const updateAlertButton = () => {
    const active = state.alerts.has(key);
    alertButtons.forEach(button => {
      button.setAttribute("aria-pressed", String(active));
      button.textContent = active ? "Dejar de vigilar" : "Vigilar cambios";
      button.setAttribute("aria-label", `${active ? "Dejar de vigilar" : "Vigilar cambios de"} ${shortQuestion}`);
    });
  };
  alertButtons.forEach(button => {
    button.addEventListener("click", () => {
      if (state.alerts.has(key)) state.alerts.delete(key);
      else state.alerts.set(key, {
        id: key, question: market.question, endDate: market.endDate, url: market.url,
        createdAt: new Date().toISOString(), checkedAt: new Date().toISOString(), snapshot: alertSnapshot(market), events: []
      });
      updateAlertButton(); persistAlerts();
    });
  });
  updateAlertButton();
  const details = {
    signal: article.querySelector(".signal-details"),
    confidence: article.querySelector(".confidence-details"),
    dossier: article.querySelector(".market-intelligence")
  };
  for (const [name, detail] of Object.entries(details)) {
    detail.open = Boolean(savedUi[name]);
    detail.addEventListener("toggle", () => {
      state.marketUi.set(key, { ...(state.marketUi.get(key) || {}), [name]: detail.open });
    });
  }
  const compareButton = article.querySelector(".compare-button");
  const updateCompareButton = () => {
    const active = state.comparison.has(key);
    compareButton.setAttribute("aria-pressed", String(active));
    compareButton.textContent = active ? "Quitar de comparación" : "Fijar para comparar";
    compareButton.setAttribute("aria-label", `${active ? "Quitar de la comparación" : "Fijar para comparar"} ${shortQuestion}`);
  };
  compareButton.addEventListener("click", () => {
    if (state.comparison.has(key)) state.comparison.delete(key);
    else if (state.comparison.size < 3) state.comparison.add(key);
    else {
      elements.comparisonStatus.textContent = "Ya hay 3 mercados fijados. Quita uno para sustituirlo.";
      return;
    }
    updateCompareButton();
    renderComparison();
    syncUrl();
  });
  updateCompareButton();
  const metrics = article.querySelectorAll(".metric strong");
  metrics[0].textContent = finite(market.volume) === null ? "Sin dato" : money.format(market.volume);
  metrics[1].textContent = finite(market.liquidity) === null ? "Sin dato" : money.format(market.liquidity);
  metrics[2].textContent = openInterestLabel(market.marketStructure?.openInterest);
  const marketLink = article.querySelector(".market-link");
  const handoffPrice = probability === null ? "precio no disponible" : `Sí ${percent.format(probability)}`;
  marketLink.href = market.url || "https://polymarket.com";
  marketLink.querySelector("span").textContent = `Abrir ${handoffPrice} en Polymarket`;
  marketLink.querySelector("small").textContent = `${lifecycle.label} · ${marketFreshness.label} · elegibilidad no verificada`;
  marketLink.setAttribute("aria-label", `Abrir mercado en Polymarket, ${handoffPrice}; ${lifecycle.label}; ${marketFreshness.label}; elegibilidad no verificada`);
  return article;
}

function renderComparison() {
  const compared = [...state.comparison]
    .map(key => state.markets.find(market => marketKey(market) === key))
    .filter(Boolean);
  const unresolvedCount = state.comparison.size - compared.length;
  elements.comparisonPanel.hidden = compared.length === 0;
  elements.comparisonStatus.textContent = state.comparison.size
    ? unresolvedCount ? `${state.comparison.size} seleccionados · ${compared.length} presentes en la muestra cargada.` : `${compared.length} de 3 mercados fijados.`
    : "Fija hasta 3 mercados desde las tarjetas.";
  elements.comparisonTray.hidden = state.comparison.size === 0;
  elements.comparisonTrayCount.textContent = `${state.comparison.size} ${state.comparison.size === 1 ? "seleccionado" : "seleccionados"}`;
  elements.comparisonTrayAction.disabled = compared.length < 2;
  elements.comparisonTrayAction.textContent = unresolvedCount ? "Esperando muestra" : compared.length < 2 ? "Selecciona otro" : "Ver comparación";
  const probabilities = compared.map(market => finite(market.probability)).filter(value => value !== null);
  const coverages = compared.map(market => finite(market.confidence?.coverage)).filter(value => value !== null);
  const gap = probabilities.length > 1 ? Math.max(...probabilities) - Math.min(...probabilities) : null;
  const lifecycleWarning = compared.some(market => classifyLifecycle(market, sourceClockNow()).code !== "open");
  elements.comparisonSummary.textContent = unresolvedCount
    ? `${unresolvedCount} ${unresolvedCount === 1 ? "selección no está" : "selecciones no están"} en la muestra cargada. Conservamos el enlace; carga más mercados para recuperarla.`
    : compared.length < 2
    ? "Añade otro mercado para medir divergencia y calidad de evidencia."
    : `${gap === null ? "Divergencia no calculable" : `Divergencia máxima ${percent.format(gap)}`} · ${coverages.length ? `cobertura ${Math.min(...coverages)}–${Math.max(...coverages)}%` : "cobertura no publicada"}${lifecycleWarning ? " · revisa el estado de ciclo de vida" : ""}.`;
  elements.comparisonItems.replaceChildren(...compared.map(market => {
    const item = document.createElement("article");
    const probability = finite(market.probability);
    const change = finite(market.intelligence?.change24h);
    const confidence = finite(market.confidence?.score);
    item.innerHTML = `<h4></h4><p class="comparison-item-state"></p><dl><div><dt>Precio de Sí</dt><dd></dd></div><div><dt>Cambio 24 h</dt><dd></dd></div><div><dt>Solidez</dt><dd></dd></div><div><dt>Spread</dt><dd></dd></div><div><dt>Interés abierto</dt><dd></dd></div><div><dt>Concentración</dt><dd></dd></div></dl><p class="comparison-item-reason"></p><button type="button">Quitar</button>`;
    item.querySelector("h4").textContent = market.question;
    const values = item.querySelectorAll("dd");
    values[0].textContent = probability === null ? "Sin dato" : percent.format(probability);
    values[1].textContent = formatChange(change);
    values[2].textContent = confidence === null ? "Sin dato" : market.confidence.coverage < 50 ? `Cobertura ${market.confidence.coverage}%` : `${confidence}/100`;
    values[3].textContent = finite(market.spread) === null ? "Sin dato" : percent.format(market.spread);
    values[4].textContent = openInterestLabel(market.marketStructure?.openInterest);
    values[5].textContent = concentrationLabel(market.marketStructure?.walletConcentration);
    item.querySelector(".comparison-item-state").textContent = `${classifyLifecycle(market, sourceClockNow()).label} · ${freshness(market.updatedAt, sourceClockNow()).label}`;
    item.querySelector(".comparison-item-reason").textContent = market.signalDossier?.summary || market.intelligence?.explanation || "Sin explicación publicada.";
    const removeButton = item.querySelector("button");
    removeButton.setAttribute("aria-label", `Quitar ${market.question} de la comparación`);
    removeButton.addEventListener("click", () => {
      state.comparison.delete(marketKey(market));
      render();
      syncUrl();
    });
    return item;
  }));
}

function render() {
  const query = state.search.trim().toLocaleLowerCase("es");
  const now = Date.now();
  const visible = state.markets.filter(market => {
    const text = `${market.question} ${market.category}`.toLocaleLowerCase("es");
    const end = safeDate(market.endDate);
    const daysToClose = end ? (end.getTime() - now) / 86400000 : Infinity;
    return (!query || text.includes(query))
      && (state.category === "all" || market.category === state.category)
      && (state.closing === "all" || (daysToClose >= 0 && daysToClose <= Number(state.closing)))
      && (state.minLiquidity === 0 || (finite(market.liquidity) !== null && market.liquidity >= state.minLiquidity))
      && (!state.opportunities || finite(market.intelligence?.activityScore) >= 70);
  });
  const sorters = {
    activity: (a, b) => (finite(b.intelligence?.activityScore) ?? -Infinity) - (finite(a.intelligence?.activityScore) ?? -Infinity),
    volume: (a, b) => (finite(b.volume) ?? -Infinity) - (finite(a.volume) ?? -Infinity),
    liquidity: (a, b) => (finite(b.liquidity) ?? -Infinity) - (finite(a.liquidity) ?? -Infinity),
    probability: (a, b) => (finite(b.probability) ?? -Infinity) - (finite(a.probability) ?? -Infinity),
    confidence: (a, b) => (finite(b.confidence?.score) ?? -Infinity) - (finite(a.confidence?.score) ?? -Infinity),
    date: (a, b) => (safeDate(a.endDate)?.getTime() || Infinity) - (safeDate(b.endDate)?.getTime() || Infinity)
  };
  visible.sort(sorters[state.sort]);
  elements.grid.replaceChildren(...visible.map(card));
  renderComparison();
  elements.grid.hidden = visible.length === 0;
  elements.empty.hidden = visible.length !== 0 || state.dataState === "error" || state.dataState === "loading";
  elements.count.textContent = state.dataState === "error" && state.markets.length === 0
    ? "Datos no disponibles"
    : `${visible.length} de ${state.markets.length} mercados`;
  const sourceEmpty = state.dataState === "empty" && state.markets.length === 0;
  elements.emptyTitle.textContent = sourceEmpty ? "Gamma no devolvió mercados abiertos en esta consulta" : "Ese mercado no está en el radar";
  elements.emptyCopy.textContent = sourceEmpty ? "La fuente respondió correctamente, pero la muestra está vacía. Vuelve a intentarlo más tarde." : "Prueba otra búsqueda o restablece los filtros para ver la muestra completa.";
  elements.reset.hidden = sourceEmpty;
  syncUrl();
}

function populateCategories() {
  elements.category.replaceChildren(new Option("Todas las categorías", "all"));
  [...new Set(state.markets.map(market => market.category).filter(Boolean))].sort().forEach(value => {
    const option = document.createElement("option");
    option.value = value; option.textContent = value; elements.category.append(option);
  });
  if (state.category !== "all" && ![...elements.category.options].some(option => option.value === state.category)) {
    elements.category.add(new Option(state.category, state.category));
  }
  elements.category.value = state.category;
}

function showNotice(message) {
  const text = document.createElement("span");
  const retry = document.createElement("button");
  text.textContent = message;
  retry.type = "button";
  retry.textContent = "Reintentar";
  retry.addEventListener("click", () => loadMarkets({ reset: true }));
  elements.notice.replaceChildren(text, retry);
  elements.notice.hidden = false;
}

async function loadMarkets({ reset = false, refresh = false } = {}) {
  if (state.loading) return;
  state.loading = true;
  elements.loadMore.setAttribute("aria-busy", "true");
  elements.loadMore.textContent = "Cargando mercados…";
  if (reset) {
    state.markets = [];
    state.nextOffset = 0;
    state.hasMore = false;
    elements.notice.hidden = true;
    elements.loading.hidden = false;
    state.dataState = "loading";
    setDataStatus("loading", "Conectando");
  }
  try {
    const requestedLimit = refresh ? Math.max(PAGE_SIZE, Math.min(state.markets.length, 100)) : PAGE_SIZE;
    const requestedOffset = refresh ? 0 : state.nextOffset;
    const response = await fetch(`/api/markets?limit=${requestedLimit}&offset=${requestedOffset}`, { headers: { Accept: "application/json" } });
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json") ? await response.json() : null;
    if (!response.ok) throw new Error(payload?.error?.message || `API ${response.status}`);
    if (!Array.isArray(payload.markets)) throw new Error("Invalid response");
    const markets = new Map((reset || refresh ? [] : state.markets).map(market => [marketKey(market), market]));
    const previousSize = markets.size;
    payload.markets.forEach(market => {
      if (!market.intelligence || market.intelligence.source !== "gamma") throw new Error("Invalid intelligence response");
      if (!market.signalDossier || !["attention", "monitoring", "insufficient_data"].includes(market.signalDossier.status)) throw new Error("Invalid signal dossier response");
      evaluateWatchedMarket(market);
      markets.set(marketKey(market), market);
    });
    state.markets = [...markets.values()];
    state.hasMore = payload.hasMore === true;
    state.nextOffset = Number(payload.nextOffset);
    if (state.hasMore && (!Number.isSafeInteger(state.nextOffset) || state.nextOffset < 0 || (!refresh && markets.size === previousSize))) throw new Error("Invalid pagination response");
    const dataTime = safeDate(payload.dataAsOf);
    setDataStatus("live", dataTime
      ? `Gamma · ${dataTime.toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit", timeZone: "UTC" })} UTC`
      : "Datos de Gamma");
    state.dataState = payload.dataStatus === "empty" ? "empty" : "live";
    state.responseGeneratedAt = payload.generatedAt || null;
    state.responseReceivedAt = Date.now();
    if (state.dataState === "empty") setDataStatus("empty", "Gamma · sin contratos");
    elements.notice.hidden = true;
  } catch (error) {
    console.warn("Live market data unavailable:", error);
    if (state.markets.length === 0) {
      state.dataState = "error";
      setDataStatus("error", "Datos no disponibles");
      showNotice("Gamma no responde en este momento. No se muestran datos ficticios.");
    } else {
      state.dataState = "partial";
      setDataStatus("partial", "Datos parciales");
      showNotice("La carga se interrumpió. Conservamos los mercados recuperados.");
    }
    state.hasMore = false;
  } finally {
    state.loading = false;
    elements.loading.hidden = true;
    elements.loading.setAttribute("aria-busy", "false");
    elements.loadMore.hidden = !state.hasMore;
    elements.loadMore.removeAttribute("aria-busy");
    elements.loadMore.textContent = "Cargar más mercados";
    populateCategories(); updateKpis(); render(); persistAlerts();
  }
}

elements.search.addEventListener("input", event => { state.search = event.target.value; render(); });
elements.category.addEventListener("change", event => { state.category = event.target.value; render(); });
elements.closing.addEventListener("change", event => { state.closing = event.target.value; render(); });
elements.minLiquidity.addEventListener("change", event => { state.minLiquidity = Number(event.target.value); render(); });
elements.opportunities.addEventListener("change", event => { state.opportunities = event.target.checked; render(); });
elements.sort.addEventListener("change", event => { state.sort = event.target.value; render(); });
elements.reset.addEventListener("click", () => {
  state.search = ""; state.category = "all"; state.closing = "all"; state.minLiquidity = 0; state.opportunities = false; state.sort = "activity";
  elements.search.value = ""; elements.category.value = "all"; elements.closing.value = "all";
  elements.minLiquidity.value = "0"; elements.opportunities.checked = false; elements.sort.value = "activity"; render();
  elements.search.focus();
});
elements.loadMore.addEventListener("click", () => loadMarkets());
elements.filterToggle.addEventListener("click", () => {
  const expanded = elements.filterToggle.getAttribute("aria-expanded") !== "true";
  elements.filterToggle.setAttribute("aria-expanded", String(expanded));
  elements.filters.dataset.mobileCollapsed = String(!expanded);
});
projectionElements.form?.addEventListener("submit", event => { event.preventDefault(); loadProjection(); });
elements.comparisonTrayAction.addEventListener("click", () => {
  if (state.comparison.size < 2) return;
  elements.comparisonPanel.scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  elements.comparisonPanel.focus({ preventScroll: true });
});
window.addEventListener("popstate", () => { applyUrlState(); populateCategories(); render(); });
const projectionLayer = document.querySelector("#projection-layer");
let projectionInitialized = false;
projectionLayer?.addEventListener("toggle", () => {
  if (projectionLayer.open && !projectionInitialized) {
    projectionInitialized = true;
    loadProjection();
  }
});
setInterval(() => {
  if (document.visibilityState === "visible" && projectionLayer?.open) loadProjection();
}, 5 * 60 * 1000);
setInterval(() => {
  if (document.visibilityState === "visible" && !state.loading) loadMarkets({ refresh: true });
}, 5 * 60 * 1000);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && state.markets.length && !state.loading) loadMarkets({ refresh: true });
});
document.querySelector('a[href="#alerts"]')?.addEventListener("click", () => {
  document.querySelector("#alert-layer").open = true;
});

applyUrlState();
persistAlerts();
loadMarkets({ reset: true });
