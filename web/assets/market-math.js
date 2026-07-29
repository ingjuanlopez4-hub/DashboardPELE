"use strict";

(function exposeMarketMath(root) {
  function simpleAverage(values) {
    const finiteValues = values
      .filter(value => value !== null && value !== undefined && value !== "")
      .map(Number)
      .filter(Number.isFinite);
    return finiteValues.length
      ? finiteValues.reduce((sum, value) => sum + value, 0) / finiteValues.length
      : null;
  }

  function expectedReturn(belief, probability, spread) {
    const values = [belief, probability, spread].map(Number);
    if (!values.every(Number.isFinite) || probability <= 0) return null;
    return belief / probability - 1 - spread / probability;
  }

  function formatChange(value, locale = "es-ES") {
    if (value === null || value === undefined || value === "") return "Sin dato";
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return "Sin dato";
    if (Math.abs(numeric) < 0.005) return "Sin cambio material";
    const formatted = new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 0 }).format(Math.abs(numeric));
    return `${numeric > 0 ? "+" : "−"}${formatted}`;
  }

  function classifyLifecycle(market, now = Date.now()) {
    const lifecycle = market?.lifecycle || {};
    if (lifecycle.resolved === true) return { code: "resolved", label: "Resuelto" };
    if (lifecycle.closed === true) return { code: "closed", label: "Cerrado" };
    if (lifecycle.acceptingOrders === false) return { code: "not_accepting", label: "No acepta órdenes" };
    if (lifecycle.active === false) return { code: "inactive", label: "Inactivo" };
    const end = market?.endDate ? new Date(market.endDate).getTime() : NaN;
    if (Number.isFinite(end) && end <= Number(now)) {
      return lifecycle.active === true || lifecycle.acceptingOrders === true
        ? { code: "deadline_conflict", label: "Activo tras plazo" }
        : { code: "past_deadline", label: "Plazo vencido" };
    }
    if (lifecycle.active === true || lifecycle.acceptingOrders === true) return { code: "open", label: "Abierto" };
    if (Number.isFinite(end)) return { code: "unconfirmed", label: "Estado no confirmado" };
    return { code: "unknown", label: "Estado no publicado" };
  }

  function freshness(updatedAt, now = Date.now()) {
    const updated = updatedAt ? new Date(updatedAt).getTime() : NaN;
    if (!Number.isFinite(updated)) return { code: "unknown", label: "Actualización no publicada", ageMinutes: null };
    const differenceMinutes = Math.floor((Number(now) - updated) / 60000);
    if (differenceMinutes < -5) return { code: "invalid", label: "Hora de fuente no válida", ageMinutes: null };
    const ageMinutes = Math.max(0, differenceMinutes);
    if (ageMinutes < 2) return { code: "fresh", label: "Actualizado ahora", ageMinutes };
    if (ageMinutes < 60) return { code: "fresh", label: `Hace ${ageMinutes} min`, ageMinutes };
    const hours = Math.floor(ageMinutes / 60);
    if (hours < 24) return { code: hours < 6 ? "fresh" : "aging", label: `Hace ${hours} h`, ageMinutes };
    const days = Math.floor(hours / 24);
    return { code: "stale", label: `Hace ${days} d`, ageMinutes };
  }

  const marketMath = { simpleAverage, expectedReturn, formatChange, classifyLifecycle, freshness };
  if (typeof module !== "undefined" && module.exports) module.exports = marketMath;
  root.PeleMarketMath = marketMath;
})(typeof globalThis === "undefined" ? this : globalThis);
