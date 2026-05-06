from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from app.logging import get_logger

from services.downloaders.base_engine import BaseEngine
from services.downloaders.types import DownloadOptions

log = get_logger(__name__)


class Aria2cEngine(BaseEngine):
    """
    Strategy: прямая загрузка через aria2c CLI.
    Используется для прямых URL (не требующих извлечения).
    """

    name = "aria2c"
    supported_protocols = frozenset({"http", "https", "ftp", "magnet"})

    DEFAULT_ARGS = [
        "--max-connection-per-server=16",
        "--split=16",
        "--min-split-size=1M",
        "--max-concurrent-downloads=4",
        "--continue=true",
        "--auto-file-renaming=false",
        "--quiet=true",
    ]

    async def download(
        self,
        url: str,
        options: DownloadOptions,
        output_dir: Path,
        cookie_file: Path | None = None,
    ) -> list[Path]:
        output_dir.mkdir(parents=True, exist_ok=True)

        cmd = ["aria2c", url, f"--dir={output_dir}", *self.DEFAULT_ARGS]

        if cookie_file:
            cmd.append(f"--load-cookies={cookie_file}")

        if options.proxy:
            cmd.append(f"--all-proxy={options.proxy}")

        log.debug("Running aria2c", cmd=" ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(f"aria2c failed: {stderr.decode()}")

        return list(output_dir.glob("*.*"))

    async def extract_info(
        self,
        url: str,
        options: DownloadOptions,
        cookie_file: Path | None = None,
    ) -> dict[str, Any]:
        # aria2c не умеет извлекать метаданные
        return {"url": url}

    async def is_available(self) -> bool:
        return shutil.which("aria2c") is not None