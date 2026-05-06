"""
Новая архитектура загрузчиков (Шаг 2 миграции).

Сейчас этот пакет содержит:
- Старая система: downloader.py (DownloadService, BaseDownloader, MediaPlatform) — продакшен
- cookie_manager.py — общий, используется обеими системами
- Новая система (паттерны): base.py, base_engine.py, aria2c_engine.py, ytdlp_engine.py,
  ffmpeg_engine.py, factory.py, registry.py, protocols.py, types.py — пока не подключена
  к рантайму, миграция платформ — Шаг 2.
"""

from services.downloaders.types import (
    Platform,
    Quality,
    MediaType,
    DownloadOptions,
    MediaInfo,
    DownloadResult,
    DownloadStatus,
)

__all__ = [
    "Platform",
    "Quality",
    "MediaType",
    "DownloadOptions",
    "MediaInfo",
    "DownloadResult",
    "DownloadStatus",
]
