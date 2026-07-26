"use strict";

const DEMO_MARKETS = [
  { id: "demo-1", question: "¿Cotizará Bitcoin por encima de 150.000 $ antes de 2027?", category: "Cripto", probability: .42, volume: 18400000, volume24h: 520000, liquidity: 840000, confidence: { score: 86, level: "high", coverage: 100, factors: [] }, endDate: "2026-12-31T00:00:00Z", url: "https://polymarket.com" },
  { id: "demo-2", question: "¿Recortará tipos la Reserva Federal en su próxima reunión?", category: "Economía", probability: .67, volume: 7200000, volume24h: 310000, liquidity: 460000, confidence: { score: 78, level: "high", coverage: 100, factors: [] }, endDate: "2026-09-16T00:00:00Z", url: "https://polymarket.com" },
  { id: "demo-3", question: "¿Liderará un modelo de IA las listas mundiales de aplicaciones este trimestre?", category: "Tecnología", probability: .31, volume: 2900000, volume24h: 94000, liquidity: 175000, confidence: { score: 61, level: "medium", coverage: 85, factors: [] }, endDate: "2026-09-30T00:00:00Z", url: "https://polymarket.com" },
  { id: "demo-4", question: "¿Debutará un nuevo álbum en el número uno este mes?", category: "Cultura", probability: .56, volume: 1300000, volume24h: 68000, liquidity: 98000, confidence: { score: 48, level: "low", coverage: 75, factors: [] }, endDate: "2026-08-31T00:00:00Z", url: "https://polymarket.com" }
];

const state = { markets: [], search: "", category: "all", sort: "volume", nextOffset: 0, hasMore: false, loading: false };
const elements = {
  grid: document.querySelector("#market-grid"), loading: document.querySelector("#loading"),
  empty: document.querySelector("#empty"), notice: document.querySelector("#notice"),
  search: document.querySelector("#search"), category: document.querySelector("#category"),
  sort: document.querySelector("#sort"), count: document.querySelector("#result-count"),
  loadMore: document.querySelector("#load-more"), dataStatus: document.querySelector("#data-status"),
  dataStatusLabel: document.querySelector("#data-status-label")
};
const money = new Intl.NumberFormat("es-ES", { style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1 });
const percent = new Intl.NumberFormat("es-ES", { style: "percent", maximumFractionDigits: 0 });
const date = new Intl.DateTimeFormat("es-ES", { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" });

function safeDate(value) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
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
  const displayedLevel = confidence.coverage < 50 ? "low" : confidence.level;
  const confidenceLabels = { high: "Alta", medium: "Media", low: "Baja" };
  article.innerHTML = `
    <div class="probability-rail" aria-hidden="true"><i></i><span></span></div>
    <div class="card-content">
      <div class="card-top"><span class="category"></span><span class="date"></span></div>
      <h3></h3>
      <div class="probability-row"><span>El mercado dice “Sí”</span><strong></strong></div>
      <div class="confidence-row"><span>Solidez de la señal</span><strong class="confidence"></strong></div>
      <details class="confidence-details"><summary>Cómo se calcula</summary><div class="factor-list"></div><small></small></details>
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
  const metrics = article.querySelectorAll(".metric strong");
  metrics[0].textContent = money.format(market.volume || 0);
  metrics[1].textContent = money.format(market.liquidity || 0);
  article.querySelector("a").href = market.url || "https://polymarket.com";
  return article;
}

function render() {
  const query = state.search.trim().toLocaleLowerCase("es");
  const visible = state.markets.filter(market => {
    const text = `${market.question} ${market.category}`.toLocaleLowerCase("es");
    return (!query || text.includes(query)) && (state.category === "all" || market.category === state.category);
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
    payload.markets.forEach(market => markets.set(market.id || `${market.question}:${market.endDate}`, market));
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
      state.markets = DEMO_MARKETS;
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
    populateCategories(); updateKpis(); render();
  }
}

elements.search.addEventListener("input", event => { state.search = event.target.value; render(); });
elements.category.addEventListener("change", event => { state.category = event.target.value; render(); });
elements.sort.addEventListener("change", event => { state.sort = event.target.value; render(); });
document.querySelector("#reset").addEventListener("click", () => {
  state.search = ""; state.category = "all"; state.sort = "volume";
  elements.search.value = ""; elements.category.value = "all"; elements.sort.value = "volume"; render();
  elements.search.focus();
});
elements.loadMore.addEventListener("click", () => loadMarkets());

loadMarkets({ reset: true });
