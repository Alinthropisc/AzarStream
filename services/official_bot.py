"""Official MediaFlow Bot Service — for ad media upload to cache channel only."""
from aiogram import Bot
from aiogram.types import FSInputFile
from aiogram.exceptions import TelegramBadRequest

from app.config import settings
from app.logging import get_logger

log = get_logger("service.official_bot")

_official_bot: Bot | None = None


def get_official_bot() -> Bot | None:
    """Get or create official bot singleton."""
    global _official_bot
    if _official_bot is None:
        if not settings.media_flow_bot_token:
            log.warning("MEDIA_FLOW_BOT_TOKEN not configured")
            return None
        _official_bot = Bot(token=settings.media_flow_bot_token)
    return _official_bot


def get_cache_channel_id() -> int | None:
    """Get cache channel ID from settings."""
    if not settings.media_flow_cache_channel_id:
        log.warning("MEDIA_FLOW_CACHE_CHANNEL_ID not configured")
    return settings.media_flow_cache_channel_id


class UploadResult:
    """Результат загрузки медиа в кэш-канал."""
    __slots__ = ("file_id", "message_id")

    def __init__(self, file_id: str, message_id: int):
        self.file_id = file_id
        self.message_id = message_id


class OfficialBotService:
    """
    Service for uploading media to the official cache channel.
    Used for ad campaigns — NOT for user downloads.
    """

    @staticmethod
    async def upload_photo(file_path: str) -> UploadResult | None:
        """Upload photo to cache channel, return file_id + message_id."""
        bot = get_official_bot()
        channel_id = get_cache_channel_id()

        if not bot:
            log.error("Official bot not initialized — check MEDIA_FLOW_BOT_TOKEN")
            return None
        if not channel_id:
            log.error("Cache channel not configured — check MEDIA_FLOW_CACHE_CHANNEL_ID")
            return None

        try:
            log.info("Uploading photo to cache channel", channel_id=channel_id, file_path=file_path)
            photo = FSInputFile(file_path)
            msg = await bot.send_photo(channel_id, photo)
            result = UploadResult(
                file_id=msg.photo[-1].file_id,
                message_id=msg.message_id,
            )
            log.info("Photo uploaded to cache channel", file_id=result.file_id[:30], message_id=result.message_id, channel_id=channel_id)
            return result
        except Exception as e:
            log.error("Failed to upload photo", error=str(e), error_type=type(e).__name__)
            return None

    @staticmethod
    async def upload_video(file_path: str) -> UploadResult | None:
        """Upload video to cache channel, return file_id + message_id."""
        bot = get_official_bot()
        channel_id = get_cache_channel_id()

        if not bot:
            log.error("Official bot not initialized — check MEDIA_FLOW_BOT_TOKEN")
            return None
        if not channel_id:
            log.error("Cache channel not configured — check MEDIA_FLOW_CACHE_CHANNEL_ID")
            return None

        try:
            log.info("Uploading video to cache channel", channel_id=channel_id, file_path=file_path)
            video = FSInputFile(file_path)
            msg = await bot.send_video(channel_id, video)
            result = UploadResult(
                file_id=msg.video.file_id,
                message_id=msg.message_id,
            )
            log.info("Video uploaded to cache channel", file_id=result.file_id[:30], message_id=result.message_id, channel_id=channel_id)
            return result
        except Exception as e:
            log.error("Failed to upload video", error=str(e), error_type=type(e).__name__)
            return None

    @staticmethod
    async def upload_animation(file_path: str) -> UploadResult | None:
        """Upload GIF/animation to cache channel, return file_id + message_id."""
        bot = get_official_bot()
        channel_id = get_cache_channel_id()

        if not bot:
            log.error("Official bot not initialized — check MEDIA_FLOW_BOT_TOKEN")
            return None
        if not channel_id:
            log.error("Cache channel not configured — check MEDIA_FLOW_CACHE_CHANNEL_ID")
            return None

        try:
            log.info("Uploading animation to cache channel", channel_id=channel_id, file_path=file_path)
            animation = FSInputFile(file_path)
            msg = await bot.send_animation(channel_id, animation)
            result = UploadResult(
                file_id=msg.animation.file_id,
                message_id=msg.message_id,
            )
            log.info("Animation uploaded to cache channel", file_id=result.file_id[:30], message_id=result.message_id, channel_id=channel_id)
            return result
        except Exception as e:
            log.error("Failed to upload animation", error=str(e), error_type=type(e).__name__)
            return None

    @staticmethod
    async def upload_media(file_path: str, media_type: str) -> UploadResult | None:
        """Upload media to cache channel by type."""
        if media_type == "photo":
            return await OfficialBotService.upload_photo(file_path)
        elif media_type == "video":
            return await OfficialBotService.upload_video(file_path)
        elif media_type == "animation":
            return await OfficialBotService.upload_animation(file_path)
        else:
            log.error("Unsupported media type", media_type=media_type)
            return None

    @staticmethod
    async def delete_message(message_id: int) -> bool:
        """Delete message from cache channel (cleanup)."""
        bot = get_official_bot()
        channel_id = get_cache_channel_id()
        if not bot or not channel_id:
            return False

        try:
            await bot.delete_message(channel_id, message_id)
            return True
        except TelegramBadRequest as e:
            log.warning("Failed to delete message", error=str(e))
            return False

    @staticmethod
    async def close() -> None:
        """Close official bot session."""
        global _official_bot
        if _official_bot:
            await _official_bot.session.close()
            _official_bot = None
