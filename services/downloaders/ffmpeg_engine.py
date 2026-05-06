from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from app.logging import get_logger

# FFmpeg processor работает без BaseEngine/DownloadOptions — используется как помощник
# в post-processing pipeline


log = get_logger(__name__)


class FFmpegProcessor:
    """
    Не движок загрузки, а процессор медиа.
    Используется как вспомогательный инструмент в Pipeline.
    """

    name = "ffmpeg"

    async def convert(
        self,
        input_path: Path,
        output_path: Path,
        extra_args: list[str] | None = None,
    ) -> Path:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            *(extra_args or []),
            str(output_path),
        ]

        log.debug("Running ffmpeg", cmd=" ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg failed: {stderr.decode()[-500:]}")

        return output_path

    async def merge(
        self,
        video_path: Path,
        audio_path: Path,
        output_path: Path,
    ) -> Path:
        return await self.convert(
            video_path,
            output_path,
            extra_args=[
                "-i", str(audio_path),
                "-c:v", "copy",
                "-c:a", "aac",
                "-strict", "experimental",
            ],
        )

    async def compress(
        self,
        input_path: Path,
        output_path: Path,
        target_mb: int = 50,
    ) -> Path:
        # Простая компрессия через CRF
        return await self.convert(
            input_path,
            output_path,
            extra_args=["-c:v", "libx264", "-crf", "28", "-preset", "fast"],
        )

    async def is_available(self) -> bool:
        return shutil.which("ffmpeg") is not None


# Singleton
ffmpeg = FFmpegProcessor()