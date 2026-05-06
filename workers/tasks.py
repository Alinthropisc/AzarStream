import asyncio
import re
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from app.logging import get_logger
from database.connection import db
from repositories.uow import UnitOfWork
from services import bot_manager, cache
from services.rate_limiter import rate_limiter, RateLimitType
from models import Ad, AdStatus, TelegramUser
from models.ads import AdDelivery, AdMediaType

log = get_logger("workers.tasks")

# ──────────────────────────────────────────────────────────────────────────────
# Telegram rate limit constants
#
# Official limits:
#   • 30 messages/second globally per bot
#   • ~1 message/second per individual chat
#   • RetryAfter exception when exceeded — respect retry_after + buffer
#
# Safe defaults:
#   • INTER_MESSAGE_DELAY = 35ms  →  ~28 msg/sec  (leaves 2 msg/sec headroom)
#   • RETRY_AFTER_BUFFER  = 3s   →  extra padding after Telegram's requested wait
#   • MAX_RETRIES         = 3    →  max RetryAfter retries per user before skip
# ──────────────────────────────────────────────────────────────────────────────
_INTER_MESSAGE_DELAY_MS = 35
_RETRY_AFTER_BUFFER_SEC = 3
_MAX_RETRIES = 3
_DELIVERY_FLUSH_SIZE = 50   # commit to DB every N deliveries
_STATS_UPDATE_EVERY = 100   # update sent/failed counters in Ad every N users


def _clean_html(text: str) -> str:
    return re.sub(r"<[^>]*>", "", text)


# ──────────────────────────────────────────────────────────────────────────────
# Low-level send helper
# ──────────────────────────────────────────────────────────────────────────────

async def _send_ad_message(ad: Ad, chat_id: int, bot: Bot) -> int | None:
    """
    Send one ad message to a chat.

    Returns telegram message_id on success, None if send silently failed.

    Strategy:
      1. If ad has media + cache_channel_message_id → use bot.copy_message
         from the cache channel. This works across bots (file_id is bound
         to the uploader bot, but copy_message is not). Requires that the
         worker bot is a member/admin of the cache channel.
      2. Else fall back to plain send_message (text-only ads).

    Raises:
        TelegramRetryAfter    → caller MUST wait and retry
        TelegramForbiddenError → user blocked the bot, caller marks as blocked
    """
    from app.config import settings

    keyboard = _build_ad_keyboard(ad)
    content = (ad.content or "")[:1024]

    has_media = (
        ad.media_type and ad.media_type != AdMediaType.NONE
        and ad.cache_channel_message_id
        and settings.media_flow_cache_channel_id
    )

    async def _try_copy(parse_mode: str | None) -> int | None:
        caption = content if parse_mode else _clean_html(content)
        try:
            msg_id = await bot.copy_message(
                chat_id=chat_id,
                from_chat_id=settings.media_flow_cache_channel_id,
                message_id=ad.cache_channel_message_id,
                caption=caption,
                parse_mode=parse_mode,
                reply_markup=keyboard,
            )
            return msg_id.message_id
        except (TelegramRetryAfter, TelegramForbiddenError):
            raise
        except TelegramBadRequest as e:
            err = str(e).lower()
            if "can't parse entities" in err and parse_mode:
                return await _try_copy(parse_mode=None)
            log.warning(
                "copy_message failed",
                ad_id=ad.id,
                chat_id=chat_id,
                error=str(e)[:200],
            )
            return None

    async def _try_send_text(parse_mode: str | None) -> int | None:
        text = content if parse_mode else _clean_html(content)
        try:
            msg = await bot.send_message(
                chat_id, text=text,
                parse_mode=parse_mode, reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            return msg.message_id
        except (TelegramRetryAfter, TelegramForbiddenError):
            raise
        except TelegramBadRequest as e:
            if "can't parse entities" in str(e) and parse_mode:
                return await _try_send_text(parse_mode=None)
            return None

    if has_media:
        msg_id = await _try_copy(parse_mode="HTML")
        if msg_id:
            return msg_id
        # copy_message failed (bot not in channel, message deleted, etc.) —
        # fall through to text-only send so the user still gets the caption.

    return await _try_send_text(parse_mode="HTML")


def _build_ad_keyboard(ad: Ad) -> InlineKeyboardMarkup | None:
    """Build inline keyboard from ad's buttons (multi-button JSON or legacy single)."""
    rows: list[list[InlineKeyboardButton]] = []

    raw_buttons = getattr(ad, "buttons", None)
    if isinstance(raw_buttons, str):
        try:
            import json as _json
            raw_buttons = _json.loads(raw_buttons)
        except Exception:
            raw_buttons = None
    if isinstance(raw_buttons, list) and raw_buttons:
        by_row: dict[int, list[InlineKeyboardButton]] = {}
        for btn in raw_buttons:
            if not isinstance(btn, dict):
                continue
            text = (btn.get("text") or "").strip()
            url = (btn.get("url") or "").strip()
            if not text or not url:
                continue
            row_idx = int(btn.get("row", 0))
            style = btn.get("style") or None
            kwargs = {"text": text[:64], "url": url}
            if style in ("danger", "success", "primary"):
                kwargs["style"] = style
            by_row.setdefault(row_idx, []).append(InlineKeyboardButton(**kwargs))
        for row_idx in sorted(by_row.keys()):
            rows.append(by_row[row_idx])

    if not rows and ad.button_text and ad.button_url:
        rows.append([InlineKeyboardButton(text=ad.button_text, url=ad.button_url)])

    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


# ──────────────────────────────────────────────────────────────────────────────
# Delivery buffer flush
# ──────────────────────────────────────────────────────────────────────────────

async def _flush_deliveries(buffer: list[dict]) -> None:
    """Bulk-insert buffered delivery records in one short transaction."""
    if not buffer:
        return
    try:
        async with UnitOfWork() as uow:
            for d in buffer:
                uow.session.add(AdDelivery(**d))
            await uow.commit()
    except Exception as e:
        log.warning("Delivery flush failed", error=str(e))


# ──────────────────────────────────────────────────────────────────────────────
# Main broadcast ARQ task
# ──────────────────────────────────────────────────────────────────────────────

async def broadcast_ad(
    ctx: dict,
    ad_id: int,
    delay_ms: int = _INTER_MESSAGE_DELAY_MS,
) -> dict[str, Any]:
    """
    ARQ background task — sends an ad to all target users sequentially.

    Rate limiting strategy:
      • Sequential sends (no parallel batches) — prevents burst floods
      • {delay_ms}ms between each message (~28 msg/sec at default 35ms)
      • TelegramRetryAfter: sleep(retry_after + buffer), retry up to MAX_RETRIES times
      • DB deliveries buffered and committed every {DELIVERY_FLUSH_SIZE} records
      • Ad sent_count / failed_count updated every {STATS_UPDATE_EVERY} users

    Returns:
        {"ad_id", "total", "sent", "failed", "blocked", "duration"}
    """
    log.info("Broadcast task started", ad_id=ad_id, delay_ms=delay_ms)
    start_time = datetime.now()
    sent = failed = blocked = total_users = 0

    try:
        # ── 1. Load ad + target users (short read transaction) ────────────────
        async with UnitOfWork() as uow:
            ad = await uow.ads.get_with_relations(ad_id)
            if not ad:
                log.error("Ad not found", ad_id=ad_id)
                return {"error": "Ad not found"}

            bot_ids = await uow.ads.get_target_bot_ids(ad_id)
            if not bot_ids:
                await uow.ads.update(ad_id, status=AdStatus.COMPLETED, total_recipients=0)
                await uow.commit()
                return {"error": "No target bots", "ad_id": ad_id}

            users: list[TelegramUser] = await uow.users.get_users_for_broadcast(
                bot_ids, ad.target_language
            )
            total_users = len(users)

            # Mark as SENDING
            await uow.ads.update(ad_id, status=AdStatus.SENDING, started_at=datetime.now())
            await uow.commit()

        log.info("Broadcast loaded", ad_id=ad_id, total_users=total_users)

        if total_users == 0:
            async with UnitOfWork() as uow:
                await uow.ads.update(
                    ad_id, status=AdStatus.COMPLETED,
                    completed_at=datetime.now(),
                    total_recipients=0, sent_count=0, failed_count=0,
                )
                await uow.commit()
            return {"ad_id": ad_id, "total": 0, "sent": 0, "failed": 0, "blocked": 0, "duration": 0.0}

        # ── 2. Group by bot ───────────────────────────────────────────────────
        users_by_bot: dict[int, list[TelegramUser]] = {}
        for user in users:
            users_by_bot.setdefault(user.bot_id, []).append(user)

        # ── 3. Sequential send loop ───────────────────────────────────────────
        delivery_buffer: list[dict] = []
        processed = 0

        for bot_id, bot_users in users_by_bot.items():
            bot = await bot_manager.get_bot_by_id(bot_id)
            if not bot:
                log.warning("Bot instance unavailable, skipping", bot_id=bot_id)
                failed += len(bot_users)
                continue

            log.info("Sending via bot", bot_id=bot_id, users=len(bot_users))

            for user in bot_users:
                attempt = 0
                delivery: dict | None = None

                # ── retry loop for TelegramRetryAfter ──
                while attempt <= _MAX_RETRIES:
                    try:
                        msg_id = await _send_ad_message(ad, user.telegram_id, bot)
                        delivery = dict(
                            ad_id=ad.id,
                            user_id=user.id,
                            bot_id=user.bot_id,
                            telegram_chat_id=user.telegram_id,
                            telegram_message_id=msg_id,
                            is_sent=True,
                            error_message=None,
                        )
                        sent += 1
                        break

                    except TelegramRetryAfter as e:
                        wait = e.retry_after + _RETRY_AFTER_BUFFER_SEC
                        log.warning(
                            "Telegram FloodWait — sleeping",
                            retry_after=e.retry_after,
                            wait=wait,
                            ad_id=ad_id,
                            attempt=attempt + 1,
                        )
                        await asyncio.sleep(wait)
                        attempt += 1
                        # Do NOT count as failed — will retry

                    except TelegramForbiddenError:
                        # User blocked the bot
                        blocked += 1
                        failed += 1
                        delivery = dict(
                            ad_id=ad.id,
                            user_id=user.id,
                            bot_id=user.bot_id,
                            telegram_chat_id=user.telegram_id,
                            is_sent=False,
                            error_message="Bot blocked by user",
                        )
                        # Mark user blocked in a separate short transaction
                        try:
                            async with UnitOfWork() as uow:
                                await uow.users.update(user.id, is_blocked=True)
                                await uow.commit()
                        except Exception:
                            pass
                        break

                    except Exception as e:
                        failed += 1
                        delivery = dict(
                            ad_id=ad.id,
                            user_id=user.id,
                            bot_id=user.bot_id,
                            telegram_chat_id=user.telegram_id,
                            is_sent=False,
                            error_message=str(e)[:250],
                        )
                        log.debug("Send failed", user_id=user.telegram_id, error=str(e)[:100])
                        break

                else:
                    # Exhausted all retries
                    failed += 1
                    delivery = dict(
                        ad_id=ad.id,
                        user_id=user.id,
                        bot_id=user.bot_id,
                        telegram_chat_id=user.telegram_id,
                        is_sent=False,
                        error_message="FloodWait — max retries exceeded",
                    )

                if delivery:
                    delivery_buffer.append(delivery)

                processed += 1

                # Flush delivery records to DB every DELIVERY_FLUSH_SIZE
                if len(delivery_buffer) >= _DELIVERY_FLUSH_SIZE:
                    await _flush_deliveries(delivery_buffer)
                    delivery_buffer.clear()

                # Periodically update live stats visible in admin panel
                if processed % _STATS_UPDATE_EVERY == 0:
                    try:
                        async with UnitOfWork() as uow:
                            await uow.ads.update(ad_id, sent_count=sent, failed_count=failed)
                            await uow.commit()
                    except Exception:
                        pass
                    log.info(
                        "Broadcast progress",
                        ad_id=ad_id,
                        processed=processed,
                        total=total_users,
                        sent=sent,
                        failed=failed,
                        blocked=blocked,
                    )

                # Rate limiting delay — sequential, not parallel
                await asyncio.sleep(delay_ms / 1000)

        # ── 4. Final DB flush ─────────────────────────────────────────────────
        await _flush_deliveries(delivery_buffer)

        duration = (datetime.now() - start_time).total_seconds()

        async with UnitOfWork() as uow:
            await uow.ads.update(
                ad_id,
                status=AdStatus.COMPLETED,
                completed_at=datetime.now(),
                total_recipients=total_users,
                sent_count=sent,
                failed_count=failed,
            )
            await uow.commit()

        result = {
            "ad_id": ad_id,
            "total": total_users,
            "sent": sent,
            "failed": failed,
            "blocked": blocked,
            "duration": round(duration, 2),
        }
        log.info("Broadcast completed", **result, duration_str=f"{duration:.1f}s")
        return result

    except Exception as e:
        log.exception("Broadcast task crashed", ad_id=ad_id, error=str(e))
        try:
            async with UnitOfWork() as uow:
                await uow.ads.update(
                    ad_id,
                    status=AdStatus.COMPLETED,
                    sent_count=sent,
                    failed_count=max(0, total_users - sent),
                )
                await uow.commit()
        except Exception:
            pass
        return {
            "error": str(e),
            "ad_id": ad_id,
            "total": total_users,
            "sent": sent,
            "failed": failed,
            "blocked": blocked,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Delete ad messages task
# ──────────────────────────────────────────────────────────────────────────────

async def delete_ad_messages(ctx: dict, ad_id: int) -> dict[str, Any]:
    """Delete all sent ad messages from users' chats."""
    log.info("Deleting ad messages", ad_id=ad_id)

    deleted = 0
    failed = 0

    async with UnitOfWork() as uow:
        deliveries = await uow.ad_deliveries.filter(ad_id=ad_id, is_sent=True)

        deliveries_by_bot: dict[int, list] = {}
        for d in deliveries:
            deliveries_by_bot.setdefault(d.bot_id, []).append(d)

        for bot_id, bot_deliveries in deliveries_by_bot.items():
            bot = await bot_manager.get_bot_by_id(bot_id)
            if not bot:
                failed += len(bot_deliveries)
                continue

            for delivery in bot_deliveries:
                if not delivery.telegram_message_id:
                    continue
                try:
                    await bot.delete_message(
                        delivery.telegram_chat_id,
                        delivery.telegram_message_id,
                    )
                    deleted += 1
                except Exception as e:
                    log.debug("Delete failed", error=str(e))
                    failed += 1

                await asyncio.sleep(0.05)

        await uow.ads.delete(ad_id)
        await uow.commit()

    result = {"ad_id": ad_id, "deleted": deleted, "failed": failed}
    log.info("Ad messages deleted", **result)
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Cleanup Tasks
# ──────────────────────────────────────────────────────────────────────────────

async def cleanup_temp_files(ctx: dict) -> dict[str, Any]:
    """Remove temp download files older than 1 hour."""
    import shutil
    from pathlib import Path
    from app.config import settings

    log.info("Starting temp files cleanup")

    temp_dir = Path(settings.temp_download_path)
    if not temp_dir.exists():
        return {"cleaned": 0}

    now = datetime.now().timestamp()
    max_age = 3600
    cleaned = 0

    for item in temp_dir.iterdir():
        try:
            if now - item.stat().st_mtime > max_age:
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
                cleaned += 1
        except Exception as e:
            log.warning("Cleanup failed", path=str(item), error=str(e))

    log.info("Temp files cleaned", count=cleaned)
    return {"cleaned": cleaned}


async def cleanup_old_downloads(ctx: dict, days: int = 30) -> dict[str, Any]:
    """Delete download history records older than {days} days."""
    from sqlalchemy import delete
    from models import Download

    log.info("Cleaning old downloads", days=days)

    cutoff = datetime.now() - timedelta(days=days)

    async with UnitOfWork() as uow:
        count = await uow.session.execute(
            delete(Download).where(Download.created_at < cutoff)
        )
        await uow.commit()
        deleted = count.rowcount

    log.info("Old downloads cleaned", deleted=deleted)
    return {"deleted": deleted}


# ──────────────────────────────────────────────────────────────────────────────
# Stats Tasks
# ──────────────────────────────────────────────────────────────────────────────

async def update_bot_stats(ctx: dict) -> dict[str, Any]:
    """Refresh cached stats (total_users, active_users, total_downloads) for all bots."""
    from sqlalchemy import select, func
    from models import Bot, TelegramUser, Download

    log.info("Updating bot stats")
    updated = 0

    async with UnitOfWork() as uow:
        bots = await uow.bots.get_all()

        for bot in bots:
            user_count = await uow.users.count(bot_id=bot.id, is_banned=False)

            download_count = await uow.session.execute(
                select(func.count()).select_from(Download).where(Download.bot_id == bot.id)
            )
            downloads = download_count.scalar() or 0

            active_cutoff = datetime.now() - timedelta(days=30)
            active_count = await uow.session.execute(
                select(func.count()).select_from(TelegramUser).where(
                    TelegramUser.bot_id == bot.id,
                    TelegramUser.updated_at >= active_cutoff,
                    ~TelegramUser.is_banned,
                )
            )
            active = active_count.scalar() or 0

            await uow.bots.update(
                bot.id,
                total_users=user_count,
                active_users=active,
                total_downloads=downloads,
            )
            updated += 1

        await uow.commit()

    log.info("Bot stats updated", count=updated)
    return {"updated": updated}


async def aggregate_daily_stats(ctx: dict) -> dict[str, Any]:
    """Aggregate yesterday's download and user stats per bot and source."""
    from sqlalchemy import select, func
    from models import Download, TelegramUser, DailyStats, MediaSource

    log.info("Aggregating daily stats")

    today = datetime.now().date()
    yesterday = today - timedelta(days=1)

    async with UnitOfWork() as uow:
        bots = await uow.bots.get_all()

        for bot in bots:
            for source in MediaSource:
                new_users = await uow.session.execute(
                    select(func.count()).select_from(TelegramUser).where(
                        TelegramUser.bot_id == bot.id,
                        func.date(TelegramUser.created_at) == yesterday,
                    )
                )
                downloads = await uow.session.execute(
                    select(func.count()).select_from(Download).where(
                        Download.bot_id == bot.id,
                        Download.source == source,
                        func.date(Download.created_at) == yesterday,
                    )
                )
                uow.session.add(DailyStats(
                    date=yesterday,
                    bot_id=bot.id,
                    source=source,
                    new_users=new_users.scalar() or 0,
                    downloads=downloads.scalar() or 0,
                ))

        await uow.commit()

    log.info("Daily stats aggregated")
    return {"date": str(yesterday)}


# ──────────────────────────────────────────────────────────────────────────────
# Health / Maintenance Tasks
# ──────────────────────────────────────────────────────────────────────────────

async def expire_expired_ads(ctx: dict) -> dict[str, Any]:
    """Auto-disable post-download ads past their expires_at date."""
    from sqlalchemy import update
    from models import Ad, AdType

    log.info("Checking for expired ads")

    async with UnitOfWork() as uow:
        now = datetime.now()
        stmt = (
            update(Ad)
            .where(
                Ad.ad_type == AdType.POST_DOWNLOAD,
                Ad.is_active,
                Ad.expires_at.isnot(None),
                Ad.expires_at < now,
            )
            .values(is_active=False)
        )
        result = await uow.session.execute(stmt)
        expired_count = result.rowcount
        await uow.commit()

    if expired_count:
        log.info("Expired ads disabled", count=expired_count)
    return {"expired": expired_count}


async def health_check(ctx: dict) -> dict[str, Any]:
    """Quick health check: DB, Redis, active bots."""
    status = {"time": datetime.now().isoformat(), "database": False, "redis": False, "bots": 0}

    try:
        from sqlalchemy import text as sa_text
        async with db.session() as session:
            await session.execute(sa_text("SELECT 1"))
            status["database"] = True
    except Exception:
        pass

    try:
        await cache.redis.ping()
        status["redis"] = True
    except Exception:
        pass

    try:
        bots = await bot_manager.get_all_active_bots()
        status["bots"] = len(bots)
    except Exception:
        pass

    log.debug("Health check", **status)
    return status
