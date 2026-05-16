"""
Módulo D — Archivo y Backtesting para Polymarket.

Almacena operaciones en SQLite, calcula métricas de rendimiento,
simula backtesting sobre señales históricas y genera informes JSON.
Opcionalmente expone métricas vía Prometheus para Grafana.
"""

import asyncio
import json
import logging
import math
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from statistics import mean, stdev, StatisticsError
from typing import Any, Awaitable, Callable

import aiosqlite

logger = logging.getLogger("archivo")

# ── Constantes ──────────────────────────────────────────────────────────
COMMISSION_PCT = Decimal("0.002")
BALANCE_SNAPSHOT_INTERVAL = 60
TICK_SIZE = Decimal("0.01")
DEFAULT_PROMETHEUS_PORT = 8000

BUY_SIDES = frozenset({"BUY", "BUY_YES", "BUY_NO"})


# ── Dataclass Trade ─────────────────────────────────────────────────────
@dataclass
class Trade:
    timestamp: str
    asset_id: str
    market: str
    side: str
    price: Decimal
    size: Decimal
    usdc_amount: Decimal
    order_id: str = ""
    success: bool = True


# ── Clase principal ─────────────────────────────────────────────────────
class ArchivoBacktest:
    """Archivo y backtesting de operaciones.

    Parameters
    ----------
    db_path : str
        Ruta al archivo SQLite.
    execution_log_queue : asyncio.Queue | None
        Cola con registros de ejecución desde el Módulo C.
    balance_provider : Callable[[], Awaitable[Decimal]] | None
        Coroutine opcional que retorna el balance USDC actual.
    """

    def __init__(
        self,
        db_path: str,
        execution_log_queue: asyncio.Queue | None = None,
        balance_provider: Callable[[], Awaitable[Decimal]] | None = None,
    ) -> None:
        self.db_path = db_path
        self.queue = execution_log_queue
        self.balance_provider = balance_provider
        self._running = False
        self._db: aiosqlite.Connection | None = None
        self._tasks: list[asyncio.Task] = []
        self._latest_metrics: dict[str, Any] = {}
        self._prom_gauges: dict[str, Any] = {}
        self._prometheus_started = False

    # ── Base de datos ──────────────────────────────────────────────────

    async def _init_db(self) -> None:
        if self._db is None:
            self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("""
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
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS balance_history (
                id INTEGER PRIMARY KEY,
                timestamp TEXT,
                balance TEXT
            )
        """)
        await self._db.commit()

    async def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            self._db = await aiosqlite.connect(self.db_path)
            await self._init_db()
        return self._db

    # ── Bucle principal ────────────────────────────────────────────────

    async def run(self) -> None:
        """Consume eventos de la cola y almacena snapshots de balance."""
        self._running = True
        await self._init_db()

        tasks = []
        if self.queue is not None:
            tasks.append(asyncio.create_task(self._consume_events()))
        if self.balance_provider is not None:
            tasks.append(asyncio.create_task(self._periodic_balance_snapshot()))
        self._tasks = tasks

        logger.info(
            "ArchivoBacktest iniciado (%d tareas)", len(self._tasks)
        )
        try:
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            for t in self._tasks:
                if not t.done():
                    t.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            if self._db:
                await self._db.close()
                self._db = None
            logger.info("ArchivoBacktest detenido")

    def stop(self) -> None:
        self._running = False

    async def _consume_events(self) -> None:
        q = self.queue
        if q is None:
            return
        while self._running:
            try:
                entry = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except Exception:
                logger.exception("error en cola de eventos")
                continue
            try:
                await self._store_trade(entry)
            except Exception:
                logger.exception("error almacenando trade")

    async def _store_trade(self, entry: dict) -> None:
        db = await self._ensure_db()
        price = Decimal(str(entry.get("price", "0")))
        size = Decimal(str(entry.get("size", "0")))
        usdc_amount = price * size

        trade = Trade(
            timestamp=entry.get(
                "timestamp", datetime.now(timezone.utc).isoformat()
            ),
            asset_id=entry.get("asset_id", ""),
            market=entry.get("market", ""),
            side=entry.get("side", ""),
            price=price,
            size=size,
            usdc_amount=usdc_amount,
            order_id=entry.get("order_id", ""),
            success=bool(entry.get("success", False)),
        )
        await db.execute(
            "INSERT INTO trades "
            "(timestamp, asset_id, market, side, price, size, usdc_amount, order_id, success) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trade.timestamp,
                trade.asset_id,
                trade.market,
                trade.side,
                str(trade.price),
                str(trade.size),
                str(trade.usdc_amount),
                trade.order_id,
                int(trade.success),
            ),
        )
        await db.commit()

    async def _periodic_balance_snapshot(self) -> None:
        provider = self.balance_provider
        if provider is None:
            return
        while self._running:
            try:
                balance = await provider()
                ts = datetime.now(timezone.utc).isoformat()
                db = await self._ensure_db()
                await db.execute(
                    "INSERT INTO balance_history (timestamp, balance) VALUES (?, ?)",
                    (ts, str(balance)),
                )
                await db.commit()
                logger.debug("balance snapshot: %s", balance)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("error en snapshot de balance")
            await asyncio.sleep(BALANCE_SNAPSHOT_INTERVAL)

    # ── Lectura de trades desde DB ─────────────────────────────────────

    async def _fetch_trades(
        self, from_date: str | None = None, to_date: str | None = None
    ) -> list[Trade]:
        db = await self._ensure_db()
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
        return [
            Trade(
                timestamp=row[0],
                asset_id=row[1],
                market=row[2],
                side=row[3],
                price=Decimal(row[4]),
                size=Decimal(row[5]),
                usdc_amount=Decimal(row[6]),
                order_id=row[7],
                success=bool(row[8]),
            )
            for row in rows
        ]

    # ── Cálculo de métricas ────────────────────────────────────────────

    def _calculate_metrics(self, trades: list[Trade]) -> dict[str, Any]:
        """Calcula métricas de rendimiento sobre una lista de trades.

        Usa mark-to-market: el último precio conocido de cada activo
        sirve como precio de valoración para posiciones abiertas.

        El PnL se distribuye así:
        - Trades BUY: reciben PnL mark-to-market por la fracción aún abierta.
        - Trades SELL: reciben el PnL realizado al cerrar contra posiciones
          previas (FIFO).
        """
        if not trades:
            return {
                "net_pnl_usdc": Decimal("0"),
                "return_pct": Decimal("0"),
                "sharpe_ratio": Decimal("0"),
                "max_drawdown_pct": Decimal("0"),
                "win_rate": Decimal("0"),
                "total_trades": 0,
            }

        sorted_trades = sorted(trades, key=lambda t: t.timestamp)

        # Último precio disponible por activo (para MTM)
        latest_price: dict[str, Decimal] = {}
        for t in reversed(sorted_trades):
            if t.asset_id and t.asset_id not in latest_price:
                latest_price[t.asset_id] = t.price

        # FIFO position tracking: asset_id -> [(entry_price, remaining_size, original_index)]
        open_positions: dict[str, list[list[Any]]] = defaultdict(list)

        trade_pnls: list[Decimal] = [Decimal("0")] * len(sorted_trades)
        winning = 0
        total_buy_usdc = Decimal("0")

        # Primera pasada: procesar compras y ventas en orden FIFO
        for i, t in enumerate(sorted_trades):
            if not t.success:
                continue

            if t.side in BUY_SIDES:
                open_positions[t.asset_id].append([t.price, t.size, i])
                total_buy_usdc += t.usdc_amount
            else:
                # SELL: realizar PnL contra posiciones abiertas (FIFO)
                pnl = Decimal("0")
                remaining = t.size
                pos_list = open_positions.get(t.asset_id, [])
                while remaining > 0 and pos_list:
                    entry_price, pos_size, buy_idx = pos_list[0]
                    close_size = min(remaining, pos_size)
                    realized = (t.price - entry_price) * close_size
                    pnl += realized
                    remaining -= close_size
                    pos_list[0][1] -= close_size
                    if pos_list[0][1] <= 0:
                        pos_list.pop(0)
                trade_pnls[i] = pnl

        # Segunda pasada: MTM para posiciones aún abiertas
        for asset_id, pos_list in open_positions.items():
            mtm = latest_price.get(asset_id)
            if mtm is None:
                continue
            for entry_price, remaining_size, buy_idx in pos_list:
                if remaining_size > 0:
                    trade_pnls[buy_idx] += (mtm - entry_price) * remaining_size

        # Contar ganadoras
        for pnl in trade_pnls:
            if pnl > 0:
                winning += 1

        net_pnl = sum(trade_pnls, Decimal("0"))

        # Rentabilidad porcentual
        return_pct = (
            (net_pnl / total_buy_usdc * Decimal("100"))
            if total_buy_usdc > 0
            else Decimal("0")
        )

        # Sharpe ratio (retornos diarios)
        daily_pnl: dict[str, Decimal] = defaultdict(Decimal)
        for t, pnl in zip(sorted_trades, trade_pnls):
            daily_pnl[t.timestamp[:10]] += pnl

        if len(daily_pnl) >= 2:
            vals = [float(v) for v in daily_pnl.values()]
            mu = mean(vals)
            try:
                sigma = stdev(vals)
            except StatisticsError:
                sigma = 0.0
            sharpe = (
                Decimal(str(round(mu / sigma * math.sqrt(252), 8)))
                if sigma > 0
                else Decimal("0")
            )
        else:
            sharpe = Decimal("0")

        # Máximo drawdown
        cumulative = Decimal("0")
        peak = Decimal("0")
        max_dd = Decimal("0")
        for pnl in trade_pnls:
            cumulative += pnl
            if cumulative > peak:
                peak = cumulative
            if peak > 0:
                dd = (peak - cumulative) / peak
                if dd > max_dd:
                    max_dd = dd

        max_drawdown_pct = max_dd * Decimal("100")

        total_trades = len(sorted_trades)
        win_rate = (
            Decimal(str(winning)) / Decimal(str(total_trades))
            if total_trades > 0
            else Decimal("0")
        )

        return {
            "net_pnl_usdc": net_pnl,
            "return_pct": return_pct,
            "sharpe_ratio": sharpe,
            "max_drawdown_pct": max_drawdown_pct,
            "win_rate": win_rate,
            "total_trades": total_trades,
        }

    async def compute_metrics(
        self, from_date: str, to_date: str
    ) -> dict[str, Any]:
        """Calcula métricas de rendimiento para un rango de fechas.

        Parameters
        ----------
        from_date : str
            ISO timestamp de inicio (inclusive).
        to_date : str
            ISO timestamp de fin (inclusive).

        Returns
        -------
        dict con net_pnl_usdc, return_pct, sharpe_ratio,
             max_drawdown_pct, win_rate, total_trades.
        """
        trades = await self._fetch_trades(from_date, to_date)
        metrics = self._calculate_metrics(trades)
        self._latest_metrics = metrics
        self._update_prometheus_metrics()
        return metrics

    # ── Backtesting ────────────────────────────────────────────────────

    def run_backtest(self, df: "Any") -> dict[str, Any]:
        """Simula ejecución sobre señales históricas y retorna métricas.

        Aplica slippage fijo de 1 tick y comisión del 0.2 % por
        operación, según las reglas de Polymarket.

        Parameters
        ----------
        df : pandas.DataFrame
            Debe tener columnas: timestamp, asset_id, market, signal,
            probability, ev, price.

        Returns
        -------
        dict con métricas (misma estructura que compute_metrics).
        """
        trades: list[Trade] = []

        for _, row in df.iterrows():
            raw_side = str(row.get("signal", "NONE"))
            raw_price = Decimal(str(row.get("price", "0")))
            raw_size = Decimal(str(row.get("size", "1")))
            asset_id = str(row.get("asset_id", ""))
            market = str(row.get("market", ""))
            ts = str(row.get("timestamp", datetime.now(timezone.utc).isoformat()))

            is_buy = raw_side in BUY_SIDES

            if is_buy:
                exec_price = (raw_price + TICK_SIZE).quantize(
                    TICK_SIZE, rounding=ROUND_HALF_UP
                )
            else:
                exec_price = (raw_price - TICK_SIZE).quantize(
                    TICK_SIZE, rounding=ROUND_HALF_UP
                )
            exec_price = max(exec_price, TICK_SIZE)

            notional = exec_price * raw_size
            fee = notional * COMMISSION_PCT
            usdc_amount = notional + fee

            trades.append(
                Trade(
                    timestamp=ts,
                    asset_id=asset_id,
                    market=market,
                    side=raw_side,
                    price=exec_price,
                    size=raw_size,
                    usdc_amount=usdc_amount,
                    order_id="backtest",
                    success=True,
                )
            )

        return self._calculate_metrics(trades)

    # ── Informe JSON ───────────────────────────────────────────────────

    async def generate_report(self) -> str:
        """Genera informe JSON con métricas del último mes.

        Returns
        -------
        str — JSON con las métricas.
        """
        to_date = datetime.now(timezone.utc)
        from_date = to_date - timedelta(days=30)
        metrics = await self.compute_metrics(
            from_date.isoformat(), to_date.isoformat()
        )
        report = {
            "report_type": "monthly_performance",
            "period": {
                "from": from_date.isoformat(),
                "to": to_date.isoformat(),
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "metrics": {k: str(v) if isinstance(v, Decimal) else v for k, v in metrics.items()},
            "metrics_raw": {k: float(v) if isinstance(v, Decimal) else v for k, v in metrics.items()},
        }
        return json.dumps(report, indent=2)

    # ── Prometheus ─────────────────────────────────────────────────────

    def start_prometheus_server(self, port: int = DEFAULT_PROMETHEUS_PORT) -> None:
        """Inicia servidor HTTP Prometheus en un hilo separado.

        Expone /metrics con las métricas más recientes para Grafana.
        """
        from prometheus_client import Gauge, start_http_server

        self._prom_gauges = {
            "archivo_pnl_usdc": Gauge(
                "archivo_pnl_usdc", "Net PnL in USDC"
            ),
            "archivo_return_pct": Gauge(
                "archivo_return_pct", "Return percentage"
            ),
            "archivo_sharpe_ratio": Gauge(
                "archivo_sharpe_ratio", "Sharpe ratio (daily)"
            ),
            "archivo_max_drawdown_pct": Gauge(
                "archivo_max_drawdown_pct", "Max drawdown percentage"
            ),
            "archivo_win_rate": Gauge(
                "archivo_win_rate", "Win rate"
            ),
            "archivo_total_trades": Gauge(
                "archivo_total_trades", "Total number of trades"
            ),
        }

        thread = threading.Thread(
            target=start_http_server,
            args=(port,),
            daemon=True,
        )
        thread.start()
        self._prometheus_started = True
        logger.info("servidor Prometheus iniciado en puerto %d", port)

    def _update_prometheus_metrics(self) -> None:
        if not self._prometheus_started or not self._latest_metrics:
            return
        for key, gauge in self._prom_gauges.items():
            metric_key = key.replace("archivo_", "", 1)
            val = self._latest_metrics.get(metric_key)
            if val is not None:
                gauge.set(float(val))


# ── main de prueba ─────────────────────────────────────────────────────

async def _test_consumer() -> None:
    """Prueba el consumidor de eventos y cálculo de métricas."""
    q: asyncio.Queue = asyncio.Queue()
    archivo = ArchivoBacktest(
        db_path="/tmp/test_archivo.db",
        execution_log_queue=q,
    )

    entries = [
        {
            "timestamp": "2026-05-01T12:00:00Z",
            "asset_id": "1001",
            "market": "market-a",
            "side": "BUY_YES",
            "price": "0.50",
            "size": "100",
            "success": True,
            "order_id": "ord-001",
        },
        {
            "timestamp": "2026-05-02T12:00:00Z",
            "asset_id": "1001",
            "market": "market-a",
            "side": "BUY_YES",
            "price": "0.55",
            "size": "50",
            "success": True,
            "order_id": "ord-002",
        },
        {
            "timestamp": "2026-05-03T12:00:00Z",
            "asset_id": "1001",
            "market": "market-a",
            "side": "SELL_YES",
            "price": "0.60",
            "size": "30",
            "success": True,
            "order_id": "ord-003",
        },
        {
            "timestamp": "2026-05-04T12:00:00Z",
            "asset_id": "1002",
            "market": "market-b",
            "side": "BUY_YES",
            "price": "0.30",
            "size": "200",
            "success": True,
            "order_id": "ord-004",
        },
    ]
    for e in entries:
        await q.put(e)

    try:
        task = asyncio.create_task(archivo.run())
        await asyncio.sleep(0.3)
        archivo.stop()
        await task

        metrics = await archivo.compute_metrics(
            "2026-05-01T00:00:00Z", "2026-05-31T23:59:59Z"
        )
        print("Métricas:", json.dumps(
            {k: str(v) if isinstance(v, Decimal) else v for k, v in metrics.items()},
            indent=2,
        ))

        report = await archivo.generate_report()
        print("\nReporte:", report)
    finally:
        import os
        if os.path.exists("/tmp/test_archivo.db"):
            os.remove("/tmp/test_archivo.db")


async def _test_backtest() -> None:
    """Prueba el backtesting con datos simulados."""
    try:
        import pandas as pd
    except ImportError:
        logger.warning("pandas no instalado — se salta test de backtest")
        return

    archivo = ArchivoBacktest(db_path="/tmp/test_bt.db")

    df = pd.DataFrame([
        {
            "timestamp": "2026-05-01T12:00:00Z",
            "asset_id": "2001",
            "market": "market-x",
            "signal": "BUY_YES",
            "probability": 0.55,
            "ev": 0.05,
            "price": 0.50,
            "size": 100,
        },
        {
            "timestamp": "2026-05-02T12:00:00Z",
            "asset_id": "2001",
            "market": "market-x",
            "signal": "BUY_YES",
            "probability": 0.62,
            "ev": 0.04,
            "price": 0.58,
            "size": 50,
        },
        {
            "timestamp": "2026-05-03T12:00:00Z",
            "asset_id": "2002",
            "market": "market-y",
            "signal": "BUY_NO",
            "probability": 0.35,
            "ev": 0.03,
            "price": 0.32,
            "size": 200,
        },
    ])

    metrics = archivo.run_backtest(df)
    print("\nBacktest Métricas:", json.dumps(
        {k: str(v) if isinstance(v, Decimal) else v for k, v in metrics.items()},
        indent=2,
    ))

    import os
    if os.path.exists("/tmp/test_bt.db"):
        os.remove("/tmp/test_bt.db")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    asyncio.run(_test_consumer())
    asyncio.run(_test_backtest())
