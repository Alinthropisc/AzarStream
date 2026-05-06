"""Controller for managing required subscription channels."""

from litestar import Controller, get, post
from litestar.response import Template, Redirect
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.di import Provide
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import get_session
from repositories.bot import BotRepository
from repositories.subscription import SubscriptionChannelRepository
from services.subscription import clear_channel_cache
from models.subscription import SubscriptionChannel
from app.middleware.auth import admin_guard
from app.logging import get_logger

log = get_logger("controller.subscription")


class SubscriptionController(Controller):
    path = "/admin/subscriptions"
    guards = [admin_guard]
    dependencies = {"session": Provide(get_session)}

    @get("/", name="subscriptions:list")
    async def list_channels(self, session: AsyncSession) -> Template:
        """List all subscription channels."""
        repo = SubscriptionChannelRepository(session)
        channels = await repo.get_all()

        bot_repo = BotRepository(session)
        bots = await bot_repo.get_all()

        return Template(
            template_name="admin/subscriptions/list.html",
            context={
                "channels": channels,
                "bots": bots,
            }
        )

    @get("/create", name="subscriptions:create_form")
    async def create_form(self, session: AsyncSession) -> Template:
        """Show create form."""
        bot_repo = BotRepository(session)
        bots = await bot_repo.get_active_bots()
        return Template(
            template_name="admin/subscriptions/create.html",
            context={"bots": bots}
        )

    @post("/create", name="subscriptions:create")
    async def create_channel(
        self,
        session: AsyncSession,
        data: dict = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        """Create a new subscription channel."""
        repo = SubscriptionChannelRepository(session)

        bot_id = int(data.get("bot_id", 0))
        channel_chat_id = int(data.get("channel_chat_id", 0))
        channel_username = data.get("channel_username", "").strip() or None
        channel_title = data.get("channel_title", "").strip() or None

        if not bot_id or not channel_chat_id:
            return Redirect(path="/admin/subscriptions/create?error=Missing required fields")

        try:
            await repo.create(
                bot_id=bot_id,
                channel_chat_id=channel_chat_id,
                channel_username=channel_username,
                channel_title=channel_title,
            )
            await session.commit()

            # Invalidate caches (both Redis and in-memory)
            await clear_channel_cache(bot_id)

            return Redirect(path="/admin/subscriptions?message=Channel added successfully")
        except Exception as e:
            log.error("Failed to create channel", error=str(e))
            return Redirect(path=f"/admin/subscriptions/create?error={e!s}")

    @post("/{channel_id:int}/toggle", name="subscriptions:toggle")
    async def toggle_channel(self, session: AsyncSession, channel_id: int) -> Redirect:
        """Toggle channel active/inactive."""
        repo = SubscriptionChannelRepository(session)
        channel = await repo.toggle_active(channel_id)
        await session.commit()

        # Invalidate cache for this bot
        if channel:
            await clear_channel_cache(channel.bot_id)

        return Redirect(path="/admin/subscriptions")

    @post("/{channel_id:int}/delete", name="subscriptions:delete")
    async def delete_channel(self, session: AsyncSession, channel_id: int) -> Redirect:
        """Delete a subscription channel."""
        # Get channel before delete to know which bot_id to invalidate
        channel = await session.get(SubscriptionChannel, channel_id)
        bot_id = channel.bot_id if channel else None

        repo = SubscriptionChannelRepository(session)
        await repo.delete(channel_id)
        await session.commit()

        # Invalidate cache
        if bot_id:
            await clear_channel_cache(bot_id)

        return Redirect(path="/admin/subscriptions?message=Channel deleted")
