"""
Bot Profiler — Integrated performance profiling for bot PELE.

Wraps cProfile to provide context-manager based profiling of specific
code sections. Outputs sorted stats to stdout or a file.

Usage:
    profiler = BotProfiler(enabled=True)
    profiler.start()
    # ... do work ...
    profile_output = profiler.stop()
    print(profile_output)
"""

import asyncio
import cProfile
import io
import logging
import pstats
import time
from contextlib import contextmanager
from typing import Any, Optional

from src.config.optimization_settings import LOCAL_OPTIMIZATION_CONFIG

logger = logging.getLogger("bot_profiler")


class BotProfiler:
    """Lightweight profiler wrapper for interactive performance debugging.

    Parameters
    ----------
    enabled : bool
        Whether profiling is active. When False, all methods are no-ops.
    top_n : int
        Number of top functions to display in stats output.
    output_file : str, optional
        Path to write profile stats. If None, only stdout is used.
    """

    def __init__(
        self,
        enabled: bool = False,
        top_n: int = 20,
        output_file: Optional[str] = None,
    ) -> None:
        self._enabled = enabled
        self._top_n = top_n
        self._output_file = output_file
        self._profiler: Optional[cProfile.Profile] = None
        self._start_time: float = 0.0

    @classmethod
    def from_config(cls) -> "BotProfiler":
        """Create a BotProfiler from the global optimization config."""
        cfg = LOCAL_OPTIMIZATION_CONFIG.get("profiling", {})
        return cls(
            enabled=cfg.get("enabled", False),
            top_n=cfg.get("top_n_functions", 20),
            output_file=cfg.get("output_file"),
        )

    def start(self) -> None:
        """Start profiling. No-op if disabled."""
        if not self._enabled:
            return
        self._profiler = cProfile.Profile()
        self._profiler.enable()
        self._start_time = time.perf_counter()
        logger.info("Profiler started")

    def stop(self) -> str:
        """Stop profiling and return formatted stats.

        Returns
        -------
        str
            Formatted profiling output (top N functions by cumulative time).
            Empty string if profiling was disabled.
        """
        if not self._enabled or self._profiler is None:
            return ""

        self._profiler.disable()
        elapsed = time.perf_counter() - self._start_time

        # Capture stats to string
        s = io.StringIO()
        ps = pstats.Stats(self._profiler, stream=s).sort_stats("cumulative")
        ps.print_stats(self._top_n)

        output = s.getvalue()
        header = (
            f"\n{'=' * 60}\n"
            f"PROFILER OUTPUT (top {self._top_n} by cumulative time)\n"
            f"Wall time: {elapsed:.3f}s\n"
            f"{'=' * 60}\n"
        )
        result = header + output

        # Write to file if configured
        if self._output_file:
            try:
                with open(self._output_file, "w") as f:
                    f.write(result)
                logger.info("Profile written to %s", self._output_file)
            except OSError as exc:
                logger.warning("Cannot write profile to %s: %s", self._output_file, exc)

        logger.info("Profiler stopped (%.3fs elapsed)", elapsed)
        return result

    def reset(self) -> None:
        """Reset the profiler. Must be called between profiling sessions."""
        self._profiler = None
        self._start_time = 0.0

    @contextmanager
    def profile_section(self, name: str = ""):
        """Context manager for profiling a specific code section.

        Usage:
            with profiler.profile_section("monte_carlo"):
                run_monte_carlo()
        """
        self.start()
        try:
            yield
        finally:
            output = self.stop()
            if output and name:
                logger.info("Profile section [%s]:\n%s", name, output)


class AggregatedTimer:
    """Simple aggregated timer for lightweight performance measurement.

    Accumulates total time and call count for named operations.
    Useful for measuring latencies without full cProfile overhead.

    Usage:
        timer = AggregatedTimer()
        with timer.measure("price_update"):
            process_price()
        print(timer.stats())
    """

    def __init__(self) -> None:
        self._times: dict[str, list[float]] = {}

    @contextmanager
    def measure(self, name: str):
        """Context manager that measures execution time."""
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = (time.perf_counter() - start) * 1000.0
            if name not in self._times:
                self._times[name] = []
            self._times[name].append(elapsed)

    def stats(self) -> dict[str, dict[str, float]]:
        """Get aggregated timing stats.

        Returns
        -------
        dict mapping name -> {count, total_ms, mean_ms, max_ms}
        """
        result: dict[str, dict[str, float]] = {}
        for name, samples in self._times.items():
            if not samples:
                continue
            result[name] = {
                "count": len(samples),
                "total_ms": round(sum(samples), 3),
                "mean_ms": round(sum(samples) / len(samples), 3),
                "max_ms": round(max(samples), 3),
            }
        return result

    def clear(self) -> None:
        self._times.clear()
