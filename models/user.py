from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from models.bot import Bot
    from models.download import Download
    from models.ads import AdDelivery
    from models.user_global import TelegramUserGlobal


class TelegramUser(Base, TimestampMixin):
    """
    Per-bot настройки пользователя.

    Профиль (имя, username, ban, total_downloads) живёт в TelegramUserGlobal —
    одна запись на человека для всех ботов. Здесь — только то, что меняется
    per-bot: язык интерфейса и факт блокировки бота юзером.
    """

    __tablename__ = "telegram_users"
    __table_args__ = (
        UniqueConstraint("telegram_id", "bot_id", name="uq_telegram_users_telegram_id_bot_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("telegram_user_globals.telegram_id", ondelete="CASCADE"),
        index=True,
    )
    bot_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("bots.bot_id", ondelete="CASCADE"),
        index=True,
    )

    language: Mapped[str] = mapped_column(String(10), default="ru", index=True)
    language_selected: Mapped[bool] = mapped_column(default=False, server_default="0")
    is_blocked: Mapped[bool] = mapped_column(default=False)

    profile: Mapped["TelegramUserGlobal"] = relationship(back_populates="bot_memberships", lazy="joined")
    bot: Mapped["Bot"] = relationship(back_populates="users")
    downloads: Mapped[list["Download"]] = relationship(back_populates="user")
    ad_deliveries: Mapped[list["AdDelivery"]] = relationship(back_populates="user")

    # Convenience proxies for code that still reads profile fields off a per-bot row.
    @property
    def username(self) -> str | None:
        return self.profile.username if self.profile else None

    @property
    def first_name(self) -> str | None:
        return self.profile.first_name if self.profile else None

    @property
    def last_name(self) -> str | None:
        return self.profile.last_name if self.profile else None

    @property
    def is_banned(self) -> bool:
        return self.profile.is_banned if self.profile else False

    @property
    def is_premium(self) -> bool:
        return self.profile.is_premium if self.profile else False

    @property
    def total_downloads(self) -> int:
        return self.profile.total_downloads if self.profile else 0

    @property
    def full_name(self) -> str:
        return self.profile.full_name if self.profile else "Unknown"