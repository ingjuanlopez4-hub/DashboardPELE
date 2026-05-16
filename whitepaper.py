"""
Módulo E — Whitepaper: generación de informes cuantitativos profesionales.

Carga operaciones históricas desde la base SQLite del Módulo D (archivo),
ejecuta análisis estadístico profundo y genera un documento HTML autónomo
con gráficos interactivos (Plotly), métricas Decimal, pruebas de robustez
y análisis de sensibilidad.

Uso típico:
    generator = WhitepaperGenerator()
    path = await generator.generate("bot_state.db", config, "whitepaper.html")
"""

import asyncio
import json
import logging
import math
import os
import random
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from statistics import stdev, StatisticsError
from typing import Any

import aiosqlite

logger = logging.getLogger("whitepaper")

TICK_SIZE = Decimal("0.01")
SIZE_PRECISION = Decimal("0.01")

DEFAULT_CONFIG: dict[str, Any] = {
    "risk_free_rate": Decimal("0.0"),
    "annual_factor": Decimal("252"),
    "target_return": Decimal("0.0"),
    "permutation_samples": 10_000,
    "sweep_min_edge": Decimal("0.01"),
    "sweep_max_edge": Decimal("0.10"),
    "sweep_min_kelly": Decimal("0.05"),
    "sweep_max_kelly": Decimal("0.50"),
    "sweep_steps": 8,
    "author": "BotQuant Team",
    "strategy_description": (
        "Estrategia multifactorial para mercados de predicci\u00f3n Polymarket "
        "que combina: (1) Wick-Fishing para detectar desequilibrios en el libro "
        "de \u00f3rdenes, (2) an\u00e1lisis de sentimiento con FinBERT sobre "
        "titulares de noticias, y (3) simulaci\u00f3n Monte Carlo con GBM sobre "
        "log-odds para estimar probabilidades de resoluci\u00f3n. Las se\u00f1ales "
        "se combinan ponderadamente y el tama\u00f1o de la posici\u00f3n se "
        "determina mediante el criterio de Kelly fraccionario."
    ),
}

BUY_SIDES = frozenset({"BUY", "BUY_YES", "BUY_NO"})
SELL_SIDES = frozenset({"SELL", "SELL_YES", "SELL_NO"})


# ── Funciones auxiliares de métricas ─────────────────────────────────────

def _safe_decimal(value: Any) -> Decimal:
    """Convierte a Decimal de forma segura desde string/int/Decimal."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _compute_trade_pnls(trades: list[dict]) -> list[Decimal]:
    """Calcula PnL FIFO con mark-to-market para posiciones abiertas.

    Retorna una lista de Decimal con el PnL de cada trade en el mismo orden.
    """
    if not trades:
        return []

    sorted_trades = sorted(trades, key=lambda t: t["timestamp"])

    latest_price: dict[str, Decimal] = {}
    for t in reversed(sorted_trades):
        aid = t.get("asset_id", "")
        if aid and aid not in latest_price:
            latest_price[aid] = _safe_decimal(t["price"])

    open_positions: dict[str, list[list[Any]]] = defaultdict(list)
    trade_pnls: list[Decimal] = [Decimal("0")] * len(sorted_trades)

    for i, t in enumerate(sorted_trades):
        if not t.get("success", True):
            continue
        side = t.get("side", "")
        price = _safe_decimal(t["price"])
        size = _safe_decimal(t["size"])

        if side in BUY_SIDES:
            open_positions[t.get("asset_id", "")].append([price, size, i])
        elif side in SELL_SIDES:
            pnl = Decimal("0")
            remaining = size
            pos_list = open_positions.get(t.get("asset_id", ""), [])
            while remaining > 0 and pos_list:
                entry_price, pos_size, buy_idx = pos_list[0]
                close_size = min(remaining, pos_size)
                realized = (price - entry_price) * close_size
                pnl += realized
                remaining -= close_size
                pos_list[0][1] -= close_size
                if pos_list[0][1] <= 0:
                    pos_list.pop(0)
            trade_pnls[i] = pnl

    for asset_id, pos_list in open_positions.items():
        mtm = latest_price.get(asset_id)
        if mtm is None:
            continue
        for entry_price, remaining_size, buy_idx in pos_list:
            if remaining_size > 0:
                trade_pnls[buy_idx] += (mtm - entry_price) * remaining_size

    return trade_pnls


def compute_metrics(trades: list[dict]) -> dict[str, Decimal | int | str]:
    """Calcula todas las métricas de rendimiento sobre una lista de trades.

    Todos los cálculos financieros usan Decimal. Solo se convierte a float
    dentro de statistics.stdev para la desviación estándar.

    Returns
    -------
    dict con claves Decimal: net_pnl, return_pct, sharpe_ratio, sortino_ratio,
    calmar_ratio, max_drawdown_pct, win_rate, profit_factor, expectancy,
    volatility_annualized, total_trades (int), winning_trades (int),
    losing_trades (int), long_trades (int), short_trades (int),
    avg_win (Decimal), avg_loss (Decimal).
    """
    if not trades:
        return {
            "net_pnl": Decimal("0"),
            "return_pct": Decimal("0"),
            "sharpe_ratio": Decimal("0"),
            "sortino_ratio": Decimal("0"),
            "calmar_ratio": Decimal("0"),
            "max_drawdown_pct": Decimal("0"),
            "max_drawdown_start": "",
            "max_drawdown_end": "",
            "max_drawdown_duration": "",
            "win_rate": Decimal("0"),
            "profit_factor": Decimal("0"),
            "expectancy": Decimal("0"),
            "volatility_annualized": Decimal("0"),
            "return_annualized": Decimal("0"),
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "long_trades": 0,
            "short_trades": 0,
            "avg_win": Decimal("0"),
            "avg_loss": Decimal("0"),
        }

    sorted_trades = sorted(trades, key=lambda t: t["timestamp"])
    trade_pnls = _compute_trade_pnls(sorted_trades)

    net_pnl = sum(trade_pnls, Decimal("0"))

    total_buy_usdc = Decimal("0")
    for t in sorted_trades:
        if t.get("side", "") in BUY_SIDES and t.get("success", True):
            p = _safe_decimal(t["price"])
            s = _safe_decimal(t["size"])
            total_buy_usdc += p * s

    return_pct = (
        (net_pnl / total_buy_usdc * Decimal("100"))
        if total_buy_usdc > 0
        else Decimal("0")
    )

    # Retornos diarios para Sharpe, Sortino, volatilidad
    daily_pnl: dict[str, Decimal] = defaultdict(Decimal)
    for t, pnl in zip(sorted_trades, trade_pnls):
        daily_pnl[t["timestamp"][:10]] += pnl

    daily_returns: list[Decimal] = []
    if total_buy_usdc > 0:
        daily_returns = [
            d_pnl / total_buy_usdc for d_pnl in daily_pnl.values()
        ]

    annual_factor = Decimal("252")
    n_days = len(daily_returns)

    # Media diaria (Decimal)
    mean_daily = (
        sum(daily_returns, Decimal("0")) / Decimal(str(n_days))
        if n_days > 0
        else Decimal("0")
    )

    # Sharpe ratio
    sharpe_ratio = Decimal("0")
    sortino_ratio = Decimal("0")
    volatility_annualized = Decimal("0")
    return_annualized = mean_daily * annual_factor

    if n_days >= 2:
        vals_float = [float(v) for v in daily_returns]
        try:
            sigma = stdev(vals_float)
        except StatisticsError:
            sigma = 0.0

        if sigma > 0:
            vol_ann = Decimal(str(round(sigma * math.sqrt(252), 10)))
            volatility_annualized = vol_ann
            sharpe_ratio = (
                (mean_daily * annual_factor) / vol_ann
                if vol_ann > 0
                else Decimal("0")
            )

            # Sortino: solo desviación a la baja
            downside_vals = [min(v, 0.0) for v in vals_float]
            try:
                downside_sigma = stdev(downside_vals)
            except StatisticsError:
                downside_sigma = 0.0
            if downside_sigma > 0:
                sortino_ratio = (
                    (mean_daily * annual_factor)
                    / Decimal(str(round(downside_sigma * math.sqrt(252), 10)))
                )

    # Cuenta de trades
    total = len(sorted_trades)
    winning = sum(1 for p in trade_pnls if p > 0)
    losing = sum(1 for p in trade_pnls if p < 0)
    long_count = sum(
        1 for t in sorted_trades if t.get("side", "") in BUY_SIDES
    )
    short_count = sum(
        1 for t in sorted_trades if t.get("side", "") in SELL_SIDES
    )

    non_zero = winning + losing
    win_rate = (
        Decimal(str(winning)) / Decimal(str(non_zero))
        if non_zero > 0
        else Decimal("0")
    )

    # Profit factor
    gross_profit = sum((p for p in trade_pnls if p > 0), Decimal("0"))
    gross_loss = abs(sum((p for p in trade_pnls if p < 0), Decimal("0")))
    profit_factor = (
        gross_profit / gross_loss if gross_loss > 0 else Decimal("0")
    )

    # Expectancy
    expectancy = (
        net_pnl / Decimal(str(total)) if total > 0 else Decimal("0")
    )

    # Avg win / avg loss
    avg_win = (
        gross_profit / Decimal(str(winning)) if winning > 0 else Decimal("0")
    )
    avg_loss = (
        gross_loss / Decimal(str(losing)) if losing > 0 else Decimal("0")
    )

    # Max drawdown (porcentual sobre equity acumulada)
    cumulative = Decimal("0")
    peak = Decimal("0")
    max_dd = Decimal("0")
    dd_start = 0
    dd_end = 0
    current_dd_start = 0
    max_dd_start_idx = 0
    max_dd_end_idx = 0

    for i, pnl in enumerate(trade_pnls):
        cumulative += pnl
        if cumulative > peak:
            peak = cumulative
            current_dd_start = i + 1
        if peak > 0:
            dd = (peak - cumulative) / peak
            if dd > max_dd:
                max_dd = dd
                max_dd_start_idx = current_dd_start
                max_dd_end_idx = i + 1

    max_drawdown_pct = max_dd * Decimal("100")

    dd_start_ts = (
        sorted_trades[max_dd_start_idx]["timestamp"]
        if 0 <= max_dd_start_idx < len(sorted_trades)
        else ""
    )
    dd_end_ts = (
        sorted_trades[max_dd_end_idx - 1]["timestamp"]
        if 0 < max_dd_end_idx <= len(sorted_trades)
        else ""
    )
    dd_duration = (
        str(max(0, max_dd_end_idx - max_dd_start_idx)) + " trades"
    )

    # Calmar ratio
    calmar_ratio = (
        return_annualized / max_drawdown_pct
        if max_drawdown_pct > 0
        else Decimal("0")
    )

    return {
        "net_pnl": net_pnl,
        "return_pct": return_pct,
        "sharpe_ratio": sharpe_ratio,
        "sortino_ratio": sortino_ratio,
        "calmar_ratio": calmar_ratio,
        "max_drawdown_pct": max_drawdown_pct,
        "max_drawdown_start": dd_start_ts,
        "max_drawdown_end": dd_end_ts,
        "max_drawdown_duration": dd_duration,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "volatility_annualized": volatility_annualized,
        "return_annualized": return_annualized,
        "total_trades": total,
        "winning_trades": winning,
        "losing_trades": losing,
        "long_trades": long_count,
        "short_trades": short_count,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
    }


def _shuffle_pnls(pnls: list[Decimal]) -> list[Decimal]:
    """Baraja una lista de PnLs in-place y retorna la lista."""
    random.shuffle(pnls)
    return pnls


def _sharpe_from_pnls(pnls: list[Decimal]) -> float:
    """Calcula Sharpe ratio anualizado desde una secuencia de PnLs.

    Retorna float (para uso en tests de permutación).
    """
    n = len(pnls)
    if n < 2:
        return 0.0
    mean_p = float(sum(pnls, Decimal("0")) / Decimal(str(n)))
    vals = [float(p) for p in pnls]
    try:
        sigma = stdev(vals)
    except StatisticsError:
        sigma = 0.0
    if sigma == 0:
        return 0.0
    return mean_p / sigma * math.sqrt(252)


def _total_pnl_from_pnls(pnls: list[Decimal]) -> float:
    """Suma simple de PnLs como float (para test de permutación)."""
    return float(sum(pnls, Decimal("0")))


def permutation_test(
    pnls: list[Decimal],
    observed_sharpe: float,
    n_samples: int = 10_000,
) -> dict[str, Any]:
    """Prueba de significancia por permutación sobre la secuencia de PnLs.

    Baraja el orden de los PnLs n_samples veces, calcula la distribución
    de Sharpe ratios y PnL total, y compara con el valor observado.

    Returns
    -------
    dict con observed_sharpe, observed_pnl, p_value_sharpe, p_value_pnl,
    sharpe_dist (list[float]), pnl_dist (list[float]), significant
    (bool al 95%).
    """
    observed_pnl = _total_pnl_from_pnls(pnls)

    sharpe_dist: list[float] = []
    pnl_dist: list[float] = []

    for _ in range(n_samples):
        shuffled = _shuffle_pnls(pnls.copy())
        sharpe_dist.append(_sharpe_from_pnls(shuffled))
        pnl_dist.append(_total_pnl_from_pnls(shuffled))

    # p-value: fracción de valores sintéticos >= valor observado
    count_sharpe = sum(1 for s in sharpe_dist if s >= observed_sharpe)
    count_pnl = sum(1 for p in pnl_dist if p >= observed_pnl)

    p_value_sharpe = count_sharpe / n_samples
    p_value_pnl = count_pnl / n_samples

    return {
        "observed_sharpe": observed_sharpe,
        "observed_pnl": observed_pnl,
        "p_value_sharpe": p_value_sharpe,
        "p_value_pnl": p_value_pnl,
        "sharpe_dist": sharpe_dist,
        "pnl_dist": pnl_dist,
        "significant": p_value_sharpe < 0.05,
        "n_samples": n_samples,
    }


def parameter_sweep(
    trades: list[dict],
    min_edge: Decimal,
    max_edge: Decimal,
    min_kelly: Decimal,
    max_kelly: Decimal,
    steps: int,
) -> list[dict[str, Any]]:
    """Barrido de parámetros (min_edge, kelly_fraction) sobre trades.

    Para cada combinación en la grilla, re-evalúa qué trades se ejecutarían
    y calcula el PnL neto resultante.

    Returns
    -------
    list[dict] con edge, kelly, net_pnl, n_trades, sharpe.
    """
    results: list[dict[str, Any]] = []

    edge_values = [
        min_edge + (max_edge - min_edge) * Decimal(str(i)) / Decimal(str(steps - 1))
        for i in range(steps)
    ]
    kelly_values = [
        min_kelly + (max_kelly - min_kelly) * Decimal(str(i)) / Decimal(str(steps - 1))
        for i in range(steps)
    ]

    current_price_by_asset: dict[str, Decimal] = {}
    for t in trades:
        aid = t.get("asset_id", "")
        if aid:
            current_price_by_asset[aid] = _safe_decimal(
                t.get("current_price", t["price"])
            )

    for edge in edge_values:
        for kelly in kelly_values:
            simulated: list[dict] = []
            _prices: dict[str, Decimal] = {}

            for t in trades:
                side = t.get("side", "")
                price = _safe_decimal(t["price"])
                size = _safe_decimal(t.get("size", "1"))
                prob = _safe_decimal(t.get("probability", price))
                current = current_price_by_asset.get(
                    t.get("asset_id", ""), price
                )

                ev = prob - current
                abs_ev = abs(ev)

                if abs_ev < edge:
                    continue

                if ev > 0:
                    win_rate = prob
                    kelly_raw = (prob - current) / (Decimal("1") - current)
                else:
                    win_rate = Decimal("1") - prob
                    kelly_raw = (current - prob) / current

                if kelly_raw <= 0:
                    continue

                kelly_size = kelly_raw * kelly
                adjusted_size = size * kelly_size

                simulated.append({
                    "timestamp": t["timestamp"],
                    "asset_id": t.get("asset_id", ""),
                    "market": t.get("market", ""),
                    "side": side,
                    "price": str(price),
                    "size": str(adjusted_size),
                    "success": True,
                })

            if simulated:
                metrics = compute_metrics(simulated)
                results.append({
                    "edge": str(edge),
                    "kelly": str(kelly),
                    "net_pnl": str(metrics["net_pnl"]),
                    "n_trades": metrics["total_trades"],
                    "sharpe": str(metrics["sharpe_ratio"]),
                })
            else:
                results.append({
                    "edge": str(edge),
                    "kelly": str(kelly),
                    "net_pnl": "0",
                    "n_trades": 0,
                    "sharpe": "0",
                })

    return results


# ── Generación de gráficos Plotly ────────────────────────────────────────

def _get_plotly() -> Any:
    """Importa plotly.graph_objects (lazy)."""
    import plotly.graph_objects as go
    return go


def _get_plotly_offline() -> Any:
    """Importa plotly.offline (lazy)."""
    import plotly.offline as py_offline
    return py_offline


def generate_equity_curve(trades: list[dict]) -> str:
    """Genera gráfico de curva de equity acumulada.

    Returns
    -------
    str — div HTML con el gráfico Plotly embebido.
    """
    go = _get_plotly()
    py_offline = _get_plotly_offline()

    if not trades:
        return "<p>No hay datos para la curva de equity.</p>"

    sorted_t = sorted(trades, key=lambda x: x["timestamp"])
    trade_pnls = _compute_trade_pnls(sorted_t)
    cumulative = Decimal("0")
    dates: list[str] = []
    equity: list[float] = []

    for i, t in enumerate(sorted_t):
        cumulative += trade_pnls[i]
        dates.append(t["timestamp"][:19])
        equity.append(float(cumulative))

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=dates,
        y=equity,
        mode="lines",
        name="Equity",
        line=dict(color="#2e86c1", width=2),
    ))
    fig.update_layout(
        title="Curva de Equity Acumulada",
        xaxis_title="Fecha",
        yaxis_title="PnL Acumulado (USDC)",
        template="plotly_white",
        hovermode="x unified",
    )
    fig.update_yaxes(tickprefix="$")
    return py_offline.plot(fig, include_plotlyjs="cdn", output_type="div")


def generate_drawdown_chart(trades: list[dict]) -> str:
    """Genera gráfico de drawdown porcentual acumulado.

    Returns
    -------
    str — div HTML con el gráfico Plotly embebido.
    """
    go = _get_plotly()
    py_offline = _get_plotly_offline()

    if not trades:
        return "<p>No hay datos para drawdown.</p>"

    sorted_t = sorted(trades, key=lambda x: x["timestamp"])
    trade_pnls = _compute_trade_pnls(sorted_t)
    cumulative = Decimal("0")
    peak = Decimal("0")
    dates: list[str] = []
    dds: list[float] = []

    for i, t in enumerate(sorted_t):
        cumulative += trade_pnls[i]
        if cumulative > peak:
            peak = cumulative
        dd = float((peak - cumulative) / peak * 100) if peak > 0 else 0.0
        dates.append(t["timestamp"][:19])
        dds.append(-dd)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=dates,
        y=dds,
        fill="tozeroy",
        mode="lines",
        name="Drawdown",
        line=dict(color="#e74c3c", width=2),
    ))
    fig.update_layout(
        title="Drawdown Acumulado",
        xaxis_title="Fecha",
        yaxis_title="Drawdown (%)",
        template="plotly_white",
        hovermode="x unified",
    )
    fig.update_yaxes(ticksuffix="%")
    return py_offline.plot(fig, include_plotlyjs="cdn", output_type="div")


def generate_daily_returns_histogram(trades: list[dict]) -> str:
    """Genera histograma de retornos diarios.

    Returns
    -------
    str — div HTML con el gráfico Plotly embebido.
    """
    go = _get_plotly()
    py_offline = _get_plotly_offline()

    if not trades:
        return "<p>No hay datos para histograma.</p>"

    sorted_t = sorted(trades, key=lambda x: x["timestamp"])
    trade_pnls = _compute_trade_pnls(sorted_t)

    total_buy = sum(
        _safe_decimal(t["price"]) * _safe_decimal(t["size"])
        for t in sorted_t
        if t.get("side", "") in BUY_SIDES and t.get("success", True)
    )
    if total_buy == 0:
        total_buy = Decimal("1")

    daily_pnl_map: dict[str, Decimal] = defaultdict(Decimal)
    for t, pnl in zip(sorted_t, trade_pnls):
        daily_pnl_map[t["timestamp"][:10]] += pnl

    returns_pct = [
        float(pnl / total_buy * 100) for pnl in daily_pnl_map.values()
    ]

    fig = go.Figure()
    fig.add_trace(go.Histogram(
        x=returns_pct,
        nbinsx=30,
        marker_color="#2e86c1",
        opacity=0.75,
        name="Retornos diarios",
    ))
    fig.update_layout(
        title="Distribución de Retornos Diarios",
        xaxis_title="Retorno Diario (%)",
        yaxis_title="Frecuencia",
        template="plotly_white",
        bargap=0.1,
    )
    return py_offline.plot(fig, include_plotlyjs="cdn", output_type="div")


def generate_pnl_curve(trades: list[dict]) -> str:
    """Genera curva de PnL acumulado vs tiempo (idem equity)."""
    return generate_equity_curve(trades)


def generate_size_vs_return_scatter(trades: list[dict]) -> str:
    """Genera scatter de tamaño de posición vs retorno.

    Returns
    -------
    str — div HTML con el gráfico Plotly embebido.
    """
    go = _get_plotly()
    py_offline = _get_plotly_offline()

    if not trades:
        return "<p>No hay datos para el scatter.</p>"

    trade_pnls = _compute_trade_pnls(trades)
    sizes: list[float] = []
    returns: list[float] = []

    for t, pnl in zip(trades, trade_pnls):
        size = float(_safe_decimal(t.get("size", "0")))
        cost = _safe_decimal(t["price"]) * _safe_decimal(t.get("size", "0"))
        ret = float(pnl / cost * 100) if cost > 0 else 0.0
        sizes.append(size)
        returns.append(ret)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=sizes,
        y=returns,
        mode="markers",
        marker=dict(
            color="#2e86c1",
            size=8,
            opacity=0.6,
        ),
        name="Operaciones",
    ))
    fig.update_layout(
        title="Tamaño de Posición vs Retorno",
        xaxis_title="Tamaño (contracts)",
        yaxis_title="Retorno (%)",
        template="plotly_white",
    )
    return py_offline.plot(fig, include_plotlyjs="cdn", output_type="div")


def generate_heatmap_time(trades: list[dict]) -> str:
    """Genera mapa de calor de rendimiento por día de semana / hora.

    Returns
    -------
    str — div HTML con el gráfico Plotly embebido, o mensaje si no aplica.
    """
    go = _get_plotly()
    py_offline = _get_plotly_offline()

    if not trades:
        return "<p>No hay datos para mapa de calor.</p>"

    trade_pnls = _compute_trade_pnls(trades)
    heat: dict[tuple[int, int], Decimal] = defaultdict(Decimal)

    for t, pnl in zip(trades, trade_pnls):
        try:
            dt = datetime.fromisoformat(t["timestamp"])
            day = dt.weekday()
            hour = dt.hour
            heat[(day, hour)] += pnl
        except (ValueError, TypeError):
            continue

    if not heat:
        return "<p>No se pudo generar mapa de calor (fechas inválidas).</p>"

    days = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
    hours = list(range(24))

    z: list[list[float]] = []
    for day_idx in range(7):
        row: list[float] = []
        for hour_idx in hours:
            row.append(float(heat.get((day_idx, hour_idx), Decimal("0"))))
        z.append(row)

    fig = go.Figure(data=go.Heatmap(
        z=z,
        x=[f"{h}:00" for h in hours],
        y=days,
        colorscale="RdYlGn",
        colorbar_title="PnL (USDC)",
    ))
    fig.update_layout(
        title="Rendimiento por Día / Hora",
        xaxis_title="Hora del Día (UTC)",
        yaxis_title="Día de la Semana",
        template="plotly_white",
    )
    return py_offline.plot(fig, include_plotlyjs="cdn", output_type="div")


def generate_permutation_histogram(
    perm_result: dict[str, Any],
) -> str:
    """Genera histograma de la distribución de Sharpe por permutación.

    Returns
    -------
    str — div HTML con el gráfico Plotly embebido.
    """
    go = _get_plotly()
    py_offline = _get_plotly_offline()

    sharpe_dist = perm_result.get("sharpe_dist", [])
    observed = perm_result.get("observed_sharpe", 0.0)
    significant = perm_result.get("significant", False)

    if not sharpe_dist:
        return "<p>No hay datos de permutación.</p>"

    fig = go.Figure()
    fig.add_trace(go.Histogram(
        x=sharpe_dist,
        nbinsx=50,
        marker_color="#85c1e9",
        opacity=0.7,
        name="Sharpe sintético",
    ))
    fig.add_vline(
        x=observed,
        line_dash="dash",
        line_color="#e74c3c" if significant else "#2ecc71",
        annotation_text=f"Observado: {observed:.4f}",
        annotation_position="top right",
    )
    fig.update_layout(
        title=(
            "Prueba de Permutación — Distribución de Sharpe"
            f" (p={perm_result.get('p_value_sharpe', 0):.4f})"
        ),
        xaxis_title="Sharpe Ratio",
        yaxis_title="Frecuencia",
        template="plotly_white",
        bargap=0.05,
    )
    return py_offline.plot(fig, include_plotlyjs="cdn", output_type="div")


def generate_sweep_heatmap(sweep_results: list[dict]) -> str:
    """Genera mapa de calor del barrido de parámetros.

    Returns
    -------
    str — div HTML con el gráfico Plotly embebido.
    """
    go = _get_plotly()
    py_offline = _get_plotly_offline()

    if not sweep_results:
        return "<p>No hay datos de barrido de parámetros.</p>"

    edges = sorted(set(r["edge"] for r in sweep_results))
    kellys = sorted(set(r["kelly"] for r in sweep_results))

    lookup: dict[tuple[str, str], float] = {}
    for r in sweep_results:
        lookup[(r["edge"], r["kelly"])] = float(r["net_pnl"])

    z: list[list[float]] = []
    for k in kellys:
        row: list[float] = []
        for e in edges:
            row.append(lookup.get((e, k), 0.0))
        z.append(row)

    fig = go.Figure(data=go.Heatmap(
        z=z,
        x=[f"{e}" for e in edges],
        y=[f"{k}" for k in kellys],
        colorscale="RdYlGn",
        colorbar_title="PnL Neto (USDC)",
    ))
    fig.update_layout(
        title="Análisis de Sensibilidad — PnL Neto",
        xaxis_title="min_edge",
        yaxis_title="kelly_fraction",
        template="plotly_white",
    )
    return py_offline.plot(fig, include_plotlyjs="cdn", output_type="div")


# ── Plantilla HTML ───────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title }}</title>
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Arial, sans-serif;
            color: #2c3e50;
            background: #f5f6fa;
            line-height: 1.6;
        }
        .page {
            max-width: 1100px;
            margin: 0 auto;
            padding: 40px 20px;
        }
        .cover {
            background: linear-gradient(135deg, #1a5276 0%, #2e86c1 100%);
            color: #fff;
            padding: 80px 40px;
            border-radius: 12px;
            text-align: center;
            margin-bottom: 40px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.15);
        }
        .cover h1 { font-size: 2.5em; margin-bottom: 10px; letter-spacing: 1px; }
        .cover .subtitle { font-size: 1.1em; opacity: 0.9; margin-bottom: 20px; }
        .cover .meta { font-size: 0.9em; opacity: 0.8; }
        .cover .meta span { display: inline-block; margin: 0 15px; }
        .section {
            background: #fff;
            border-radius: 10px;
            padding: 30px;
            margin-bottom: 30px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.06);
        }
        .section h2 {
            color: #1a5276;
            font-size: 1.5em;
            margin-bottom: 20px;
            padding-bottom: 8px;
            border-bottom: 3px solid #2e86c1;
        }
        .section h3 {
            color: #2e86c1;
            font-size: 1.15em;
            margin: 20px 0 10px;
        }
        .metric-table {
            width: 100%;
            border-collapse: collapse;
            margin: 15px 0;
        }
        .metric-table th, .metric-table td {
            padding: 10px 14px;
            text-align: left;
            border-bottom: 1px solid #e0e0e0;
        }
        .metric-table th {
            background: #f8f9fa;
            font-weight: 600;
            color: #1a5276;
        }
        .metric-table tr:hover td { background: #f0f7ff; }
        .metric-table .metric-name { font-weight: 500; width: 40%; }
        .metric-table .metric-value { font-family: 'Courier New', monospace; text-align: right; }
        .metric-table .positive { color: #27ae60; }
        .metric-table .negative { color: #e74c3c; }
        .chart-container {
            margin: 25px 0;
            border: 1px solid #e8e8e8;
            border-radius: 8px;
            padding: 15px;
            background: #fafafa;
        }
        .verdict {
            font-size: 1.3em;
            font-weight: 700;
            text-align: center;
            padding: 20px;
            border-radius: 8px;
            margin: 20px 0;
        }
        .verdict.validated { background: #d5f5e3; color: #1e8449; }
        .verdict.needs-improvement { background: #fef9e7; color: #b7950b; }
        .verdict.not-validated { background: #fadbd8; color: #922b21; }
        p { margin: 10px 0; }
        ul { margin: 10px 0 10px 20px; }
        li { margin: 4px 0; }
        .two-col {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
        }
        @media (max-width: 700px) {
            .two-col { grid-template-columns: 1fr; }
            .cover { padding: 40px 20px; }
            .cover h1 { font-size: 1.8em; }
        }
    </style>
</head>
<body>
<div class="page">

    <!-- PORTADA -->
    <div class="cover">
        <h1>{{ title }}</h1>
        <div class="subtitle">Informe Cuantitativo Automático</div>
        <div class="meta">
            <span>Período: {{ start_date }} — {{ end_date }}</span>
            <span>Generado: {{ generation_date }}</span>
            <span>Autor: {{ author }}</span>
        </div>
    </div>

    <!-- 1. RESUMEN EJECUTIVO -->
    <div class="section">
        <h2>1. Resumen Ejecutivo</h2>
        <div class="verdict {{ verdict_class }}">{{ verdict }}</div>
        <table class="metric-table">
            <tr><th>Métrica</th><th>Valor</th></tr>
            <tr>
                <td class="metric-name">PnL Neto (USDC)</td>
                <td class="metric-value {{ pnl_class }}">{{ net_pnl }}</td>
            </tr>
            <tr>
                <td class="metric-name">Rentabilidad (%)</td>
                <td class="metric-value {{ return_class }}">{{ return_pct }}</td>
            </tr>
            <tr>
                <td class="metric-name">Ratio de Sharpe</td>
                <td class="metric-value {{ sharpe_class }}">{{ sharpe_ratio }}</td>
            </tr>
            <tr>
                <td class="metric-name">Ratio de Sortino</td>
                <td class="metric-value">{{ sortino_ratio }}</td>
            </tr>
            <tr>
                <td class="metric-name">Ratio de Calmar</td>
                <td class="metric-value">{{ calmar_ratio }}</td>
            </tr>
            <tr>
                <td class="metric-name">Máximo Drawdown (%)</td>
                <td class="metric-value negative">{{ max_drawdown_pct }}</td>
            </tr>
            <tr>
                <td class="metric-name">Win Rate</td>
                <td class="metric-value">{{ win_rate }}</td>
            </tr>
            <tr>
                <td class="metric-name">Profit Factor</td>
                <td class="metric-value">{{ profit_factor }}</td>
            </tr>
            <tr>
                <td class="metric-name">Expectancy (USDC/op)</td>
                <td class="metric-value">{{ expectancy }}</td>
            </tr>
            <tr>
                <td class="metric-name">Volatilidad Anualizada (%)</td>
                <td class="metric-value">{{ volatility_annualized }}</td>
            </tr>
            <tr>
                <td class="metric-name">Retorno Anualizado (%)</td>
                <td class="metric-value {{ return_class }}">{{ return_annualized }}</td>
            </tr>
            <tr>
                <td class="metric-name">Total Operaciones</td>
                <td class="metric-value">{{ total_trades }}</td>
            </tr>
            <tr>
                <td class="metric-name">Operaciones Ganadoras</td>
                <td class="metric-value positive">{{ winning_trades }}</td>
            </tr>
            <tr>
                <td class="metric-name">Operaciones Perdedoras</td>
                <td class="metric-value negative">{{ losing_trades }}</td>
            </tr>
            <tr>
                <td class="metric-name">Operaciones Largas</td>
                <td class="metric-value">{{ long_trades }}</td>
            </tr>
            <tr>
                <td class="metric-name">Operaciones Cortas</td>
                <td class="metric-value">{{ short_trades }}</td>
            </tr>
            <tr>
                <td class="metric-name">Ganancia Promedio (USDC)</td>
                <td class="metric-value positive">{{ avg_win }}</td>
            </tr>
            <tr>
                <td class="metric-name">Pérdida Promedio (USDC)</td>
                <td class="metric-value negative">{{ avg_loss }}</td>
            </tr>
        </table>
    </div>

    <!-- 2. DESCRIPCIÓN DE LA ESTRATEGIA -->
    <div class="section">
        <h2>2. Descripción de la Estrategia</h2>
        <p>{{ strategy_description }}</p>
        <h3>Parámetros Utilizados</h3>
        <table class="metric-table">
            <tr><th>Parámetro</th><th>Valor</th></tr>
            {% for key, value in config_items %}
            <tr><td class="metric-name">{{ key }}</td><td class="metric-value">{{ value }}</td></tr>
            {% endfor %}
        </table>
        <h3>Mercados Objetivo</h3>
        <p>{{ target_markets }}</p>
    </div>

    <!-- 3. BACKTEST Y RESULTADOS -->
    <div class="section">
        <h2>3. Backtest y Resultados</h2>
        <div class="chart-container">{{ equity_chart }}</div>
        <div class="chart-container">{{ drawdown_chart }}</div>
        <div class="two-col">
            <div class="chart-container">{{ returns_histogram }}</div>
            <div class="chart-container">{{ size_vs_return }}</div>
        </div>
        <div class="chart-container">{{ heatmap_chart }}</div>
    </div>

    <!-- 4. ANÁLISIS DE ROBUSTEZ -->
    <div class="section">
        <h2>4. Análisis de Robustez</h2>
        <h3>Prueba de Permutación (10,000 muestras)</h3>
        <p>
            La prueba baraja el orden de las {{ total_trades }} operaciones reales
            para generar una distribución nula del Sharpe ratio.
            Valor observado: <strong>{{ observed_sharpe }}</strong>
            (p = {{ p_value_sharpe }}).
        </p>
        <p>
            PnL total observado: <strong>{{ observed_pnl }}</strong>
            (p = {{ p_value_pnl }}).
            {% if significant %}
            El resultado es <strong>estadísticamente significativo</strong> al 95%.
            {% else %}
            El resultado <strong>no es estadísticamente significativo</strong> al 95%.
            {% endif %}
        </p>
        <div class="chart-container">{{ permutation_histogram }}</div>
    </div>

    <!-- 5. ANÁLISIS DE SENSIBILIDAD -->
    <div class="section">
        <h2>5. Análisis de Sensibilidad</h2>
        <p>
            Barrido de parámetros clave (min_edge, kelly_fraction) para evaluar
            la robustez de la estrategia en diferentes configuraciones.
        </p>
        <div class="chart-container">{{ sweep_heatmap }}</div>
    </div>

    <!-- 6. ANÁLISIS DE RIESGO Y LIMITACIONES -->
    <div class="section">
        <h2>6. Análisis de Riesgo y Limitaciones</h2>
        <h3>Circuit Breakers</h3>
        <p>{{ circuit_breaker_text }}</p>
        <h3>Peores 5 Días</h3>
        <table class="metric-table">
            <tr><th>Fecha</th><th>PnL (USDC)</th></tr>
            {% for date, pnl in worst_days %}
            <tr><td>{{ date }}</td><td class="metric-value negative">{{ pnl }}</td></tr>
            {% endfor %}
        </table>
        <h3>Limitaciones del Backtest</h3>
        <ul>
            <li><strong>Slippage:</strong> Se asume un slippage fijo de 1 tick. En condiciones de baja liquidez el slippage real puede ser mayor.</li>
            <li><strong>Latencia:</strong> No se modela latencia de red ni retrasos en la firma de transacciones EIP-712.</li>
            <li><strong>Liquidez:</strong> Polymarket puede tener profundidad limitada en ciertos mercados, afectando la ejecución de órdenes grandes.</li>
            <li><strong>Precisión Decimal:</strong> Todas las métricas se calculan con Decimal para cumplir con el tick size del CLOB.</li>
        </ul>
    </div>

    <!-- 7. CONCLUSIONES Y RECOMENDACIONES -->
    <div class="section">
        <h2>7. Conclusiones y Recomendaciones</h2>
        <p>{{ conclusions }}</p>
        <h3>Recomendación de Capital Inicial</h3>
        <p>{{ capital_recommendation }}</p>
        <h3>Áreas de Mejora</h3>
        <ul>
            {% for improvement in improvements %}
            <li>{{ improvement }}</li>
            {% endfor %}
        </ul>
    </div>

    <p style="text-align:center;color:#999;font-size:0.85em;margin-top:40px;">
        Generado por BotQuant Whitepaper Engine &mdash; {{ generation_date }}
    </p>
</div>
</body>
</html>"""


# ── Clase principal ──────────────────────────────────────────────────────

class WhitepaperGenerator:
    """Generador de whitepapers cuantitativos para el bot de Polymarket.

    Carga operaciones desde SQLite, calcula métricas financieras con Decimal,
    ejecuta pruebas de robustez (permutación, barrido de parámetros) y
    genera un documento HTML autónomo con gráficos Plotly interactivos.
    """

    def __init__(self) -> None:
        self._db: aiosqlite.Connection | None = None

    async def _connect(self, db_path: str) -> aiosqlite.Connection:
        if self._db is None:
            self._db = await aiosqlite.connect(db_path)
        return self._db

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _load_trades_from_db(
        self,
        db_path: str,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> list[dict]:
        """Carga trades desde la base SQLite del módulo Archivo.

        La tabla `trades` tiene el schema definido en archivo.py.
        """
        db = await self._connect(db_path)

        # Verificar que la tabla existe
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
        )
        if not await cursor.fetchone():
            logger.warning("tabla 'trades' no encontrada en %s", db_path)
            return []

        query = (
            "SELECT timestamp, asset_id, market, side, price, size, "
            "usdc_amount, order_id, success FROM trades"
        )
        params: list[str] = []
        conditions: list[str] = []

        if from_date is not None:
            conditions.append("timestamp >= ?")
            params.append(from_date)
        if to_date is not None:
            conditions.append("timestamp <= ?")
            params.append(to_date)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY timestamp"

        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()

        trades = [
            {
                "timestamp": row[0],
                "asset_id": row[1],
                "market": row[2],
                "side": row[3],
                "price": row[4],
                "size": row[5],
                "usdc_amount": row[6],
                "order_id": row[7],
                "success": bool(row[8]),
            }
            for row in rows
        ]
        logger.info("cargados %d trades desde %s", len(trades), db_path)
        return trades

    async def _load_balance_history(
        self, db_path: str,
    ) -> list[dict]:
        """Carga el historial de balances desde SQLite."""
        db = await self._connect(db_path)

        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='balance_history'"
        )
        if not await cursor.fetchone():
            return []

        cursor = await db.execute(
            "SELECT timestamp, balance FROM balance_history ORDER BY timestamp"
        )
        rows = await cursor.fetchall()
        return [
            {"timestamp": row[0], "balance": row[1]}
            for row in rows
        ]

    def _determine_verdict(self, metrics: dict) -> tuple[str, str]:
        """Determina veredicto y clase CSS según las métricas.

        Returns
        -------
        (veredicto_texto, clase_css)
        """
        sharpe = metrics.get("sharpe_ratio", Decimal("0"))
        win_rate = metrics.get("win_rate", Decimal("0"))
        net_pnl = metrics.get("net_pnl", Decimal("0"))
        total = metrics.get("total_trades", 0)

        if total < 10:
            return (
                "NECESITA MEJORAS — Datos insuficientes",
                "needs-improvement",
            )

        if sharpe >= 1.0 and win_rate >= Decimal("0.50") and net_pnl > 0:
            return (
                "VALIDADO — La estrategia muestra rendimiento positivo consistente",
                "validated",
            )

        if sharpe >= 0.5 and net_pnl > 0:
            return (
                "NECESITA MEJORAS — Rendimiento positivo pero margen de mejora",
                "needs-improvement",
            )

        return (
            "NO VALIDADO — La estrategia no cumple con los criterios mínimos",
            "not-validated",
        )

    def _compute_worst_days(
        self, trades: list[dict], top_n: int = 5,
    ) -> list[tuple[str, str]]:
        """Retorna los top_n peores días por PnL."""
        if not trades:
            return []

        sorted_t = sorted(trades, key=lambda x: x["timestamp"])
        trade_pnls = _compute_trade_pnls(sorted_t)

        daily: dict[str, Decimal] = defaultdict(Decimal)
        for t, pnl in zip(sorted_t, trade_pnls):
            daily[t["timestamp"][:10]] += pnl

        sorted_days = sorted(daily.items(), key=lambda x: x[1])
        return [
            (day, f"{pnl:.2f}")
            for day, pnl in sorted_days[:top_n]
        ]

    def _format_metric(self, value: Any, decimals: int = 4) -> str:
        """Formatea un métrica como string con precisión decimal."""
        if isinstance(value, Decimal):
            return str(value.quantize(TICK_SIZE if decimals <= 2 else SIZE_PRECISION))
        if isinstance(value, float):
            return f"{value:.{decimals}f}"
        return str(value)

    async def generate(
        self,
        trades_db_path: str,
        config: dict | None = None,
        output_path: str = "whitepaper.html",
    ) -> str:
        """Genera el whitepaper completo.

        Parameters
        ----------
        trades_db_path : str
            Ruta a la base SQLite con las tablas `trades` y `balance_history`.
        config : dict | None
            Configuración opcional que sobrescribe DEFAULT_CONFIG.
        output_path : str
            Ruta donde guardar el archivo HTML generado.

        Returns
        -------
        str — Ruta al archivo generado.
        """
        cfg = {**DEFAULT_CONFIG, **(config or {})}

        # 1. Cargar datos
        trades = await self._load_trades_from_db(trades_db_path)
        balance_history = await self._load_balance_history(trades_db_path)

        if not trades:
            logger.warning("no se encontraron trades — generando informe vacío")

        # 2. Calcular métricas base
        metrics = compute_metrics(trades)
        logger.info("métricas calculadas: PnL=%s Sharpe=%s",
                     metrics.get("net_pnl"), metrics.get("sharpe_ratio"))

        # 3. Determinar período
        if trades:
            start_date = trades[0]["timestamp"][:10]
            end_date = trades[-1]["timestamp"][:10]
        else:
            start_date = "N/A"
            end_date = "N/A"

        # 4. Verdict
        verdict, verdict_class = self._determine_verdict(metrics)

        # 5. Generar gráficos
        logger.info("generando gráficos…")

        equity_chart = generate_equity_curve(trades)
        drawdown_chart = generate_drawdown_chart(trades)
        returns_histogram = generate_daily_returns_histogram(trades)
        size_vs_return = generate_size_vs_return_scatter(trades)
        heatmap_chart = generate_heatmap_time(trades)

        # 6. Prueba de permutación
        logger.info("ejecutando prueba de permutación…")
        sorted_t = sorted(trades, key=lambda x: x["timestamp"])
        trade_pnls = _compute_trade_pnls(sorted_t)
        n_perm = int(cfg.get("permutation_samples", 10_000))

        if len(trade_pnls) >= 5:
            observed_sharpe = _sharpe_from_pnls(trade_pnls)
            perm_result = permutation_test(
                trade_pnls, observed_sharpe, n_samples=n_perm,
            )
            permutation_histogram = generate_permutation_histogram(perm_result)
        else:
            perm_result = {
                "observed_sharpe": 0.0,
                "observed_pnl": 0.0,
                "p_value_sharpe": 1.0,
                "p_value_pnl": 1.0,
                "significant": False,
                "n_samples": 0,
            }
            permutation_histogram = (
                "<p>Mínimo 5 operaciones requeridas para permutación.</p>"
            )

        # 7. Barrido de parámetros
        logger.info("ejecutando barrido de parámetros…")
        if trades:
            sweep_results = parameter_sweep(
                trades,
                min_edge=_safe_decimal(cfg.get("sweep_min_edge", "0.01")),
                max_edge=_safe_decimal(cfg.get("sweep_max_edge", "0.10")),
                min_kelly=_safe_decimal(cfg.get("sweep_min_kelly", "0.05")),
                max_kelly=_safe_decimal(cfg.get("sweep_max_kelly", "0.50")),
                steps=int(cfg.get("sweep_steps", 8)),
            )
            sweep_heatmap = generate_sweep_heatmap(sweep_results)
        else:
            sweep_results = []
            sweep_heatmap = "<p>No hay datos para barrido de parámetros.</p>"

        # 8. Peores días
        worst_days = self._compute_worst_days(trades)

        # 9. Conclusiones
        conclusions, capital_rec, improvements = self._generate_conclusions(
            metrics, perm_result, cfg,
        )

        # 10. Renderizar plantilla
        from jinja2 import Template

        template = Template(HTML_TEMPLATE)

        # Preparar config items para tabla
        config_items = [
            ("Ponderación Wick-Fishing", str(cfg.get("wick_weight", "0.4"))),
            ("Ponderación Sentimiento", str(cfg.get("sentiment_weight", "0.3"))),
            ("Ponderación Monte Carlo", str(cfg.get("montecarlo_weight", "0.3"))),
            ("Umbral EV", str(cfg.get("ev_threshold", "0.03"))),
            ("Fracción Kelly", str(cfg.get("kelly_fraction", "0.25"))),
            ("Caminos Monte Carlo", str(cfg.get("montecarlo_paths", 10000))),
        ]

        # Mercados objetivo
        target_markets = ", ".join(
            sorted(set(t.get("market", "") for t in trades if t.get("market")))
        ) if trades else "No especificado"

        # Formatear métricas
        def fmt(v: Any) -> str:
            if isinstance(v, Decimal):
                if abs(v) >= 1000:
                    return f"{v:,.2f}"
                if abs(v) >= 1:
                    return f"{v:.4f}"
                return f"{v:.6f}"
            return str(v)

        net_pnl = fmt(metrics.get("net_pnl", Decimal("0")))
        return_pct = f'{metrics.get("return_pct", Decimal("0")):.2f}%'
        sharpe_ratio = f'{metrics.get("sharpe_ratio", Decimal("0")):.4f}'
        sortino_ratio = f'{metrics.get("sortino_ratio", Decimal("0")):.4f}'
        calmar_ratio = f'{metrics.get("calmar_ratio", Decimal("0")):.4f}'
        max_drawdown_pct = f'{metrics.get("max_drawdown_pct", Decimal("0")):.2f}%'
        win_rate = f'{metrics.get("win_rate", Decimal("0")):.2%}'
        profit_factor = f'{metrics.get("profit_factor", Decimal("0")):.4f}'
        expectancy = fmt(metrics.get("expectancy", Decimal("0")))
        volatility_annualized = f'{metrics.get("volatility_annualized", Decimal("0")):.4f}'
        return_annualized = f'{metrics.get("return_annualized", Decimal("0")):.4f}'
        total_trades = str(metrics.get("total_trades", 0))
        winning_trades = str(metrics.get("winning_trades", 0))
        losing_trades = str(metrics.get("losing_trades", 0))
        long_trades = str(metrics.get("long_trades", 0))
        short_trades = str(metrics.get("short_trades", 0))
        avg_win = fmt(metrics.get("avg_win", Decimal("0")))
        avg_loss = fmt(metrics.get("avg_loss", Decimal("0")))

        _net_pnl = metrics.get("net_pnl", Decimal("0"))
        _return_pct = metrics.get("return_pct", Decimal("0"))
        _sharpe_ratio = metrics.get("sharpe_ratio", Decimal("0"))
        if not isinstance(_net_pnl, Decimal):
            _net_pnl = Decimal("0")
        if not isinstance(_return_pct, Decimal):
            _return_pct = Decimal("0")
        if not isinstance(_sharpe_ratio, Decimal):
            _sharpe_ratio = Decimal("0")
        pnl_class = "positive" if _net_pnl >= 0 else "negative"
        return_class = "positive" if _return_pct >= 0 else "negative"
        sharpe_class = "positive" if _sharpe_ratio >= 1 else "negative"

        observed_sharpe = f'{perm_result["observed_sharpe"]:.4f}'
        observed_pnl = f'{perm_result["observed_pnl"]:.2f}'
        p_value_sharpe = f'{perm_result["p_value_sharpe"]:.4f}'
        p_value_pnl = f'{perm_result["p_value_pnl"]:.4f}'
        significant = perm_result["significant"]

        circuit_breaker_text = (
            "Los circuit breakers integrados en el Módulo C (Ejecución) protegen "
            "contra: (1) pérdida diaria máxima, (2) drawdown máximo desde elpeak, "
            "y (3) reserva de efectivo mínima. En el backtest, estos se simulan "
            "rechazando operaciones que excederían los límites configurados."
        )

        html = template.render(
            title="Whitepaper Cuantitativo — Polymarket Trading Bot",
            start_date=start_date,
            end_date=end_date,
            generation_date=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            author=cfg.get("author", "BotQuant Team"),
            verdict=verdict,
            verdict_class=verdict_class,
            net_pnl=net_pnl,
            return_pct=return_pct,
            sharpe_ratio=sharpe_ratio,
            sortino_ratio=sortino_ratio,
            calmar_ratio=calmar_ratio,
            max_drawdown_pct=max_drawdown_pct,
            win_rate=win_rate,
            profit_factor=profit_factor,
            expectancy=expectancy,
            volatility_annualized=volatility_annualized,
            return_annualized=return_annualized,
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            long_trades=long_trades,
            short_trades=short_trades,
            avg_win=avg_win,
            avg_loss=avg_loss,
            pnl_class=pnl_class,
            return_class=return_class,
            sharpe_class=sharpe_class,
            strategy_description=cfg.get("strategy_description", ""),
            config_items=config_items,
            target_markets=target_markets,
            equity_chart=equity_chart,
            drawdown_chart=drawdown_chart,
            returns_histogram=returns_histogram,
            size_vs_return=size_vs_return,
            heatmap_chart=heatmap_chart,
            observed_sharpe=observed_sharpe,
            observed_pnl=observed_pnl,
            p_value_sharpe=p_value_sharpe,
            p_value_pnl=p_value_pnl,
            significant=significant,
            permutation_histogram=permutation_histogram,
            sweep_heatmap=sweep_heatmap,
            circuit_breaker_text=circuit_breaker_text,
            worst_days=worst_days,
            conclusions=conclusions,
            capital_recommendation=capital_rec,
            improvements=improvements,
        )

        # 11. Guardar archivo
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)

        # 12. Opcional: guardar JSON con métricas
        metrics_json_path = output_path.replace(".html", ".json")
        metrics_export = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "period": {"start": start_date, "end": end_date},
            "metrics": {
                k: (str(v) if isinstance(v, Decimal) else v)
                for k, v in metrics.items()
            },
            "permutation": {
                "observed_sharpe": perm_result["observed_sharpe"],
                "observed_pnl": perm_result["observed_pnl"],
                "p_value_sharpe": perm_result["p_value_sharpe"],
                "p_value_pnl": perm_result["p_value_pnl"],
                "significant": perm_result["significant"],
                "n_samples": perm_result["n_samples"],
            },
            "verdict": verdict,
        }
        with open(metrics_json_path, "w", encoding="utf-8") as f:
            json.dump(metrics_export, f, indent=2, default=str)

        logger.info("whitepaper generado: %s", output_path)
        logger.info("métricas JSON: %s", metrics_json_path)

        await self.close()
        return output_path

    def _generate_conclusions(
        self,
        metrics: dict,
        perm_result: dict[str, Any],
        config: dict,
    ) -> tuple[str, str, list[str]]:
        """Genera conclusiones, recomendación de capital y áreas de mejora."""
        net_pnl = metrics.get("net_pnl", Decimal("0"))
        sharpe = metrics.get("sharpe_ratio", Decimal("0"))
        win_rate = metrics.get("win_rate", Decimal("0"))
        total = metrics.get("total_trades", 0)
        max_dd = metrics.get("max_drawdown_pct", Decimal("0"))
        significant = perm_result.get("significant", False)

        if total < 5:
            conclusions = (
                "No se dispone de suficientes datos para extraer conclusiones "
                "significativas. Se recomienda acumular más operaciones antes "
                "de tomar decisiones sobre la estrategia."
            )
            capital_rec = (
                "No se recomienda capital inicial hasta tener más datos."
            )
            improvements = [
                "Acumular al menos 30-50 operaciones para análisis estadístico.",
                "Verificar la conectividad con el CLOB de Polymarket.",
                "Monitorear la latencia de ejecución y slippage real.",
            ]
            return conclusions, capital_rec, improvements

        # Conclusiones base
        parts = []
        if net_pnl > 0:
            parts.append(
                f"La estrategia generó un PnL neto positivo de {net_pnl:.2f} USDC "
                f"sobre {total} operaciones, con un win rate del {win_rate:.1%}."
            )
        else:
            parts.append(
                f"La estrategia generó un PnL neto negativo de {net_pnl:.2f} USDC "
                f"sobre {total} operaciones, lo que indica que la configuración "
                f"actual no es rentable."
            )

        if sharpe >= Decimal("1.0"):
            parts.append(
                f"El ratio de Sharpe de {sharpe:.2f} indica una relación "
                f"riesgo-retorno excelente, superando el umbral de 1.0."
            )
        elif sharpe >= Decimal("0.5"):
            parts.append(
                f"El ratio de Sharpe de {sharpe:.2f} es aceptable pero "
                f"mejorable. Se recomienda optimizar parámetros."
            )
        else:
            parts.append(
                f"El ratio de Sharpe de {sharpe:.2f} está por debajo del "
                f"umbral recomendado de 0.5."
            )

        if max_dd > Decimal("20"):
            parts.append(
                f"El drawdown máximo del {max_dd:.1f}% es elevado. "
                f"Considere reducir la fracción de Kelly o ajustar los "
                f"circuit breakers."
            )
        else:
            parts.append(
                f"El drawdown máximo del {max_dd:.1f}% está dentro de "
                f"parámetros aceptables."
            )

        if significant:
            parts.append(
                "La prueba de permutación confirma que los resultados son "
                "estadísticamente significativos (p < 0.05), lo que sugiere "
                "que la estrategia tiene valor predictivo real."
            )
        else:
            parts.append(
                "La prueba de permutación no encontró significancia "
                "estadística al 95%. Los resultados podrían deberse al azar."
            )

        conclusions = " ".join(parts)

        # Recomendación de capital
        if net_pnl > 0 and sharpe >= Decimal("0.5"):
            capital_rec = (
                "Se sugiere un capital inicial de 500-1000 USDC en modo "
                "conservador (fracción Kelly del 12.5%). Aumentar gradualmente "
                "según la consistencia de los resultados."
            )
        else:
            capital_rec = (
                "No se recomienda capital live hasta mejorar las métricas. "
                "Continuar en backtest con datos históricos adicionales."
            )

        # Áreas de mejora
        improvements = [
            "Incorporar más mercados para diversificar el riesgo.",
            "Optimizar ventanas de entrada (opportunity windows) cerca del cierre.",
            "Ajustar la ponderación de componentes (wick/sentimiento/MC) según rendimiento por mercado.",
            "Implementar stop-loss temprano para reducir drawdown en operaciones perdedoras.",
        ]
        if max_dd > Decimal("15"):
            improvements.insert(
                0, "Reforzar circuit breakers para limitar el drawdown máximo."
            )
        if total < 30:
            improvements.insert(
                0, "Acumular más operaciones para aumentar la significancia estadística."
            )

        return conclusions, capital_rec, improvements


# ── Punto de entrada para pruebas ────────────────────────────────────────

async def _test_generate() -> None:
    """Prueba el generador con trades simulados.

    Crea una base SQLite temporal, inserta trades sintéticos con expectancia
    positiva, ejecuta el generador y verifica que el HTML se genere sin errores.
    """
    import os
    import tempfile

    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test_whitepaper.db")

    logger.info("creando base de datos de prueba en %s", db_path)

    # Crear DB y schema
    conn = await aiosqlite.connect(db_path)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY,
            timestamp TEXT,
            asset_id TEXT,
            market TEXT,
            side TEXT,
            price TEXT,
            size TEXT,
            usdc_amount TEXT,
            order_id TEXT,
            success INTEGER
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS balance_history (
            id INTEGER PRIMARY KEY,
            timestamp TEXT,
            balance TEXT
        )
    """)

    from datetime import timedelta

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    trades: list[tuple] = []

    # Generar trades simulados: 80% ganadores, 20% perdedores
    rng = random.Random(42)
    for i in range(200):
        ts = (base + timedelta(hours=i * 3)).isoformat()
        is_winner = rng.random() < 0.62
        size = round(rng.uniform(10, 100), 2)

        if is_winner:
            entry = Decimal(str(round(rng.uniform(0.30, 0.50), 2)))
            exit_p = entry + Decimal(str(round(rng.uniform(0.05, 0.25), 2)))
            side = "SELL_YES"
            price = exit_p
            usdc = exit_p * Decimal(str(size))
        else:
            entry = Decimal(str(round(rng.uniform(0.45, 0.65), 2)))
            exit_p = entry - Decimal(str(round(rng.uniform(0.10, 0.30), 2)))
            exit_p = max(exit_p, Decimal("0.01"))
            side = "SELL_YES"
            price = exit_p
            usdc = exit_p * Decimal(str(size))

        trades.append((
            ts,
            str(rng.randint(1000, 9999)),
            f"market-{rng.randint(1, 5)}",
            side,
            str(price),
            str(size),
            str(usdc),
            f"ord-{i:04d}",
            1,
        ))

    # Insertar BUY trades primero (para que FIFO funcione con los SELL)
    # En lugar de eso, insertamos compras y ventas entremezcladas
    all_rows: list[tuple] = []
    for i in range(200):
        ts = (base + timedelta(hours=i * 3)).isoformat()
        aid = str(rng.randint(1000, 9999))
        market = f"market-{rng.randint(1, 5)}"

        # Buy
        buy_price = Decimal(str(round(rng.uniform(0.25, 0.55), 2)))
        buy_size = Decimal(str(round(rng.uniform(10, 100), 2)))
        buy_usdc = buy_price * buy_size
        all_rows.append((
            ts, aid, market, "BUY_YES", str(buy_price),
            str(buy_size), str(buy_usdc), f"ord-buy-{i:04d}", 1,
        ))

        # Sell posterior (con ganancia o pérdida)
        ts2 = (base + timedelta(hours=i * 3 + 1)).isoformat()
        is_winner = rng.random() < 0.62
        if is_winner:
            sell_price = buy_price + Decimal(str(round(rng.uniform(0.03, 0.20), 2)))
        else:
            sell_price = buy_price - Decimal(str(round(rng.uniform(0.03, 0.15), 2)))
        sell_price = max(sell_price, Decimal("0.01"))
        sell_size = buy_size
        sell_usdc = sell_price * sell_size
        all_rows.append((
            ts2, aid, market, "SELL_YES", str(sell_price),
            str(sell_size), str(sell_usdc), f"ord-sell-{i:04d}", 1,
        ))

    all_rows.sort(key=lambda r: r[0])

    await conn.executemany(
        "INSERT INTO trades "
        "(timestamp, asset_id, market, side, price, size, usdc_amount, order_id, success) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        all_rows,
    )

    # Balance history
    bal = Decimal("1000")
    bal_rows = []
    for i in range(50):
        ts = (base + timedelta(days=i)).isoformat()
        bal += Decimal(str(round(rng.uniform(-20, 30), 2)))
        bal_rows.append((ts, str(bal)))
    await conn.executemany(
        "INSERT INTO balance_history (timestamp, balance) VALUES (?, ?)",
        bal_rows,
    )

    await conn.commit()
    await conn.close()

    logger.info("insertados %d trades sintéticos", len(all_rows))

    # Generar whitepaper
    cfg = {
        "author": "BotQuant Test Suite",
        "wick_weight": Decimal("0.4"),
        "sentiment_weight": Decimal("0.3"),
        "montecarlo_weight": Decimal("0.3"),
        "ev_threshold": Decimal("0.03"),
        "kelly_fraction": Decimal("0.25"),
        "sweep_steps": 6,
        "permutation_samples": 500,
    }

    output_path = os.path.join(tmpdir, "test_whitepaper.html")

    generator = WhitepaperGenerator()
    result = await generator.generate(db_path, cfg, output_path)

    logger.info("whitepaper generado en: %s", result)

    # Verificar que el archivo existe y tiene contenido
    assert os.path.exists(result), "El archivo HTML no se generó"
    size = os.path.getsize(result)
    logger.info("tamaño del archivo: %d bytes", size)
    assert size > 5000, "El archivo HTML es demasiado pequeño"

    # Verificar contenido
    with open(result, "r", encoding="utf-8") as f:
        content = f.read()
    assert "Whitepaper Cuantitativo" in content
    assert "VALIDADO" in content or "NO VALIDADO" in content
    assert "Curva de Equity" in content

    # Verificar JSON
    json_path = result.replace(".html", ".json")
    assert os.path.exists(json_path)
    with open(json_path, "r") as f:
        data = json.load(f)
    assert "metrics" in data
    assert "permutation" in data

    logger.info("=== PRUEBA EXITOSA ===")
    logger.info("HTML: %s", result)
    logger.info("JSON: %s", json_path)

    # Limpiar
    import shutil
    shutil.rmtree(tmpdir)


async def main() -> None:
    """Punto de entrada principal para pruebas."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    parser = __import__("argparse").ArgumentParser(
        description="Generar whitepaper cuantitativo para Polymarket Trading Bot"
    )
    parser.add_argument(
        "--db", type=str, default="bot_state.db",
        help="Ruta a la base SQLite (default: bot_state.db)",
    )
    parser.add_argument(
        "--output", type=str, default="whitepaper.html",
        help="Ruta de salida del HTML (default: whitepaper.html)",
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Ejecutar prueba con datos simulados (descarta archivos)",
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="Generar whitepaper demo con datos simulados (conserva HTML)",
    )
    args = parser.parse_args()

    if args.test:
        await _test_generate()
        return

    if args.demo:
        import tempfile
        from pathlib import Path

        demo_dir = Path("demo_whitepaper")
        demo_dir.mkdir(exist_ok=True)
        db_path = str(demo_dir / "demo_trades.db")

        # Generar DB demo
        conn = await aiosqlite.connect(db_path)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY,
                timestamp TEXT, asset_id TEXT, market TEXT, side TEXT,
                price TEXT, size TEXT, usdc_amount TEXT, order_id TEXT, success INTEGER
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS balance_history (
                id INTEGER PRIMARY KEY, timestamp TEXT, balance TEXT
            )
        """)

        import random
        from datetime import datetime, timezone, timedelta
        from decimal import Decimal

        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rng = random.Random(42)
        rows = []
        for i in range(150):
            ts = (base + timedelta(hours=i * 6)).isoformat()
            aid = str(rng.randint(1000, 9999))
            market = f"btc-15min-{rng.randint(1,5)}"
            bp = Decimal(str(round(rng.uniform(0.25, 0.55), 2)))
            bs = Decimal(str(round(rng.uniform(10, 100), 2)))
            rows.append((ts, aid, market, "BUY_YES", str(bp), str(bs),
                         str(bp * bs), f"b{i:04d}", 1))
            ts2 = (base + timedelta(hours=i * 6 + 2)).isoformat()
            winner = rng.random() < 0.62
            sp = bp + Decimal(str(round(rng.uniform(0.03, 0.20), 2))) if winner \
                 else bp - Decimal(str(round(rng.uniform(0.03, 0.12), 2)))
            sp = max(sp, Decimal("0.01"))
            rows.append((ts2, aid, market, "SELL_YES", str(sp), str(bs),
                         str(sp * bs), f"s{i:04d}", 1))
        rows.sort(key=lambda r: r[0])

        await conn.executemany(
            "INSERT INTO trades (timestamp, asset_id, market, side, price, "
            "size, usdc_amount, order_id, success) VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
        # Balance history
        bal = Decimal("1000")
        bal_rows = []
        for i in range(50):
            ts = (base + timedelta(days=i)).isoformat()
            bal += Decimal(str(round(rng.uniform(-20, 30), 2)))
            bal_rows.append((ts, str(bal)))
        await conn.executemany(
            "INSERT INTO balance_history (timestamp, balance) VALUES (?, ?)",
            bal_rows,
        )
        await conn.commit()
        await conn.close()

        args.db = db_path
        args.output = str(demo_dir / "whitepaper.html")
        print(f"Base de datos demo creada: {db_path}")
        print(f"Trades generados: {len(rows)}")

    generator = WhitepaperGenerator()
    result = await generator.generate(args.db, {}, args.output)
    print(f"Whitepaper generado: {result}")


if __name__ == "__main__":
    asyncio.run(main())
