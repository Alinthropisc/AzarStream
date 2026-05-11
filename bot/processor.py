import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Update, Message, CallbackQuery, User as TgUser

from app.logging import get_logger
from database.connection import db
from repositories.uow import UnitOfWork
from repositories.ad import AdRepository
from services import (
    bot_manager,
    cache,
    download_service,
    queue_service,
    MediaPlatform,
    DownloadRequest,
    DownloadResult,
    advanced_rate_limiter,
    user_download_queue,
)
from services.user import UserService
from services.subscription import subscription_service
from services.ad_formatting import prepare_telegram_html, prepare_telegram_compat_html, strip_telegram_markup
from services.content_filter import is_nsfw_url
from models import Bot as BotModel, BotType, DownloadStatus, MediaSource
from bot import search_handler

log = get_logger("bot.processor")


@dataclass
class ProcessingContext:
    """Контекст обработки update"""

    bot: Bot
    bot_model: BotModel
    update: Update
    user_id: int
    chat_id: int
    language: str = "en"
    is_new_user: bool = False  # True если пользователь впервые нажал /start
    needs_language_pick: bool = False  # True пока пользователь не выбрал язык явно


class UpdateProcessor:
    """
    Обработчик Telegram updates для multi-bot webhook

    Роутинг:
    - /start -> start_handler
    - Callback set_language:* -> language_handler
    - URL message -> download_handler
    - Callback yt_download:* -> youtube_handler
    """

    # URL паттерны для определения платформы
    URL_PATTERNS = {
        MediaPlatform.YOUTUBE: [
            r"(?:https?://)?(?:www\.)?youtube\.com",
            r"(?:https?://)?youtu\.be",
        ],
        MediaPlatform.INSTAGRAM: [
            r"(?:https?://)?(?:www\.)?instagram\.com",
        ],
        MediaPlatform.TIKTOK: [
            r"(?:https?://)?(?:www\.)?tiktok\.com",
            r"(?:https?://)?(?:vm|vt)\.tiktok\.com",
        ],
        MediaPlatform.PINTEREST: [
            r"(?:https?://)?(?:www\.)?pinterest\.",
            r"(?:https?://)?pin\.it",
        ],
    }

    def __init__(self):
        # Кеш каналов для проверки подписки (bot_id -> channels, time)
        self._channel_cache: dict[int, dict] = {}
        self._channel_cache_ttl = 300  # 5 минут

        # Хранилище отложенных загрузок для проверки подписки
        # Key: (user_id, bot_id) — изолировано по боту
        self._pending_downloads: dict[tuple[int, int], dict] = {}

    def _build_send_error_message(self, error: str | None, language: str, messages: dict) -> str:
        if not error:
            return messages["error_processing"].get(language, messages["error_processing"]["en"])

        if error.startswith("FILE_TOO_LARGE:"):
            # format: FILE_TOO_LARGE:<filename>:<size_mb>:<limit_mb>
            parts = error.split(":")
            filename = parts[1] if len(parts) > 1 else "file"
            size_mb = parts[2] if len(parts) > 2 else "?"
            limit_mb = parts[3] if len(parts) > 3 else "49MB"

            if language == "ru":
                return f"❌ Файл слишком большой для отправки в Telegram: {filename} ({size_mb}). Лимит: {limit_mb}. Выберите качество ниже."
            if language == "uz":
                return f"❌ Fayl Telegram orqali yuborish uchun juda katta: {filename} ({size_mb}). Limit: {limit_mb}. Pastroq sifatni tanlang."
            return f"❌ File is too large for Telegram upload: {filename} ({size_mb}). Limit: {limit_mb}. Please choose a lower quality."

        return messages["error_processing"].get(language, messages["error_processing"]["en"])

    async def process(self, bot_token: str, update_data: dict) -> bool:
        """
        Обработать incoming update

        Returns:
            True если обработано успешно
        """
        try:
            log.info(
                "📥 UpdateProcessor.process called",
                token=bot_token[:10],
                update_id=update_data.get("update_id"),
                has_message="message" in update_data,
                has_callback="callback_query" in update_data,
            )

            # Получаем бота
            instance = await bot_manager.get_bot_instance(bot_token)
            if not instance:
                log.warning("Bot not found", token=bot_token[:10])
                return False

            log.info(
                "✅ Bot instance acquired",
                username=instance.model.username,
            )

            bot = instance.bot
            bot_model = instance.model

            # Парсим update
            update = Update.model_validate(update_data)

            # chat_member updates — для подсчёта подписчиков на gate-кампаниях.
            # Идут отдельным путём (без rate limiting, без user creation в БД бота).
            if update.chat_member is not None:
                await self._handle_chat_member(bot_model.bot_id, update.chat_member)
                return True

            # Извлекаем данные
            user = self._get_user(update)
            chat_id = self._get_chat_id(update)

            if not user or not chat_id:
                return False

            # Advanced Rate Limiting — multi-layer check
            rate_result = await advanced_rate_limiter.check_multi(
                user_id=user.id,
                bot_id=bot_model.bot_id,
            )
            if not rate_result.allowed:
                log.warning(
                    "Rate limit exceeded — request blocked",
                    user_id=user.id,
                    bot_id=bot_model.bot_id,
                    penalty=rate_result.penalty.value,
                    retry_after=rate_result.retry_after,
                    message=rate_result.message,
                )
                if update.message:
                    msg = rate_result.message or f"⏳ Rate limit exceeded. Wait {rate_result.retry_after}s"
                    await bot.send_message(chat_id, msg, reply_to_message_id=update.message.message_id)
                return True  # Processed (blocked)

             # Получаем/создаём пользователя в БД (быстро, с commit для новых)
            async with UnitOfWork() as uow:
                user_service = UserService(uow)
                existing_user = await user_service.get_by_telegram_id(user.id, bot_model.bot_id)
                is_new_user = existing_user is None
                db_user = await user_service.get_or_create_fast(user, bot_model.bot_id)
                language = db_user.language
                needs_language_pick = not db_user.language_selected

            # Создаём контекст
            ctx = ProcessingContext(
                bot=bot,
                bot_model=bot_model,
                update=update,
                user_id=user.id,
                chat_id=chat_id,
                language=language,
                is_new_user=is_new_user,
                needs_language_pick=needs_language_pick,
            )

            # Роутинг
            try:
                if update.message:
                    return await self._handle_message(ctx, update.message)
                elif update.callback_query:
                    return await self._handle_callback(ctx, update.callback_query)
            except Exception as e:
                log.exception("Route handler failed", user_id=user.id, error=str(e))
                # ГАРАНТИРУЕМ продвижение очереди
                await self._process_next_in_queue(ctx)
                return False

            return True

        except Exception as e:
            log.exception("Update processing failed", error=str(e))
            return False

    def _get_user(self, update: Update) -> TgUser | None:
        """Получить User из update"""
        if update.message:
            return update.message.from_user
        if update.callback_query:
            return update.callback_query.from_user
        return None

    def _get_chat_id(self, update: Update) -> int | None:
        """Получить chat_id из update"""
        if update.message:
            return update.message.chat.id
        if update.callback_query and update.callback_query.message:
            return update.callback_query.message.chat.id
        return None

    async def _handle_message(self, ctx: ProcessingContext, message: Message) -> bool:
        """Обработка сообщений"""
        text = message.text or ""

        log.info(
            "💬 Handling message",
            user_id=ctx.user_id,
            chat_id=ctx.chat_id,
            text=text[:100] if text else "(empty)",
        )

        # Media Search бот — другая ветка
        if getattr(ctx.bot_model, "bot_type", None) == BotType.MEDIA_SEARCH:
            if text == "/start":
                # Показываем выбор языка, пока он не выбран ЯВНО (новые + старые,
                # которые ещё ни разу не нажимали кнопку языка).
                if ctx.needs_language_pick:
                    from i18n.lang import MESSAGES as _M
                    from bot.keyboards import get_language_keyboard
                    welcome = _M["search_welcome"].get(ctx.language, _M["search_welcome"]["en"])
                    lang_prompt = _M["lang_prompt"].get(ctx.language, _M["lang_prompt"]["en"])
                    await ctx.bot.send_message(
                        ctx.chat_id,
                        f"{welcome}\n\n{lang_prompt}",
                        reply_markup=get_language_keyboard(),
                        reply_to_message_id=message.message_id,
                        parse_mode="HTML",
                    )
                else:
                    await search_handler.handle_welcome(
                        ctx.bot, ctx.chat_id, language=ctx.language,
                        reply_to=message.message_id,
                    )
                return True
            if text == "/lang":
                return await self._handle_lang_command(ctx, message)
            if not text.strip():
                return True
            await search_handler.handle_query(
                ctx.bot,
                chat_id=ctx.chat_id,
                bot_id=ctx.bot_model.bot_id,
                user_id=ctx.user_id,
                query=text,
                language=ctx.language,
            )
            return True

        # Команда /start
        if text == "/start":
            log.info("🚀 Handling /start command")
            return await self._handle_start(ctx, message)

        # Команда /lang
        if text == "/lang":
            return await self._handle_lang_command(ctx, message)

        # Проверяем URL (теперь ищем в любом месте текста)
        if re.search(r"https?://\S+", text):
            # Извлекаем первый найденный URL
            url_match = re.search(r"https?://\S+", text)
            url = url_match.group(0)
            return await self._handle_url(ctx, message, url)

        # Остальной текст — просим прислать ссылку
        return await self._handle_unknown(ctx, message)

    async def _handle_callback(self, ctx: ProcessingContext, callback: CallbackQuery) -> bool:
        """Обработка callback query"""
        data = callback.data or ""

        # Media Search callbacks
        if data == "noop":
            await ctx.bot.answer_callback_query(callback.id)
            return True
        if data.startswith("sq:"):
            try:
                _, sq_id_str, page_str = data.split(":", 2)
                await search_handler.handle_pagination(
                    ctx.bot, callback, int(sq_id_str), int(page_str), language=ctx.language,
                )
            except Exception:
                await ctx.bot.answer_callback_query(callback.id)
            return True
        if data.startswith("t:"):
            try:
                _, tid_str = data.split(":", 1)
                delivered = await search_handler.handle_play(
                    ctx.bot,
                    callback,
                    int(tid_str),
                    language=ctx.language,
                    bot_username=ctx.bot_model.username,
                )
                if delivered:
                    await self._bump_bot_hits(ctx)
                    await self._send_post_download_ad(ctx)
            except Exception:
                await ctx.bot.answer_callback_query(callback.id)
            return True
        if data.startswith("tv:"):
            try:
                _, tid_str, val_str = data.split(":", 2)
                await search_handler.handle_vote(
                    ctx.bot,
                    callback,
                    int(tid_str),
                    int(val_str),
                    language=ctx.language,
                )
            except Exception:
                await ctx.bot.answer_callback_query(callback.id)
            return True

        # Выбор языка
        if data.startswith("set_language:"):
            return await self._handle_language_callback(ctx, callback)

        # Format selection
        if data.startswith("yt_fmt:") or data.startswith("downloader_format:"):
            return await self._handle_format_callback(ctx, callback)

        # YouTube audio download
        if data.startswith("yt_audio:"):
            return await self._handle_youtube_audio_callback(ctx, callback)

        # Subscription check retry
        if data == "check_subscription":
            return await self._handle_subscription_retry(ctx, callback)

        await ctx.bot.answer_callback_query(callback.id)
        return True

    # === Handlers ===

    async def _handle_start(self, ctx: ProcessingContext, message: Message) -> bool:
        """Обработка /start — показываем выбор языка только НОВЫМ пользователям"""
        from i18n.lang import MESSAGES
        from bot.keyboards import get_language_keyboard

        log.info(
            "📤 Handling /start command",
            user_id=ctx.user_id,
            language=ctx.language,
            is_new_user=ctx.is_new_user,
        )

        if ctx.needs_language_pick:
            # Язык не выбран явно — показываем picker
            text = MESSAGES["start"].get(ctx.language, MESSAGES["start"]["en"])
            lang_prompt = MESSAGES["lang_prompt"].get(ctx.language, MESSAGES["lang_prompt"]["en"])
            full_text = f"{text}\n\n{lang_prompt}"

            kb = get_language_keyboard()
            await ctx.bot.send_message(ctx.chat_id, full_text, reply_markup=kb, reply_to_message_id=message.message_id)
            log.info("✅ New user - showing language selection", user_id=ctx.user_id)
        else:
            # Существующий пользователь — показываем сообщение о ссылке
            text = MESSAGES["send_link"].get(ctx.language, MESSAGES["send_link"]["en"])
            await ctx.bot.send_message(ctx.chat_id, text, reply_to_message_id=message.message_id)
            log.info("✅ Existing user - showing send link message", user_id=ctx.user_id, language=ctx.language)

        return True

    async def _handle_lang_command(self, ctx: ProcessingContext, message: Message) -> bool:
        """Обработка /lang — показать выбор языка"""
        from bot.keyboards import get_language_keyboard
        from i18n.lang import MESSAGES

        text = MESSAGES["lang_prompt"].get(ctx.language, MESSAGES["lang_prompt"]["en"])
        kb = get_language_keyboard()
        await ctx.bot.send_message(ctx.chat_id, text, reply_markup=kb, reply_to_message_id=message.message_id)
        return True

    async def _handle_language_callback(self, ctx: ProcessingContext, callback: CallbackQuery) -> bool:
        """Обработка выбора языка"""
        from i18n.lang import MESSAGES

        language = callback.data.split(":")[1]

        # Обновляем контекст
        ctx.language = language

        async with UnitOfWork() as uow:
            user_service = UserService(uow)
            db_user = await user_service.get_by_telegram_id(ctx.user_id, ctx.bot_model.bot_id)
            if db_user:
                await user_service.update_language(
                    user_id=db_user.id,
                    language=language,
                )
            await uow.commit()

        # Отвечаем
        answer_text = MESSAGES["lang_changed"].get(language, MESSAGES["lang_changed"]["en"])
        await ctx.bot.answer_callback_query(callback.id, answer_text)

        # Для Media Search показываем search-welcome, иначе обычный start
        if getattr(ctx.bot_model, "bot_type", None) == BotType.MEDIA_SEARCH:
            text = MESSAGES["search_welcome"].get(language, MESSAGES["search_welcome"]["en"])
        else:
            text = MESSAGES["start"].get(language, MESSAGES["start"]["en"])
        await ctx.bot.edit_message_text(
            text, chat_id=callback.message.chat.id,
            message_id=callback.message.message_id, parse_mode="HTML",
        )

        return True

    async def _handle_url(self, ctx: ProcessingContext, message: Message, url: str) -> bool:
        """Обработка URL — с очередью для пользователя"""
        from i18n.lang import MESSAGES

        if is_nsfw_url(url):
            log.warning(
                "🚫 NSFW link blocked",
                user_id=ctx.user_id,
                url=url[:120],
            )
            text = MESSAGES["nsfw_blocked"].get(ctx.language, MESSAGES["nsfw_blocked"]["en"])
            await ctx.bot.send_message(
                ctx.chat_id,
                text,
                reply_to_message_id=message.message_id,
                parse_mode="HTML",
            )
            return True

        # Определяем платформу заранее для передачи в проверку подписки
        platform = download_service.detect_platform(url)

        if platform == MediaPlatform.UNKNOWN:
            text = MESSAGES["unsupported_link"].get(ctx.language, MESSAGES["unsupported_link"]["en"])
            await ctx.bot.send_message(ctx.chat_id, text, reply_to_message_id=message.message_id)
            return True

        # Быстрая проверка подписки — передаем URL и платформу для сохранения контекста
        sub_check = await self._check_subscription_fast(ctx, message, url=url, platform=platform)
        if not sub_check:
            return True

        # Add to user's download queue
        success, position, queue_msg = await user_download_queue.add(
            user_id=ctx.user_id,
            bot_id=ctx.bot_model.bot_id,
            chat_id=ctx.chat_id,
            message_id=message.message_id,
            url=url,
        )

        if not success:
            await ctx.bot.send_message(ctx.chat_id, queue_msg, reply_to_message_id=message.message_id)
            return True

        # Send queue status message
        status_msg = await ctx.bot.send_message(
            ctx.chat_id,
            queue_msg,
            reply_to_message_id=message.message_id,
        )

        # Store progress message ID for queued items
        if position > 0:
            await user_download_queue.set_progress_message(
                user_id=ctx.user_id,
                bot_id=ctx.bot_model.bot_id,
                url=url,
                progress_message_id=status_msg.message_id,
            )

        if position == 0:
            # Start downloading immediately
            if platform == MediaPlatform.YOUTUBE:
                if "/shorts/" in url.lower():
                    return await self._handle_direct_download(ctx, message, url, platform, status_msg)
                return await self._handle_youtube_url(ctx, message, url, status_msg)
            else:
                return await self._handle_direct_download(ctx, message, url, platform, status_msg)
        else:
            # In queue — just inform user, _process_next_in_queue will handle it
            log.info(
                "📋 Download queued",
                user_id=ctx.user_id,
                position=position,
                url=url[:80],
            )
            return True

    async def _handle_youtube_url(self, ctx: ProcessingContext, message: Message, url: str, status_msg: Message | None = None) -> bool:
        """Обработка YouTube URL - показываем превью + информацию + форматы"""
        from bot.keyboards import get_youtube_formats_keyboard_v2
        from i18n.lang import MESSAGES
        from services.media.youtube import YouTubeDownloader

        # Edit existing status message to avoid duplicates, or send new if not available
        loading_text = MESSAGES["loading_youtube_info"].get(ctx.language, MESSAGES["loading_youtube_info"]["en"])

        if status_msg:
            progress_msg = status_msg
            try:
                await ctx.bot.edit_message_text(
                    text=loading_text,
                    chat_id=ctx.chat_id,
                    message_id=status_msg.message_id,
                )
            except Exception:
                pass
        else:
            progress_msg = await ctx.bot.send_message(
                ctx.chat_id,
                loading_text,
                reply_to_message_id=message.message_id,
            )

        try:
            downloader = download_service.get_downloader(MediaPlatform.YOUTUBE)
            if not downloader:
                raise Exception("YouTube downloader not available")
            
            info = await downloader.get_video_info(url)

            if not info:
                await ctx.bot.delete_message(ctx.chat_id, progress_msg.message_id)
                await ctx.bot.send_message(
                    ctx.chat_id,
                    MESSAGES["error_processing"].get(ctx.language, MESSAGES["error_processing"].get("ru")),
                    reply_to_message_id=message.message_id,
                )
                await self._process_next_in_queue(ctx)
                return False

            # Проверка на плейлист
            if isinstance(info, dict) and info.get("error") == "playlist":
                await ctx.bot.delete_message(ctx.chat_id, progress_msg.message_id)
                text = MESSAGES["playlists_not_supported"].get(ctx.language, MESSAGES["playlists_not_supported"]["en"])
                await ctx.bot.send_message(
                    ctx.chat_id,
                    text,
                    reply_to_message_id=message.message_id,
                )
                await self._process_next_in_queue(ctx)
                return True

            video_id = info.get("id", "")
            title = info.get("title", "")
            thumbnail = info.get("thumbnail", "")
            uploader = info.get("uploader", "")
            views_str = info.get("views_str", "")
            likes_str = info.get("likes_str", "")
            date_str = info.get("date_str", "")
            duration_str = info.get("duration_str", "")
            formats = info.get("formats", [])

            if not formats:
                await ctx.bot.delete_message(ctx.chat_id, progress_msg.message_id)
                await ctx.bot.send_message(
                    ctx.chat_id,
                    MESSAGES["no_formats_found"].get(ctx.language, MESSAGES["no_formats_found"].get("ru")),
                    reply_to_message_id=message.message_id,
                )
                await self._process_next_in_queue(ctx)
                return False

            # Формируем текст с информацией о видео
            caption = f"🎬 <b>{title}</b>\n\n"
            if views_str or likes_str:
                stats = []
                if views_str:
                    stats.append(views_str)
                if likes_str:
                    stats.append(likes_str.replace("👍", "👍"))
                caption += " | ".join(stats) + "\n"
            if date_str:
                caption += f"📅 {date_str}\n"
            if uploader:
                caption += f"👤 {uploader}\n"
            if duration_str:
                caption += f"⏱ {duration_str}"

            # Формируем каноничный URL как в DownloadRequest
            canonical_url = f"https://www.youtube.com/watch?v={video_id}"

            # Проверяем кеш для каждого качества (оптимизировано)
            cache_status = {}
            
            async def check_fmt_cache(q):
                res = await cache.get_cached_media(canonical_url, q)
                if res:
                    return q, {"cached": True, "size_str": res.get("filesize_str")}
                return q, None

            qualities_to_check = [f.get("quality") for f in formats if f.get("quality")]
            if qualities_to_check:
                cache_results = await asyncio.gather(*[check_fmt_cache(q) for q in qualities_to_check])
                for q, res in cache_results:
                    if res:
                        cache_status[q] = res
            # Проверяем кеш для аудио
            audio_cached = await cache.get_cached_media(canonical_url, "audio")
            if audio_cached:
                cache_status["audio"] = {
                    "cached": True,
                    "size_str": audio_cached.get("filesize_str"),
                }

            # Создаём клавиатуру с форматами
            kb = get_youtube_formats_keyboard_v2(formats, video_id, cache_status)

            # Delete progress message safely (ignore errors to not break download flow)
            try:
                await ctx.bot.delete_message(ctx.chat_id, progress_msg.message_id)
            except Exception:
                pass

            # Отправляем превью + информация + кнопки
            if thumbnail:
                await ctx.bot.send_photo(
                    ctx.chat_id,
                    photo=thumbnail,
                    caption=caption,
                    reply_markup=kb,
                    reply_to_message_id=message.message_id,
                )
            else:
                await ctx.bot.send_message(
                    ctx.chat_id,
                    caption,
                    reply_markup=kb,
                    reply_to_message_id=message.message_id,
                )

            return True

        except Exception as e:
            log.exception("Failed to get YouTube info", error=str(e))
            try:
                await ctx.bot.delete_message(ctx.chat_id, progress_msg.message_id)
            except Exception:
                pass
            await ctx.bot.send_message(
                ctx.chat_id,
                MESSAGES["error_processing"].get(ctx.language, MESSAGES["error_processing"].get("ru")),
                reply_to_message_id=message.message_id,
            )
            await self._process_next_in_queue(ctx)
            return False

    async def _handle_youtube_url_resumed(
        self,
        ctx: ProcessingContext,
        *,
        chat_id: int,
        url: str,
        progress_message_id: int,
        pending: dict,
    ) -> bool:
        """
        Обработка YouTube URL после подписки.
        Отличается от _handle_youtube_url тем, что:
        - НЕ пытается удалить progress_message (сообщение подписки)
        - НЕ использует reply_to_message_id (так как оригинальное сообщение уже есть)
        - Редактирует progress_message в формат-кнопки или отправляет новое
        """
        from bot.keyboards import get_youtube_formats_keyboard_v2
        from i18n.lang import MESSAGES
        from services.media.youtube import YouTubeDownloader

        try:
            downloader = download_service.get_downloader(MediaPlatform.YOUTUBE)
            if not downloader:
                raise Exception("YouTube downloader not available")

            info = await downloader.get_video_info(url)

            if not info:
                # Ошибка - редактируем progress_message
                error_text = MESSAGES["error_processing"].get(ctx.language, MESSAGES["error_processing"]["en"])
                try:
                    await ctx.bot.edit_message_text(
                        text=error_text,
                        chat_id=chat_id,
                        message_id=progress_message_id,
                    )
                except Exception:
                    pass
                await self._process_next_in_queue(ctx)
                return False

            video_id = info.get("id", "")
            title = info.get("title", "")
            thumbnail = info.get("thumbnail", "")
            uploader = info.get("uploader", "")
            views_str = info.get("views_str", "")
            likes_str = info.get("likes_str", "")
            date_str = info.get("date_str", "")
            duration_str = info.get("duration_str", "")
            formats = info.get("formats", [])

            if not formats:
                error_text = MESSAGES["no_formats_found"].get(ctx.language, MESSAGES["no_formats_found"]["en"])
                try:
                    await ctx.bot.edit_message_text(
                        text=error_text,
                        chat_id=chat_id,
                        message_id=progress_message_id,
                    )
                except Exception:
                    pass
                await self._process_next_in_queue(ctx)
                return False

            # Формируем текст с информацией о видео
            caption = f"🎬 <b>{title}</b>\n\n"
            if views_str or likes_str:
                stats = []
                if views_str:
                    stats.append(views_str)
                if likes_str:
                    stats.append(likes_str.replace("👍", "👍"))
                caption += " | ".join(stats) + "\n"
            if date_str:
                caption += f"📅 {date_str}\n"
            if uploader:
                caption += f"👤 {uploader}\n"
            if duration_str:
                caption += f"⏱ {duration_str}"

            # Формируем каноничный URL
            canonical_url = f"https://www.youtube.com/watch?v={video_id}"

            # Проверяем кеш
            cache_status = {}
            for fmt in formats:
                quality = fmt.get("quality", "")
                if quality:
                    cached = await cache.get_cached_media(canonical_url, quality)
                    if cached:
                        cache_status[quality] = {
                            "cached": True,
                            "size_str": cached.get("filesize_str"),
                        }
            audio_cached = await cache.get_cached_media(canonical_url, "audio")
            if audio_cached:
                cache_status["audio"] = {
                    "cached": True,
                    "size_str": audio_cached.get("filesize_str"),
                }

            # Создаём клавиатуру
            from bot.keyboards import get_youtube_formats_keyboard_v2

            kb = get_youtube_formats_keyboard_v2(formats, video_id, cache_status)

            # Удаляем сообщение подписки и отправляем новое с фото + текст + кнопки
            # Так нельзя превратить текст в фото через edit_message_text
            try:
                await ctx.bot.delete_message(
                    chat_id=chat_id,
                    message_id=progress_message_id,
                )
            except Exception:
                pass  # Игнорируем ошибки удаления

            # Отправляем превью + информация + кнопки
            # Используем original_message_id из pending для reply_to
            original_message_id = pending.get("message_id")

            if thumbnail:
                await ctx.bot.send_photo(
                    chat_id=chat_id,
                    photo=thumbnail,
                    caption=caption,
                    reply_markup=kb,
                    reply_to_message_id=original_message_id,
                )
            else:
                await ctx.bot.send_message(
                    chat_id=chat_id,
                    text=caption,
                    reply_markup=kb,
                    reply_to_message_id=original_message_id,
                )

            return True

        except Exception as e:
            log.exception("Failed to get YouTube info (resumed)", error=str(e))
            # Не пытаемся удалить сообщение - оно может уже быть другим
            await self._process_next_in_queue(ctx)
            return False

    async def _handle_format_callback(self, ctx: ProcessingContext, callback: CallbackQuery) -> bool:
        """Обработка выбора формата"""
        from i18n.lang import MESSAGES

        parts = callback.data.split(":")
        if parts[0] == "yt_fmt":
            # Новый формат: yt_fmt:{format_id}:{quality}:{video_id}
            if len(parts) == 4:
                format_id = parts[1]
                quality = parts[2]
                video_id = parts[3]
            else:
                format_id = parts[1]
                video_id = parts[2]
                quality = None
            url = f"https://www.youtube.com/watch?v={video_id}"
        else:
            # Старый формат
            format_id = parts[1]
            url = parts[2]
            quality = None

        await ctx.bot.answer_callback_query(callback.id)

        # Редактируем сообщение с форматами вместо удаления
        chat_id = callback.message.chat.id
        msg_id = callback.message.message_id
        text = MESSAGES["start_download"].get(ctx.language, "⏬ Downloading...")

        try:
            if getattr(callback.message, "photo", None):
                await ctx.bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=msg_id,
                    caption=text,
                )
            else:
                await ctx.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=text,
                )
        except Exception:
            pass

        progress_msg = callback.message

        # Создаём request
        # ВАЖНО: Если есть quality (480p, 720p), используем его как format, 
        # так как YouTube загрузчик сопоставляет именно по строке качества.
        request_format = format_id
        if quality and quality.endswith("p"):
            request_format = quality

        request = DownloadRequest(
            url=url,
            platform=MediaPlatform.YOUTUBE,
            user_id=ctx.user_id,
            bot_id=ctx.bot_model.bot_id,
            chat_id=ctx.chat_id,
            message_id=progress_msg.message_id,
            quality=quality,
            format=request_format,
        )

        # Запускаем скачивание
        return await self._process_download(ctx, progress_msg, request)

    async def _handle_youtube_audio_callback(self, ctx: ProcessingContext, callback: CallbackQuery) -> bool:
        """Обработка callback audio из нового интерфейса"""
        from i18n.lang import MESSAGES

        parts = callback.data.split(":")
        video_id = parts[1]

        url = f"https://www.youtube.com/watch?v={video_id}"

        await ctx.bot.answer_callback_query(callback.id)

        # Редактируем сообщение с форматами вместо удаления
        chat_id = callback.message.chat.id
        msg_id = callback.message.message_id
        text = MESSAGES["downloading_audio_mp3"].get(ctx.language, "⏬ Downloading MP3 audio...")

        try:
            if getattr(callback.message, "photo", None):
                await ctx.bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=msg_id,
                    caption=text,
                )
            else:
                await ctx.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=text,
                )
        except Exception:
            pass

        progress_msg = callback.message

        request = DownloadRequest(
            url=url,
            platform=MediaPlatform.YOUTUBE,
            user_id=ctx.user_id,
            bot_id=ctx.bot_model.bot_id,
            chat_id=ctx.chat_id,
            message_id=progress_msg.message_id,
            quality="audio",
            format="audio",
        )

        return await self._process_download(ctx, progress_msg, request)

    # _download_youtube_audio removed — merged into _handle_youtube_audio_callback

    async def _handle_direct_download(
        self,
        ctx: ProcessingContext,
        message: Message,
        url: str,
        platform: MediaPlatform,
        status_msg: Message | None = None,
    ) -> bool:
        """Прямое скачивание (Instagram, TikTok, Pinterest, VK, YouTube Shorts)"""
        from i18n.lang import MESSAGES

        # Progress messages
        platform_msgs = {
            MediaPlatform.INSTAGRAM: "downloading_instagram",
            MediaPlatform.TIKTOK: "downloading_tiktok",
            MediaPlatform.PINTEREST: "downloading_pinterest",
            MediaPlatform.YOUTUBE: "processing",  # YouTube Shorts
        }

        msg_key = platform_msgs.get(platform, "processing")
        progress_text = MESSAGES.get(msg_key, {}).get(ctx.language, MESSAGES.get("processing", {}).get(ctx.language, "⏬ Downloading..."))

        log.info("📥 Direct download started", platform=platform.value, url=url[:80])

        # Update status message if we have one
        if status_msg:
            try:
                if getattr(status_msg, "photo", None):
                    await ctx.bot.edit_message_caption(
                        caption=progress_text,
                        chat_id=status_msg.chat.id,
                        message_id=status_msg.message_id,
                    )
                else:
                    await ctx.bot.edit_message_text(
                        progress_text,
                        chat_id=status_msg.chat.id,
                        message_id=status_msg.message_id,
                    )
            except Exception:
                pass
        else:
            try:
                status_msg = await ctx.bot.send_message(
                    ctx.chat_id,
                    progress_text,
                    reply_to_message_id=message.message_id,
                )
            except Exception as e:
                log.error("Failed to send progress message", error=str(e))

        request = DownloadRequest(
            url=url,
            platform=platform,
            user_id=ctx.user_id,
            bot_id=ctx.bot_model.bot_id,
            chat_id=ctx.chat_id,
            message_id=status_msg.message_id if status_msg else None,
        )

        return await self._process_download(ctx, status_msg, request)

    async def _process_download(
        self,
        ctx: ProcessingContext,
        progress_msg: Message | None,
        request: DownloadRequest,
    ) -> bool:
        """Основной процесс скачивания"""
        from i18n.lang import MESSAGES

        async def update_progress(text: str):
            if not progress_msg:
                return
            try:
                if getattr(progress_msg, "photo", None):
                    await ctx.bot.edit_message_caption(
                        caption=text,
                        chat_id=progress_msg.chat.id,
                        message_id=progress_msg.message_id,
                    )
                else:
                    await ctx.bot.edit_message_text(
                        text,
                        chat_id=progress_msg.chat.id,
                        message_id=progress_msg.message_id,
                    )
            except Exception:
                pass  # Ignore edit_message errors

        try:
            log.info(
                "⬇️ Starting download",
                user_id=ctx.user_id,
                url=request.url[:80],
                platform=request.platform.value,
            )

            # Скачиваем (таймаут 3 минуты: предотвращает вечно-зависшие загрузки)
            try:
                result = await asyncio.wait_for(
                    download_service.download(
                        request,
                        ctx.bot,
                        progress_callback=update_progress,
                        language=ctx.language,
                    ),
                    timeout=180,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "Download timed out",
                    user_id=ctx.user_id,
                    url=request.url[:80],
                    platform=request.platform.value,
                )
                result = DownloadResult(success=False, error="timeout")

            log.info(
                "✅ Download completed",
                user_id=ctx.user_id,
                success=result.success,
                error=result.error,
            )

            if not result.success:
                # Process next in queue even on failure
                await self._process_next_in_queue(ctx)

                if progress_msg:
                    error_text = self._build_send_error_message(result.error, ctx.language, MESSAGES)
                    try:
                        if getattr(progress_msg, "photo", None):
                            await ctx.bot.edit_message_caption(
                                chat_id=progress_msg.chat.id,
                                message_id=progress_msg.message_id,
                                caption=error_text,
                            )
                        else:
                            await ctx.bot.edit_message_text(
                                error_text,
                                chat_id=progress_msg.chat.id,
                                message_id=progress_msg.message_id,
                            )
                    except Exception:
                        pass
                else:
                    await ctx.bot.send_message(
                        ctx.chat_id,
                        self._build_send_error_message(result.error, ctx.language, MESSAGES),
                        reply_to_message_id=request.message_id if request.message_id else None,
                    )
                return False

            # Send to user
            # reply_to = original user's link message (not our progress message)
            reply_to_message_id = None
            if progress_msg and getattr(progress_msg, "reply_to_message", None):
                reply_to_message_id = progress_msg.reply_to_message.message_id

            platform_name = "YouTube" if request.platform == MediaPlatform.YOUTUBE else request.platform.name.capitalize()
            video_text = MESSAGES.get("your_media_from", {}).get(ctx.language, "🎥 Ваше Видео из {}").format(platform_name)

            # Очищаем заголовок от ссылок и юзернеймов
            clean_title = self._clean_text(result.title)
            title_part = f"🎬 {clean_title}\n\n" if clean_title else ""

            # Добавим информацию о качестве и весе
            quality_info = ""
            if result.quality and result.quality.lower() != "none" and result.quality != "audio":
                quality_info = f"📹 {result.quality}"
                if result.filesize_str:
                    quality_info += f" - 💾 {result.filesize_str}"
                quality_info += "\n\n"
            elif result.quality == "audio":
                quality_info = "🔊 Audio"
                if result.filesize_str:
                    quality_info += f" - 💾 {result.filesize_str}"
                quality_info += "\n\n"

            caption = f"{title_part}{quality_info}{video_text} | 🤖 @{ctx.bot_model.username}"

            success = await download_service.send_to_user(
                ctx.bot,
                ctx.chat_id,
                result,
                message_id=progress_msg.message_id if progress_msg else None,
                caption=caption,
                reply_to=reply_to_message_id,
            )

            if success:
                # Save download record to DB (only for NEW downloads, not cache hits)
                if not result.from_cache:
                    await self._save_download_record(ctx, request, result)
                    log.debug("Download record saved (new)", user_id=ctx.user_id, url=request.url[:60])
                else:
                    log.debug("Cache hit — not counting as new download", user_id=ctx.user_id)

                # Update status to completed
                if progress_msg:
                    try:
                        if getattr(progress_msg, "photo", None):
                            await ctx.bot.edit_message_caption(
                                chat_id=progress_msg.chat.id,
                                message_id=progress_msg.message_id,
                                caption="✅ Downloaded!",
                            )
                        else:
                            await ctx.bot.edit_message_text(
                                "✅ Downloaded!",
                                chat_id=progress_msg.chat.id,
                                message_id=progress_msg.message_id,
                            )
                    except Exception:
                        pass

                # ✅ Send post-download ad IMMEDIATELY after content
                await self._send_post_download_ad(ctx)

                # Process next in queue
                await self._process_next_in_queue(ctx)
            else:
                log.error("Failed to send media to user", user_id=ctx.user_id, url=request.url[:60])
                if progress_msg:
                    try:
                        error_msg = self._build_send_error_message(result.error, ctx.language, MESSAGES)
                        if getattr(progress_msg, "photo", None):
                            await ctx.bot.edit_message_caption(
                                chat_id=progress_msg.chat.id,
                                message_id=progress_msg.message_id,
                                caption=error_msg,
                            )
                        else:
                            await ctx.bot.edit_message_text(
                                error_msg,
                                chat_id=progress_msg.chat.id,
                                message_id=progress_msg.message_id,
                            )
                    except Exception:
                        pass
                
                # Still process next in queue
                await self._process_next_in_queue(ctx)

            return success

        except Exception as e:
            log.exception("Download failed", error=str(e))
            if progress_msg:
                error_text = MESSAGES["error_processing"].get(ctx.language, "❌ Error")
                try:
                    if getattr(progress_msg, "photo", None):
                        await ctx.bot.edit_message_caption(
                            chat_id=progress_msg.chat.id,
                            message_id=progress_msg.message_id,
                            caption=error_text,
                        )
                    else:
                        await ctx.bot.edit_message_text(
                            error_text,
                            chat_id=progress_msg.chat.id,
                            message_id=progress_msg.message_id,
                        )
                except Exception:
                    pass
            else:
                await ctx.bot.send_message(
                    ctx.chat_id,
                    MESSAGES["error_processing"].get(ctx.language, "❌ Error"),
                    reply_to_message_id=request.message_id if request.message_id else None,
                )
            return False

    def _clean_text(self, text: str | None) -> str:
        """Очистка текста от ссылок и юзернеймов"""
        if not text:
            return ""
        # Удаляем ссылки
        text = re.sub(r"https?://\S+", "", text)
        # Удаляем юзернеймы Telegram/Instagram и т.д.
        text = re.sub(r"@[a-zA-Z0-9_]+", "", text)
        # Удаляем лишние пробелы
        text = re.sub(r"\s+", " ", text).strip()
        return text
    # === Download Record ===

    async def _process_next_in_queue(self, ctx: ProcessingContext) -> None:
        """Check and process next item in user's download queue."""
        res = await user_download_queue.get_next(ctx.user_id)
        if not res:
            return
            
        # Unpack result
        if isinstance(res, tuple) and len(res) == 2:
            next_dl, remaining = res
        else:
            # Fallback for unexpected return type
            next_dl = res
            remaining = []

        if not next_dl:
            return
            
        # Handle case where next_dl itself might be a tuple (happened in some environments)
        if isinstance(next_dl, tuple) and len(next_dl) > 0:
            next_dl = next_dl[0]

        log.info(
            "🔄 Processing next from queue",
            user_id=ctx.user_id,
            url=next_dl.url[:80] if hasattr(next_dl, 'url') else "unknown",
        )

        # ✅ Обновляем тексты у всех оставшихся элементов в очереди
        await self._update_queue_positions(ctx)

        # Detect platform
        platform = download_service.detect_platform(next_dl.url)
        if platform == MediaPlatform.UNKNOWN:
            try:
                await ctx.bot.edit_message_text(
                    "❌ Unsupported link",
                    chat_id=next_dl.chat_id,
                    message_id=next_dl.progress_message_id,
                )
            except Exception:
                pass

            await self._process_next_in_queue(ctx)
            return

        # Build request
        request = DownloadRequest(
            url=next_dl.url,
            platform=platform,
            user_id=next_dl.user_id,
            bot_id=ctx.bot_model.bot_id,
            chat_id=next_dl.chat_id,
            message_id=next_dl.progress_message_id,
        )

        # Update status to downloading
        from i18n.lang import MESSAGES

        progress_text = MESSAGES.get("processing", {}).get(
            ctx.language, "⏬ Downloading..."
        )
        try:
            await ctx.bot.edit_message_text(
                progress_text,
                chat_id=next_dl.chat_id,
                message_id=next_dl.progress_message_id,
            )
        except Exception:
            pass

        async def update_progress(text: str):
            try:
                await ctx.bot.edit_message_text(
                    text,
                    chat_id=next_dl.chat_id,
                    message_id=next_dl.progress_message_id,
                )
            except Exception:
                pass

        result = await download_service.download(
            request,
            ctx.bot,
            progress_callback=update_progress,
            language=ctx.language,
        )

        if result.success:
            try:
                platform_name = (
                    "YouTube"
                    if platform == MediaPlatform.YOUTUBE
                    else platform.name.capitalize()
                )
                video_text = MESSAGES.get("your_media_from", {}).get(
                    ctx.language, "🎥 Ваше Видео из {}"
                ).format(platform_name)

                clean_title = self._clean_text(result.title)
                title_part = f"🎬 {clean_title}\n\n" if clean_title else ""
                caption = f"{title_part}{video_text} | 🤖 @{ctx.bot_model.username}"

                await download_service.send_to_user(
                    ctx.bot,
                    next_dl.chat_id,
                    result,
                    message_id=next_dl.progress_message_id,
                    caption=caption,
                    reply_to=next_dl.message_id,
                )

                try:
                    await ctx.bot.edit_message_text(
                        "✅ Downloaded!",
                        chat_id=next_dl.chat_id,
                        message_id=next_dl.progress_message_id,
                    )
                except Exception:
                    pass

                if not result.from_cache:
                    await self._save_download_record(ctx, request, result)

                await self._send_post_download_ad(ctx)

            except Exception as e:
                log.error("Queued send failed", error=str(e))
                try:
                    await ctx.bot.edit_message_text(
                        f"❌ Failed: {str(e)[:100]}",
                        chat_id=next_dl.chat_id,
                        message_id=next_dl.progress_message_id,
                    )
                except Exception:
                    pass
        else:
            try:
                await ctx.bot.edit_message_text(
                    f"❌ {result.error[:100] if result.error else 'Download failed'}",
                    chat_id=next_dl.chat_id,
                    message_id=next_dl.progress_message_id,
                )
            except Exception:
                pass

        # Process next item in queue
        await self._process_next_in_queue(ctx)

    async def _update_queue_positions(self, ctx: ProcessingContext) -> None:
        """
        Update queue messages:
        queue 2 -> queue 1
        queue 3 -> queue 2
        etc.
        """
        from i18n.lang import MESSAGES
        waiting = await user_download_queue.get_waiting_downloads(ctx.user_id)

        for position, dl in enumerate(waiting, start=1):
            if not dl.progress_message_id:
                continue

            # Localized queue message
            text = MESSAGES.get("queue_status", {}).get(ctx.language, MESSAGES["queue_status"]["en"]).format(position=position)

            try:
                await ctx.bot.edit_message_text(
                    text,
                    chat_id=dl.chat_id,
                    message_id=dl.progress_message_id,
                )
            except Exception as e:
                # Чтобы не спамить ошибками если текст такой же
                if "message is not modified" not in str(e).lower():
                    log.warning(
                        "Failed to update queue position",
                        user_id=ctx.user_id,
                        position=position,
                        error=str(e),
                    )


    async def _bump_bot_hits(self, ctx: ProcessingContext) -> None:
        """Инкрементировать bot.total_downloads (для Media Search — нет Download-записи)."""
        try:
            async with UnitOfWork() as uow:
                db_bot = await uow.bots.get_by_id(ctx.bot_model.id)
                if db_bot:
                    db_bot.total_downloads = (db_bot.total_downloads or 0) + 1
                    uow.session.add(db_bot)
                    await uow.commit()
        except Exception as e:
            log.warning("Failed to bump bot hits", error=str(e))

    async def _save_download_record(self, ctx: ProcessingContext, request: DownloadRequest, result) -> None:
        """Save a download record to the database for statistics."""
        from models import Download
        from models.media import MediaSource as MediaSourceModel

        try:
            # Map platform to MediaSource enum
            source_map = {
                MediaPlatform.YOUTUBE: MediaSourceModel.YOUTUBE,
                MediaPlatform.INSTAGRAM: MediaSourceModel.INSTAGRAM,
                MediaPlatform.TIKTOK: MediaSourceModel.TIKTOK,
                MediaPlatform.PINTEREST: MediaSourceModel.PINTEREST,
            }
            source = source_map.get(request.platform, MediaSourceModel.OTHER)

            # Get user DB ID
            async with UnitOfWork() as uow:
                db_user = await uow.users.get_by_telegram_id(ctx.user_id, ctx.bot_model.bot_id)
                if not db_user:
                    log.warning("User not found in DB for download record", telegram_id=ctx.user_id)
                    return

                download = Download(
                    user_id=db_user.id,
                    # downloads.bot_id references bots.id (internal PK), not Telegram bot_id
                    bot_id=ctx.bot_model.id,
                    original_url=request.url,
                    source=source,
                    requested_quality=request.quality,
                    status=DownloadStatus.COMPLETED,
                )
                uow.session.add(download)

                # Increment user's total_downloads counter (global profile)
                await uow.users.increment_downloads(db_user.id)

                # Real-time bump on the bot itself, чтобы admin Activity не ждал
                # суточный пересчёт от воркера.
                db_bot = await uow.bots.get_by_id(ctx.bot_model.id)
                if db_bot:
                    db_bot.total_downloads = (db_bot.total_downloads or 0) + 1
                    uow.session.add(db_bot)

                await uow.commit()
                log.debug("Download record saved", user_id=db_user.id)
        except Exception as e:
            log.warning("Failed to save download record", error=str(e))

    # === Subscription Check ===

    async def _check_subscription_fast(
        self,
        ctx: ProcessingContext,
        message: Message,
        url: str | None = None,
        platform: MediaPlatform | None = None,
    ) -> bool:
        """
        Быстрая проверка подписки с кешированием результатов.
        """
        import time

        current_time = time.time()

        cache_key = ctx.bot_model.bot_id
        cached = self._channel_cache.get(cache_key)

        if cached and (current_time - cached["time"]) < self._channel_cache_ttl:
            channels = cached["channels"]
        else:
            channels = await subscription_service.get_required_channels(ctx.bot_model.id)
            self._channel_cache[cache_key] = {
                "channels": channels,
                "time": current_time,
            }

        if not channels:
            return True

        result = await subscription_service.check_user_subscription(
            ctx.user_id,
            ctx.bot,
            channels,
        )

        if result.is_subscribed:
            return True

        # Сохраняем контекст загрузки перед показом подписки (изолировано по боту)
        if url and platform:
            self._pending_downloads[(ctx.user_id, ctx.bot_model.bot_id)] = {
                "url": url,
                "platform": platform,
                "chat_id": ctx.chat_id,
                "message_id": message.message_id,
                "language": ctx.language,
                "bot_id": ctx.bot_model.bot_id,
            }
            log.info(
                "💾 Pending download stored",
                user_id=ctx.user_id,
                url=url[:80],
                platform=platform.value,
            )

        ad = await subscription_service.get_linked_ad(result.channels)
        text = subscription_service.build_prompt_message(result.channels, ctx.language, ad=ad)
        keyboard = subscription_service.build_subscribe_keyboard(result.channels, ctx.language, ad=ad)
        await ctx.bot.send_message(
            ctx.chat_id, text,
            reply_markup=keyboard,
            reply_to_message_id=message.message_id,
            parse_mode="HTML" if ad else None,
            disable_web_page_preview=True,
        )
        return False

    # _check_subscription removed — was identical to _check_subscription_fast

    async def _handle_subscription_retry(
        self,
        ctx: ProcessingContext,
        callback: CallbackQuery,
    ) -> bool:
        """User pressed 'I've subscribed' — re-check immediately (bypass caches)."""
        from i18n.lang import MESSAGES
        from services.subscription import clear_channel_cache

        # Drop caches so the user sees fresh state, not a stale 5-min result.
        await clear_channel_cache(ctx.bot_model.id)
        self._channel_cache.pop(ctx.bot_model.bot_id, None)

        channels = await subscription_service.get_required_channels(ctx.bot_model.id)
        if not channels:
            success_msg = MESSAGES.get("subscribed_success", {}).get(ctx.language, MESSAGES["subscribed_success"]["en"])
            await ctx.bot.answer_callback_query(callback.id, success_msg)
            # Delete the gate message — no channels required anymore (goal hit, etc.)
            try:
                await ctx.bot.delete_message(
                    chat_id=callback.message.chat.id,
                    message_id=callback.message.message_id,
                )
            except Exception as e:
                log.debug("Could not delete gate message (no-channels branch)", error=str(e)[:100])
            return True

        result = await subscription_service.check_user_subscription(
            ctx.user_id,
            ctx.bot,
            channels,
        )

        if result.is_subscribed:
            # User is now subscribed — always delete the gate message first.
            try:
                await ctx.bot.delete_message(
                    chat_id=callback.message.chat.id,
                    message_id=callback.message.message_id,
                )
            except Exception as e:
                log.debug("Could not delete gate message", error=str(e)[:100])

            pending = self._pending_downloads.pop((ctx.user_id, ctx.bot_model.bot_id), None)

            if pending:
                log.info(
                    "🔄 Resuming pending download after subscription",
                    user_id=ctx.user_id,
                    url=pending["url"][:80],
                    platform=pending["platform"].value,
                )

                from i18n.lang import MESSAGES

                url = pending["url"]
                platform = pending["platform"]

                platform_progress_msgs = {
                    MediaPlatform.INSTAGRAM: "downloading_instagram",
                    MediaPlatform.TIKTOK: "downloading_tiktok",
                    MediaPlatform.PINTEREST: "downloading_pinterest",
                    MediaPlatform.YOUTUBE: "loading_youtube_info",
                    MediaPlatform.VK: "downloading_vk",
                }
                msg_key = platform_progress_msgs.get(platform, "processing")
                progress_text = MESSAGES.get(msg_key, {}).get(ctx.language, MESSAGES.get("processing", {}).get(ctx.language, "⏬ Downloading..."))

                # Send a fresh progress message (gate was deleted above).
                progress_msg = await ctx.bot.send_message(ctx.chat_id, progress_text)
                # Re-bind so all downstream callback.message.* references work
                # on the new progress message instead of the deleted gate.
                try:
                    callback.message = progress_msg
                except Exception:
                    object.__setattr__(callback, "message", progress_msg)

                await ctx.bot.answer_callback_query(callback.id)

                # Add to queue and start download
                success, position, queue_msg = await user_download_queue.add(
                    user_id=ctx.user_id,
                    bot_id=pending["bot_id"],
                    chat_id=pending["chat_id"],
                    message_id=pending["message_id"],
                    url=url,
                )

                if not success:
                    await ctx.bot.send_message(ctx.chat_id, queue_msg)
                    return True

                # Обновляем сообщение статусом очереди
                if position > 0:
                    await user_download_queue.set_progress_message(
                        user_id=ctx.user_id,
                        bot_id=pending["bot_id"],
                        url=url,
                        progress_message_id=callback.message.message_id,
                    )
                    # Обновляем текст сообщения на статус очереди
                    try:
                        await ctx.bot.edit_message_text(
                            text=queue_msg,
                            chat_id=callback.message.chat.id,
                            message_id=callback.message.message_id,
                        )
                    except Exception:
                        pass
                elif position == 0:
                    # Start downloading immediately
                    # ВАЖНО: Не используем callback.message как message для reply_to
                    # Вместо этого используем original_message_id из pending
                    original_message_id = pending.get("message_id")

                    if platform == MediaPlatform.YOUTUBE:
                        if "/shorts/" in url.lower():
                            return await self._handle_direct_download(ctx, callback.message, url, platform, callback.message)
                        # Для YouTube - показываем thumbnail + форматы без reply_to
                        return await self._handle_youtube_url_resumed(
                            ctx,
                            chat_id=callback.message.chat.id,
                            url=url,
                            progress_message_id=callback.message.message_id,
                            pending=pending,
                        )
                    else:
                        # Для прямых загрузок - продолжаем использовать текущее сообщение
                        return await self._handle_direct_download(ctx, callback.message, url, platform, callback.message)
            else:
                # No pending download — show success toast + delete gate message
                success_msg = MESSAGES.get("subscribed_success", {}).get(ctx.language, MESSAGES["subscribed_success"]["en"])
                await ctx.bot.answer_callback_query(callback.id, success_msg)
                try:
                    await ctx.bot.delete_message(
                        chat_id=callback.message.chat.id,
                        message_id=callback.message.message_id,
                    )
                except Exception:
                    pass
        else:
            # User hasn't subscribed yet - show alert in their language
            alert_msg = MESSAGES.get("not_subscribed_alert", {}).get(ctx.language, MESSAGES["not_subscribed_alert"]["en"])

            # Update the message with subscription prompt — prefer linked Ad
            ad = await subscription_service.get_linked_ad(result.channels)
            text = subscription_service.build_prompt_message(result.channels, ctx.language, ad=ad)
            keyboard = subscription_service.build_subscribe_keyboard(result.channels, ctx.language, ad=ad)

            try:
                await ctx.bot.edit_message_text(
                    text=text,
                    chat_id=callback.message.chat.id,
                    message_id=callback.message.message_id,
                    reply_markup=keyboard,
                    parse_mode="HTML" if ad else None,
                    disable_web_page_preview=True,
                )
            except Exception:
                # If message content is same as current, just update markup
                try:
                    await ctx.bot.edit_message_reply_markup(
                        chat_id=callback.message.chat.id,
                        message_id=callback.message.message_id,
                        reply_markup=keyboard,
                    )
                except Exception:
                    pass  # Ignore if message can't be modified

            await ctx.bot.answer_callback_query(callback.id, alert_msg, show_alert=True)

        return True

    # === Post-Download Ad ===

    async def _send_post_download_ad(self, ctx: ProcessingContext) -> None:
        try:
            async with UnitOfWork() as uow:
                repo = AdRepository(uow.session)
                ad = await repo.get_post_download_ad(ctx.bot_model.bot_id)
                await uow.commit()

            if not ad:
                return

            log.info("Sending ad", ad_id=ad.id, media_type=ad.media_type)

            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            from aiogram.exceptions import TelegramBadRequest
            from app.config import settings

            keyboard = self._build_ad_keyboard(ad)

            # Готовим 3 варианта текста (от лучшего к простейшему)
            html_content = prepare_telegram_html(ad.content)
            compat_content = prepare_telegram_compat_html(ad.content)
            plain_content = strip_telegram_markup(ad.content)

            sent_message_id: int | None = None

            async def _send_media(caption: str, parse_mode: str | None) -> int | None:
                """Копирует медиа из кеш-канала через copy_message (без 'forwarded from')."""
                if not (ad.cache_channel_message_id and settings.media_flow_cache_channel_id):
                    return None
                try:
                    msg = await ctx.bot.copy_message(
                        chat_id=ctx.chat_id,
                        from_chat_id=settings.media_flow_cache_channel_id,
                        message_id=ad.cache_channel_message_id,
                        caption=caption,
                        parse_mode=parse_mode,
                        reply_markup=keyboard,
                    )
                    return msg.message_id if msg else None
                except TelegramBadRequest as e:
                    log.warning(
                        "post_download copy_message failed",
                        ad_id=ad.id,
                        error=str(e)[:200],
                    )
                    return None

            async def _send_text(text: str, parse_mode: str | None) -> int | None:
                """Отправляет текстовое сообщение."""
                try:
                    msg = await ctx.bot.send_message(
                        ctx.chat_id,
                        text=text,
                        parse_mode=parse_mode,
                        reply_markup=keyboard,
                        disable_web_page_preview=True,
                    )
                    return msg.message_id if msg else None
                except TelegramBadRequest:
                    return None

            sent = False

            # ── Медиа ─────────────────────────────────────────────────
            if ad.cache_channel_message_id and ad.media_type and ad.media_type.value != "none":
                for caption, mode in [
                    (html_content, "HTML"),
                    (compat_content, "HTML"),
                    (plain_content, None),
                ]:
                    mid = await _send_media(caption, mode)
                    if mid:
                        log.info("Ad sent as media", mode=mode)
                        sent = True
                        sent_message_id = mid
                        break

            # ── Текст (fallback) ───────────────────────────────────────
            if not sent and ad.content:
                for text, mode in [
                    (html_content, "HTML"),
                    (compat_content, "HTML"),
                    (plain_content, None),
                ]:
                    mid = await _send_text(text, mode)
                    if mid:
                        log.info("Ad sent as text", mode=mode)
                        sent = True
                        sent_message_id = mid
                        break

            if not sent:
                log.warning("Ad has no content and no media", ad_id=ad.id)
                return

            # ── Авто-удаление поста через N секунд ──────────────────────
            auto_delete = getattr(ad, "auto_delete_seconds", None)
            if sent_message_id and auto_delete and auto_delete > 0:
                asyncio.create_task(
                    self._auto_delete_message(
                        ctx.bot, ctx.chat_id, sent_message_id, int(auto_delete), ad.id
                    )
                )

        except Exception as exc:
            log.error(
                "Failed to send ad",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    # === chat_member handler (subscription-gate counter) ===

    _MEMBER_STATUSES = {"member", "administrator", "creator", "restricted"}
    _NON_MEMBER_STATUSES = {"left", "kicked"}

    async def _handle_chat_member(self, bot_id: int, cm) -> None:
        """
        Increment subscriber counter for any active SUBSCRIPTION_GATE ad
        whose channel matches this chat. When the goal is reached, mark
        the ad COMPLETED and deactivate the gate (SubscriptionChannel rows).
        """
        try:
            old_status = cm.old_chat_member.status if cm.old_chat_member else None
            new_status = cm.new_chat_member.status if cm.new_chat_member else None
            chat_id = cm.chat.id

            # We only care about transitions OUT-of-channel → INTO-channel
            became_member = (
                new_status in self._MEMBER_STATUSES
                and old_status in self._NON_MEMBER_STATUSES
            )
            if not became_member:
                return

            from sqlalchemy import select, update as sa_update
            from models import Ad, AdStatus, AdType
            from models.subscription import SubscriptionChannel
            from services.subscription import clear_channel_cache

            async with UnitOfWork() as uow:
                rows = (await uow.session.execute(
                    select(Ad).where(
                        Ad.ad_type == AdType.SUBSCRIPTION_GATE,
                        Ad.is_active == True,  # noqa: E712
                        Ad.subscription_channel_chat_id == chat_id,
                    )
                )).scalars().all()

                completed_chat_ids: list[int] = []
                for ad in rows:
                    ad.subscribers_gained = (ad.subscribers_gained or 0) + 1
                    if ad.subscriber_goal and ad.subscribers_gained >= ad.subscriber_goal:
                        ad.is_active = False
                        ad.status = AdStatus.COMPLETED
                        ad.completed_at = datetime.now()
                        completed_chat_ids.append(ad.subscription_channel_chat_id)
                        log.info(
                            "Subscription-gate goal reached",
                            ad_id=ad.id, gained=ad.subscribers_gained, goal=ad.subscriber_goal,
                        )

                if completed_chat_ids:
                    await uow.session.execute(
                        sa_update(SubscriptionChannel)
                        .where(SubscriptionChannel.channel_chat_id.in_(completed_chat_ids))
                        .values(is_active=False)
                    )
                    affected_bots = (await uow.session.execute(
                        select(SubscriptionChannel.bot_id).where(
                            SubscriptionChannel.channel_chat_id.in_(completed_chat_ids)
                        )
                    )).scalars().all()
                else:
                    affected_bots = []

                await uow.commit()

            for b_id in set(affected_bots):
                await clear_channel_cache(b_id)

        except Exception as e:
            log.exception("chat_member handler failed", error=str(e), bot_id=bot_id)

    async def _auto_delete_message(self, bot, chat_id: int, message_id: int, delay: int, ad_id: int) -> None:
        """Удаляет сообщение через delay секунд. Live-fire-and-forget."""
        try:
            await asyncio.sleep(delay)
            await bot.delete_message(chat_id, message_id)
            log.debug("Auto-deleted ad message", ad_id=ad_id, chat_id=chat_id, message_id=message_id, delay=delay)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Уже удалено, чат заблокирован, прошло >48ч — всё штатные исходы.
            log.debug("Auto-delete skipped", ad_id=ad_id, error=str(e)[:120])

    def _build_ad_keyboard(self, ad) -> "InlineKeyboardMarkup | None":
        """Build inline keyboard from ad's buttons (multi-button JSON or legacy single)."""
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

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

    def _clean_html(self, text: str) -> str:
        """Strip HTML tags from text."""
        return re.sub(r"<[^>]*>", "", text)

    async def _handle_unknown(self, ctx: ProcessingContext, message: Message) -> bool:
        """Обработка неизвестного сообщения"""
        from i18n.lang import MESSAGES

        text = MESSAGES["send_link"].get(ctx.language, MESSAGES["send_link"]["en"])
        try:
            await ctx.bot.send_message(ctx.chat_id, text, reply_to_message_id=message.message_id)
        except Exception:
            await ctx.bot.send_message(ctx.chat_id, text)
        return True


# === Singleton ===
update_processor = UpdateProcessor()
