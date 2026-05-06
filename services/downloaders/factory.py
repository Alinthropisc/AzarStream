from __future__ import annotations

from app.logging import get_logger

from services.downloaders.base import BasePlatformDownloader
from services.downloaders.registry import registry
# Адаптеры платформ на новый BasePlatformDownloader пока не созданы (Шаг 2 миграции).
# Когда они появятся в services/downloaders/platforms/*, раскомментируем импорты ниже.
# from services.downloaders.platforms.instagram import InstagramDownloader
# from services.downloaders.platforms.pinterest import PinterestDownloader
# from services.downloaders.platforms.tiktok import TikTokDownloader
# from services.downloaders.platforms.youtube import YouTubeDownloader

log = get_logger(__name__)


class DownloaderFactory:
    """
    Factory Pattern.
    Создаёт и регистрирует все загрузчики.
    Для добавления новой платформы — один метод.
    """

    def __init__(self) -> None:
        self._setup_platforms()

    def _setup_platforms(self) -> None:
        """Зарегистрировать все платформы.

        На Шаге 2 миграции сюда вернутся:
            YouTubeDownloader(), TikTokDownloader(),
            InstagramDownloader(), PinterestDownloader().
        Сейчас платформы живут на старом BaseDownloader в services/media/*.
        """
        platforms: list[BasePlatformDownloader] = []
        for platform in platforms:
            registry.register(platform)

        log.info(
            "Platform registry initialized (Step 2 pending)",
            count=len(platforms),
            platforms=registry.all_platforms(),
        )

    def get_downloader(self, url: str) -> BasePlatformDownloader:
        """
        Найти подходящий загрузчик по URL.
        Raises ValueError если платформа не поддерживается.
        """
        downloader = registry.find_by_url(url)
        if not downloader:
            raise ValueError(
                f"No downloader found for URL: {url}\n"
                f"Supported platforms: {registry.all_platforms()}"
            )
        return downloader

    def get_by_platform(self, platform: str) -> BasePlatformDownloader:
        downloader = registry.get(platform)
        if not downloader:
            raise ValueError(f"Platform not registered: {platform}")
        return downloader


# Singleton factory
downloader_factory = DownloaderFactory()