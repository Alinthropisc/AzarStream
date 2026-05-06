import asyncio
from typing import Callable, Any
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import Update

from app.config import settings
from app.logging import get_logger
from database.connection import db
from repositories import BotRepository
from models import Bot as BotModel, BotStatus

log = get_logger("service.bot_manager")


@dataclass
class BotInstance:
    """Инстанс бота с метаданными"""
    model: BotModel
    bot: Bot
    created_at: float = field(default_factory=lambda: asyncio.get_event_loop().time())


class BotManager:
    """
    Менеджер ботов для multi-bot webhook архитектуры

    - Хранит пул Bot instances
    - Автоматически создаёт/кеширует инстансы
    - Управляет webhook'ами
    """

    def __init__(self, max_bots: int = 100, bot_ttl: int = 3600):
        self._bots: dict[str, BotInstance] = {}  # token -> BotInstance
        self._bots_by_id: dict[int, str] = {}  # bot_id -> token
        self._max_bots = max_bots
        self._bot_ttl = bot_ttl
        self._session: AiohttpSession | None = None
        self._lock = asyncio.Lock()

    async def setup(self) -> None:
        """Инициализация менеджера"""
        # Создаём общую сессию для всех ботов
        if settings.telegram_api_server:
            from aiogram.client.telegram import TelegramAPIServer
            self._session = AiohttpSession(
                api=TelegramAPIServer.from_base(
                    settings.telegram_api_server,
                    is_local=settings.telegram_api_local,
                )
            )
            log.info(
                "Using custom Telegram API server",
                url=settings.telegram_api_server,
                local=settings.telegram_api_local,
            )
        else:
            from aiohttp import ClientTimeout
            self._session = AiohttpSession(
                timeout=ClientTimeout(total=600)  # 10 minutes
            )

        # Загружаем активные боты из БД
        await self._preload_active_bots()

    async def shutdown(self) -> None:
        """Закрытие всех соединений"""
        log.info("Shutting down bot manager...")

        for token, instance in self._bots.items():
            try:
                await instance.bot.session.close()
            except Exception as e:
                log.error("Error closing bot session", error=str(e))

        self._bots.clear()
        self._bots_by_id.clear()

        if self._session:
            await self._session.close()

    async def _preload_active_bots(self) -> None:
        """Предзагрузка активных ботов"""
        async with db.session() as session:
            repo = BotRepository(session)
            bots = await repo.get_active_bots()

            for bot_model in bots:
                await self._create_bot_instance(bot_model)

            log.info("Preloaded bots", count=len(bots))

    async def _create_bot_instance(self, model: BotModel) -> BotInstance:
        """Создать инстанс бота"""
        bot = Bot(
            token=model.token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            session=self._session,
        )

        instance = BotInstance(model=model, bot=bot)
        self._bots[model.token] = instance
        self._bots_by_id[model.bot_id] = model.token

        log.debug("Created bot instance", username=model.username)
        return instance

    async def get_bot(self, token: str) -> Bot | None:
        """Получить Bot по токену"""
        instance = await self.get_bot_instance(token)
        return instance.bot if instance else None

    async def get_bot_instance(self, token: str) -> BotInstance | None:
        """Получить BotInstance по токену"""
        # Проверяем кеш
        if token in self._bots:
            return self._bots[token]

        # Ищем в БД
        async with self._lock:
            # Double-check после получения блокировки
            if token in self._bots:
                return self._bots[token]

            async with db.session() as session:
                repo = BotRepository(session)
                model = await repo.get_by_token(token)

                if not model or model.status != BotStatus.ACTIVE:
                    return None

                # Очищаем старые инстансы если превышен лимит
                await self._cleanup_old_bots()

                return await self._create_bot_instance(model)

    async def get_bot_by_id(self, bot_id: int) -> Bot | None:
        """Получить Bot по bot_id"""
        if bot_id in self._bots_by_id:
            token = self._bots_by_id[bot_id]
            return self._bots[token].bot

        async with db.session() as session:
            repo = BotRepository(session)
            model = await repo.get_by_bot_id(bot_id)

            if model and model.status == BotStatus.ACTIVE:
                instance = await self._create_bot_instance(model)
                return instance.bot

        return None

    async def get_all_active_bots(self) -> list[tuple[BotModel, Bot]]:
        """Получить все активные боты"""
        async with db.session() as session:
            repo = BotRepository(session)
            models = await repo.get_active_bots()

            result = []
            for model in models:
                if model.token in self._bots:
                    result.append((model, self._bots[model.token].bot))
                else:
                    instance = await self._create_bot_instance(model)
                    result.append((model, instance.bot))

            return result

    async def _cleanup_old_bots(self) -> None:
        """Очистка старых инстансов"""
        if len(self._bots) < self._max_bots:
            return

        current_time = asyncio.get_event_loop().time()
        to_remove = []

        for token, instance in self._bots.items():
            if current_time - instance.created_at > self._bot_ttl:
                to_remove.append(token)

        for token in to_remove[:len(self._bots) - self._max_bots // 2]:
            instance = self._bots.pop(token, None)
            if instance:
                self._bots_by_id.pop(instance.model.bot_id, None)
                try:
                    await instance.bot.session.close()
                except Exception:
                    pass

        if to_remove:
            log.debug("Cleaned up old bot instances", count=len(to_remove))

    # === Webhook Management ===

    async def setup_webhook(self, bot_model: BotModel, base_url: str) -> bool:
        """Установить webhook для бота"""
        bot = await self.get_bot(bot_model.token)
        if not bot:
            return False

        webhook_url = f"{base_url}/webhook/{bot_model.token}"

        # Генерируем секрет если его нет — Telegram будет слать его в заголовке,
        # контроллер проверяет совпадение → жёсткая изоляция между ботами.
        if not bot_model.webhook_secret:
            import secrets as _secrets
            # token_hex даёт безопасный набор [a-f0-9] — ни один прокси/туннель
            # не пытается его url-encode'ить (с token_urlsafe бывали рассинхроны
            # из-за символов `_` и `-`).
            new_secret = _secrets.token_hex(32)
            async with db.session() as session:
                repo = BotRepository(session)
                db_model = await repo.get_by_token(bot_model.token)
                if db_model:
                    db_model.webhook_secret = new_secret
                    await session.commit()
            bot_model.webhook_secret = new_secret
            # Обновим закешированный instance
            if bot_model.token in self._bots:
                self._bots[bot_model.token].model.webhook_secret = new_secret

        try:
            await bot.set_webhook(
                url=webhook_url,
                secret_token=bot_model.webhook_secret,
                drop_pending_updates=True,
                allowed_updates=[
                    "message", "edited_message", "callback_query",
                    "inline_query", "chosen_inline_result",
                    "my_chat_member", "chat_member",
                ],
            )
            # Публикуем актуальный секрет в Redis — все worker'ы Granian
            # используют его как source-of-truth (см. webhook handler),
            # иначе у них в локальном кэше остаётся старый BotInstance.
            try:
                from services import cache
                await cache.set(
                    f"bot:webhook_secret:{bot_model.token}",
                    bot_model.webhook_secret,
                    ttl=7 * 24 * 3600,
                )
            except Exception as e:
                log.warning("Failed to publish webhook secret to Redis", error=str(e))
            log.info(
                "Webhook set",
                username=bot_model.username,
                url=webhook_url,
                secret_prefix=(bot_model.webhook_secret[:6] + "…") if bot_model.webhook_secret else None,
                secret_len=len(bot_model.webhook_secret) if bot_model.webhook_secret else 0,
            )
            return True
        except Exception as e:
            log.error("Failed to set webhook", username=bot_model.username, error=str(e))
            return False

    async def setup_all_webhooks(self, base_url: str) -> dict[str, bool]:
        """Установить webhook'и для всех активных ботов"""
        async with db.session() as session:
            repo = BotRepository(session)
            bots = await repo.get_active_bots()

            results = {}
            for i, bot_model in enumerate(bots):
                # Задержка между ботами чтобы не попасть в flood control
                if i > 0:
                    await asyncio.sleep(1)

                success = await self.setup_webhook(bot_model, base_url)
                results[bot_model.username] = success

                # Если flood control — ждём и пробуем снова
                if not success:
                    await asyncio.sleep(2)
                    success = await self.setup_webhook(bot_model, base_url)
                    results[bot_model.username] = success

            return results

    async def remove_webhook(self, token: str) -> bool:
        """Удалить webhook"""
        bot = await self.get_bot(token)
        if not bot:
            return False

        try:
            await bot.delete_webhook()
            return True
        except Exception as e:
            log.error("Failed to remove webhook", error=str(e))
            return False

    async def evict(self, token: str) -> None:
        """
        Полностью выгрузить инстанс бота из кэша.
        Нужно при удалении/пересоздании бота — иначе webhook-хендлер будет
        сравнивать заголовок Telegram со старым (закэшированным) webhook_secret.
        """
        instance = self._bots.pop(token, None)
        if instance:
            self._bots_by_id.pop(instance.model.bot_id, None)
            # Сессия aiogram шарится между всеми ботами (self._session) —
            # закрывать её здесь нельзя, иначе порвём остальные инстансы.
            log.debug("Evicted bot instance from cache", username=instance.model.username)
        # Удаляем shared-секрет из Redis, чтобы соседние worker'ы тоже
        # перестали его использовать.
        try:
            from services import cache
            await cache.delete(f"bot:webhook_secret:{token}")
        except Exception:
            pass


# === Singleton ===
bot_manager = BotManager()
