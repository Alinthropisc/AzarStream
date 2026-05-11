from __future__ import annotations

import re
import tempfile
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar

from app.logging import get_logger

from services.downloaders.cookie_manager import cookie_manager
from services.downloaders.base_engine import BaseEngine
from services.downloaders.ffmpeg_engine import ffmpeg
from services.downloaders.ytdlp_engine import YtDlpEngine
from services.downloaders.types import (
    DownloadOptions,
    DownloadResult,
    DownloadStatus,
    MediaInfo,
    MediaType,
    Platform,
)

log = get_logger(__name__)

TMP_ROOT = Path(__file__).resolve().parent.parent.parent / "storage" / "temp"


class BasePlatformDownloader(ABC):
    """
    Template Method Pattern.

    Базовый класс для всех платформ.
    Определяет скелет алгоритма:
        1. can_handle()    — проверка URL
        2. extract_info()  — получить метаданные
        3. _pre_download() — хук до загрузки (переопределяемый)
        4. _do_download()  — основная загрузка (переопределяемый)
        5. _post_process() — хук после загрузки (переопределяемый)
        6. _build_result() — собрать DownloadResult

    Подкласс ОБЯЗАН реализовать:
        - platform_name
        - supported_domains
        - _build_engine()  — какой движок использовать

    Подкласс МОЖЕТ переопределить:
        - _pre_download()
        - _post_process()
        - _build_ydl_extra_opts()
        - _detect_media_type()
    """

    # --- Обязательные атрибуты ---
    platform_name: ClassVar[str]
    supported_domains: ClassVar[frozenset[str]]

    # --- Настройки по умолчанию (можно переопределить) ---
    default_engine_class: ClassVar[type[BaseEngine]] = YtDlpEngine
    use_cookies: ClassVar[bool] = True

    def __init__(self) -> None:
        self._engine: BaseEngine = self._build_engine()
        self._log = log.bind(platform=self.platform_name)

    # ------------------------------------------------------------------
    # Template Method — главный алгоритм
    # ------------------------------------------------------------------

    async def download(
            self,
            url: str,
            options: DownloadOptions | None = None,
    ) -> DownloadResult:
        """
        Шаблонный метод. Не переопределять в подклассах.
        Вместо этого — переопределять _pre/_do/_post хуки.
        """
        opts = options or DownloadOptions()
        result = DownloadResult(
            original_url=url,
            platform=self._get_platform_enum(),
            status=DownloadStatus.PENDING,
        )
        tmp_dir = self._make_tmp_dir()

        try:
            # Шаг 1: pre-download хук (валидация, специфичная подготовка)
            result.status = DownloadStatus.EXTRACTING
            await self._pre_download(url, opts, result)

            # Шаг 2: получить метаданные
            info = await self.extract_info(url, opts)
            self._apply_info_to_result(info, result)

            # Шаг 3: загрузка
            result.status = DownloadStatus.DOWNLOADING
            cookie_file = self._get_cookie_file() if opts.use_cookies else None
            paths = await self._do_download(url, opts, tmp_dir, cookie_file)

            # Шаг 4: post-process хук (конвертация, слияние и т.д.)
            result.status = DownloadStatus.PROCESSING
            paths = await self._post_process(paths, opts, result)

            # Шаг 5: финализация
            result.mark_done(paths)
            self._log.info("Download completed", url=url, files=len(paths))

        except Exception as exc:
            self._log.error("Download failed", url=url, error=str(exc))
            result.mark_failed(str(exc))
        finally:
            # tmp_dir очищается снаружи (DownloadService после отправки)
            result._tmp_dir = tmp_dir  # type: ignore[attr-defined]

        return result

    async def extract_info(
            self,
            url: str,
            options: DownloadOptions | None = None,
    ) -> dict[str, Any]:
        """Получить метаданные (можно переопределить для платформы)"""
        opts = options or DownloadOptions()
        cookie_file = self._get_cookie_file() if opts.use_cookies else None
        return await self._engine.extract_info(url, opts, cookie_file=cookie_file)

    def can_handle(self, url: str) -> bool:
        """Проверить, умеет ли этот загрузчик работать с URL"""
        try:
            from urllib.parse import urlparse
            host = urlparse(url).netloc.lower()
            return any(domain in host for domain in self.supported_domains)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Хуки — переопределяются в подклассах (необязательно)
    # ------------------------------------------------------------------

    async def _pre_download(
            self,
            url: str,
            options: DownloadOptions,
            result: DownloadResult,
    ) -> None:
        """Хук: действия до загрузки. По умолчанию — ничего."""

    async def _do_download(
            self,
            url: str,
            options: DownloadOptions,
            output_dir: Path,
            cookie_file: Path | None,
    ) -> list[Path]:
        """Основная загрузка. Можно переопределить для платформы."""
        return await self._engine.download(
            url=url,
            options=options,
            output_dir=output_dir,
            cookie_file=cookie_file,
        )

    async def _post_process(
            self,
            paths: list[Path],
            options: DownloadOptions,
            result: DownloadResult,
    ) -> list[Path]:
        """
        Хук: постобработка (конвертация, слияние).
        По умолчанию — возвращает пути без изменений.
        """
        return paths

    def _build_engine(self) -> BaseEngine:
        """Создать движок. Переопределить если нужен другой движок."""
        return self.default_engine_class()

    def _get_platform_enum(self) -> Platform:
        try:
            return Platform(self.platform_name.lower())
        except ValueError:
            return Platform.UNKNOWN

    def _get_cookie_file(self) -> Path | None:
        if not self.use_cookies:
            return None
        return cookie_manager.get_cookie_file(self.platform_name)

    def _apply_info_to_result(
            self,
            info: dict[str, Any],
            result: DownloadResult,
    ) -> None:
        result.title = info.get("title", "")
        result.uploader = info.get("uploader", "")
        result.duration = info.get("duration", 0) or 0
        result.filesize = info.get("filesize", 0) or 0
        result.thumbnail_url = info.get("thumbnail", "")

        # Определить тип медиа
        result.media_type = self._detect_media_type(info)

    def _detect_media_type(self, info: dict[str, Any]) -> MediaType:
        """Определить тип медиа из метаданных. Переопределяется в подклассах."""
        if info.get("_type") == "playlist":
            return MediaType.VIDEO
        vcodec = info.get("vcodec", "")
        acodec = info.get("acodec", "")
        if vcodec and vcodec != "none":
            return MediaType.VIDEO
        if acodec and acodec != "none":
            return MediaType.AUDIO
        return MediaType.VIDEO

    @staticmethod
    def _make_tmp_dir() -> Path:
        tmp = TMP_ROOT / str(uuid.uuid4())
        tmp.mkdir(parents=True, exist_ok=True)
        return tmp

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} platform={self.platform_name}>"