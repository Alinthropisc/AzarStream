from __future__ import annotations

from abc import abstractmethod
from pathlib import Path
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from services.downloaders.types import DownloadOptions, DownloadResult


@runtime_checkable
class DownloadEngine(Protocol):
    """Strategy interface для движков загрузки"""

    name: str
    supported_protocols: frozenset[str]

    async def download(
        self,
        url: str,
        options: DownloadOptions,
        output_dir: Path,
    ) -> list[Path]:
        """Скачать медиа, вернуть пути к файлам"""
        ...

    async def extract_info(
        self,
        url: str,
        options: DownloadOptions,
    ) -> dict[str, Any]:
        """Извлечь метаданные без скачивания"""
        ...

    async def is_available(self) -> bool:
        """Проверить доступность движка"""
        ...


@runtime_checkable
class MediaProcessor(Protocol):
    """Strategy interface для обработки медиа (FFmpeg и т.д.)"""

    name: str

    async def process(
        self,
        input_paths: list[Path],
        options: DownloadOptions,
    ) -> list[Path]:
        """Обработать медиа файлы"""
        ...

    async def is_available(self) -> bool:
        ...


@runtime_checkable
class CookieProvider(Protocol):
    """Interface для работы с куками"""

    def get_cookie_file(self, platform: str) -> Path | None:
        ...

    def get_cookie_dict(self, platform: str) -> dict[str, str]:
        ...


@runtime_checkable
class PlatformDownloader(Protocol):
    """Interface который должен реализовать каждый платформенный загрузчик"""

    platform_name: str
    supported_domains: frozenset[str]

    async def download(
        self,
        url: str,
        options: DownloadOptions,
    ) -> DownloadResult:
        ...

    async def extract_info(
        self,
        url: str,
        options: DownloadOptions,
    ) -> dict[str, Any]:
        ...

    def can_handle(self, url: str) -> bool:
        ...