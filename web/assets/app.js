"use strict";

const DEMO_MARKETS = [
  { id: "demo-1", question: "¿Cotizará Bitcoin por encima de 150.000 $ antes de 2027?", category: "Cripto", probability: .42, volume: 18400000, volume24h: 520000, liquidity: 840000, confidence: { score: 86, level: "high", coverage: 100, factors: [] }, endDate: "2026-12-31T00:00:00Z", url: "https://polymarket.com" },
  { id: "demo-2", question: "¿Recortará tipos la Reserva Federal en su próxima reunión?", category: "Economía", probability: .67, volume: 7200000, volume24h: 310000, liquidity: 460000, confidence: { score: 78, level: "high", coverage: 100, factors: [] }, endDate: "2026-09-16T00:00:00Z", url: "https://polymarket.com" },
  { id: "demo-3", question: "¿Liderará un modelo de IA las listas mundiales de aplicaciones este trimestre?", category: "Tecnología", probability: .31, volume: 2900000, volume24h: 94000, liquidity: 175000, confidence: { score: 61, level: "medium", coverage: 85, factors: [] }, endDate: "2026-09-30T00:00:00Z", url: "https://polymarket.com" },
  { id: "demo-4", question: "¿Debutará un nuevo álbum en el número uno este mes?", category: "Cultura", probability: .56, volume: 1300000, volume24h: 68000, liquidity: 98000, confidence: { score: 48, level: "low", coverage: 75, factors: [] }, endDate: "2026-08-31T00:00:00Z", url: "https://polymarket.com" }
];

const storedAlerts = (() => {
  try { return new Set(JSON.parse(localStorage.getItem("pele-alerts") || "[]")); }
  catch { return new Set(); }
})();
const state = {
  markets: [], search: "", category: "all", closing: "all", minLiquidity: 0,
  opportunities: false, sort: "volume", nextOffset: 0, hasMore: false, loading: false,
  alerts: storedAlerts
};
const elements = {
  grid: document.querySelector("#market-grid"), loading: document.querySelector("#loading"),
  empty: document.querySelector("#empty"), notice: document.querySelector("#notice"),
  search: document.querySelector("#search"), category: document.querySelector("#category"),
  sort: document.querySelector("#sort"), count: document.querySelector("#result-count"),
  closing: document.querySelector("#closing"), minLiquidity: document.querySelector("#min-liquidity"),
  opportunities: document.querySelector("#opportunities"), loadMore: document.querySelector("#load-more"),
  dataStatus: document.querySelector("#data-status"), dataStatusLabel: document.querySelector("#data-status-label"),
  alertCount: document.querySelector("#alert-count"), alertDeskCount: document.querySelector("#alert-desk-count")
};
const money = new Intl.NumberFormat("es-ES", { style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1 });
const percent = new Intl.NumberFormat("es-ES", { style: "percent", maximumFractionDigits: 0 });
const date = new Intl.DateTimeFormat("es-ES", { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" });

function safeDate(value) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function clamp(value, minimum = 0, maximum = 1) {
  return Math.min(maximum, Math.max(minimum, value));
}

function hash(value) {
  let result = 2166136261;
  for (const character of String(value)) {
    result ^= character.charCodeAt(0);
    result = Math.imul(result, 16777619);
  }
  return result >>> 0;
}

function enrichMarket(market) {
  const seed = hash(market.id || `${market.question}:${market.endDate}`);
  const probability = clamp(Number(market.probability || 0));
  const reportedChange = Number(market.priceChange24h);
  const change24h = Number.isFinite(reportedChange) ? reportedChange : ((seed % 1901) - 950) / 10000;
  let random = seed || 1;
  const points = Array.from({ length: 12 }, (_, index) => {
    random = (Math.imul(random, 1664525) + 1013904223) >>> 0;
    const progress = index / 11;
    const noise = ((random / 4294967295) - .5) * .035;
    return clamp(probability - change24h * (1 - progress) + noise * Math.sin(progress * Math.PI));
  });
  points[points.length - 1] = probability;
  const whaleShare = 32 + seed % 59;
  const activityRatio = Number(market.volume24h || 0) / Math.max(Number(market.liquidity || 0), 1);
  const anomalyScore = Math.round(clamp((whaleShare - 35) / 65 * .65 + activityRatio * .35) * 100);
  const risk = anomalyScore >= 68 ? "high" : anomalyScore >= 42 ? "medium" : "low";
  const external = clamp(probability + (((seed >> 8) % 2101) - 1050) / 10000);
  const personal = clamp(probability + (((seed >> 16) % 2601) - 800) / 10000);
  const cost = Math.max(Number(market.spread || 0), .012);
  const edge = personal / Math.max(probability, .01) - 1 - cost / Math.max(probability, .01);
  const externalSource = /polit/i.test(market.category) ? "Encuestas" : /sport|deport/i.test(market.category)
    ? "Casas tradicionales" : /econom/i.test(market.category) ? "Referencia macro" : "Índice externo";
  const direction = change24h >= 0 ? "subió" : "bajó";
  const catalyst = activityRatio > .6 ? "un pico de actividad" : whaleShare > 72
    ? "una concentración inusual de capital" : "un reajuste gradual del consenso";
  return {
    ...market,
    intelligence: {
      points, change24h, whaleShare, anomalyScore, risk, external, externalSource, personal, edge,
      explanation: `La probabilidad ${direction} ${percent.format(Math.abs(change24h))} tras ${catalyst}. No hay una fuente causal verificada conectada; esta lectura es algorítmica.`
    }
  };
}

function persistAlerts() {
  try { localStorage.setItem("pele-alerts", JSON.stringify([...state.alerts])); } catch {}
  elements.alertCount.textContent = state.alerts.size;
  elements.alertDeskCount.textContent = state.alerts.size
    ? `${state.alerts.size} ${state.alerts.size === 1 ? "mercado vigilado" : "mercados vigilados"}`
    : "Sin mercados vigilados";
  const eventDesk = document.querySelector("#alert-events");
  const events = state.markets.filter(market => state.alerts.has(String(market.id || `${market.question}:${market.endDate}`))).map(market => {
    const triggers = [];
    if (Math.abs(market.intelligence.change24h) >= .05) triggers.push("cambio de convicción");
    if (market.intelligence.anomalyScore >= 68) triggers.push("actividad anómala");
    if (market.intelligence.edge >= .08) triggers.push("EV superior a 8%");
    const item = document.createElement("span");
    item.dataset.triggered = String(triggers.length > 0);
    item.textContent = `${triggers.length ? "Disparo" : "Vigilando"}: ${market.question.slice(0, 48)}${market.question.length > 48 ? "…" : ""}${triggers.length ? ` · ${triggers.join(" + ")}` : ""}`;
    return item;
  });
  const empty = document.createElement("span");
  empty.textContent = state.alerts.size ? "Esperando la siguiente evaluación de mercado." : "Sin eventos evaluados.";
  eventDesk.replaceChildren(...(events.length ? events : [empty]));
}

function updateKpis() {
  const total = key => state.markets.reduce((sum, market) => sum + Number(market[key] || 0), 0);
  document.querySelector("#kpi-count").textContent = state.markets.length.toLocaleString("es-ES");
  document.querySelector("#kpi-volume").textContent = money.format(total("volume"));
  document.querySelector("#kpi-liquidity").textContent = money.format(total("liquidity"));
  const average = state.markets.length ? total("probability") / state.markets.length : 0;
  document.querySelector("#kpi-probability").textContent = percent.format(average);
}

function setDataStatus(status, label) {
  elements.dataStatus.dataset.state = status;
  elements.dataStatusLabel.textContent = label;
}

function card(market) {
  const article = document.createElement("article");
  article.className = "market-card";
  const end = safeDate(market.endDate);
  const probability = Math.max(0, Math.min(1, Number(market.probability || 0)));
  const confidence = market.confidence || { score: 0, level: "low", coverage: 0, factors: [] };
  const intelligence = market.intelligence;
  const displayedLevel = confidence.coverage < 50 ? "low" : confidence.level;
  const confidenceLabels = { high: "Alta", medium: "Media", low: "Baja" };
  article.innerHTML = `
    <div class="probability-rail" aria-hidden="true"><i></i><span></span></div>
    <div class="card-content">
      <div class="card-top"><span class="category"></span><span class="date"></span></div>
      <h3></h3>
      <div class="probability-row"><span>El mercado dice “Sí”</span><strong></strong></div>
      <div class="trend-block">
        <div class="trend-head"><span>Convicción · 24h · demo</span><strong class="trend-change"></strong></div>
        <svg class="trend-chart" viewBox="0 0 240 54" preserveAspectRatio="none" role="img"><title></title><line class="baseline" x1="0" y1="27" x2="240" y2="27"></line><polyline class="trend-line"></polyline><circle class="trend-dot" r="3.5"></circle></svg>
      </div>
      <div class="confidence-row"><span>Solidez de la señal</span><strong class="confidence"></strong></div>
      <div class="signal-grid">
        <div><span>Ballenas / retail · demo</span><strong class="whale-share"></strong></div>
        <div><span>Manipulación · demo</span><strong class="anomaly-risk"></strong></div>
        <div><span>EV / 1 USDC · demo</span><strong class="edge"></strong></div>
      </div>
      <details class="confidence-details"><summary>Cómo se calcula</summary><div class="factor-list"></div><small></small></details>
      <details class="market-intelligence">
        <summary>Abrir expediente</summary>
        <div class="intelligence-detail">
          <div class="comparison-row">
            <div><span>Polymarket</span><strong class="market-comparison"></strong></div>
            <div><span class="external-source"></span><strong class="external-comparison"></strong></div>
          </div>
          <div class="why-change"><span>Qué explica el cambio</span><p></p></div>
          <label class="personal-belief"><span>Tu estimación (%)</span><input type="number" min="1" max="99" step="1"><strong class="personal-edge"></strong></label>
          <small class="ev-formula">EV = probabilidad propia ÷ precio − 1 − spread estimado.</small>
          <button class="alert-button" type="button" aria-pressed="false"></button>
        </div>
      </details>
      <div class="card-footer">
        <div class="metric"><span>Volumen</span><strong></strong></div>
        <div class="metric"><span>Liquidez</span><strong></strong></div>
        <a class="market-link" target="_blank" rel="noopener noreferrer" aria-label="Abrir mercado en Polymarket"><span>Ver mercado</span> &#8599;</a>
      </div>
    </div>`;
  article.querySelector(".category").textContent = market.category || "Other";
  article.querySelector(".date").textContent = end ? date.format(end) : "Sin fecha";
  article.querySelector("h3").textContent = market.question;
  article.querySelector(".probability-rail i").style.height = `${probability * 100}%`;
  article.querySelector(".probability-rail span").style.bottom = `calc(${probability * 100}% - 5px)`;
  article.querySelector(".probability-row strong").textContent = percent.format(probability);
  const trendChange = article.querySelector(".trend-change");
  trendChange.textContent = `${intelligence.change24h >= 0 ? "+" : "−"}${percent.format(Math.abs(intelligence.change24h))}`;
  trendChange.classList.toggle("down", intelligence.change24h < 0);
  const chart = article.querySelector(".trend-chart");
  const chartOrigin = intelligence.points[0];
  const chartPoints = intelligence.points.map((value, index) => {
    const y = Math.max(4, Math.min(50, 27 - (value - chartOrigin) / .2 * 42));
    return `${index / (intelligence.points.length - 1) * 240},${y}`;
  });
  chart.querySelector("polyline").setAttribute("points", chartPoints.join(" "));
  const [lastX, lastY] = chartPoints[chartPoints.length - 1].split(",");
  chart.querySelector("circle").setAttribute("cx", lastX);
  chart.querySelector("circle").setAttribute("cy", lastY);
  chart.querySelector("title").textContent = `La convicción cambió ${percent.format(intelligence.change24h)} en 24 horas`;
  const confidenceElement = article.querySelector(".confidence");
  confidenceElement.classList.add(`confidence-${displayedLevel}`);
  confidenceElement.textContent = `${confidenceLabels[displayedLevel] || "Baja"} · ${confidence.score}/100`;
  confidenceElement.setAttribute("aria-label", `Calidad de señal ${confidence.score} de 100, cobertura ${confidence.coverage}%`);
  const factorList = article.querySelector(".factor-list");
  for (const factor of confidence.factors || []) {
    const item = document.createElement("span");
    item.textContent = `${factor.label} ${factor.score}/100`;
    factorList.append(item);
  }
  if (!factorList.children.length) factorList.textContent = "Detalle no disponible en datos demo.";
  article.querySelector(".confidence-details small").textContent = `Cobertura de datos: ${confidence.coverage}%`;
  article.querySelector(".whale-share").textContent = `${intelligence.whaleShare} / ${100 - intelligence.whaleShare}%`;
  const riskLabels = { high: "Alto", medium: "Medio", low: "Bajo" };
  const anomaly = article.querySelector(".anomaly-risk");
  anomaly.textContent = `${riskLabels[intelligence.risk]} · ${intelligence.anomalyScore}`;
  anomaly.classList.add(`risk-${intelligence.risk}`);
  const edge = article.querySelector(".edge");
  edge.textContent = `${intelligence.edge >= 0 ? "+" : "−"}${money.format(Math.abs(intelligence.edge))}`;
  edge.classList.add(intelligence.edge >= 0 ? "edge-positive" : "edge-negative");
  article.querySelector(".market-comparison").textContent = percent.format(probability);
  article.querySelector(".external-source").textContent = `${intelligence.externalSource} · demo`;
  article.querySelector(".external-comparison").textContent = percent.format(intelligence.external);
  article.querySelector(".why-change p").textContent = intelligence.explanation;
  const personalInput = article.querySelector(".personal-belief input");
  const personalEdge = article.querySelector(".personal-edge");
  personalInput.value = Math.round(intelligence.personal * 100);
  const updatePersonalEdge = () => {
    const belief = clamp(Number(personalInput.value) / 100);
    const cost = Math.max(Number(market.spread || 0), .012);
    const value = belief / Math.max(probability, .01) - 1 - cost / Math.max(probability, .01);
    personalEdge.textContent = `${value >= 0 ? "+" : "−"}${money.format(Math.abs(value))}`;
    personalEdge.className = value >= 0 ? "personal-edge edge-positive" : "personal-edge edge-negative";
  };
  personalInput.addEventListener("input", updatePersonalEdge);
  updatePersonalEdge();
  const alertButton = article.querySelector(".alert-button");
  const marketKey = String(market.id || `${market.question}:${market.endDate}`);
  const updateAlertButton = () => {
    const active = state.alerts.has(marketKey);
    alertButton.setAttribute("aria-pressed", String(active));
    alertButton.textContent = active ? "Alerta activa · desactivar" : "Vigilar cambios y anomalías";
  };
  alertButton.addEventListener("click", () => {
    if (state.alerts.has(marketKey)) state.alerts.delete(marketKey); else state.alerts.add(marketKey);
    updateAlertButton(); persistAlerts();
  });
  updateAlertButton();
  const metrics = article.querySelectorAll(".metric strong");
  metrics[0].textContent = money.format(market.volume || 0);
  metrics[1].textContent = money.format(market.liquidity || 0);
  article.querySelector("a").href = market.url || "https://polymarket.com";
  return article;
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
      && Number(market.liquidity || 0) >= state.minLiquidity
      && (!state.opportunities || market.intelligence.edge >= .08);
  });
  const sorters = {
    volume: (a, b) => b.volume - a.volume,
    liquidity: (a, b) => b.liquidity - a.liquidity,
    probability: (a, b) => b.probability - a.probability,
    confidence: (a, b) => (b.confidence?.score || 0) - (a.confidence?.score || 0),
    date: (a, b) => (safeDate(a.endDate)?.getTime() || Infinity) - (safeDate(b.endDate)?.getTime() || Infinity)
  };
  visible.sort(sorters[state.sort]);
  elements.grid.replaceChildren(...visible.map(card));
  elements.grid.hidden = visible.length === 0;
  elements.empty.hidden = visible.length !== 0;
  elements.count.textContent = `${visible.length} de ${state.markets.length} mercados`;
}

function populateCategories() {
  elements.category.replaceChildren(new Option("Todas las categorías", "all"));
  [...new Set(state.markets.map(market => market.category).filter(Boolean))].sort().forEach(value => {
    const option = document.createElement("option");
    option.value = value; option.textContent = value; elements.category.append(option);
  });
  elements.category.value = [...elements.category.options].some(option => option.value === state.category) ? state.category : "all";
  state.category = elements.category.value;
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

async function loadMarkets({ reset = false } = {}) {
  if (state.loading) return;
  state.loading = true;
  elements.loadMore.setAttribute("aria-busy", "true");
  elements.loadMore.textContent = "Cargando...";
  if (reset) {
    state.markets = [];
    state.nextOffset = 0;
    state.hasMore = false;
    elements.notice.hidden = true;
    elements.loading.hidden = false;
    setDataStatus("loading", "Conectando");
  }
  try {
    const response = await fetch(`/api/markets?limit=100&offset=${state.nextOffset}`, { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`API ${response.status}`);
    const payload = await response.json();
    if (!Array.isArray(payload.markets)) throw new Error("Invalid response");
    const markets = new Map(state.markets.map(market => [market.id || `${market.question}:${market.endDate}`, market]));
    const previousSize = markets.size;
    payload.markets.forEach(market => markets.set(market.id || `${market.question}:${market.endDate}`, enrichMarket(market)));
    state.markets = [...markets.values()];
    state.hasMore = payload.hasMore === true;
    state.nextOffset = Number(payload.nextOffset);
    if (state.hasMore && (!Number.isSafeInteger(state.nextOffset) || state.nextOffset < 0 || markets.size === previousSize)) throw new Error("Invalid pagination response");
    if (state.markets.length === 0) throw new Error("Empty response");
    setDataStatus("live", "Datos en directo");
    elements.notice.hidden = true;
  } catch (error) {
    console.warn("Using demo market fallback:", error);
    if (state.markets.length === 0) {
      state.markets = DEMO_MARKETS.map(enrichMarket);
      setDataStatus("demo", "Modo demostración");
      showNotice("Gamma no responde en este momento. Mostramos datos de demostración.");
    } else {
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
document.querySelector("#reset").addEventListener("click", () => {
  state.search = ""; state.category = "all"; state.closing = "all"; state.minLiquidity = 0; state.opportunities = false; state.sort = "volume";
  elements.search.value = ""; elements.category.value = "all"; elements.closing.value = "all";
  elements.minLiquidity.value = "0"; elements.opportunities.checked = false; elements.sort.value = "volume"; render();
  elements.search.focus();
});
elements.loadMore.addEventListener("click", () => loadMarkets());

persistAlerts();
loadMarkets({ reset: true });
