"use strict";

const PAGE_SIZE = 10;

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
  alertCount: document.querySelector("#alert-count"), alertDeskCount: document.querySelector("#alert-desk-count"),
  filters: document.querySelector("#filters"), filterToggle: document.querySelector("#filter-toggle")
};
const money = new Intl.NumberFormat("es-ES", { style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1 });
const percent = new Intl.NumberFormat("es-ES", { style: "percent", maximumFractionDigits: 0 });
const date = new Intl.DateTimeFormat("es-ES", { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" });

function safeDate(value) {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function clamp(value, minimum = 0, maximum = 1) {
  return Math.min(maximum, Math.max(minimum, value));
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
    if (market.intelligence.change24h !== null && Math.abs(market.intelligence.change24h) >= .05) triggers.push("cambio de precio");
    if (market.intelligence.activityScore >= 70) triggers.push("presión de actividad");
    if (market.spread !== null && market.spread >= .05) triggers.push("spread amplio");
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
  const verdict = document.querySelector("#verdict");
  const yes = Math.round(average * 100);
  verdict.style.setProperty("--yes", `${yes}%`);
  document.querySelector("#verdict-yes").textContent = `${yes}%`;
  document.querySelector("#verdict-no").textContent = `${100 - yes}%`;
  verdict.setAttribute("aria-label", `Consenso agregado: Sí ${yes}%, No ${100 - yes}%`);
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
      <div class="model-readout" aria-label="Lecturas derivadas de datos de mercado">
        <div class="readout-label"><span>Lectura PELE</span><small>Datos Gamma + cálculo</small></div>
        <div class="trend-block">
          <div class="trend-head"><span>Convicción · 24h</span><strong class="trend-change"></strong></div>
          <svg class="trend-chart" viewBox="0 0 240 54" preserveAspectRatio="none" role="img"><title></title><line class="baseline" x1="0" y1="27" x2="240" y2="27"></line><polyline class="trend-line"></polyline><circle class="trend-dot" r="3.5"></circle></svg>
        </div>
        <div class="confidence-row"><span>Solidez de la señal</span><strong class="confidence"></strong></div>
        <div class="signal-grid">
          <div><span>Rotación 24h</span><strong class="activity-ratio"></strong></div>
          <div><span>Presión actividad</span><strong class="activity-pressure"></strong></div>
          <div><span>Spread</span><strong class="spread"></strong></div>
        </div>
      </div>
      <details class="confidence-details"><summary>Cómo se calcula</summary><div class="factor-list"></div><small></small></details>
      <details class="market-intelligence">
        <summary>Abrir expediente</summary>
        <div class="intelligence-detail">
          <div class="comparison-row">
            <div><span>Polymarket</span><strong class="market-comparison"></strong></div>
            <div><span>Bid / ask</span><strong class="book-comparison"></strong></div>
          </div>
          <div class="gbm-projection">
            <div class="gbm-heading"><span>Movimiento Browniano Geométrico</span><small class="gbm-meta"></small></div>
            <div class="gbm-values">
              <div><span>Mediana al cierre</span><strong class="gbm-median"></strong></div>
              <div><span>Rango P5–P95</span><strong class="gbm-range"></strong></div>
            </div>
            <p class="gbm-note"></p>
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
  const hasDailyChange = intelligence.change24h !== null;
  trendChange.textContent = hasDailyChange
    ? `${intelligence.change24h >= 0 ? "+" : "−"}${percent.format(Math.abs(intelligence.change24h))}`
    : "Sin dato";
  trendChange.classList.toggle("down", hasDailyChange && intelligence.change24h < 0);
  const chart = article.querySelector(".trend-chart");
  const observedPoints = intelligence.points || [{ hoursAgo: 0, probability }];
  const chartOrigin = observedPoints[0].probability;
  const chartPoints = observedPoints.map((point, index) => {
    const y = Math.max(4, Math.min(50, 27 - (point.probability - chartOrigin) / .2 * 42));
    const x = observedPoints.length === 1 ? 0 : index / (observedPoints.length - 1) * 240;
    return `${x},${y}`;
  });
  if (chartPoints.length === 1) chartPoints.push(`240,${chartPoints[0].split(",")[1]}`);
  chart.querySelector("polyline").setAttribute("points", chartPoints.join(" "));
  const [lastX, lastY] = chartPoints[chartPoints.length - 1].split(",");
  chart.querySelector("circle").setAttribute("cx", lastX);
  chart.querySelector("circle").setAttribute("cy", lastY);
  chart.querySelector("title").textContent = hasDailyChange
    ? `El precio cambió ${percent.format(intelligence.change24h)} en 24 horas`
    : "Gamma no publicó una variación de 24 horas para este mercado";
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
  if (!factorList.children.length) factorList.textContent = "Gamma no publicó factores suficientes.";
  article.querySelector(".confidence-details small").textContent = `Cobertura de datos: ${confidence.coverage}%`;
  article.querySelector(".activity-ratio").textContent = intelligence.activityRatio === null ? "Sin dato" : `${intelligence.activityRatio.toFixed(1)}×`;
  const activityLabels = { high: "Alta", medium: "Media", low: "Baja", unknown: "Sin dato" };
  const activity = article.querySelector(".activity-pressure");
  activity.textContent = intelligence.activityScore === null ? "Sin dato" : `${activityLabels[intelligence.activityLevel]} · ${intelligence.activityScore}`;
  activity.classList.add(`risk-${intelligence.activityLevel}`);
  article.querySelector(".spread").textContent = market.spread === null ? "Sin dato" : percent.format(market.spread);
  article.querySelector(".market-comparison").textContent = percent.format(probability);
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
      price_boundary: "El precio está en un límite incompatible con el modelo."
    };
    gbmMeta.textContent = "No disponible";
    gbmMedian.textContent = "--";
    gbmRange.textContent = "--";
    gbmNote.textContent = reasons[gbm?.reason] || "No hay datos suficientes para ejecutar el modelo.";
  }
  article.querySelector(".why-change p").textContent = intelligence.explanation;
  const personalInput = article.querySelector(".personal-belief input");
  const personalEdge = article.querySelector(".personal-edge");
  personalInput.value = "";
  personalInput.placeholder = "1–99";
  const updatePersonalEdge = () => {
    if (personalInput.value === "") {
      personalEdge.textContent = "Introduce tu estimación";
      personalEdge.className = "personal-edge";
      return;
    }
    const belief = clamp(Number(personalInput.value) / 100);
    const cost = Number(market.spread || 0);
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
      && (!state.opportunities || market.intelligence.activityScore >= 70);
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
    const response = await fetch(`/api/markets?limit=${PAGE_SIZE}&offset=${state.nextOffset}`, { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`API ${response.status}`);
    const payload = await response.json();
    if (!Array.isArray(payload.markets)) throw new Error("Invalid response");
    const markets = new Map(state.markets.map(market => [market.id || `${market.question}:${market.endDate}`, market]));
    const previousSize = markets.size;
    payload.markets.forEach(market => {
      if (!market.intelligence || market.intelligence.source !== "gamma") throw new Error("Invalid intelligence response");
      markets.set(market.id || `${market.question}:${market.endDate}`, market);
    });
    state.markets = [...markets.values()];
    state.hasMore = payload.hasMore === true;
    state.nextOffset = Number(payload.nextOffset);
    if (state.hasMore && (!Number.isSafeInteger(state.nextOffset) || state.nextOffset < 0 || markets.size === previousSize)) throw new Error("Invalid pagination response");
    const dataTime = safeDate(payload.dataAsOf);
    setDataStatus("live", dataTime
      ? `Gamma · ${dataTime.toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit", timeZone: "UTC" })} UTC`
      : "Gamma en directo");
    elements.notice.hidden = true;
  } catch (error) {
    console.warn("Live market data unavailable:", error);
    if (state.markets.length === 0) {
      setDataStatus("error", "Datos no disponibles");
      showNotice("Gamma no responde en este momento. No se muestran datos ficticios.");
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
elements.filterToggle.addEventListener("click", () => {
  const expanded = elements.filterToggle.getAttribute("aria-expanded") !== "true";
  elements.filterToggle.setAttribute("aria-expanded", String(expanded));
  elements.filters.dataset.mobileCollapsed = String(!expanded);
});

persistAlerts();
loadMarkets({ reset: true });
