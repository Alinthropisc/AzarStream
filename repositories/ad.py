from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from repositories.base import BaseRepository
from models import Ad, AdBot, AdDelivery, AdStatus, AdType


class AdRepository(BaseRepository[Ad]):
    model = Ad

    async def get_by_uuid(self, ad_uuid: str) -> Ad | None:
        stmt = (
            select(Ad)
            .where(Ad.ad_uuid == ad_uuid)
            .options(
                selectinload(Ad.target_bots),
                selectinload(Ad.deliveries),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_with_relations(self, ad_id: int) -> Ad | None:
        stmt = (
            select(Ad)
            .where(Ad.id == ad_id)
            .options(
                selectinload(Ad.target_bots).selectinload(AdBot.bot),
                selectinload(Ad.deliveries),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active(self, offset: int = 0, limit: int = 50) -> list[Ad]:
        return list(await self.filter(
            is_active=True,
            offset=offset,
            limit=limit,
            order_by="created_at",
        ))

    async def get_post_download_ad(self, bot_id: int) -> Ad | None:
        """Get active post-download ad for a specific bot (not expired)."""
        from datetime import datetime
        from models import AdStatus
        stmt = (
            select(Ad)
            .where(
                Ad.ad_type == AdType.POST_DOWNLOAD,
                Ad.is_active == True,
                Ad.status.in_([AdStatus.DRAFT, AdStatus.SCHEDULED, AdStatus.SENDING, AdStatus.COMPLETED]),
                (Ad.expires_at.is_(None)) | (Ad.expires_at > datetime.now()),
            )
            .options(
                selectinload(Ad.target_bots).selectinload(AdBot.bot),
            )
            .order_by(Ad.created_at.desc())
        )
        result = await self.session.execute(stmt)
        ads = result.scalars().all()

        # Filter by bot_id (ads with no target_bots apply to all bots)
        for ad in ads:
            if not ad.target_bots:
                # Applies to all bots
                return ad
            for tb in ad.target_bots:
                if tb.bot_id == bot_id:
                    return ad

        return None

    async def add_target_bots(self, ad_id: int, bot_ids: list[int]) -> None:
        """Добавить боты для рассылки"""
        for bot_id in bot_ids:
            ad_bot = AdBot(ad_id=ad_id, bot_id=bot_id)
            self.session.add(ad_bot)
        await self.session.flush()

    async def get_target_bot_ids(self, ad_id: int) -> list[int]:
        """Получить ID ботов для рассылки"""
        stmt = select(AdBot.bot_id).where(AdBot.ad_id == ad_id)
        result = await self.session.execute(stmt)
        return [row[0] for row in result.all()]

    async def update_delivery_stats(self, ad_id: int) -> None:
        """Пересчитать статистику доставки"""
        stmt = select(
            func.count().filter(AdDelivery.is_sent == True),
            func.count().filter(AdDelivery.is_sent == False),
        ).where(AdDelivery.ad_id == ad_id)

        result = await self.session.execute(stmt)
        sent, failed = result.one()

        await self.update(ad_id, sent_count=sent, failed_count=failed)

    async def count_active_post_download(self) -> int:
        """Count active, non-expired post-download ads."""
        from datetime import datetime
        stmt = (
            select(func.count())
            .select_from(Ad)
            .where(
                Ad.ad_type == AdType.POST_DOWNLOAD,
                Ad.is_active,
                (Ad.expires_at.is_(None)) | (Ad.expires_at > datetime.now()),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def count_active_broadcast(self) -> int:
        """Count active broadcast ads."""
        stmt = (
            select(func.count())
            .select_from(Ad)
            .where(
                Ad.ad_type == AdType.BROADCAST,
                Ad.is_active,
                Ad.status.in_([AdStatus.DRAFT, AdStatus.SCHEDULED, AdStatus.SENDING]),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def get_top_post_download_ads(self, limit: int = 3) -> list[dict]:
        """Get top post-download ads by sent_count."""
        stmt = (
            select(Ad.id, Ad.name, Ad.sent_count, Ad.is_active, Ad.expires_at, Ad.duration_days)
            .where(Ad.ad_type == AdType.POST_DOWNLOAD, Ad.is_active)
            .order_by(Ad.sent_count.desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        rows = result.all()
        return [
            {
                "id": r.id,
                "name": r.name,
                "sent_count": r.sent_count or 0,
                "is_active": r.is_active,
                "expires_at": r.expires_at,
                "duration_days": r.duration_days,
            }
            for r in rows
        ]


class AdDeliveryRepository(BaseRepository[AdDelivery]):
    model = AdDelivery

    async def create_delivery(
        self,
        ad_id: int,
        user_id: int,
        bot_id: int,
        telegram_chat_id: int,
        telegram_message_id: int | None = None,
        is_sent: bool = False,
        error_message: str | None = None,
    ) -> AdDelivery:
        return await self.create(
            ad_id=ad_id,
            user_id=user_id,
            bot_id=bot_id,
            telegram_chat_id=telegram_chat_id,
            telegram_message_id=telegram_message_id,
            is_sent=is_sent,
            error_message=error_message,
        )

    async def mark_sent(
        self,
        delivery_id: int,
        telegram_message_id: int,
    ) -> AdDelivery | None:
        return await self.update(
            delivery_id,
            is_sent=True,
            telegram_message_id=telegram_message_id,
        )

    async def mark_failed(
        self,
        delivery_id: int,
        error: str,
    ) -> AdDelivery | None:
        return await self.update(
            delivery_id,
            is_sent=False,
            error_message=error[:256],
        )

    async def get_deliveries_for_deletion(self, ad_id: int) -> list[AdDelivery]:
        """Получить доставки для удаления сообщений"""
        return list(await self.filter(
            ad_id=ad_id,
            is_sent=True,
            telegram_message_id__is_null=False,
        ))
