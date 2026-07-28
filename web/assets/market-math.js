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

  const marketMath = { simpleAverage, expectedReturn };
  if (typeof module !== "undefined" && module.exports) module.exports = marketMath;
  root.PeleMarketMath = marketMath;
})(typeof globalThis === "undefined" ? this : globalThis);
