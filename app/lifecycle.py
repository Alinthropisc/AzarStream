from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from litestar import Litestar
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.static_files import create_static_files_router
from litestar.template import TemplateConfig

from app.config import settings
from app.logging import setup_logging, get_logger

if TYPE_CHECKING:
    pass

# Логирование настраивается один раз — при явном вызове setup_logging() из main.py,
# а не на уровне модуля. Здесь только получаем logger.
log = get_logger("app")

# ---------------------------------------------------------------------------
# Webhook coordination via Redis lock (replaces fragile /tmp file approach)
# ---------------------------------------------------------------------------

_WEBHOOK_LOCK_KEY = "mediaflow:webhook_setup_lock"
_SEED_LOCK_KEY = "mediaflow:superadmin_seed_lock"
_WEBHOOK_LOCK_TTL = 60  # seconds


async def _acquire_lock(key: str) -> bool:
    """
    Try to acquire a distributed Redis lock.
    Returns True if this process is the one that should perform the action.
    """
    from services import cache  # local import to avoid circular deps

    acquired = await cache.set_nx(key, str(os.getpid()), ttl=_WEBHOOK_LOCK_TTL)
    return bool(acquired)


async def _setup_webhooks_once() -> dict:
    """
    Set webhooks exactly once across all Granian workers using a Redis lock.
    If another worker already holds the lock — skip silently.
    """
    from services import bot_manager

    pid = os.getpid()

    if not await _acquire_lock(_WEBHOOK_LOCK_KEY):
        log.info("Webhooks already set by another worker, skipping", pid=pid)
        return {"skipped": True, "pid": pid}

    log.info("This worker acquired webhook lock, setting webhooks", pid=pid)
    try:
        result = await bot_manager.setup_all_webhooks(settings.webhook_base_url)
        log.info("Webhooks configured", result=result, pid=pid)
        return result
    except Exception:
        log.exception("Failed to set webhooks", pid=pid)
        raise


async def _seed_superadmin_once() -> None:
    """Seed superadmin exactly once across all Granian workers."""
    if not await _acquire_lock(_SEED_LOCK_KEY):
        return

    try:
        from scripts.seed_admin import seed_superadmin
        await seed_superadmin()
        log.info("Superadmin seed completed")
    except Exception:
        log.exception("Superadmin seed failed (non-critical)")


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: Litestar):
    """Application lifecycle — startup → yield → shutdown."""
    from database.connection import db
    from services import cache, bot_manager, queue_service, advanced_rate_limiter, user_download_queue
    from services.rate_limiter import rate_limiter
    from workers.scheduler import scheduler
    from workers.queue import queue_service as arq_queue

    log.info("Starting MediaFlow...", pid=os.getpid(), debug=settings.debug)

    # --- Startup ---
    try:
        await db.connect()
        log.debug("Database connected")

        await cache.connect()
        log.debug("Cache connected")

        await rate_limiter.start()
        await advanced_rate_limiter.start()
        await user_download_queue.start()
        await bot_manager.setup()
        await queue_service.start()  # Download queue

        # ARQ queue — запускаем в фоне чтобы не блокировать старт
        asyncio.ensure_future(_start_arq_queue_safely(arq_queue))

        await scheduler.start()

        if settings.webhook_base_url:
            results = await _setup_webhooks_once()
            log.info("Webhook setup done", results=results)

        # Seed superadmin from env if needed (once across workers)
        await _seed_superadmin_once()

    except Exception:
        log.exception("Startup failed — shutting down")
        raise

    log.info("MediaFlow started successfully")
    yield

    # --- Shutdown (reverse order) ---
    log.info("Shutting down MediaFlow...")

    await scheduler.stop()
    await arq_queue.stop()  # ARQ queue
    await queue_service.stop()  # Download queue
    await bot_manager.shutdown()
    await user_download_queue.stop()
    await advanced_rate_limiter.stop()
    await rate_limiter.stop()
    await cache.disconnect()
    await db.disconnect()

    log.info("MediaFlow stopped cleanly")


async def _start_arq_queue_safely(arq_queue) -> None:
    """Start ARQ queue in background to avoid blocking startup"""
    try:
        await arq_queue.start()
    except Exception as e:
        log.warning("ARQ queue start failed (broadcasts unavailable)", error=str(e))


# ---------------------------------------------------------------------------
# Jinja2 helpers
# ---------------------------------------------------------------------------


def _format_bytes(n: int | float) -> str:
    """Human-readable byte size: 1048576 → '1.0 MB'."""
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PB"


def _get_now(*_args, **_kwargs) -> datetime:
    return datetime.now()


def _setup_jinja2(engine) -> None:
    """Register custom Jinja2 filters and globals."""
    engine.register_template_callable("now", _get_now)
    engine.engine.filters["format_bytes"] = _format_bytes


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(lifespan_handlers: list | None = None) -> Litestar:
    from app.controllers import (
        AdminController,
        AuthController,
        BotController,
        AdController,
        StatsController,
        QueueController,
        WebhookController,
        HealthController,
        IndexController,
        UserController,
        TelemetryController,
        SubscriptionController,
    )
    from app.controllers.cache_channels import CacheChannelWebController
    from app.controllers.admin_mgmt import AdminManagementController
    from app.controllers.cookies import CookieController
    from app.controllers.tracks import TrackController
    from app.controllers.media_upload import upload_media_to_telegram
    from app.middleware.rate_limit import RateLimitMiddleware
    from app.middleware.auth import AuthMiddleware

    # Use provided lifespan_handlers (for tests) or default
    lifespan_to_use = lifespan_handlers if lifespan_handlers is not None else [lifespan]

    return Litestar(
        route_handlers=[
            AdminController,
            AuthController,
            BotController,
            AdController,
            StatsController,
            WebhookController,
            QueueController,
            HealthController,
            IndexController,
            UserController,
            TelemetryController,
            SubscriptionController,
            CacheChannelWebController,
            AdminManagementController,
            CookieController,
            TrackController,
            upload_media_to_telegram,
            create_static_files_router(path="/static", directories=["static"]),
        ],
        template_config=TemplateConfig(
            engine=JinjaTemplateEngine,
            directory=Path("resources"),
            engine_callback=_setup_jinja2,
        ),
        middleware=[
            RateLimitMiddleware,
            AuthMiddleware,
        ],
        lifespan=lifespan_to_use,
        debug=settings.debug,
    )


app = create_app()
