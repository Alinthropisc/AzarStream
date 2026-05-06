from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from services.downloaders.types import DownloadOptions


class BaseEngine(ABC):
    """
    Абстрактный базовый класс для движков загрузки.
    Strategy Pattern.
    """

    name: str = "base"
    supported_protocols: frozenset[str] = frozenset()

    @abstractmethod
    async def download(
        self,
        url: str,
        options: DownloadOptions,
        output_dir: Path,
        cookie_file: Path | None = None,
    ) -> list[Path]:
        """Скачать и вернуть пути к файлам"""

    @abstractmethod
    async def extract_info(
        self,
        url: str,
        options: DownloadOptions,
        cookie_file: Path | None = None,
    ) -> dict[str, Any]:
        """Получить метаданные"""

    @abstractmethod
    async def is_available(self) -> bool:
        """Проверить что движок установлен"""

    def __repr__(self) -> str:
        return f"<Engine: {self.name}>"