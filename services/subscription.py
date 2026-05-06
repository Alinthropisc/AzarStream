"""Service for managing required subscription channels."""

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError

from app.logging import get_logger
from repositories.subscription import SubscriptionChannelRepository
from repositories.uow import UnitOfWork
from services import cache

log = get_logger("service.subscription")


class SubscriptionCheckResult:
    """Result of subscription check."""
    is_subscribed: bool
    channels: list  # List of channels user hasn't subscribed to

    def __init__(self, is_subscribed: bool, channels: list | None = None):
        self.is_subscribed = is_subscribed
        self.channels = channels or []


class SubscriptionService:
    """
    Manages required subscription channels.

    - Check if user is subscribed to all required channels
    - Get subscription prompt message with buttons
    - Caches results to avoid slow Telegram API calls
    """

    # Кэш проверок подписки: 5 минут
    CACHE_TTL = 300

    async def get_required_channels(self, bot_id: int) -> list:
        """Get all active required channels for a bot."""
        from types import SimpleNamespace

        # Кэшируем каналы на 10 минут
        cache_key = f"channels:{bot_id}"
        cached = await cache.get(cache_key)
        if cached is not None:
            # Restore as objects with attribute access
            return [SimpleNamespace(**ch) for ch in cached]

        async with UnitOfWork() as uow:
            repo = SubscriptionChannelRepository(uow.session)
            channels = await repo.get_active_required(bot_id)
            await uow.commit()

        # Serialize ORM objects to plain dicts for Redis
        channels_data = [
            {
                "id": ch.id,
                "bot_id": ch.bot_id,
                "channel_chat_id": ch.channel_chat_id,
                "channel_username": ch.channel_username,
                "channel_title": ch.channel_title,
                "is_active": ch.is_active,
            }
            for ch in channels
        ]
        await cache.set(cache_key, channels_data, ttl=600)
        return channels

    async def check_user_subscription(
        self,
        user_id: int,
        bot: Bot,
        channels: list | None = None,
    ) -> SubscriptionCheckResult:
        """
        Быстрая проверка: если каналов нет — сразу True.
        Если каналы есть — проверяем с кэшированием.
        """
        if not channels:
            # Нет каналов — сразу пропускаем
            return SubscriptionCheckResult(is_subscribed=True)

        # Если каналы есть — проверяем подписку
        unsubscribed = []

        for channel in channels:
            try:
                if channel.channel_chat_id:
                    member = await bot.get_chat_member(
                        chat_id=channel.channel_chat_id,
                        user_id=user_id,
                    )
                    if member.status in ("left", "kicked"):
                        unsubscribed.append(channel)
                elif channel.channel_username:
                    member = await bot.get_chat_member(
                        chat_id=f"@{channel.channel_username.lstrip('@')}",
                        user_id=user_id,
                    )
                    if member.status in ("left", "kicked"):
                        unsubscribed.append(channel)
            except TelegramForbiddenError:
                # Bot not in channel — fail-open
                pass
            except Exception as e:
                log.warning(
                    "Failed to check channel membership",
                    channel_id=channel.channel_chat_id,
                    error=str(e),
                )
                unsubscribed.append(channel)

        return SubscriptionCheckResult(
            is_subscribed=len(unsubscribed) == 0,
            channels=unsubscribed,
        )

    async def get_linked_ad(self, channels: list):
        """
        Find the active SUBSCRIPTION_GATE Ad linked to these unsubscribed channels.
        Match by channel_chat_id. Returns the first match, or None.
        """
        if not channels:
            return None
        from sqlalchemy import select
        from models import Ad, AdType
        from repositories.uow import UnitOfWork

        chat_ids = [c.channel_chat_id for c in channels if c.channel_chat_id]
        if not chat_ids:
            return None

        async with UnitOfWork() as uow:
            row = (await uow.session.execute(
                select(Ad).where(
                    Ad.ad_type == AdType.SUBSCRIPTION_GATE,
                    Ad.is_active == True,  # noqa: E712
                    Ad.subscription_channel_chat_id.in_(chat_ids),
                ).limit(1)
            )).scalars().first()
            return row

    def build_subscribe_keyboard(self, channels: list, language: str = "en", ad=None) -> object:
        """
        Build inline keyboard for the subscription gate.
        If `ad` is given, use ad.buttons (multi-button JSON, with emoji-accent text)
        instead of generic per-channel subscribe links.
        Always appends the 'I've subscribed' check button at the bottom.
        """
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        from i18n.lang import MESSAGES

        rows: list[list[InlineKeyboardButton]] = []

        used_ad_buttons = False
        ad_buttons = getattr(ad, "buttons", None) if ad is not None else None
        if isinstance(ad_buttons, str):
            try:
                import json as _json
                ad_buttons = _json.loads(ad_buttons)
            except Exception:
                ad_buttons = None
        if isinstance(ad_buttons, list) and ad_buttons:
            by_row: dict[int, list[InlineKeyboardButton]] = {}
            for btn in ad_buttons:
                if not isinstance(btn, dict):
                    continue
                text = (btn.get("text") or "").strip()
                url = (btn.get("url") or "").strip()
                if not text or not url:
                    continue
                row_idx = int(btn.get("row", 0))
                by_row.setdefault(row_idx, []).append(
                    InlineKeyboardButton(text=text[:64], url=url)
                )
            for row_idx in sorted(by_row.keys()):
                rows.append(by_row[row_idx])
            used_ad_buttons = bool(rows)
            log.info("Gate keyboard: using ad.buttons", count=len(ad_buttons), rows=len(rows))
        elif ad is not None:
            log.info("Gate keyboard: ad found but no usable buttons", ad_id=getattr(ad, "id", None), buttons_type=type(getattr(ad, "buttons", None)).__name__)

        if not used_ad_buttons:
            for channel in channels:
                if channel.channel_username:
                    url = f"https://t.me/{channel.channel_username.lstrip('@')}"
                else:
                    url = f"https://t.me/c/{channel.channel_chat_id}" if channel.channel_chat_id else "#"
                label = channel.channel_title or f"Channel #{channel.id}"
                button_template = MESSAGES.get("subscribe_button", {}).get(language, MESSAGES["subscribe_button"]["en"])
                rows.append([InlineKeyboardButton(text=button_template.format(channel=label), url=url)])

        ive_text = MESSAGES.get("ive_subscribed_button", {}).get(language, MESSAGES["ive_subscribed_button"]["en"])
        rows.append([InlineKeyboardButton(text=ive_text, callback_data="check_subscription")])

        return InlineKeyboardMarkup(inline_keyboard=rows)

    def build_prompt_message(self, channels: list, language: str = "en", ad=None) -> str:
        """Build the subscription prompt message text — prefer ad.content if given."""
        if ad is not None and getattr(ad, "content", None):
            return ad.content

        if not channels:
            from i18n.lang import MESSAGES
            return MESSAGES.get("subscribed_success", {}).get(language, MESSAGES["subscribed_success"]["en"])

        from i18n.lang import MESSAGES
        return MESSAGES.get("subscription_prompt", {}).get(language, MESSAGES["subscription_prompt"]["en"])


# Singleton
subscription_service = SubscriptionService()


async def clear_channel_cache(bot_id: int) -> None:
    """Clear both Redis and in-memory cache for a bot's channels."""
    # Clear Redis cache
    await cache.delete(f"channels:{bot_id}")

    # Clear in-memory cache in UpdateProcessor
    from bot.processor import update_processor
    if bot_id in update_processor._channel_cache:
        del update_processor._channel_cache[bot_id]

    log.info("Cleared channel cache", bot_id=bot_id)
