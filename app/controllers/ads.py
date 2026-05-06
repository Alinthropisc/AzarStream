import re
from datetime import datetime, timedelta
from litestar import Controller, get, post
from litestar.response import Template, Redirect
from litestar.enums import RequestEncodingType
from litestar.params import Body
from aiogram.exceptions import TelegramBadRequest

from database.connection import db
from repositories import AdRepository, BotRepository
from models import AdStatus, AdMediaType, AdType
from services import bot_manager
from services.ad_formatting import prepare_telegram_html, prepare_telegram_compat_html, strip_telegram_markup
from app.logging import get_logger

log = get_logger("controller.ads")


def _clean_html(text: str) -> str:
    """Strip HTML tags from text."""
    if not text:
        return ""
    return re.sub(r"<[^>]*>", "", text)


async def _send_ad_direct(bot, chat_id: int, ad, keyboard):
    """Direct send ad message (fallback when forward is not possible)"""
    message = None
    formatted_content = prepare_telegram_html(ad.content)
    compat_content = prepare_telegram_compat_html(ad.content)
    plain_content = strip_telegram_markup(ad.content)
    if ad.media_file_id:
        try:
            if ad.media_type == AdMediaType.PHOTO:
                message = await bot.send_photo(chat_id, photo=ad.media_file_id, caption=formatted_content, reply_markup=keyboard, parse_mode="HTML")
            elif ad.media_type == AdMediaType.VIDEO:
                message = await bot.send_video(chat_id, video=ad.media_file_id, caption=formatted_content, reply_markup=keyboard, parse_mode="HTML")
            elif ad.media_type == AdMediaType.ANIMATION:
                message = await bot.send_animation(chat_id, animation=ad.media_file_id, caption=formatted_content, reply_markup=keyboard, parse_mode="HTML")
        except TelegramBadRequest as parse_err:
            if "can't parse entities" in str(parse_err):
                plain_caption = plain_content
                try:
                    if ad.media_type == AdMediaType.PHOTO:
                        message = await bot.send_photo(chat_id, photo=ad.media_file_id, caption=plain_caption, reply_markup=keyboard, parse_mode=None)
                    elif ad.media_type == AdMediaType.VIDEO:
                        message = await bot.send_video(chat_id, video=ad.media_file_id, caption=plain_caption, reply_markup=keyboard, parse_mode=None)
                    elif ad.media_type == AdMediaType.ANIMATION:
                        message = await bot.send_animation(chat_id, animation=ad.media_file_id, caption=plain_caption, reply_markup=keyboard, parse_mode=None)
                except Exception:
                    pass
        except Exception:
            pass

    if not message:
        try:
            message = await bot.send_message(chat_id, text=formatted_content, reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True)
        except TelegramBadRequest as parse_err:
            if "can't parse entities" in str(parse_err):
                try:
                    message = await bot.send_message(chat_id, text=compat_content, reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True)
                except Exception:
                    plain_text = plain_content
                    try:
                        message = await bot.send_message(chat_id, text=plain_text, reply_markup=keyboard, parse_mode=None, disable_web_page_preview=True)
                    except Exception:
                        pass
            else:
                pass
        except Exception:
            pass

    return message


class AdController(Controller):
    path = "/admin/ads"

    @get("/", name="ads:list")
    async def list_ads(self, page: int = 1, per_page: int = 10) -> Template:
        async with db.session() as session:
            repo = AdRepository(session)
            offset = (page - 1) * per_page
            ads = await repo.get_all(offset=offset, limit=per_page, order_by="created_at")
            total = await repo.count()

            return Template(
                template_name="admin/ads/list.html",
                context={"ads": ads, "page": page, "per_page": per_page, "total": total, "total_pages": (total + per_page - 1) // per_page},
            )

    @get("/create", name="ads:create_form")
    async def create_form(self) -> Template:
        async with db.session() as session:
            bot_repo = BotRepository(session)
            bots = await bot_repo.get_active_bots()
            log.info("Loaded bots for ad form", bot_count=len(bots))
            return Template(template_name="admin/ads/create.html", context={"bots": bots, "languages": ["ru", "en", "uz"]})

    @post("/create", name="ads:create")
    async def create_ad(
        self,
        data: dict = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        async with db.session() as session:
            ad_repo = AdRepository(session)

            name = data.get("name", "").strip()
            content = data.get("content", "").strip()
            ad_type = data.get("ad_type", "broadcast")
            media_type = data.get("media_type", "none")
            media_file_id_raw = data.get("media_file_id", "").strip()
            cache_channel_message_id_raw = data.get("cache_channel_message_id", "").strip()
            # Handle JavaScript "undefined" value
            media_file_id = media_file_id_raw if media_file_id_raw and media_file_id_raw.lower() != "undefined" else None
            cache_channel_message_id = int(cache_channel_message_id_raw) if cache_channel_message_id_raw and cache_channel_message_id_raw.isdigit() else None
            duration_days_raw = data.get("duration_days", "30").strip()
            duration_days = int(duration_days_raw) if duration_days_raw and duration_days_raw.isdigit() and int(duration_days_raw) > 0 else None
            expires_at = (datetime.now() + timedelta(days=duration_days)) if duration_days else None

            auto_delete_raw = data.get("auto_delete_seconds", "").strip()
            auto_delete_seconds: int | None = None
            if auto_delete_raw and auto_delete_raw.isdigit():
                _v = int(auto_delete_raw)
                # Telegram allows deleting bot messages within ~48h; cap accordingly.
                if 0 < _v <= 48 * 3600:
                    auto_delete_seconds = _v
            button_text = data.get("button_text", "").strip() or None
            button_url = data.get("button_url", "").strip() or None
            target_language = data.get("target_language") or None
            bot_ids_raw = data.get("bot_ids", [])

            # Multi-button JSON: [{text, url, row}, ...]
            buttons_raw = data.get("buttons_json", "").strip()
            buttons: list | None = None
            if buttons_raw:
                import json as _json
                try:
                    parsed = _json.loads(buttons_raw)
                    if isinstance(parsed, list):
                        _allowed_styles = {"danger", "success", "primary"}
                        buttons = [
                            {
                                "text": str(b.get("text", "")).strip()[:64],
                                "url": str(b.get("url", "")).strip()[:512],
                                "row": int(b.get("row", 0)),
                                "style": (str(b.get("style", "")).strip() or None) if str(b.get("style", "")).strip() in _allowed_styles else None,
                            }
                            for b in parsed
                            if isinstance(b, dict) and b.get("text") and b.get("url")
                        ] or None
                except Exception as e:
                    log.warning("buttons_json parse failed", error=str(e))

            # Subscription-gate campaign fields
            sub_channel_chat_id_raw = data.get("subscription_channel_chat_id", "").strip()
            sub_channel_chat_id = int(sub_channel_chat_id_raw) if sub_channel_chat_id_raw and sub_channel_chat_id_raw.lstrip("-").isdigit() else None
            sub_channel_username = data.get("subscription_channel_username", "").strip().lstrip("@") or None
            sub_channel_title = data.get("subscription_channel_title", "").strip() or None
            subscriber_goal_raw = data.get("subscriber_goal", "").strip()
            subscriber_goal = int(subscriber_goal_raw) if subscriber_goal_raw and subscriber_goal_raw.isdigit() and int(subscriber_goal_raw) > 0 else None

            log.info(
                "Ad create request received",
                name=name,
                ad_type=ad_type,
                media_type=media_type,
                media_file_id=media_file_id[:50] if media_file_id else None,
                media_file_id_length=len(media_file_id) if media_file_id else 0,
            )

            # Validate: if media type selected but no file_id, fallback to text
            if media_type != "none" and not media_file_id:
                log.warning(
                    "Media type selected but no file_id provided, falling back to text",
                    media_type=media_type,
                )
                media_type = "none"

            # Log file_id details for debugging
            if media_file_id:
                log.info(
                    "Media file_id received",
                    length=len(media_file_id),
                    value=media_file_id[:60] if len(media_file_id) > 60 else media_file_id,
                )

            # Handle both string and list formats
            if isinstance(bot_ids_raw, str):
                bot_ids = [int(bid) for bid in bot_ids_raw.split(",") if bid.strip()]
            elif isinstance(bot_ids_raw, list):
                bot_ids = [int(bid) for bid in bot_ids_raw if bid]
            else:
                bot_ids = []

            if not name or not content:
                return Redirect(path="/admin/ads/create?error=Missing required fields")

            # Post-download ads don't require bot_ids
            if ad_type == "broadcast" and not bot_ids:
                return Redirect(path="/admin/ads/create?error=Select at least one bot for broadcast")

            if ad_type == "subscription_gate":
                if not sub_channel_chat_id:
                    return Redirect(path="/admin/ads/create?error=Subscription gate requires channel chat_id")
                if not subscriber_goal:
                    return Redirect(path="/admin/ads/create?error=Subscription gate requires subscriber goal")
                if not bot_ids:
                    return Redirect(path="/admin/ads/create?error=Select at least one bot for subscription gate")

            log.info(
                "Creating ad",
                name=name,
                ad_type=ad_type,
                media_type=media_type,
                has_file_id=bool(media_file_id),
                bot_count=len(bot_ids),
            )

            # Create ad
            ad = await ad_repo.create(
                name=name,
                content=content,
                media_type=AdMediaType(media_type),
                media_file_id=media_file_id,
                cache_channel_message_id=cache_channel_message_id,
                ad_type=AdType(ad_type),
                duration_days=duration_days,
                expires_at=expires_at,
                button_text=button_text,
                button_url=button_url,
                buttons=buttons,
                target_language=target_language,
                status=AdStatus.DRAFT,
                is_active=True,
                subscription_channel_chat_id=sub_channel_chat_id,
                subscription_channel_username=sub_channel_username,
                subscription_channel_title=sub_channel_title,
                subscriber_goal=subscriber_goal,
                auto_delete_seconds=auto_delete_seconds,
            )

            # Add target bots (only for broadcast)
            if bot_ids:
                await ad_repo.add_target_bots(ad.id, bot_ids)
                log.info("Added target bots to ad", ad_id=ad.id, bot_count=len(bot_ids))

            # For subscription_gate ads — auto-create SubscriptionChannel rows
            # per target bot so the existing required-subscription gate kicks in.
            if ad_type == "subscription_gate" and sub_channel_chat_id and bot_ids:
                from models import Bot as BotModel
                from models.subscription import SubscriptionChannel
                from services.subscription import clear_channel_cache
                from sqlalchemy import select as _select

                bot_rows = (await session.execute(
                    _select(BotModel).where(BotModel.bot_id.in_(bot_ids))
                )).scalars().all()
                for b in bot_rows:
                    session.add(SubscriptionChannel(
                        bot_id=b.id,
                        channel_chat_id=sub_channel_chat_id,
                        channel_username=sub_channel_username,
                        channel_title=sub_channel_title,
                        is_active=True,
                    ))
                await session.commit()
                for b in bot_rows:
                    await clear_channel_cache(b.id)
                log.info(
                    "Subscription gate channels created",
                    ad_id=ad.id, bots=len(bot_rows), goal=subscriber_goal,
                )

            return Redirect(path=f"/admin/ads/{ad.id}")

    @get("/{ad_id:int}", name="ads:detail")
    async def ad_detail(self, ad_id: int) -> Template:
        async with db.session() as session:
            repo = AdRepository(session)
            ad = await repo.get_with_relations(ad_id)
            if not ad:
                return Redirect(path="/admin/ads")

            return Template(template_name="admin/ads/detail.html", context={"ad": ad})

    @post("/{ad_id:int}/send", name="ads:send")
    async def send_ad(self, ad_id: int) -> Redirect:
        """
        Enqueue broadcast to ARQ worker.

        The actual sending happens in the background (workers/tasks.py::broadcast_ad).
        The admin panel shows live progress via sent_count / failed_count fields
        that the worker updates every 100 users.
        """
        from workers.queue import queue_service

        async with db.session() as session:
            ad_repo = AdRepository(session)
            ad = await ad_repo.get_with_relations(ad_id)
            if not ad:
                log.error("Ad not found", ad_id=ad_id)
                return Redirect(path="/admin/ads")

            bot_ids = await ad_repo.get_target_bot_ids(ad_id)
            if not bot_ids:
                log.error("No target bots", ad_id=ad_id)
                return Redirect(path=f"/admin/ads/{ad_id}?error=no_target_bots")

            # Mark as SENDING so admin panel reflects state immediately
            await ad_repo.update(ad_id, status=AdStatus.SENDING, started_at=datetime.now())
            await session.commit()

        # Push to ARQ — worker sends sequentially with rate limiting
        job_id = await queue_service.enqueue_broadcast(ad_id)

        if job_id:
            log.info("Broadcast enqueued", ad_id=ad_id, job_id=job_id)
            return Redirect(path=f"/admin/ads/{ad_id}?status=queued")
        else:
            # ARQ unavailable — revert status
            log.error("ARQ unavailable — could not enqueue broadcast", ad_id=ad_id)
            async with db.session() as session:
                ad_repo = AdRepository(session)
                await ad_repo.update(ad_id, status=AdStatus.DRAFT)
                await session.commit()
            return Redirect(path=f"/admin/ads/{ad_id}?error=arq_unavailable")

    @post("/{ad_id:int}/delete-with-messages", name="ads:delete_with_messages")
    async def delete_with_messages(self, ad_id: int) -> Redirect:
        """Delete ad and all sent messages (CRUD delete operation)"""
        import asyncio
        from aiogram.exceptions import TelegramBadRequest
        from sqlalchemy import select
        from models import AdDelivery

        async with db.session() as session:
            ad_repo = AdRepository(session)
            ad = await ad_repo.get_with_relations(ad_id)
            if not ad:
                return Redirect(path="/admin/ads?error=not_found")

            result = await session.execute(
                select(AdDelivery).where(AdDelivery.ad_id == ad.id, AdDelivery.telegram_message_id.isnot(None))
            )
            deliveries = list(result.scalars().all())

            deleted_count = 0
            failed_count = 0

            deliveries_by_bot: dict[int, list] = {}
            for d in deliveries:
                deliveries_by_bot.setdefault(d.bot_id, []).append(d)

            for bot_id, bot_deliveries in deliveries_by_bot.items():
                bot = await bot_manager.get_bot_by_id(bot_id)
                if not bot:
                    failed_count += len(bot_deliveries)
                    continue

                for delivery in bot_deliveries:
                    try:
                        await bot.delete_message(delivery.telegram_chat_id, delivery.telegram_message_id)
                        deleted_count += 1
                        await asyncio.sleep(0.05)
                    except TelegramBadRequest as e:
                        if "message to delete not found" not in str(e).lower():
                            failed_count += 1
                    except Exception:
                        failed_count += 1

            await session.execute(AdDelivery.__table__.delete().where(AdDelivery.ad_id == ad.id))
            await ad_repo.delete(ad.id)
            await session.commit()

            log.info("Ad and messages deleted", ad_id=ad_id, deleted=deleted_count, failed=failed_count)
            return Redirect(path=f"/admin/ads?status=deleted&deleted={deleted_count}")

    @post("/{ad_id:int}/delete", name="ads:delete")
    async def delete_ad(self, ad_id: int) -> Redirect:
        """Delete ad and all sent messages from Telegram chats"""
        import asyncio
        from aiogram.exceptions import TelegramBadRequest
        from sqlalchemy import select
        from models import AdDelivery

        async with db.session() as session:
            ad_repo = AdRepository(session)
            ad = await ad_repo.get_with_relations(ad_id)
            if not ad:
                return Redirect(path="/admin/ads?error=not_found")

            result = await session.execute(
                select(AdDelivery).where(AdDelivery.ad_id == ad.id, AdDelivery.telegram_message_id.isnot(None))
            )
            deliveries = list(result.scalars().all())

            deleted_count = 0
            deliveries_by_bot: dict[int, list] = {}
            for d in deliveries:
                deliveries_by_bot.setdefault(d.bot_id, []).append(d)

            for bot_id, bot_deliveries in deliveries_by_bot.items():
                bot = await bot_manager.get_bot_by_id(bot_id)
                if not bot:
                    continue

                for delivery in bot_deliveries:
                    try:
                        await bot.delete_message(delivery.telegram_chat_id, delivery.telegram_message_id)
                        deleted_count += 1
                        await asyncio.sleep(0.05)
                    except TelegramBadRequest:
                        pass
                    except Exception:
                        pass

            await session.execute(AdDelivery.__table__.delete().where(AdDelivery.ad_id == ad.id))
            await ad_repo.delete(ad.id)
            await session.commit()

            log.info("Ad deleted", ad_id=ad_id, deleted_from_telegram=deleted_count)
            return Redirect(path=f"/admin/ads?status=deleted&deleted={deleted_count}")
