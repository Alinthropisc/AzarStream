from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.logging import get_logger
import yt_dlp

from services.downloaders.base_engine import BaseEngine
from services.downloaders.types import DownloadOptions, Quality

log = get_logger(__name__)

QUALITY_FORMAT_MAP: dict[Quality, str] = {
    Quality.BEST:   "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
    Quality.Q_2160: "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/best[height<=2160]",
    Quality.Q_1440: "bestvideo[height<=1440][ext=mp4]+bestaudio[ext=m4a]/best[height<=1440]",
    Quality.Q_1080: "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
    Quality.Q_720:  "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]",
    Quality.Q_480:  "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]",
    Quality.Q_360:  "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360]",
    Quality.AUDIO:  "bestaudio[ext=m4a]/bestaudio",
}


class YtDlpEngine(BaseEngine):
    """
    Strategy: загрузка через yt-dlp.
    Поддерживает yt-dlp-ejs и другие форки через подмену бинаря.
    """

    name = "yt-dlp"
    supported_protocols = frozenset({"https", "http", "m3u8", "dash"})

    def __init__(self, use_ejs: bool = False) -> None:
        self._use_ejs = use_ejs
        # Если нужен yt-dlp-ejs — патчим путь к бинарю
        if use_ejs:
            try:
                import yt_dlp_ejs  # noqa: F401 - регистрирует плагины
                log.info("yt-dlp-ejs plugins loaded")
            except ImportError:
                log.warning("yt-dlp-ejs not installed, using standard yt-dlp")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def download(
        self,
        url: str,
        options: DownloadOptions,
        output_dir: Path,
        cookie_file: Path | None = None,
    ) -> list[Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        ydl_opts = self._build_opts(options, output_dir, cookie_file)

        downloaded: list[Path] = []

        def _progress_hook(d: dict[str, Any]) -> None:
            if d["status"] == "finished":
                fp = d.get("filename") or d.get("info_dict", {}).get("_filename")
                if fp:
                    downloaded.append(Path(fp))

        ydl_opts["progress_hooks"] = [_progress_hook]

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._run_download, url, ydl_opts)

        # Fallback: собрать файлы из директории если хуки не сработали
        if not downloaded:
            downloaded = list(output_dir.glob("*.*"))

        log.info("yt-dlp download complete", url=url, files=len(downloaded))
        return downloaded

    async def extract_info(
        self,
        url: str,
        options: DownloadOptions,
        cookie_file: Path | None = None,
    ) -> dict[str, Any]:
        ydl_opts = self._build_opts(options, output_dir=None, cookie_file=cookie_file)
        ydl_opts["skip_download"] = True

        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, self._run_extract, url, ydl_opts)
        return info or {}

    async def is_available(self) -> bool:
        try:
            import yt_dlp  # noqa: F401
            return True
        except ImportError:
            return False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_opts(
        self,
        options: DownloadOptions,
        output_dir: Path | None,
        cookie_file: Path | None,
    ) -> dict[str, Any]:
        fmt = (
            "bestaudio/best"
            if options.audio_only
            else QUALITY_FORMAT_MAP.get(options.quality, QUALITY_FORMAT_MAP[Quality.BEST])
        )

        opts: dict[str, Any] = {
            "format": fmt,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "retries": 3,
            "fragment_retries": 5,
            "concurrent_fragment_downloads": 4,
            "merge_output_format": "mp4",
            "postprocessors": [],
        }

        if output_dir:
            opts["outtmpl"] = str(output_dir / "%(title).80s.%(ext)s")

        if cookie_file:
            opts["cookiefile"] = str(cookie_file)

        if options.proxy:
            opts["proxy"] = options.proxy

        if options.max_filesize_mb:
            opts["max_filesize"] = options.max_filesize_mb * 1024 * 1024

        if options.embed_metadata:
            opts["postprocessors"].append({"key": "FFmpegMetadata"})

        if options.embed_thumbnail:
            opts["writethumbnail"] = True
            opts["postprocessors"].append({"key": "EmbedThumbnail"})

        if options.audio_only:
            opts["postprocessors"].append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            })

        # Внешний загрузчик Aria2C
        if options.use_aria2c:
            opts["external_downloader"] = "aria2c"
            opts["external_downloader_args"] = {
                "aria2c": [
                    "--max-connection-per-server=16",
                    "--split=16",
                    "--min-split-size=1M",
                    "--max-concurrent-downloads=4",
                    "--continue=true",
                    "--auto-file-renaming=false",
                ]
            }

        return opts

    def _run_download(self, url: str, opts: dict[str, Any]) -> None:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

    def _run_extract(self, url: str, opts: dict[str, Any]) -> dict[str, Any] | None:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)