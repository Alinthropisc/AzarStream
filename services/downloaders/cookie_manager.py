"""
Cookie Management Service

Управление cookies для разных платформ и ботов.
- Загрузка/удаление cookies
- Проверка срока действия
- Привязка к конкретным ботам
"""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.logging import get_logger

log = get_logger("service.cookies")

# Путь для хранения cookies
COOKIES_DIR = Path("storage/cookies")
COOKIES_DIR.mkdir(parents=True, exist_ok=True)

# Срок действия cookies по умолчанию (дней)
DEFAULT_COOKIE_TTL_DAYS = 60

# Платформы для которых имеет смысл хранить cookies. Можно добавить любую
# платформу yt-dlp — формат файла единый (Netscape). Список используется
# админ-UI как dropdown и cookie_manager как whitelist при загрузке.
SUPPORTED_PLATFORMS = [
    "youtube",
    "instagram",
    "tiktok",
    "pinterest",
    "vk",
    "twitter",     # Twitter / X
    "facebook",
    "reddit",
    "soundcloud",
    "vimeo",
    "twitch",
    "dailymotion",
    "tumblr",
    "snapchat",
    "threads",
    "likee",
]


@dataclass
class CookieInfo:
    """Информация о cookies файле."""

    platform: str
    bot_id: Optional[int] = None  # None = глобальные cookies
    account_name: Optional[str] = None  # Имя аккаунта
    file_path: Path = field(default_factory=Path)
    created_at: float = 0.0
    file_size: int = 0
    cookie_count: int = 0
    is_valid: bool = False
    expires_at: float = 0.0

    @property
    def age_days(self) -> float:
        """Возраст cookies в днях."""
        if not self.created_at:
            return 0
        return (time.time() - self.created_at) / 86400

    @property
    def days_until_expiry(self) -> float:
        """Дней до истечения."""
        if not self.expires_at:
            return 0
        return (self.expires_at - time.time()) / 86400

    @property
    def is_expired(self) -> bool:
        """Истекли ли cookies."""
        return self.days_until_expiry <= 0 if self.expires_at else False

    @property
    def is_expiring_soon(self) -> bool:
        """Истекают ли в ближайшие 7 дней."""
        return 0 < self.days_until_expiry <= 7

    def to_dict(self) -> dict:
        """Конвертация в словарь для шаблонов."""
        return {
            "platform": self.platform,
            "bot_id": self.bot_id,
            "account_name": self.account_name or "Default",
            "file_path": str(self.file_path),
            "created_at": self.created_at,
            "file_size": self.file_size,
            "cookie_count": self.cookie_count,
            "is_valid": self.is_valid,
            "expires_at": self.expires_at,
            "age_days": round(self.age_days, 1),
            "days_until_expiry": round(self.days_until_expiry, 1),
            "is_expired": self.is_expired,
            "is_expiring_soon": self.is_expiring_soon,
        }


class CookieManager:
    """Сервис для управления cookies."""

    def __init__(self):
        self.cookies_dir = COOKIES_DIR
        self.default_ttl_days = DEFAULT_COOKIE_TTL_DAYS

    def _get_cookie_filename(self, platform: str, bot_id: int = None, account_name: str = None) -> str:
        """Генерирует имя файла cookies."""
        parts = [platform]
        if bot_id:
            parts.append(f"bot{bot_id}")
        if account_name:
            parts.append(account_name.replace(" ", "_"))
        return "_".join(parts) + ".txt"

    def get_cookie_file_path(
        self,
        platform: str,
        bot_id: int = None,
        account_name: str = None,
    ) -> Path:
        """Получить путь к файлу cookies."""
        filename = self._get_cookie_filename(platform, bot_id, account_name)
        return self.cookies_dir / filename

    def save_cookies(
        self,
        platform: str,
        cookies_content: str,
        bot_id: int = None,
        account_name: str = None,
        ttl_days: int = None,
    ) -> CookieInfo:
        """
        Сохранить cookies.

        Args:
            platform: instagram, youtube, etc.
            cookies_content: содержимое файла cookies в Netscape формате
            bot_id: ID бота (для мульти-ботов)
            account_name: имя аккаунта
            ttl_days: срок действия в днях
        """
        file_path = self.get_cookie_file_path(platform, bot_id, account_name)
        file_path.write_text(cookies_content)

        # Парсим cookies для подсчета
        cookie_count = self._count_cookies(cookies_content)
        ttl = ttl_days or self.default_ttl_days

        info = CookieInfo(
            platform=platform,
            bot_id=bot_id,
            account_name=account_name,
            file_path=file_path,
            created_at=time.time(),
            file_size=len(cookies_content),
            cookie_count=cookie_count,
            is_valid=True,
            expires_at=time.time() + (ttl * 86400),
        )

        # Сохраняем метаданные
        self._save_metadata(info)

        log.info(
            "Cookies saved",
            platform=platform,
            bot_id=bot_id,
            account=account_name,
            cookie_count=cookie_count,
            ttl_days=ttl,
        )

        return info

    def load_cookies(
        self,
        platform: str,
        bot_id: int = None,
        account_name: str = None,
    ) -> str | None:
        """Загрузить cookies из файла."""
        file_path = self.get_cookie_file_path(platform, bot_id, account_name)
        if file_path.exists():
            return file_path.read_text()
        return None

    def delete_cookies(
        self,
        platform: str,
        bot_id: int = None,
        account_name: str = None,
    ) -> bool:
        """Удалить cookies."""
        file_path = self.get_cookie_file_path(platform, bot_id, account_name)
        if file_path.exists():
            file_path.unlink()
            # Удаляем метаданные
            meta_path = file_path.with_suffix(".meta.json")
            if meta_path.exists():
                meta_path.unlink()
            log.info("Cookies deleted", platform=platform, bot_id=bot_id, account=account_name)
            return True
        return False

    def get_cookie_info(
        self,
        platform: str,
        bot_id: int = None,
        account_name: str = None,
    ) -> CookieInfo | None:
        """Получить информацию о cookies."""
        file_path = self.get_cookie_file_path(platform, bot_id, account_name)
        if not file_path.exists():
            return None

        # Пробуем загрузить метаданные
        meta = self._load_metadata(file_path)
        if meta:
            return CookieInfo(**meta)

        # Если метаданных нет - создаем из файла
        content = file_path.read_text()
        cookie_count = self._count_cookies(content)

        return CookieInfo(
            platform=platform,
            bot_id=bot_id,
            account_name=account_name,
            file_path=file_path,
            created_at=file_path.stat().st_mtime,
            file_size=len(content),
            cookie_count=cookie_count,
            is_valid=True,
            expires_at=file_path.stat().st_mtime + (self.default_ttl_days * 86400),
        )

    def list_all_cookies(self) -> list[CookieInfo]:
        """Получить все cookies файлы."""
        cookies = []
        for file_path in self.cookies_dir.glob("*.txt"):
            # Парсим имя файла
            name = file_path.stem
            parts = name.split("_")

            platform = parts[0]
            bot_id = None
            account_name = None

            for part in parts[1:]:
                if part.startswith("bot"):
                    try:
                        bot_id = int(part[3:])
                    except ValueError:
                        pass
                else:
                    account_name = part.replace("_", " ")

            info = self.get_cookie_info(platform, bot_id, account_name)
            if info:
                cookies.append(info)

        return sorted(cookies, key=lambda x: x.created_at, reverse=True)

    def get_cookies_for_bot(self, bot_id: int, platform: str) -> str | None:
        """
        Получить cookies для конкретного бота.
        Если нет cookies для бота - возвращает глобальные.
        """
        # Сначала ищем cookies для бота
        content = self.load_cookies(platform, bot_id=bot_id)
        if content:
            return content

        # Fallback на глобальные cookies
        return self.load_cookies(platform)

    def check_expiring_cookies(self, days_threshold: int = 7) -> list[CookieInfo]:
        """Найти cookies которые истекают."""
        expiring = []
        for info in self.list_all_cookies():
            if info.is_expiring_soon or info.is_expired:
                expiring.append(info)
        return expiring

    def _count_cookies(self, content: str) -> int:
        """Подсчитать количество cookies в файле."""
        count = 0
        for line in content.split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                # Netscape формат: domain\tflag\tpath\tsecure\texpiry\tname\tvalue
                parts = line.split("\t")
                if len(parts) >= 6:
                    count += 1
        return count

    def _save_metadata(self, info: CookieInfo):
        """Сохранить метаданные в JSON."""
        import json
        meta_path = info.file_path.with_suffix(".meta.json")
        meta_path.write_text(json.dumps(info.to_dict(), indent=2))

    def _load_metadata(self, file_path: Path) -> dict | None:
        """Загрузить метаданные из JSON."""
        import json
        meta_path = file_path.with_suffix(".meta.json")
        if meta_path.exists():
            try:
                return json.loads(meta_path.read_text())
            except Exception:
                return None
        return None


# Singleton
cookie_manager = CookieManager()
