from __future__ import annotations

import asyncio
import gc
import shutil
from pathlib import Path
from typing import Any

from app.logging import get_logger

from services.downloaders.cookie_manager import cookie_manager
from services.downloaders.factory import downloader_factory
from services.downloaders.registry import registry
from services.downloaders.types import (
    DownloadOptions,
    DownloadResult,
    DownloadStatus,
    Platform,
    Quality,
)
from app.config import settings
from app.logging import get_logger

log = get_logger("service.DownloadService")


class DownloadService:
    """
    Facade Pattern.
    Единая точка входа для всей системы загрузки.
    Скрывает сложность фабрики, реестра и платформ.
    """

    _instance: "DownloadService | None" = None

    def __new__(cls) -> "DownloadService":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def download(
        self,
        url: str,
        quality: Quality = Quality.BEST,
        audio_only: bool = False,
        proxy: str | None = None,
        **kwargs: Any,
    ) -> DownloadResult:
        """
        Главный метод. Автоматически определяет платформу и скачивает.
        """
        options = DownloadOptions(
            quality=quality,
            audio_only=audio_only,
            proxy=proxy,
            **kwargs,
        )

        try:
            downloader = downloader_factory.get_downloader(url)
            log.info(
                "Download started",
                url=url,
                platform=downloader.platform_name,
                quality=quality,
            )
            result = await downloader.download(url, options)

        except ValueError as exc:
            # Платформа не поддерживается
            result = DownloadResult(
                original_url=url,
                platform=Platform.UNKNOWN,
                status=DownloadStatus.FAILED,
                error=str(exc),
            )
        except Exception as exc:
            log.exception("Unexpected download error", url=url)
            result = DownloadResult(
                original_url=url,
                platform=Platform.UNKNOWN,
                status=DownloadStatus.FAILED,
                error=str(exc),
            )

        return result

    async def extract_info(
        self,
        url: str,
        options: DownloadOptions | None = None,
    ) -> dict[str, Any]:
        """Получить метаданные без скачивания"""
        downloader = downloader_factory.get_downloader(url)
        return await downloader.extract_info(url, options)

    async def cleanup(self, result: DownloadResult) -> None:
        """Удалить временные файлы после отправки"""
        tmp_dir: Path | None = getattr(result, "_tmp_dir", None)
        if tmp_dir and tmp_dir.exists():
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                log.debug("Temp dir cleaned", path=str(tmp_dir))
            except Exception as exc:
                log.warning("Cleanup failed", error=str(exc))

        # Также почистим конкретные файлы
        for path in result.file_paths:
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                pass

        gc.collect()

    def get_supported_platforms(self) -> list[str]:
        return registry.all_platforms()

    def get_available_cookies(self) -> list[str]:
        return cookie_manager.list_available()

    def is_supported(self, url: str) -> bool:
        return registry.find_by_url(url) is not None


# Singleton
download_service = DownloadService()