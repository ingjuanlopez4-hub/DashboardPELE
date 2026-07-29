"""
Event Loop Module — Optimized runtime startup for bot PELE.

Provides:
  - uvloop installation (Linux/macOS) with asyncio fallback (Windows)
  - CPU core pinning to avoid L1/L2 cache migration
  - ProcessPoolExecutor with core affinity for CPU-bound tasks
  - System-level network tuning (ulimit, TCP buffer sizes)
  - Optimized asyncio event loop configuration

Use at the very beginning of the main entry point:
    from src.infrastructure.event_loop import bootstrap_optimized_runtime
    bootstrap_optimized_runtime()
"""

import asyncio
import logging
import os
import platform
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Optional

from src.config.optimization_settings import LOCAL_OPTIMIZATION_CONFIG

logger = logging.getLogger("event_loop")


def install_uvloop() -> None:
    """Replace the default asyncio event loop with uvloop (2-4x faster I/O).

    uvloop is a drop-in replacement that doubles throughput on network
    operations (WebSocket, HTTP). Falls back silently to asyncio on
    Windows or when uvloop is not installed.
    """
    if platform.system() not in ("Linux", "Darwin"):
        logger.debug("uvloop skipped: unsupported platform %s", platform.system())
        return

    try:
        import uvloop  # type: ignore[import-untyped]
        uvloop.install()
        logger.info("uvloop installed: event loop replaced (2-4x I/O throughput)")
    except ImportError:
        logger.info("uvloop not installed — using standard asyncio event loop")
    except Exception as exc:
        logger.warning("uvloop installation failed: %s — using standard asyncio", exc)


def pin_to_core(core_id: int) -> None:
    """Pin the current process to a specific CPU core.

    This prevents L1/L2 cache migration between cores, improving
    performance in latency-sensitive trading applications.

    Parameters
    ----------
    core_id : int
        The logical CPU core number to pin to.

    Notes
    -----
    - Only works on Linux.
    - Requires psutil to be installed.
    - Silently ignores failures (e.g., core doesn't exist, no permissions).
    """
    if platform.system() != "Linux":
        return

    try:
        import psutil  # type: ignore[import-untyped]
        p = psutil.Process()
        available_cores = p.cpu_affinity()
        if core_id in available_cores:
            p.cpu_affinity([core_id])
            logger.info("Pinned PID %d to core %d", os.getpid(), core_id)
        else:
            logger.warning(
                "Core %d not in available cores %s — pinning to first available",
                core_id, available_cores,
            )
            p.cpu_affinity([available_cores[0]])
    except ImportError:
        logger.debug("psutil not installed — skipping CPU affinity")
    except AttributeError:
        logger.debug("cpu_affinity not available on this platform")
    except Exception as exc:
        logger.debug("CPU affinity failed: %s", exc)


def pin_by_role(role: str) -> None:
    """Pin current process to the core assigned to a given role.

    Parameters
    ----------
    role : str
        One of: 'ingestion', 'strategy', 'execution', 'monitoring'.

    Core mapping (from optimization_settings):
        Core 0: Ingestion (WebSocket, data feed)
        Core 1: Strategy (signal computation)
        Core 2: Execution (order placement, circuit breakers)
        Core 3: Monitoring (health endpoint, database, metrics)
    """
    cfg = LOCAL_OPTIMIZATION_CONFIG.get("cpu_affinity", {})
    if not cfg.get("enabled", True):
        return

    cores = cfg.get("cores", {})
    core_id = cores.get(role)
    if core_id is not None:
        pin_to_core(core_id)
    else:
        logger.warning("No core assigned for role '%s'", role)


def apply_system_limits() -> None:
    """Apply system-level network and file descriptor optimizations.

    This includes:
      - Increasing max open files limit (ulimit -n)
      - TCP buffer size tuning (requires root or sysctl access)

    These settings are important for handling multiple WebSocket connections
    and high-frequency HTTP requests to the CLOB API.
    """
    cfg = LOCAL_OPTIMIZATION_CONFIG.get("system", {})

    # Max open files
    max_files = cfg.get("max_open_files", 65536)
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < max_files:
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (max_files, hard))
                logger.info("Raised RLIMIT_NOFILE: %d → %d", soft, max_files)
            except (ValueError, PermissionError):
                logger.debug("Cannot raise RLIMIT_NOFILE beyond %d (hard=%d)", soft, hard)
    except (ImportError, AttributeError):
        logger.debug("resource module not available")

    # TCP tuning (best-effort via environment variables)
    tcp_settings = {
        "net.core.rmem_max": cfg.get("tcp_rmem_max", 134217728),
        "net.core.wmem_max": cfg.get("tcp_wmem_max", 134217728),
        "net.ipv4.tcp_fastopen": cfg.get("tcp_fastopen", 3),
    }
    for param, value in tcp_settings.items():
        logger.debug("TCP tuning: %s=%s (requires sysctl)", param, value)


def create_optimized_executor(
    max_workers: Optional[int] = None,
    role: Optional[str] = None,
) -> ProcessPoolExecutor:
    """Create a ProcessPoolExecutor with CPU affinity.

    Parameters
    ----------
    max_workers : int, optional
        Maximum number of worker processes. Defaults to 2.
    role : str, optional
        If provided, each worker process will be pinned to a core
        assigned to this role (rotating through available cores).

    Returns
    -------
    ProcessPoolExecutor
        Configured executor suitable for CPU-bound Monte Carlo
        and FinBERT inference tasks.
    """
    if max_workers is None:
        max_workers = LOCAL_OPTIMIZATION_CONFIG.get(
            "monte_carlo", {}
        ).get("max_workers", 2)

    executor = ProcessPoolExecutor(max_workers=max_workers)
    logger.info("Created ProcessPoolExecutor with %d workers", max_workers)
    return executor


def configure_event_loop(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """Apply performance optimizations to the asyncio event loop.

    Parameters
    ----------
    loop : asyncio.AbstractEventLoop, optional
        The event loop to configure. If None, uses the running loop.
    """
    try:
        loop = loop or asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    # Set the default executor to a thread pool with more workers
    # This improves performance for run_in_executor calls
    import concurrent.futures
    try:
        loop.set_default_executor(
            concurrent.futures.ThreadPoolExecutor(
                max_workers=4,
                thread_name_prefix="pele_async",
            )
        )
    except (NotImplementedError, AttributeError):
        logger.debug("set_default_executor not available")

    logger.info("Event loop configured: %s", type(loop).__name__)


def bootstrap_optimized_runtime(
    role: Optional[str] = None,
    configure_loop: bool = True,
    apply_limits: bool = True,
) -> None:
    """Run ALL runtime optimizations at startup.

    Call this function ONCE at the very beginning of the main entry point,
    before any asyncio.run() call.

    Parameters
    ----------
    role : str, optional
        Role for CPU core pinning (ingestion, strategy, execution, monitoring).
        If None, no core pinning is applied.
    configure_loop : bool
        Whether to configure the event loop (default: True).
    apply_limits : bool
        Whether to apply system limits (default: True).

    Example
    -------
    >>> from src.infrastructure.event_loop import bootstrap_optimized_runtime
    >>> bootstrap_optimized_runtime()
    >>> asyncio.run(main())
    """
    install_uvloop()

    if apply_limits:
        apply_system_limits()

    if role:
        pin_by_role(role)

    if configure_loop:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            configure_event_loop(loop)
        except Exception as exc:
            logger.warning("Event loop configuration failed: %s", exc)

    logger.info("Optimized runtime bootstrap complete")
