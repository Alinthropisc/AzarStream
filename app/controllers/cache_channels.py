from uuid import UUID
from litestar import Controller, get, post
from litestar.di import Provide
from litestar.response import Template, Redirect
from litestar.enums import RequestEncodingType
from litestar.params import Body
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import get_session
from services.cache_channel import CacheChannelService, CreateCacheChannelDTO, UpdateCacheChannelDTO
from models import BotType
from app.logging import get_logger


def _parse_bot_type(raw: str | None) -> BotType | None:
    if not raw:
        return None
    try:
        return BotType(raw)
    except ValueError:
        return None

log = get_logger("controller.cache_channel_web")

class CacheChannelWebController(Controller):
    path = "/admin/cache_channels"
    dependencies = {"session": Provide(get_session)}

    @get("/", name="cache_channels:list")
    async def list_channels(self, session: AsyncSession, type: str | None = None) -> Template:
        service = CacheChannelService(session)
        bot_type = _parse_bot_type(type)
        channels = await service.list_all(bot_type=bot_type)
        return Template(
            template_name="admin/cache_channels/list.html",
            context={
                "channels": channels,
                "bot_type": bot_type.value if bot_type else None,
            },
        )

    @get("/create", name="cache_channels:create_form")
    async def create_form(self, type: str | None = None) -> Template:
        bot_type = _parse_bot_type(type) or BotType.MEDIA_STREAM
        return Template(
            template_name="admin/cache_channels/create.html",
            context={"selected_bot_type": bot_type.value},
        )

    @post("/create", name="cache_channels:create")
    async def create_channel(
        self,
        session: AsyncSession,
        data: dict = Body(media_type=RequestEncodingType.URL_ENCODED),
    ) -> Redirect:
        service = CacheChannelService(session)
        
        try:
            dto = CreateCacheChannelDTO(
                name=data.get("name", "").strip(),
                telegram_id=int(data.get("telegram_id", 0)),
                username=data.get("username", "").strip() or None,
                description=data.get("description", "").strip() or None,
                is_active=data.get("is_active") == "true",
                bot_type=_parse_bot_type(data.get("bot_type")) or BotType.MEDIA_STREAM,
            )
            await service.create(dto)
            await session.commit()
            return Redirect(path="/admin/cache_channels?message=Channel added successfully")
        except Exception as e:
            log.error("Failed to create cache channel", error=str(e))
            return Redirect(path=f"/admin/cache_channels/create?error={str(e)}")

    @post("/{channel_id:uuid}/toggle", name="cache_channels:toggle")
    async def toggle_channel(self, session: AsyncSession, channel_id: UUID) -> Redirect:
        service = CacheChannelService(session)
        await service.toggle_active(channel_id)
        await session.commit()
        return Redirect(path="/admin/cache_channels")

    @post("/{channel_id:uuid}/delete", name="cache_channels:delete")
    async def delete_channel(self, session: AsyncSession, channel_id: UUID) -> Redirect:
        service = CacheChannelService(session)
        await service.delete(channel_id)
        await session.commit()
        return Redirect(path="/admin/cache_channels?message=Channel deleted")
