import asyncio
import time
import contextlib

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.types import FSInputFile

from app.config import settings
from app.logging import get_logger
from services.cache import cache

log = get_logger("service.downloader")


class MediaPlatform(StrEnum):
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"
    PINTEREST = "pinterest"
    VK = "vk"
    TWITTER = "twitter"
    SOUNDCLOUD = "soundcloud"
    REDDIT = "reddit"
    VIMEO = "vimeo"
    FACEBOOK = "facebook"
    TWITCH = "twitch"
    DAILYMOTION = "dailymotion"
    TUMBLR = "tumblr"
    THREADS = "threads"
    SNAPCHAT = "snapchat"
    LIKEE = "likee"
    OTHER = "other"
    UNKNOWN = "unknown"


@dataclass
class DownloadRequest:
    """Запрос на загрузку"""

    url: str
    platform: MediaPlatform
    user_id: int
    bot_id: int
    chat_id: int
    message_id: int  # Progress message ID
    quality: str | None = None
    format: str | None = None  # "audio", format_id, etc.


@dataclass
class DownloadResult:
    """Результат загрузки"""

    success: bool
    file_path: Path | None = None
    file_paths: list[Path] | None = None  # For multiple files (carousels, etc.)
    file_id: str | None = None
    file_ids: list[str] | None = None  # For multiple files
    title: str | None = None
    duration: int | None = None
    error: str | None = None
    from_cache: bool = False

    # Дополнительные данные для caption/metadata
    quality: str | None = None
    filesize_str: str | None = None
    file_count: int = 1
    media_info: dict[str, Any] | None = None  # {"photos": 2, "videos": 1}
    platform_icon: str | None = None  # "📸", "🎥", "🎵" etc.
    media_type: str | None = None  # "video", "photo", "audio", "album"
    original_url: str | None = None # For background caching
    platform: MediaPlatform | None = None # For cleanup and metadata


class BaseDownloader(ABC):
    """Базовый класс для загрузчиков"""

    platform: MediaPlatform

    def __init__(self):
        self.log = get_logger(f"downloader.{self.platform.value}")
        self.temp_dir = Path(settings.temp_download_path)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    @abstractmethod
    async def download(self, request: DownloadRequest) -> DownloadResult:
        """Загрузить медиа"""
        pass

    @abstractmethod
    def match_url(self, url: str) -> bool:
        """Проверить соответствие URL"""
        pass

    def extract_id(self, url: str) -> str | None:
        """Извлечь ID контента"""
        return None

    def get_cookies_path(self, bot_id: int | None = None) -> Path | None:
        """Получить путь к файлу cookies. bot_id из request имеет приоритет над self.bot_id."""
        from services.downloaders.cookie_manager import cookie_manager

        effective_bot_id = bot_id or getattr(self, 'bot_id', None)

        # 1. Bot-specific cookies
        if effective_bot_id:
            path = cookie_manager.get_cookie_file_path(self.platform.value, bot_id=effective_bot_id)
            if path.exists():
                return path

        # 2. Global platform cookies
        path = cookie_manager.get_cookie_file_path(self.platform.value)
        if path.exists():
            return path

        return None

    def get_cookies(self, bot_id: int | None = None) -> str | None:
        """Получить содержимое cookies."""
        path = self.get_cookies_path(bot_id=bot_id)
        if path and path.exists():
            return path.read_text()
        return None

    def get_headers(self) -> dict[str, str]:
        """Получить реалистичные заголовки для запросов"""
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        }

    async def cleanup(self, file_path: Path) -> None:
        """Удалить временный файл"""
        try:
            if file_path and file_path.exists():
                file_path.unlink()
                self.log.debug("Cleaned up", path=str(file_path))

                # Удаляем родительскую директорию если пустая
                parent = file_path.parent
                if parent != self.temp_dir and parent.exists():
                    with contextlib.suppress(OSError):
                        parent.rmdir()

        except Exception as e:
            self.log.error("Cleanup failed", error=str(e))

    def get_platform_icon(self) -> str:
        """Get default icon for platform"""
        icons = {
            MediaPlatform.INSTAGRAM: "📸",
            MediaPlatform.TIKTOK: "🎵",
            MediaPlatform.YOUTUBE: "📺",
            MediaPlatform.PINTEREST: "📌",
            MediaPlatform.VK: "💙",
            MediaPlatform.TWITTER: "🐦",
            MediaPlatform.SOUNDCLOUD: "🔊",
            MediaPlatform.REDDIT: "👽",
            MediaPlatform.VIMEO: "🎬",
            MediaPlatform.FACEBOOK: "📘",
            MediaPlatform.TWITCH: "🎮",
            MediaPlatform.DAILYMOTION: "📹",
            MediaPlatform.TUMBLR: "📝",
            MediaPlatform.THREADS: "🧵",
            MediaPlatform.SNAPCHAT: "👻",
            MediaPlatform.LIKEE: "❤️",
        }
        return icons.get(self.platform, "📁")


class DownloadService:
    """
    Главный сервис загрузки
    """

    def __init__(self):
        self.downloaders: list[BaseDownloader] = []
        # Глобальный ограничитель одновременных загрузок для всего сервера
        self.global_semaphore = asyncio.Semaphore(10)
        # Для больших YouTube-файлов send_document обычно быстрее send_video,
        # потому что Telegram не тратит время на видео-обработку перед отправкой.
        self.fast_youtube_document_threshold_bytes = 20 * 1024 * 1024
        self.telegram_upload_limit_bytes = max(1, settings.telegram_upload_limit_mb) * 1024 * 1024
        self._register_downloaders()

    def _mark_file_too_large_error(self, result: DownloadResult, path: Path) -> None:
        size_mb = path.stat().st_size / (1024 * 1024) if path.exists() else 0.0
        result.error = (
            f"FILE_TOO_LARGE:{path.name}:{size_mb:.1f}MB:"
            f"{settings.telegram_upload_limit_mb}MB"
        )

    def _register_downloaders(self) -> None:
        """Регистрация загрузчиков"""
        from services.media.youtube import YouTubeDownloader
        from services.media.instagram import InstagramDownloader
        from services.media.tiktok import TikTokDownloader
        from services.media.pinterest import PinterestDownloader
        from services.media.vk import VKDownloader
        from services.media.generic import (
            TwitterDownloader,
            SoundCloudDownloader,
            RedditDownloader,
            VimeoDownloader,
            FacebookDownloader,
            TwitchDownloader,
            DailymotionDownloader,
            TumblrDownloader,
            ThreadsDownloader,
            SnapchatDownloader,
            LikeeDownloader,
        )

        self.downloaders = [
            YouTubeDownloader(),
            InstagramDownloader(),
            TikTokDownloader(),
            PinterestDownloader(),
            VKDownloader(),
            # Generic platforms (yt-dlp out of the box).
            TwitterDownloader(),
            SoundCloudDownloader(),
            RedditDownloader(),
            VimeoDownloader(),
            FacebookDownloader(),
            TwitchDownloader(),
            DailymotionDownloader(),
            TumblrDownloader(),
            ThreadsDownloader(),
            SnapchatDownloader(),
            LikeeDownloader(),
        ]

    def detect_platform(self, url: str) -> MediaPlatform:
        """Определить платформу по URL"""
        for downloader in self.downloaders:
            if downloader.match_url(url):
                return downloader.platform
        return MediaPlatform.UNKNOWN

    def get_downloader(self, platform: MediaPlatform, bot_id: int = None) -> BaseDownloader | None:
        """Получить загрузчик. bot_id уже в DownloadRequest — не мутируем shared instance."""
        for downloader in self.downloaders:
            if downloader.platform == platform:
                return downloader
        return None

    async def check_cache(
        self,
        url: str,
        quality: str | None = None,
        format_type: str | None = None,
    ) -> DownloadResult | None:
        """Проверить кеш (Redis + DB)"""
        # 1. Проверяем Redis
        cached = await cache.get_cached_media(url, quality)

        if cached:
            log.debug("Cache HIT (Redis)", url=url[:50])
            m_type = cached.get("media_type")
            icon = cached.get("platform_icon") or ""
            platform = cached.get("platform")
            if not m_type:
                if icon == "📌" or platform == "pinterest": m_type = "photo"
                elif icon == "📸": m_type = "video"
                elif icon in ("🎵", "🎶"): m_type = "audio"
                else: m_type = "video"

            return DownloadResult(
                success=True,
                file_id=cached.get("file_id"),
                file_ids=cached.get("file_ids"),
                title=cached.get("title"),
                quality=cached.get("quality"),
                filesize_str=cached.get("filesize_str"),
                file_count=cached.get("file_count", 1),
                platform_icon=cached.get("platform_icon"),
                media_info=cached.get("media_info"),
                media_type=m_type,
                from_cache=True,
            )

        # 2. Проверяем БД
        from repositories.uow import UnitOfWork
        async with UnitOfWork() as uow:
            try:
                media = await uow.media.find_cached(url, quality)
                if media:
                    log.debug("Cache HIT (DB)", url=url[:50])
                    m_type = media.media_type.value if hasattr(media.media_type, 'value') else str(media.media_type)
                    res = DownloadResult(
                        success=True,
                        file_id=media.telegram_file_id,
                        file_ids=[media.telegram_file_id] if media.telegram_file_id else None,
                        title=media.title,
                        quality=media.quality,
                        filesize_str=f"{media.file_size / (1024 * 1024):.1f}MB" if media.file_size else None,
                        file_count=media.file_count,
                        platform_icon=media.platform_icon,
                        media_info=media.media_info,
                        media_type=m_type,
                        from_cache=True,
                    )
                    await cache.cache_media(
                        url=url, file_id=res.file_id, file_ids=res.file_ids,
                        message_id=media.telegram_message_id, chat_id=media.telegram_chat_id,
                        quality=quality, title=res.title, filesize_str=res.filesize_str,
                        platform=media.source.value, file_count=res.file_count,
                        platform_icon=res.platform_icon, media_info=res.media_info, media_type=m_type,
                    )
                    await uow.media.increment_downloads(media.id)
                    await uow.commit()
                    return res
            except Exception as e:
                log.warning("DB cache check failed", error=str(e))
        return None

    async def download(
            self,
            request: DownloadRequest,
            bot: Bot,
            progress_callback: Callable[[str], Any] | None = None,
            language: str = "en",
    ) -> DownloadResult:
        log.info("📥 DownloadService.download called", user_id=request.user_id, url=request.url[:80])

        from i18n.lang import MESSAGES
        def _t(key: str, **fmt) -> str:
            entry = MESSAGES.get(key, {})
            text = entry.get(language) or entry.get("en") or ""
            return text.format(**fmt) if fmt else text

        # Rate limiting
        allowed, remaining = await cache.get_user_rate_limit(request.user_id)
        if not allowed:
            return DownloadResult(success=False, error=f"Rate limit exceeded. Wait {remaining} seconds.")

        global_allowed, _ = await cache.get_global_rate_limit()
        if not global_allowed:
            return DownloadResult(success=False, error="Server busy. Try again later.")

        # Проверяем кеш
        if progress_callback:
            await progress_callback(_t("checking_cache"))
        cached = await self.check_cache(request.url, request.quality, request.format)
        if cached:
            asyncio.create_task(cache.track_download(from_cache=True))
            return cached

        asyncio.create_task(cache.track_download(from_cache=False))

        downloader = self.get_downloader(request.platform, request.bot_id)
        if not downloader:
            return DownloadResult(success=False, error=f"Unsupported platform: {request.platform}")

        if progress_callback:
            await progress_callback(_t("downloading_generic"))
        await cache.increment_active_downloads()

        try:
            result = await downloader.download(request)
            if not result.success:
                await cache.decrement_active_downloads()
                return result

            result.original_url = request.url
            result.platform = request.platform
            if not result.quality:
                result.quality = request.quality
            if not result.platform_icon:
                result.platform_icon = downloader.get_platform_icon()

            if not result.filesize_str and result.file_path and result.file_path.exists():
                size_bytes = result.file_path.stat().st_size
                result.filesize_str = (
                    f"{size_bytes / (1024 * 1024):.1f}MB"
                    if size_bytes > 1024 * 1024
                    else f"{size_bytes / 1024:.1f}KB"
                )

            # Файлы для загрузки
            files_to_upload: list[Path] = []
            if result.file_path:
                files_to_upload = [result.file_path]
            elif result.file_paths:
                files_to_upload = result.file_paths

            if files_to_upload and not (result.file_id or result.file_ids):
                if progress_callback:
                    await progress_callback(_t("uploading_files", count=len(files_to_upload)))

                from repositories.uow import UnitOfWork
                from services.cache_channel import CacheChannelService

                storage_chat_id = None
                async with UnitOfWork() as uow:
                    channel_service = CacheChannelService(uow.session)
                    try:
                        storage_channel = await channel_service.get_next_active_channel()
                        storage_chat_id = storage_channel.telegram_id
                        await uow.commit()
                    except Exception:
                        storage_chat_id = settings.storage_channel_id

                if not storage_chat_id:
                    # Нет канала — отдаём файлы напрямую (НЕ чистим!)
                    result.file_paths = files_to_upload
                    await cache.decrement_active_downloads()
                    return result

                # Загружаем в кеш-канал
                uploaded: list[tuple[str, str]] = []  # [(file_id, media_type), ...]
                last_message_id: int | None = None
                upload_semaphore = asyncio.Semaphore(3)

                async def upload_single(path: Path) -> tuple[str | None, int | None, str]:
                    async with upload_semaphore:
                        try:
                            return await self._upload_to_storage_with_retry(
                                bot, path, request, result.title, chat_id=storage_chat_id
                            )
                        except Exception as exc:
                            log.warning("Upload failed", path=path.name, error=str(exc))
                            return None, None, "document"

                upload_results = await asyncio.gather(
                    *[upload_single(p) for p in files_to_upload],
                    return_exceptions=True,
                )

                uploaded: list[tuple[str, str]] = []
                for upload_result in upload_results:
                    if isinstance(upload_result, Exception):
                        continue
                    if upload_result and len(upload_result) == 3:
                        file_id, message_id, media_type = upload_result
                        if file_id:
                            uploaded.append((file_id, media_type))
                            if message_id:
                                last_message_id = message_id

                uploaded_ids = [fid for fid, _ in uploaded]
                uploaded_types = [t for _, t in uploaded]

                log.info(
                    "Upload results",
                    total=len(files_to_upload),
                    uploaded=len(uploaded),
                    types=uploaded_types,  # ← теперь видим реальные типы
                )

                if not uploaded:
                    log.warning("Cache upload failed, will send files directly")
                    result.file_paths = files_to_upload
                    await cache.decrement_active_downloads()
                    return result

                # Определяем общий тип медиа
                uploaded_ids = [fid for fid, _ in uploaded]
                uploaded_types = [t for _, t in uploaded]

                if len(uploaded) == 1:
                    m_type = uploaded_types[0]
                elif all(t == "photo" for t in uploaded_types):
                    m_type = "album_photo"
                elif all(t == "video" for t in uploaded_types):
                    m_type = "album_video"
                else:
                    m_type = "album_mixed"  # фото + видео вместе

                result.media_type = m_type
                result.file_ids = uploaded_ids
                result.file_id = uploaded_ids[0] if len(uploaded_ids) == 1 else None
                result.file_count = len(uploaded_ids)

                # Сохраняем типы в media_info для send_to_user
                if not result.media_info:
                    result.media_info = {}
                result.media_info["file_types"] = uploaded_types

                log.info(
                    "file_ids saved",
                    count=len(uploaded_ids),
                    media_type=m_type,
                    types=uploaded_types,
                )

                # ── Кешируем в Redis и БД ─────────────────────────────────
                await cache.cache_media(
                    url=request.url,
                    file_id=uploaded_ids[0] if len(uploaded_ids) == 1 else None,
                    file_ids=uploaded_ids if len(uploaded_ids) > 1 else None,
                    message_id=last_message_id,
                    chat_id=storage_chat_id,
                    quality=request.quality,
                    title=result.title,
                    filesize_str=result.filesize_str,
                    platform=request.platform.value,
                    file_count=len(uploaded_ids),
                    platform_icon=result.platform_icon,
                    # ── ДОБАВЛЯЕМ file_types ──
                    media_info={
                        **(result.media_info or {}),
                        "file_types": uploaded_types,  # ["photo", "video", "photo"]
                    },
                    media_type=m_type,
                )

                async with UnitOfWork() as uow:
                    try:
                        await uow.media.create_or_update_cache(
                            original_url=request.url,
                            source=request.platform.value,
                            media_type=m_type,
                            quality=request.quality,
                            telegram_file_id=uploaded_ids[0] if len(uploaded_ids) == 1 else None,
                            telegram_chat_id=storage_chat_id,
                            telegram_message_id=last_message_id,
                            title=result.title,
                            file_count=len(uploaded_ids),
                            media_info=result.media_info,
                            platform_icon=result.platform_icon,
                            file_size=sum(
                                p.stat().st_size for p in files_to_upload if p.exists()
                            ),
                        )
                        await uow.commit()
                    except Exception as exc:
                        log.warning("DB cache save failed", error=str(exc))

                # ── Чистим временные файлы ПОСЛЕ кеширования ─────────────
                for path in files_to_upload:
                    await downloader.cleanup(path)

            await cache.decrement_active_downloads()
            return result

        except Exception as exc:
            log.exception("Download failed", error=str(exc))
            await cache.decrement_active_downloads()
            return DownloadResult(success=False, error=str(exc))

    async def _upload_to_storage(
            self,
            bot: Bot,
            file_path: Path,
            request: DownloadRequest,
            title: str | None = None,
            chat_id: int | None = None,
    ) -> tuple[str | None, int | None, str]:
        """
        Возвращает (file_id, message_id, media_type).
        media_type: "video" | "photo" | "audio" | "document"
        """
        target_chat_id = chat_id or settings.storage_channel_id
        if not target_chat_id:
            return None, None, "document"

        try:
            suffix = file_path.suffix.lower()
            caption = f"Cache: {request.url[:100]}"
            if title:
                caption = f"{title}\n{caption}"
            fs_file = FSInputFile(path=file_path, filename=file_path.name)

            if suffix in (".mp4", ".webm", ".mkv", ".mov"):
                msg = await bot.send_video(
                    target_chat_id, video=fs_file,
                    caption=caption[:1024], supports_streaming=True,
                )
                return msg.video.file_id, msg.message_id, "video"

            elif suffix in (".mp3", ".m4a", ".ogg", ".wav"):
                msg = await bot.send_audio(
                    target_chat_id, audio=fs_file, caption=caption[:1024]
                )
                return msg.audio.file_id, msg.message_id, "audio"

            elif suffix in (".jpg", ".jpeg", ".png", ".webp"):
                msg = await bot.send_photo(
                    target_chat_id, photo=fs_file, caption=caption[:1024]
                )
                return msg.photo[-1].file_id, msg.message_id, "photo"

            else:
                msg = await bot.send_document(
                    target_chat_id, document=fs_file, caption=caption[:1024]
                )
                return msg.document.file_id, msg.message_id, "document"

        except Exception as exc:
            log.warning("Upload to storage failed", path=file_path.name, error=str(exc))
            return None, None, "document"

    async def _upload_to_storage_with_retry(
            self,
            bot: Bot,
            file_path: Path,
            request: DownloadRequest,
            title: str | None = None,
            chat_id: int | None = None,
            max_retries: int = 1,
    ) -> tuple[str | None, int | None, str]:  # ← 3 значения!
        for attempt in range(max_retries + 1):
            res = await self._upload_to_storage(bot, file_path, request, title, chat_id)
            if res and res[0]:
                return res  # (file_id, message_id, media_type)
            if attempt < max_retries:
                await asyncio.sleep(2)
        return None, None, "document"

    async def send_to_user(
            self,
            bot: Bot,
            chat_id: int,
            result: DownloadResult,
            message_id: int | None = None,
            caption: str | None = None,
            reply_to: int | None = None,
            bot_username: str | None = None,
    ) -> bool:
        try:
            if not caption:
                parts = []
                if result.platform_icon: parts.append(result.platform_icon)
                if result.title: parts.append(result.title[:200])
                if result.quality: parts.append(f"📹 {result.quality}")
                if result.filesize_str: parts.append(f"💾 {result.filesize_str}")
                if bot_username: parts.append(f"📥 via @{bot_username}")
                caption = "\n".join(parts) if parts else None

            file_ids = result.file_ids or ([result.file_id] if result.file_id else [])

            if not file_ids:
                if result.file_paths:
                    return await self._send_files_direct(
                        bot, chat_id, result.file_paths, result,
                        message_id=message_id, caption=caption, reply_to=reply_to,
                    )
                log.error("send_to_user: no file_ids and no file_paths")
                return False

            # Типы файлов из media_info (сохранённые при загрузке)
            file_types: list[str] = list(
                (result.media_info or {}).get("file_types", [])
            )

            # Дополняем если типов не хватает
            while len(file_types) < len(file_ids):
                # Угадываем по media_type
                m = result.media_type or "video"
                if "photo" in m:
                    file_types.append("photo")
                elif "audio" in m:
                    file_types.append("audio")
                else:
                    file_types.append("video")

            log.info(
                "Sending to user",
                count=len(file_ids),
                types=file_types,
                media_type=result.media_type,
            )

            kwargs: dict = {
                "chat_id": chat_id,
                "caption": caption[:1024] if caption else None,
            }
            if reply_to:
                kwargs["reply_to_message_id"] = reply_to

            # ── Одиночный файл ────────────────────────────────────────
            if len(file_ids) == 1:
                await self._send_single_file_id(
                    bot, file_ids[0], file_types[0], kwargs
                )

            # ── Альбом ────────────────────────────────────────────────
            else:
                await self._send_album_file_ids(
                    bot, file_ids, file_types, caption, chat_id, reply_to
                )

            if message_id:
                with contextlib.suppress(Exception):
                    await bot.delete_message(chat_id, message_id)

            return True

        except Exception as exc:
            log.error("send_to_user failed", error=str(exc))
            if result.file_paths:
                return await self._send_files_direct(
                    bot, chat_id, result.file_paths, result,
                    message_id, caption, reply_to=reply_to,
                )
            return False

    async def _send_single_file_id(
            self,
            bot: Bot,
            file_id: str,
            file_type: str,
            kwargs: dict,
    ) -> None:
        """
        Отправляет одиночный file_id.
        Если тип неверный — перебирает все варианты.
        """
        send_order = {
            "video": [bot.send_video, bot.send_document, bot.send_photo],
            "photo": [bot.send_photo, bot.send_document],
            "audio": [bot.send_audio, bot.send_document],
            "document": [bot.send_document],
        }

        methods = send_order.get(file_type, [bot.send_video, bot.send_photo, bot.send_document])

        for i, method in enumerate(methods):
            try:
                method_kwargs = dict(kwargs)
                name = method.__name__

                if "send_video" in name:
                    await method(video=file_id, **method_kwargs)
                elif "send_photo" in name:
                    await method(photo=file_id, **method_kwargs)
                elif "send_audio" in name:
                    await method(audio=file_id, **method_kwargs)
                else:
                    await method(document=file_id, **method_kwargs)

                if i > 0:
                    log.info(f"Sent as {name} after fallback", original_type=file_type)
                return

            except Exception as exc:
                err = str(exc)
                if "Bad Request" in err and i < len(methods) - 1:
                    log.debug(
                        f"Send as {method.__name__} failed, trying next",
                        error=err[:80],
                    )
                    continue
                if i == len(methods) - 1:
                    raise

    async def _send_album_file_ids(
            self,
            bot: Bot,
            file_ids: list[str],
            file_types: list[str],
            caption: str | None,
            chat_id: int,
            reply_to: int | None,
    ) -> None:
        """
        Отправляет альбом. Если тип неверный — определяет автоматически.
        """
        from aiogram.utils.media_group import MediaGroupBuilder

        # Сначала пробуем определить реальный тип каждого file_id
        # через отправку в тестовом режиме (не нужно — просто пробуем)

        async def _try_send_group(
                fids: list[str], ftypes: list[str]
        ) -> bool:
            builder = MediaGroupBuilder()
            for i, (fid, ftype) in enumerate(zip(fids, ftypes)):
                item_caption = (caption[:1024] if caption else None) if i == 0 else None
                if ftype == "photo":
                    builder.add_photo(media=fid, caption=item_caption)
                elif ftype == "video":
                    builder.add_video(media=fid, caption=item_caption)
                elif ftype == "audio":
                    builder.add_audio(media=fid, caption=item_caption)
                else:
                    builder.add_document(media=fid, caption=item_caption)

            kwargs: dict = {"chat_id": chat_id, "media": builder.build()}
            if reply_to:
                kwargs["reply_to_message_id"] = reply_to
            await bot.send_media_group(**kwargs)
            return True

        # Стратегии отправки — от наиболее вероятной к fallback
        strategies = [
            file_types,  # Сохранённые типы
            ["photo"] * len(file_ids),  # Все как фото
            ["video"] * len(file_ids),  # Все как видео
            ["document"] * len(file_ids),  # Все как документы
        ]

        # Убираем дубликаты стратегий
        seen = []
        unique_strategies = []
        for s in strategies:
            key = tuple(s)
            if key not in seen:
                seen.append(key)
                unique_strategies.append(s)

        for strategy_idx, strategy in enumerate(unique_strategies):
            try:
                log.info(
                    "Trying album strategy",
                    strategy_idx=strategy_idx,
                    types=strategy,
                    count=len(file_ids),
                )
                await _try_send_group(file_ids, strategy)
                log.info("Album sent successfully", strategy=strategy)
                return
            except Exception as exc:
                err = str(exc)
                log.debug(
                    "Album strategy failed",
                    strategy_idx=strategy_idx,
                    types=strategy,
                    error=err[:100],
                )
                if strategy_idx == len(unique_strategies) - 1:
                    # Все стратегии провалились — отправляем по одному
                    log.warning("All album strategies failed, sending individually")
                    await self._send_files_individually(
                        bot, file_ids, file_types, caption, chat_id, reply_to
                    )
                    return

    async def _send_files_individually(
            self,
            bot: Bot,
            file_ids: list[str],
            file_types: list[str],
            caption: str | None,
            chat_id: int,
            reply_to: int | None,
    ) -> None:
        """Отправляет каждый файл отдельно когда альбом не работает."""
        for i, (fid, ftype) in enumerate(zip(file_ids, file_types)):
            kwargs: dict = {
                "chat_id": chat_id,
                "caption": (caption[:1024] if caption else None) if i == 0 else None,
            }
            if reply_to and i == 0:
                kwargs["reply_to_message_id"] = reply_to

            # Пробуем все типы
            for try_type in [ftype, "photo", "video", "document"]:
                try:
                    if try_type == "photo":
                        await bot.send_photo(photo=fid, **kwargs)
                    elif try_type == "video":
                        await bot.send_video(video=fid, **kwargs)
                    elif try_type == "audio":
                        await bot.send_audio(audio=fid, **kwargs)
                    else:
                        await bot.send_document(document=fid, **kwargs)
                    break
                except Exception as exc:
                    if try_type == "document":
                        log.error(
                            "Failed to send file individually",
                            fid=fid[:15],
                            error=str(exc)[:80],
                        )
                    continue

    async def _send_files_direct(self, bot: Bot, chat_id: int, file_paths: list[Path], result: DownloadResult, message_id: int | None = None, caption: str | None = None, reply_to: int | None = None) -> bool:
        """Прямая отправка файлов пользователю"""
        try:
            sent_message = None
            valid_paths = [p for p in file_paths if p.exists() and p.stat().st_size > 0]
            if not valid_paths: return False

            if len(valid_paths) == 1:
                path = valid_paths[0]
                if path.stat().st_size > self.telegram_upload_limit_bytes:
                    self._mark_file_too_large_error(result, path)
                    log.warning(
                        "Direct send blocked: file exceeds Telegram upload limit",
                        filename=path.name,
                        size_mb=round(path.stat().st_size / (1024 * 1024), 2),
                        limit_mb=settings.telegram_upload_limit_mb,
                    )
                    return False

                fs_file = FSInputFile(path=path, filename=path.name)
                suffix = path.suffix.lower()
                kwargs = {"chat_id": chat_id, "caption": caption[:1024] if caption else None}
                if reply_to:
                    kwargs["reply_to_message_id"] = reply_to
                file_size = path.stat().st_size
                start_ts = time.perf_counter()
                use_fast_document_mode = (
                    result.platform == MediaPlatform.YOUTUBE
                    and suffix in (".mp4", ".webm", ".mkv")
                    and file_size >= self.fast_youtube_document_threshold_bytes
                )

                log.info(
                    "Direct sending file started",
                    filename=path.name,
                    size_mb=round(file_size / (1024 * 1024), 2),
                    as_document=use_fast_document_mode,
                )
                if suffix in (".mp4", ".webm", ".mkv"):
                    if use_fast_document_mode:
                        sent_message = await bot.send_document(document=fs_file, **kwargs)
                    else:
                        sent_message = await bot.send_video(video=fs_file, supports_streaming=True, **kwargs)
                elif suffix in (".mp3", ".m4a", ".ogg", ".wav"):
                    sent_message = await bot.send_audio(audio=fs_file, **kwargs)
                elif suffix in (".jpg", ".jpeg", ".png", ".webp"):
                    sent_message = await bot.send_photo(photo=fs_file, **kwargs)
                else:
                    sent_message = await bot.send_document(document=fs_file, **kwargs)

                elapsed_ms = round((time.perf_counter() - start_ts) * 1000, 2)
                log.info(
                    "Direct sending file completed",
                    filename=path.name,
                    elapsed_ms=elapsed_ms,
                )
            else:
                from aiogram.utils.media_group import MediaGroupBuilder

                builder = MediaGroupBuilder()
                for i, path in enumerate(valid_paths):
                    item_caption = (caption[:1024] if caption else None) if i == 0 else None
                    suffix = path.suffix.lower()
                    fs_file = FSInputFile(path=path, filename=path.name)

                    if suffix in (".jpg", ".jpeg", ".png", ".webp"):
                        builder.add_photo(media=fs_file, caption=item_caption)
                    elif suffix in (".mp4", ".mov", ".webm"):
                        builder.add_video(media=fs_file, caption=item_caption)
                    elif suffix in (".mp3", ".m4a"):
                        builder.add_audio(media=fs_file, caption=item_caption)
                    else:
                        builder.add_document(media=fs_file, caption=item_caption)

                media_group_kwargs = {
                    "chat_id": chat_id,
                    "media": builder.build(),
                }
                if reply_to:
                    media_group_kwargs["reply_to_message_id"] = reply_to

                messages = await bot.send_media_group(**media_group_kwargs)
                if messages:
                    sent_message = messages[0]

            total_size_bytes = sum(path.stat().st_size for path in valid_paths if path.exists())

            # Фоновое кеширование
            if sent_message and result.original_url:
                asyncio.create_task(self._background_cache(sent_message, result, total_size_bytes))

            # Удаляем "Downloading..."
            if message_id:
                with contextlib.suppress(Exception): await bot.delete_message(chat_id, message_id)

            # Очистка
            for path in valid_paths:
                try: 
                    if path.exists(): path.unlink()
                except Exception: pass
            return True
        except Exception as e:
            if "Request Entity Too Large" in str(e):
                first_path = file_paths[0] if file_paths else None
                if first_path:
                    self._mark_file_too_large_error(result, first_path)
            log.error("Direct send failed", error=str(e))
            return False

    async def _background_cache(
        self,
        message: Any,
        result: DownloadResult,
        file_size_bytes: int | None = None,
    ):
        """Сохранение file_id в фоне"""
        try:
            file_id = None
            if hasattr(message, 'video') and message.video: file_id = message.video.file_id
            elif hasattr(message, 'audio') and message.audio: file_id = message.audio.file_id
            elif hasattr(message, 'photo') and message.photo: file_id = message.photo[-1].file_id
            elif hasattr(message, 'document') and message.document: file_id = message.document.file_id
            if not file_id: return

            m_type = result.media_type or "video"
            if hasattr(message, 'photo') and message.photo: m_type = "photo"
            elif hasattr(message, 'audio') and message.audio: m_type = "audio"

            await cache.cache_media(
                url=result.original_url, file_id=file_id, chat_id=message.chat.id, message_id=message.message_id,
                quality=result.quality, title=result.title, filesize_str=result.filesize_str,
                platform=result.platform.value if result.platform else "youtube", media_type=m_type,
            )

            from repositories.uow import UnitOfWork
            async with UnitOfWork() as uow:
                await uow.media.create_or_update_cache(
                    original_url=result.original_url, source=result.platform.value if result.platform else "youtube",
                    media_type=m_type, quality=result.quality, telegram_file_id=file_id,
                    telegram_chat_id=message.chat.id, telegram_message_id=message.message_id,
                    title=result.title, file_size=file_size_bytes or 0
                )
                await uow.commit()
            log.info("Background caching completed", file_id=file_id[:15])
        except Exception as e: log.warning("Background cache failed", error=str(e))


# === Singleton ===
download_service = DownloadService()
