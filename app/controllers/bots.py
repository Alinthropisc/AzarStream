from litestar import Controller, get, post, put, delete
from litestar.response import Template, Redirect
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.di import Provide
from sqlalchemy.ext.asyncio import AsyncSession
from aiogram import Bot as AiogramBot
import re

from app.config import settings
from app.logging import get_logger
from database.connection import get_session
from repositories import BotRepository
from models import BotStatus
from services import bot_manager

log = get_logger("controller.bots")


class BotController(Controller):
    path = "/admin/bots"
    dependencies = {"session": Provide(get_session)}

    @get("/", name="bots:list")
    async def list_bots(self, session: AsyncSession) -> Template:
        repo = BotRepository(session)
        bots = await repo.get_all(order_by="created_at")
        return Template(
            template_name="admin/bots/list.html",
            context={"bots": bots}
        )

    @get("/create", name="bots:create_form")
    async def create_form(self) -> Template:
        return Template(template_name="admin/bots/create.html")

    @post("/create", name="bots:create")
    async def create_bot(
        self,
        session: AsyncSession,
        data: dict = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        repo = BotRepository(session)

        token = data.get("token", "").strip()
        name = data.get("name", "").strip()
        description = data.get("description", "").strip() or None
        status = data.get("status", BotStatus.ACTIVE)
        is_webhook = data.get("is_webhook") == "true"
        webhook_url = data.get("webhook_url", "").strip() or None
        webhook_secret = data.get("webhook_secret", "").strip() or None

        # Validate webhook secret (Telegram only allows A-Z, a-z, 0-9, -, _)
        if webhook_secret and not re.match(r'^[A-Za-z0-9_-]+$', webhook_secret):
            return Redirect(path="/admin/bots/create?error=Webhook secret can only contain letters, numbers, hyphens and underscores")
        if webhook_secret and len(webhook_secret) > 256:
            return Redirect(path="/admin/bots/create?error=Webhook secret must be 256 characters or less")

        # Validate token with Telegram
        try:
            aiogram_bot = AiogramBot(token=token)
            bot_info = await aiogram_bot.get_me()
            await aiogram_bot.session.close()
        except Exception as e:
            return Redirect(path=f"/admin/bots/create?error=Invalid token: {e}")

        # Check if already exists
        existing = await repo.get_by_bot_id(bot_info.id)
        if existing:
            return Redirect(path="/admin/bots/create?error=Bot already registered")

        # Create bot
        new_bot = await repo.create(
            token=token,
            bot_id=bot_info.id,
            username=bot_info.username,
            name=name or bot_info.first_name,
            description=description,
            status=status,
            is_webhook=is_webhook,
            webhook_url=webhook_url,
            webhook_secret=webhook_secret,
        )
        await session.commit()

        # Авто-регистрация webhook'а — без рестарта сервиса.
        message = "Bot registered successfully"
        if new_bot.status == BotStatus.ACTIVE and settings.webhook_base_url:
            try:
                # Чистим возможный устаревший кэш для этого токена
                # (если бот был удалён и регистрируется снова с тем же токеном).
                await bot_manager.evict(new_bot.token)
                ok = await bot_manager.setup_webhook(new_bot, settings.webhook_base_url)
                if ok:
                    message = "Bot registered and webhook activated"
                else:
                    message = "Bot registered, but webhook setup failed — check logs"
            except Exception as exc:
                log.exception("Auto webhook setup failed", username=new_bot.username, error=str(exc))
                message = "Bot registered, but webhook setup raised an error"

        return Redirect(path=f"/admin/bots?message={message}")

    @get("/{bot_id:int}", name="bots:detail")
    async def bot_detail(self, session: AsyncSession, bot_id: int) -> Template:
        repo = BotRepository(session)
        bot = await repo.get_by_id(bot_id)
        if not bot:
            return Redirect(path="/admin/bots")

        return Template(
            template_name="admin/bots/detail.html",
            context={"bot": bot}
        )

    @post("/{bot_id:int}/toggle", name="bots:toggle")
    async def toggle_bot(self, session: AsyncSession, bot_id: int) -> Redirect:
        repo = BotRepository(session)
        bot = await repo.get_by_id(bot_id)
        if not bot:
            return Redirect(path="/admin/bots")

        new_status = BotStatus.INACTIVE if bot.status == BotStatus.ACTIVE else BotStatus.ACTIVE
        await repo.update(bot_id, status=new_status)
        await session.commit()

        if settings.webhook_base_url:
            try:
                if new_status == BotStatus.ACTIVE:
                    fresh = await repo.get_by_id(bot_id)
                    if fresh:
                        await bot_manager.setup_webhook(fresh, settings.webhook_base_url)
                else:
                    await bot_manager.remove_webhook(bot.token)
                    await bot_manager.evict(bot.token)
            except Exception as exc:
                log.exception("Webhook toggle failed", username=bot.username, error=str(exc))

        return Redirect(path="/admin/bots")

    @post("/{bot_id:int}/delete", name="bots:delete")
    async def delete_bot(self, session: AsyncSession, bot_id: int) -> Redirect:
        repo = BotRepository(session)
        bot = await repo.get_by_id(bot_id)
        if bot:
            try:
                await bot_manager.remove_webhook(bot.token)
            except Exception as exc:
                log.exception("Webhook removal failed", username=bot.username, error=str(exc))
            # Удаляем инстанс из кэша, иначе при повторной регистрации
            # этого токена webhook-хендлер будет сравнивать с устаревшим секретом.
            await bot_manager.evict(bot.token)
        await repo.delete(bot_id)
        return Redirect(path="/admin/bots")
