from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from models.user import TelegramUser


class TelegramUserGlobal(Base, TimestampMixin):
    """
    Глобальный профиль Telegram-пользователя — один на человека,
    независимо от того, в скольких ботах он зарегистрирован.

    Per-bot настройки (язык, is_blocked) живут в TelegramUser.
    """

    __tablename__ = "telegram_user_globals"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)

    username: Mapped[str | None] = mapped_column(String(64), index=True)
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    phone: Mapped[str | None] = mapped_column(String(20))
    bio: Mapped[str | None] = mapped_column(String(512))

    is_banned: Mapped[bool] = mapped_column(default=False, index=True)
    is_premium: Mapped[bool] = mapped_column(default=False)

    total_downloads: Mapped[int] = mapped_column(default=0)

    bot_memberships: Mapped[list["TelegramUser"]] = relationship(
        back_populates="profile",
        cascade="all, delete-orphan",
    )

    @property
    def full_name(self) -> str:
        parts = [self.first_name, self.last_name]
        return " ".join(p for p in parts if p) or "Unknown"