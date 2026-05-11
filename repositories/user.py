from sqlalchemy import select, func, or_

from repositories.base import BaseRepository
from models import TelegramUser, TelegramUserGlobal


class UserRepository(BaseRepository[TelegramUser]):
    """
    Repository для per-bot записей пользователя.

    Профиль (имя, username, ban, total_downloads) живёт в TelegramUserGlobal
    и подгружается через joined load на `TelegramUser.profile`.
    """

    model = TelegramUser

    # ── basic lookups ────────────────────────────────────────────────────────

    async def get_by_telegram_id(self, telegram_id: int, bot_id: int) -> TelegramUser | None:
        return await self.get_one(telegram_id=telegram_id, bot_id=bot_id)

    async def get_global(self, telegram_id: int) -> TelegramUserGlobal | None:
        return await self.session.get(TelegramUserGlobal, telegram_id)

    # ── creation / upsert ────────────────────────────────────────────────────

    async def get_or_create(
        self,
        telegram_id: int,
        bot_id: int,
        *,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        language: str = "ru",
        **_ignored,
    ) -> tuple[TelegramUser, bool]:
        """
        Upsert: global профиль + per-bot запись.
        Returns: (per-bot row, created_per_bot_row: bool)
        """
        # 1. Global profile
        profile = await self.session.get(TelegramUserGlobal, telegram_id)
        if profile is None:
            profile = TelegramUserGlobal(
                telegram_id=telegram_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
            )
            self.session.add(profile)
            await self.session.flush()
        else:
            if username is not None:
                profile.username = username
            if first_name is not None:
                profile.first_name = first_name
            if last_name is not None:
                profile.last_name = last_name

        # 2. Per-bot row
        per_bot = await self.get_by_telegram_id(telegram_id, bot_id)
        created = False
        if per_bot is None:
            per_bot = TelegramUser(
                telegram_id=telegram_id,
                bot_id=bot_id,
                language=language,
                profile=profile,  # явно — иначе async lazy-load после flush
            )
            self.session.add(per_bot)
            await self.session.flush()
            created = True
        elif per_bot.profile is None:
            per_bot.profile = profile

        return per_bot, created

    async def create(self, **kwargs):
        """
        Override: создание per-bot записи также создаёт/обновляет global профиль,
        если в kwargs пришли поля профиля (для обратной совместимости со старыми
        вызовами).
        """
        profile_fields = {
            k: kwargs.pop(k, None)
            for k in ("username", "first_name", "last_name", "phone", "bio", "is_premium")
        }
        profile: TelegramUserGlobal | None = None
        telegram_id = kwargs.get("telegram_id")
        if telegram_id is not None:
            profile = await self.session.get(TelegramUserGlobal, telegram_id)
            if profile is None:
                profile = TelegramUserGlobal(
                    telegram_id=telegram_id,
                    **{k: v for k, v in profile_fields.items() if v is not None},
                )
                self.session.add(profile)
            else:
                for k, v in profile_fields.items():
                    if v is not None:
                        setattr(profile, k, v)
            await self.session.flush()

        # Создаём per-bot напрямую с явной relationship, чтобы избежать
        # async lazy-load на post-flush обращении к .profile.
        instance = TelegramUser(**kwargs)
        if profile is not None:
            instance.profile = profile
        self.session.add(instance)
        await self.session.flush()
        return instance

    async def update(self, id: int, **kwargs):
        """
        Override: поля профиля (username, first_name, is_banned, и т.д.) идут
        в TelegramUserGlobal, per-bot поля — в TelegramUser. Базовый update
        делает setattr на per-bot модели, что валится на read-only proxy-property.
        """
        PROFILE_FIELDS = {
            "username", "first_name", "last_name", "phone", "bio",
            "is_banned", "is_premium", "total_downloads",
        }

        instance = await self.get_by_id(id)
        if instance is None:
            return None

        profile_updates = {k: kwargs.pop(k) for k in list(kwargs) if k in PROFILE_FIELDS}

        for key, value in kwargs.items():
            if hasattr(instance, key):
                setattr(instance, key, value)

        if profile_updates and instance.profile is not None:
            for k, v in profile_updates.items():
                setattr(instance.profile, k, v)

        await self.session.flush()
        await self.session.refresh(instance)
        self._log.debug("Updated", id=id)
        return instance

    # ── listings & filters ───────────────────────────────────────────────────

    async def get_users_by_bot(
        self,
        bot_id: int,
        language: str | None = None,
        exclude_blocked: bool = True,
        offset: int = 0,
        limit: int = 100,
    ) -> list[TelegramUser]:
        stmt = (
            select(TelegramUser)
            .join(TelegramUserGlobal, TelegramUser.telegram_id == TelegramUserGlobal.telegram_id)
            .where(TelegramUser.bot_id == bot_id)
        )
        if language:
            stmt = stmt.where(TelegramUser.language == language)
        if exclude_blocked:
            stmt = stmt.where(TelegramUser.is_blocked == False, TelegramUserGlobal.is_banned == False)
        stmt = stmt.offset(offset).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_users_for_broadcast(
        self,
        bot_ids: list[int],
        language: str | None = None,
    ) -> list[TelegramUser]:
        stmt = (
            select(TelegramUser)
            .join(TelegramUserGlobal, TelegramUser.telegram_id == TelegramUserGlobal.telegram_id)
            .where(
                TelegramUser.bot_id.in_(bot_ids),
                TelegramUser.is_blocked == False,
                TelegramUserGlobal.is_banned == False,
            )
        )
        if language:
            stmt = stmt.where(TelegramUser.language == language)

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_active_in_bot(self, bot_id: int) -> int:
        """Юзеры бота, не забаненные глобально."""
        stmt = (
            select(func.count(TelegramUser.id))
            .join(TelegramUserGlobal, TelegramUser.telegram_id == TelegramUserGlobal.telegram_id)
            .where(TelegramUser.bot_id == bot_id, TelegramUserGlobal.is_banned == False)
        )
        result = await self.session.execute(stmt)
        return int(result.scalar() or 0)

    async def get_language_stats(self, bot_id: int | None = None) -> dict[str, int]:
        stmt = select(TelegramUser.language, func.count(TelegramUser.id)).group_by(TelegramUser.language)
        if bot_id:
            stmt = stmt.where(TelegramUser.bot_id == bot_id)
        result = await self.session.execute(stmt)
        return dict(result.all())

    async def count_by_bot(self, bot_id: int) -> int:
        return await self.count_active_in_bot(bot_id)

    # ── download counter (global) ────────────────────────────────────────────

    async def increment_downloads(self, user_id: int) -> None:
        """Инкрементирует глобальный счётчик скачиваний пользователя."""
        per_bot = await self.get_by_id(user_id)
        if per_bot is None:
            return
        profile = await self.session.get(TelegramUserGlobal, per_bot.telegram_id)
        if profile:
            profile.total_downloads = (profile.total_downloads or 0) + 1
            await self.session.flush()

    # ── unique listing for admin UI ──────────────────────────────────────────

    async def list_unique_telegram_users(
        self,
        offset: int = 0,
        limit: int = 20,
        search: str | None = None,
        sort: str = "downloads",
    ) -> list[dict]:
        """
        Один глобальный профиль = одна строка. Добавляем bots_count и
        blocked_bots_count (агрегаты по per-bot записям).

        sort: downloads (default) | newest | recent | oldest
        """
        from sqlalchemy import case
        bots_count = func.count(TelegramUser.id).label("bots_count")
        # CASE WHEN ... THEN 1 ELSE 0 — портабельно (MySQL + SQLite + Postgres),
        # в отличие от MySQL-only func.if_().
        blocked_count = func.sum(
            case((TelegramUser.is_blocked, 1), else_=0)
        ).label("blocked_count")

        stmt = (
            select(TelegramUserGlobal, bots_count, blocked_count)
            .outerjoin(TelegramUser, TelegramUser.telegram_id == TelegramUserGlobal.telegram_id)
            .group_by(TelegramUserGlobal.telegram_id)
        )

        if search:
            like = f"%{search}%"
            stmt = stmt.where(
                or_(
                    TelegramUserGlobal.username.ilike(like),
                    TelegramUserGlobal.first_name.ilike(like),
                    TelegramUserGlobal.last_name.ilike(like),
                )
            )

        order_map = {
            "downloads": TelegramUserGlobal.total_downloads.desc(),
            "newest": TelegramUserGlobal.created_at.desc(),
            "oldest": TelegramUserGlobal.created_at.asc(),
            "recent": TelegramUserGlobal.updated_at.desc(),
        }
        stmt = stmt.order_by(order_map.get(sort, order_map["downloads"])).offset(offset).limit(limit)
        result = await self.session.execute(stmt)

        rows: list[dict] = []
        for profile, n_bots, n_blocked in result.all():
            rows.append({
                "user": profile,
                "bots_count": int(n_bots or 0),
                "total_downloads": int(profile.total_downloads or 0),
                "is_blocked": bool(n_blocked and n_blocked > 0),
            })
        return rows

    async def count_unique_telegram_users(self, search: str | None = None) -> int:
        stmt = select(func.count(TelegramUserGlobal.telegram_id))
        if search:
            like = f"%{search}%"
            stmt = stmt.where(
                or_(
                    TelegramUserGlobal.username.ilike(like),
                    TelegramUserGlobal.first_name.ilike(like),
                    TelegramUserGlobal.last_name.ilike(like),
                )
            )
        result = await self.session.execute(stmt)
        return int(result.scalar() or 0)

    # ── moderation ───────────────────────────────────────────────────────────

    async def set_global_ban(self, telegram_id: int, banned: bool) -> TelegramUserGlobal | None:
        profile = await self.session.get(TelegramUserGlobal, telegram_id)
        if profile is None:
            return None
        profile.is_banned = banned
        await self.session.flush()
        return profile
