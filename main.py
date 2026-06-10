"""NewsEngine process entry point — async main() with FIFO startup and LIFO shutdown.

Usage:
    python main.py                     # normal start
    python main.py                     # Ctrl+C for graceful shutdown
    NEO4J_URI=bolt://... python main.py  # override via env

Startup order (FIFO):
    1. Load .env config via get_settings()
    2. Initialize structured JSON logging
    3. Open and verify Neo4j Bolt connection (hard block — exits on failure)
    4. Create FastAPI app + register whitelist router
    5. Initialize Graphiti SDK (LLM + Embedder clients)
    6. Create EpisodeWriter
    7. Start IngestionScheduler background task
    8. Start uvicorn Server (programmatic API, shared event loop)

Shutdown order (LIFO):
    1. Stop IngestionScheduler
    2. Signal uvicorn to stop accepting new requests
    3. Close EpisodeWriter
    4. Close Neo4j driver

Signal handling:
    - SIGINT  → LIFO graceful shutdown
    - SIGTERM → LIFO graceful shutdown
    - Double SIGINT → immediate force exit
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Any

import uvicorn

from src.api.server import create_app
from src.core.config import get_settings
from src.core.graphiti_client import create_graphiti
from src.core.neo4j_client import get_neo4j_driver, close_neo4j_driver
from src.graphiti.episode_writer import EpisodeWriter
from src.ingestion.scheduler import IngestionScheduler
from src.utils.logging_config import setup_logging, get_logger

# ── Module-level state ──────────────────────────────────────────────────

_shutting_down: bool = False
"""Flag set to True once shutdown starts, protects against double signals."""

_SHUTDOWN_TIMEOUT: int = 30
"""Maximum seconds to wait for graceful shutdown before force exit."""

_logger: Any = None
"""Module-level logger reference, set during step 2."""


# ═══════════════════════════════════════════════════════════════════════
# Signal handler
# ═══════════════════════════════════════════════════════════════════════


async def _shutdown_sequence(
    scheduler: IngestionScheduler,
    server: uvicorn.Server,
    writer: EpisodeWriter,
) -> None:
    """Execute LIFO shutdown sequence.

    Steps:
        1. Stop IngestionScheduler (cancel cycle loop, await completion)
        2. Signal uvicorn to stop accepting requests
        3. Close EpisodeWriter (release Graphiti resources)
        4. Close Neo4j driver (release connection pool)
    """
    # LIFO step 1
    try:
        await scheduler.stop()
        _logger.info("LIFO step 1/4: IngestionScheduler stopped")
    except Exception as exc:
        _logger.warning("LIFO step 1/4: scheduler stop error (ignored): %s", exc)

    # LIFO step 2
    server.should_exit = True
    _logger.info("LIFO step 2/4: uvicorn shutdown signaled")

    # LIFO step 3
    try:
        await writer.close()
        _logger.info("LIFO step 3/4: EpisodeWriter closed")
    except Exception as exc:
        _logger.warning("LIFO step 3/4: writer close error (ignored): %s", exc)

    # LIFO step 4
    try:
        close_neo4j_driver()
        _logger.info("LIFO step 4/4: Neo4j driver closed")
    except Exception as exc:
        _logger.warning("LIFO step 4/4: Neo4j close error (ignored): %s", exc)

    _logger.info("=== NewsEngine shutdown complete ===")


async def _do_shutdown(
    scheduler: IngestionScheduler,
    server: uvicorn.Server,
    writer: EpisodeWriter,
) -> None:
    """Run the shutdown sequence with 30-second timeout protection."""
    global _shutting_down  # noqa: PLW0603  — module-level flag for double-signal

    try:
        await asyncio.wait_for(
            _shutdown_sequence(scheduler, server, writer),
            timeout=_SHUTDOWN_TIMEOUT,
        )
    except asyncio.TimeoutError:
        _logger.warning(
            "Shutdown timed out after %ds — forcing exit",
            _SHUTDOWN_TIMEOUT,
        )
        sys.exit(1)


def _make_signal_handler(
    scheduler: IngestionScheduler,
    server: uvicorn.Server,
    writer: EpisodeWriter,
) -> Any:
    """Create a signal handler closure for SIGINT/SIGTERM.

    Returns a callable suitable for asyncio.loop.add_signal_handler().
    """

    def _handle(sig: signal.Signals) -> None:
        """Handle SIGINT/SIGTERM — initiate LIFO graceful shutdown.

        On double signal (e.g., second Ctrl+C), force immediate exit.
        """
        global _shutting_down  # noqa: PLW0603

        if _shutting_down:
            sig_name = signal.Signals(sig).name
            _logger.warning(
                "Double %s received — forcing immediate exit", sig_name
            )
            sys.exit(1)

        _shutting_down = True
        sig_name = signal.Signals(sig).name
        _logger.warning(
            "Received %s — initiating graceful shutdown", sig_name
        )

        # Schedule the async shutdown on the running event loop
        asyncio.ensure_future(
            _do_shutdown(scheduler, server, writer)
        )

    return _handle


# ═══════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════


async def main() -> None:
    """NewsEngine entry point — 8-step FIFO startup → serve → LIFO shutdown.

    All async components (IngestionScheduler, uvicorn) share a single
    asyncio event loop. uvicorn is started via the programmatic
    ``uvicorn.Server`` API (not ``uvicorn.run()``) to enable loop sharing.
    """
    global _logger  # noqa: PLW0603

    # ═════════════════════════════════════════════════════════════════
    # FIFO step 1: Load and validate .env configuration
    # ═════════════════════════════════════════════════════════════════
    try:
        settings = get_settings()
    except Exception as exc:
        print(f"CRITICAL: Config loading failed: {exc}", file=sys.stderr)
        sys.exit(1)

    # ═════════════════════════════════════════════════════════════════
    # FIFO step 2: Initialize structured JSON logging
    # ═════════════════════════════════════════════════════════════════
    try:
        setup_logging(level=settings.log_level, log_file=settings.log_file)
    except Exception as exc:
        print(
            f"WARNING: Logging setup failed, falling back to stdout: {exc}",
            file=sys.stderr,
        )
    _logger = get_logger(__name__)

    _logger.info("=== NewsEngine starting ===")
    _logger.info(
        "FIFO step 1/8: config OK — api_port=%d, neo4j=%s, log_level=%s",
        settings.api_port,
        settings.neo4j_uri,
        settings.log_level,
    )
    _logger.info("FIFO step 2/8: structured JSON logging initialized")

    # ═════════════════════════════════════════════════════════════════
    # FIFO step 3: Open and verify Neo4j Bolt connection (hard block)
    # ═════════════════════════════════════════════════════════════════
    _logger.info("FIFO step 3/8: connecting to Neo4j...")
    try:
        driver = get_neo4j_driver()
        _logger.info(
            "FIFO step 3/8: Neo4j connection OK — %s", settings.neo4j_uri
        )
    except Exception as exc:
        _logger.critical("FIFO step 3/8: Neo4j connection FAILED: %s", exc)
        sys.exit(1)

    # ═════════════════════════════════════════════════════════════════
    # FIFO step 4: Create FastAPI app + register whitelist router
    # ═════════════════════════════════════════════════════════════════
    _logger.info("FIFO step 4/8: creating FastAPI app...")

    app = create_app()
    try:
        from src.api.routers.whitelist import router as whitelist_router

        app.include_router(whitelist_router)
        _logger.info(
            "FIFO step 4/8: FastAPI app ready, whitelist router registered"
        )
    except Exception as exc:
        _logger.warning(
            "FIFO step 4/8: whitelist router registration failed "
            "(non-fatal, continuing): %s",
            exc,
        )

    # ═════════════════════════════════════════════════════════════════
    # FIFO step 5: Initialize Graphiti SDK
    # ═════════════════════════════════════════════════════════════════
    _logger.info("FIFO step 5/8: initializing Graphiti SDK...")
    try:
        gti = create_graphiti()
        _logger.info("FIFO step 5/8: Graphiti SDK ready")
    except Exception as exc:
        _logger.critical(
            "FIFO step 5/8: Graphiti SDK initialization FAILED: %s", exc
        )
        sys.exit(1)

    # ═════════════════════════════════════════════════════════════════
    # FIFO step 6: Create EpisodeWriter
    # ═════════════════════════════════════════════════════════════════
    _logger.info("FIFO step 6/8: creating EpisodeWriter...")
    try:
        writer = EpisodeWriter(graphiti=gti)
        _logger.info("FIFO step 6/8: EpisodeWriter ready")
    except Exception as exc:
        _logger.critical(
            "FIFO step 6/8: EpisodeWriter creation FAILED: %s", exc
        )
        sys.exit(1)

    # ═════════════════════════════════════════════════════════════════
    # FIFO step 7: Create and start IngestionScheduler
    # ═════════════════════════════════════════════════════════════════
    _logger.info("FIFO step 7/8: starting IngestionScheduler...")
    scheduler = IngestionScheduler(
        neo4j_driver=driver,
        graphiti=gti,
    )
    scheduler_started = False
    try:
        await scheduler.start()
        scheduler_started = True
        _logger.info("FIFO step 7/8: IngestionScheduler started")
    except Exception as exc:
        _logger.warning(
            "FIFO step 7/8: IngestionScheduler start failed "
            "(non-fatal, continuing without ingestion): %s",
            exc,
        )

    # ═════════════════════════════════════════════════════════════════
    # FIFO step 8: Start uvicorn server (programmatic API)
    # ═════════════════════════════════════════════════════════════════
    _logger.info(
        "FIFO step 8/8: starting uvicorn on %s:%d",
        settings.api_host,
        settings.api_port,
    )

    config = uvicorn.Config(
        app=app,
        host=settings.api_host,
        port=settings.api_port,
        loop="asyncio",
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)

    # Disable uvicorn's own signal handlers — we manage signals ourselves
    # via asyncio.loop.add_signal_handler() to integrate with the LIFO
    # shutdown sequence and share the event loop.
    server.install_signal_handlers = lambda: None

    # ── Register SIGINT/SIGTERM handlers ───────────────────────────
    loop = asyncio.get_running_loop()
    signal_handler = _make_signal_handler(scheduler, server, writer)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                sig, lambda s=sig: signal_handler(s)
            )
            _logger.debug(
                "Signal handler registered for %s",
                signal.Signals(sig).name,
            )
        except NotImplementedError:
            _logger.warning(
                "add_signal_handler not supported for %s on this platform",
                signal.Signals(sig).name,
            )
        except ValueError as exc:
            # ValueError when signal is not supported on this platform
            _logger.warning(
                "Cannot register handler for %s: %s",
                signal.Signals(sig).name,
                exc,
            )

    _logger.info(
        "=== NewsEngine startup complete — listening on %s:%d ===",
        settings.api_host,
        settings.api_port,
    )

    # ── Block: serve HTTP requests ─────────────────────────────────
    try:
        await server.serve()
    except asyncio.CancelledError:
        _logger.debug("server.serve() was cancelled")
    except Exception as exc:
        _logger.critical(
            "uvicorn server error: %s", exc, exc_info=True
        )
        # On fatal error, trigger LIFO shutdown
        if not _shutting_down:
            asyncio.ensure_future(
                _do_shutdown(scheduler, server, writer)
            )
        raise

    _logger.info("=== NewsEngine stopped ===")


# ═══════════════════════════════════════════════════════════════════════
# Script entry point
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    asyncio.run(main())
