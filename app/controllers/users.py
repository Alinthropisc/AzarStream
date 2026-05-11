from litestar import Controller, get, post
from litestar.response import Template, Redirect
from litestar.di import Provide
from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import get_session
from models import TelegramUser, TelegramUserGlobal
from repositories import UserRepository
from app.middleware.auth import admin_guard


class UserController(Controller):
    path = "/admin/users"
    guards = [admin_guard]
    dependencies = {"session": Provide(get_session)}

    @get("/", name="users:list")
    async def list_users(
        self,
        session: AsyncSession,
        page: int = 1,
        limit: int = 20,
        search: str | None = None,
        sort: str = "downloads",
    ) -> Template:
        repo = UserRepository(session)

        if sort not in ("downloads", "newest", "oldest", "recent"):
            sort = "downloads"

        users = await repo.list_unique_telegram_users(
            offset=(page - 1) * limit,
            limit=limit,
            search=search,
            sort=sort,
        )
        total = await repo.count_unique_telegram_users(search=search)

        return Template(
            template_name="admin/users/list.html",
            context={
                "users": users,
                "page": page,
                "limit": limit,
                "total": total,
                "search": search,
                "sort": sort,
            }
        )

    @get("/{telegram_id:int}", name="users:detail")
    async def user_detail(
        self,
        session: AsyncSession,
        telegram_id: int,
    ) -> Template:
        profile = await session.get(TelegramUserGlobal, telegram_id)

        bots_count = 0
        blocked_count = 0
        if profile is not None:
            agg = await session.execute(
                select(
                    func.count(TelegramUser.id),
                    func.sum(case((TelegramUser.is_blocked, 1), else_=0)),
                ).where(TelegramUser.telegram_id == telegram_id)
            )
            n_bots, n_blocked = agg.one()
            bots_count = int(n_bots or 0)
            blocked_count = int(n_blocked or 0)

        return Template(
            template_name="admin/users/detail.html",
            context={
                "user": profile,
                "bots_count": bots_count,
                "blocked_count": blocked_count,
            }
        )

    @post("/{telegram_id:int}/toggle-ban", name="users:toggle_ban")
    async def toggle_ban(self, session: AsyncSession, telegram_id: int) -> Redirect:
        repo = UserRepository(session)
        profile = await session.get(TelegramUserGlobal, telegram_id)
        if profile:
            new_state = not profile.is_banned
            await repo.set_global_ban(telegram_id, banned=new_state)
            await session.commit()
            return Redirect(path=f"/admin/users?message=User {'banned' if new_state else 'unbanned'}")
        return Redirect(path="/admin/users")

    @post("/{telegram_id:int}/delete", name="users:delete")
    async def delete_user(self, session: AsyncSession, telegram_id: int) -> Redirect:
        await session.execute(delete(TelegramUserGlobal).where(TelegramUserGlobal.telegram_id == telegram_id))
        await session.commit()
        return Redirect(path="/admin/users?message=User deleted")
