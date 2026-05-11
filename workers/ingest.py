"""
ARQ-таска ingest_audio: качает аудио из YouTube/SoundCloud (или берёт локальный
файл), заливает в Telegram cache-канал, пишет Track в БД и удаляет локальный
файл. На диске после успешной загрузки ничего не остаётся.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from aiogram.types import FSInputFile
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.logging import get_logger

# Cloud Bot API: лимит 50 MB на upload. Берём 49 MB с запасом на multipart-обвязку.
# Если поднят локальный Bot API server (settings.telegram_api_local=True
# И настоящий URL в telegram_api_server) — поднимаем до ~1.9 GB.
def _upload_size_limit() -> int:
    if getattr(settings, "telegram_api_local", False) and (settings.telegram_api_server or "").strip():
        return 1900 * 1024 * 1024
    return 49 * 1024 * 1024
from models import (
    BotType,
    IngestJob,
    IngestJobStatus,
    IngestSourceType,
    TrackSource,
)
from repositories.uow import UnitOfWork
from services import bot_manager

log = get_logger("worker.ingest")


# ──────────────────────────────────────────────────────────────────────────────
# ARQ entry point
# ──────────────────────────────────────────────────────────────────────────────


async def ingest_audio(ctx: dict, job_id: int) -> dict[str, Any]:
    log.info("Ingest job started", job_id=job_id)

    async with UnitOfWork() as uow:
        job = await uow.ingest_jobs.get_by_id(job_id)
        if job is None:
            log.error("Ingest job not found", job_id=job_id)
            return {"ok": False, "error": "job_not_found"}
        await uow.ingest_jobs.mark_running(job_id)
        await uow.commit()

    try:
        if job.source_type in (IngestSourceType.YOUTUBE, IngestSourceType.SOUNDCLOUD):
            stats = await _ingest_url(job)
        elif job.source_type == IngestSourceType.FILE_UPLOAD:
            stats = await _ingest_local_file(job)
        else:
            stats = {"processed": 0, "failed": 0, "total": 0, "error": "unsupported"}

        final_status = _decide_status(stats)
        async with UnitOfWork() as uow:
            await uow.ingest_jobs.mark_progress(
                job_id,
                processed=stats["processed"],
                failed=stats["failed"],
                total=stats["total"],
            )
            await uow.ingest_jobs.mark_finished(job_id, final_status, error_message=stats.get("error"))
            await uow.commit()
        log.info("Ingest job finished", job_id=job_id, **stats)
        return {"ok": True, **stats}

    except Exception as exc:
        log.exception("Ingest job failed", job_id=job_id, error=str(exc))
        async with UnitOfWork() as uow:
            await uow.ingest_jobs.mark_finished(
                job_id, IngestJobStatus.FAILED, error_message=str(exc)[:1000]
            )
            await uow.commit()
        return {"ok": False, "error": str(exc)}


def _decide_status(stats: dict[str, int]) -> IngestJobStatus:
    if stats["total"] == 0:
        return IngestJobStatus.FAILED
    if stats["failed"] == 0:
        return IngestJobStatus.COMPLETED
    if stats["processed"] > 0:
        return IngestJobStatus.PARTIAL
    return IngestJobStatus.FAILED


# ──────────────────────────────────────────────────────────────────────────────
# YouTube / SoundCloud
# ──────────────────────────────────────────────────────────────────────────────


def _ytdlp_common_opts(out_dir: Path) -> dict[str, Any]:
    """Базовый набор опций yt-dlp для всех вызовов (extract + download)."""
    import shutil as _sh

    has_aria2c = _sh.which("aria2c") is not None
    cookie_path = Path("storage/cookies/youtube.txt")
    cookiefile = str(cookie_path) if cookie_path.exists() else None

    opts: dict[str, Any] = {
        # Жёстко аудио: запрещаем фоллбэк на видео (раньше 55MB .webm-видео ловились).
        "format": "bestaudio[ext=m4a]/bestaudio",
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        "noplaylist": False,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "writethumbnail": False,
        "concurrent_fragment_downloads": 8,
        "retries": 5,
        "fragment_retries": 5,
        "http_chunk_size": 10 * 1024 * 1024,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "0",
            },
            {"key": "FFmpegMetadata"},
        ],
        # -threads 0 = все ядра для mp3 энкодинга
        "postprocessor_args": {"FFmpegExtractAudio": ["-threads", "0"]},
    }

    if has_aria2c:
        opts["external_downloader"] = {"default": "aria2c"}
        opts["external_downloader_args"] = {
            "aria2c": [
                "-x", "16",
                "-s", "16",
                "-k", "1M",
                "--min-split-size=1M",
                "--max-tries=5",
                "--retry-wait=2",
                "--summary-interval=0",
                "--console-log-level=warn",
                "--allow-overwrite=true",
            ],
        }

    if cookiefile:
        opts["cookiefile"] = cookiefile

    return opts


def _ytdlp_list_entries(url: str) -> list[dict]:
    """
    Быстрый flat-extract: возвращает список stub-entries (id, title, url),
    БЕЗ скачивания. Используется чтобы заранее знать total для прогресса.
    """
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if info is None:
            return []
        entries = info.get("entries")
        if entries is None:
            return [info]
        return [e for e in entries if e is not None]


def _ytdlp_download_one(entry_url: str, out_dir: Path) -> dict | None:
    """Скачивает одну запись и возвращает её infodict (с уже сконвертированным mp3)."""
    import yt_dlp

    opts = _ytdlp_common_opts(out_dir)
    opts["noplaylist"] = True  # на всякий — мы качаем одну запись

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(entry_url, download=True)
        return info


async def _ingest_url(job: IngestJob) -> dict[str, Any]:
    """
    Стримовая обработка: extract_flat → для каждой записи download → upload →
    mark_progress. Так админка видит processed=k/N в реальном времени,
    а не 0/? до самого конца.
    """
    url = job.source_url or ""
    work_dir = Path(tempfile.mkdtemp(prefix=f"ingest_{job.id}_"))
    loop = asyncio.get_running_loop()

    try:
        stubs = await loop.run_in_executor(None, _ytdlp_list_entries, url)
        total = len(stubs)

        async with UnitOfWork() as uow:
            await uow.ingest_jobs.mark_progress(job.id, total=total, processed=0, failed=0)
            await uow.commit()

        if total == 0:
            return {"processed": 0, "failed": 0, "total": 0, "error": "yt-dlp returned 0 entries"}

        cache_chat_id, uploader_token = await _pick_uploader(job.target_cache_channel_id)
        if cache_chat_id is None or uploader_token is None:
            return {
                "processed": 0, "failed": total, "total": total,
                "error": "Нет активного media_search бота или cache-канала",
            }

        bot_inst = await bot_manager.get_bot_instance(uploader_token)
        if bot_inst is None:
            return {
                "processed": 0, "failed": total, "total": total,
                "error": "Не удалось получить bot instance для uploader",
            }
        bot = bot_inst.bot

        processed = 0
        failed = 0

        for stub in stubs:
            entry_url = stub.get("webpage_url") or stub.get("url") or stub.get("id")
            stub_id = stub.get("id")
            if not entry_url:
                failed += 1
                await _save_progress(job.id, processed, failed)
                continue

            # Дедуп до скачивания — экономит трафик
            if stub_id:
                async with UnitOfWork() as uow:
                    existing = await uow.tracks.get_by_source(
                        TrackSource(job.source_type.value), stub_id
                    )
                    if existing is not None:
                        processed += 1
                        await uow.ingest_jobs.mark_progress(
                            job.id, processed=processed, failed=failed
                        )
                        await uow.commit()
                        continue

            entry_dir = work_dir / (stub_id or f"e{processed + failed}")
            entry_dir.mkdir(parents=True, exist_ok=True)

            try:
                entry = await loop.run_in_executor(
                    None, _ytdlp_download_one, entry_url, entry_dir
                )
                if entry is None:
                    failed += 1
                    await _save_progress(job.id, processed, failed)
                    continue

                mp3_path = _find_mp3(entry_dir, entry.get("id") or stub_id)
                if not mp3_path or not mp3_path.exists():
                    failed += 1
                    await _save_progress(job.id, processed, failed)
                    continue

                title, artist = _parse_title_artist(entry, fallback_stem=mp3_path.stem)
                duration = int(entry.get("duration") or 0) or None
                source_id = entry.get("id") or stub_id

                mp3_size = mp3_path.stat().st_size
                size_limit = _upload_size_limit()
                if mp3_size > size_limit:
                    log.warning(
                        "Track skipped: exceeds Telegram upload limit",
                        job_id=job.id,
                        track_id=source_id,
                        size_mb=round(mp3_size / 1024 / 1024, 1),
                        limit_mb=round(size_limit / 1024 / 1024),
                    )
                    failed += 1
                    await _save_progress(job.id, processed, failed)
                    continue

                msg = await bot.send_audio(
                    chat_id=cache_chat_id,
                    audio=FSInputFile(str(mp3_path)),
                    title=title[:64] if title else None,
                    performer=artist[:64] if artist else None,
                    duration=duration,
                )

                audio = msg.audio
                file_id = audio.file_id if audio else None
                file_unique = audio.file_unique_id if audio else None
                file_size = audio.file_size if audio else None

                if not file_id:
                    failed += 1
                    await _save_progress(job.id, processed, failed)
                    continue

                try:
                    async with UnitOfWork() as uow:
                        track = await uow.tracks.create(
                            title=title,
                            artist=artist,
                            duration_sec=duration,
                            source_platform=TrackSource(job.source_type.value),
                            source_url=entry.get("webpage_url") or entry_url,
                            source_id=source_id,
                            cache_chat_id=cache_chat_id,
                            cache_message_id=msg.message_id,
                            file_id=file_id,
                            file_unique_id=file_unique,
                            file_size=file_size,
                            added_by_admin_id=job.requested_by_admin_id,
                        )
                        await uow.commit()
                        new_track_id = track.id
                    processed += 1

                    # Зеркалим во все остальные активные cache-каналы того же bot_type.
                    # Используем copy_message — без 'Forwarded from'.
                    await _mirror_to_other_channels(
                        bot=bot,
                        track_id=new_track_id,
                        primary_chat_id=cache_chat_id,
                        primary_message_id=msg.message_id,
                        bot_type=BotType.MEDIA_SEARCH,
                    )
                except IntegrityError:
                    # Гонка между параллельными ingest-job'ами на один и тот же URL —
                    # дубль вылез после нашего pre-check. Считаем за «уже есть».
                    log.info(
                        "Duplicate track skipped (race)",
                        job_id=job.id,
                        source_id=source_id,
                    )
                    processed += 1
                await _save_progress(job.id, processed, failed)

                log.info(
                    "Track ingested",
                    job_id=job.id,
                    track_id=source_id,
                    progress=f"{processed + failed}/{total}",
                )

            except Exception as exc:
                failed += 1
                await _save_progress(job.id, processed, failed)
                log.warning("Track ingest failed", job_id=job.id, error=str(exc))
            finally:
                shutil.rmtree(entry_dir, ignore_errors=True)

        return {"processed": processed, "failed": failed, "total": total}

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


async def _save_progress(job_id: int, processed: int, failed: int) -> None:
    try:
        async with UnitOfWork() as uow:
            await uow.ingest_jobs.mark_progress(job_id, processed=processed, failed=failed)
            await uow.commit()
    except Exception as exc:
        log.warning("save_progress failed", job_id=job_id, error=str(exc))


def _parse_title_artist(entry: dict, fallback_stem: str) -> tuple[str, str | None]:
    """
    Возвращает (title, artist) для Telegram-аудио.

    Правила:
    - Если yt-dlp дал явные track/artist (YouTube Music и т.п.) — используем их.
    - Иначе берём title и пытаемся разобрать «Artist - Title».
    - НИКОГДА не используем uploader/creator/channel как artist (это название
      YouTube-канала, типа «Unique Sound» — не имя исполнителя).
    """
    track = (entry.get("track") or "").strip()
    artist = (entry.get("artist") or "").strip()
    if track:
        return track, (artist or None)

    raw = (entry.get("title") or fallback_stem or "Untitled").strip()
    if artist:
        return raw, artist

    # Пробуем разные виды тире
    for sep in (" — ", " – ", " - "):
        if sep in raw:
            left, right = raw.split(sep, 1)
            left, right = left.strip(), right.strip()
            if left and right:
                return right, left

    return raw, None


async def _mirror_to_other_channels(
    bot,
    track_id: int,
    primary_chat_id: int,
    primary_message_id: int,
    bot_type: "BotType",
) -> None:
    """
    Копирует только что залитый трек во все остальные активные cache-каналы
    того же bot_type через copy_message (без 'Forwarded from'). Каждую копию
    сохраняет в track_cache_mirrors.

    Ошибки отдельных каналов проглатываем — если один канал умер, остальные
    зеркала всё равно должны записаться.
    """
    from sqlalchemy import select
    from models import CacheChannel
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

    async with UnitOfWork() as uow:
        stmt = (
            select(CacheChannel)
            .where(
                CacheChannel.is_active == True,
                CacheChannel.bot_type == bot_type,
                CacheChannel.telegram_id != primary_chat_id,
            )
        )
        others = (await uow.session.execute(stmt)).scalars().all()

    for ch in others:
        mirror_chat_id = int(ch.telegram_id)
        try:
            copied = await bot.copy_message(
                chat_id=mirror_chat_id,
                from_chat_id=primary_chat_id,
                message_id=primary_message_id,
            )
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            log.warning(
                "Mirror copy failed",
                track_id=track_id,
                chat_id=mirror_chat_id,
                error=str(exc)[:200],
            )
            continue
        except Exception as exc:
            log.warning(
                "Mirror copy unexpected error",
                track_id=track_id,
                chat_id=mirror_chat_id,
                error=str(exc)[:200],
            )
            continue

        # copy_message возвращает MessageId (только id), без полного сообщения.
        # Чтобы получить file_id зеркальной копии, надо запросить getMessage —
        # этого нет в Bot API. Поэтому file_id зеркала берём из исходного:
        # file_id внутри одного бота идентичен в любом чате, file_unique_id —
        # тем более. Просто хранится связка (track_id, chat_id, message_id).
        # При использовании зеркала send_audio пройдёт по file_id из основной
        # записи. Если основной канал забанили — копию из зеркала можно
        # достать через copy_message обратно пользователю.
        try:
            async with UnitOfWork() as uow:
                # file_id/file_unique_id берём из основной записи трека
                main = await uow.tracks.get_by_id(track_id)
                if main is None:
                    return
                await uow.track_mirrors.add_mirror(
                    track_id=track_id,
                    cache_chat_id=mirror_chat_id,
                    cache_message_id=int(copied.message_id),
                    file_id=main.file_id,
                    file_unique_id=main.file_unique_id,
                )
                await uow.commit()
        except Exception as exc:
            log.warning(
                "Mirror save failed",
                track_id=track_id,
                chat_id=mirror_chat_id,
                error=str(exc)[:200],
            )


def _find_mp3(work_dir: Path, video_id: str | None) -> Path | None:
    if not video_id:
        # fallback: first mp3
        for p in work_dir.glob("*.mp3"):
            return p
        return None
    p = work_dir / f"{video_id}.mp3"
    if p.exists():
        return p
    # yt-dlp may sanitize the id; fallback to glob
    for cand in work_dir.glob(f"{video_id}*.mp3"):
        return cand
    return None


# ──────────────────────────────────────────────────────────────────────────────
# File upload
# ──────────────────────────────────────────────────────────────────────────────


async def _ingest_local_file(job: IngestJob) -> dict[str, Any]:
    path_str = job.source_url or ""
    p = Path(path_str)
    if not p.exists():
        return {"processed": 0, "failed": 1, "total": 1, "error": "file not found"}

    title, artist, duration = _read_audio_tags(p)
    cache_chat_id, uploader_token = await _pick_uploader(job.target_cache_channel_id)
    if cache_chat_id is None or uploader_token is None:
        return {
            "processed": 0, "failed": 1, "total": 1,
            "error": "Нет активного media_search бота или cache-канала",
        }

    bot_inst = await bot_manager.get_bot_instance(uploader_token)
    if bot_inst is None:
        return {
            "processed": 0, "failed": 1, "total": 1,
            "error": "Не удалось получить bot instance для uploader",
        }
    bot = bot_inst.bot
    try:
        msg = await bot.send_audio(
            chat_id=cache_chat_id,
            audio=FSInputFile(str(p)),
            title=title[:64] if title else None,
            performer=artist[:64] if artist else None,
            duration=duration,
        )
        audio = msg.audio
        if not audio or not audio.file_id:
            return {"processed": 0, "failed": 1, "total": 1, "error": "no file_id from telegram"}

        async with UnitOfWork() as uow:
            await uow.tracks.create(
                title=title or job.source_filename or p.stem,
                artist=artist,
                duration_sec=duration,
                source_platform=TrackSource.UPLOAD,
                source_id=audio.file_unique_id,
                cache_chat_id=cache_chat_id,
                cache_message_id=msg.message_id,
                file_id=audio.file_id,
                file_unique_id=audio.file_unique_id,
                file_size=audio.file_size,
                added_by_admin_id=job.requested_by_admin_id,
            )
            await uow.commit()
        return {"processed": 1, "failed": 0, "total": 1}
    finally:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


def _read_audio_tags(path: Path) -> tuple[str | None, str | None, int | None]:
    """Тянем title/artist/duration через mutagen, если оно установлено."""
    try:
        import mutagen  # type: ignore
        f = mutagen.File(str(path), easy=True)
        if f is None:
            return None, None, None
        title = (f.get("title") or [None])[0]
        artist = (f.get("artist") or [None])[0]
        duration = int(getattr(f.info, "length", 0)) or None
        return title, artist, duration
    except Exception:
        return None, None, None


# ──────────────────────────────────────────────────────────────────────────────
# Uploader selection
# ──────────────────────────────────────────────────────────────────────────────


async def _pick_uploader(target_cache_channel_id: str | None) -> tuple[int | None, str | None]:
    """
    Возвращает (cache_chat_id, bot_token). Берём media_search cache-канал
    (LRU или указанный) и любой активный media_search бот.
    """
    async with UnitOfWork() as uow:
        from sqlalchemy import select
        from models import Bot, BotStatus, CacheChannel

        # cache channel
        if target_cache_channel_id:
            channel = await uow.cache_channels.get_by_id(target_cache_channel_id)
        else:
            stmt = (
                select(CacheChannel)
                .where(
                    CacheChannel.is_active == True,
                    CacheChannel.bot_type == BotType.MEDIA_SEARCH,
                )
                .order_by(
                    CacheChannel.last_used_at.is_(None).desc(),
                    CacheChannel.last_used_at.asc(),
                    CacheChannel.created_at.asc(),
                )
                .limit(1)
            )
            channel = (await uow.session.execute(stmt)).scalar_one_or_none()
        if channel is None:
            return None, None

        # bot
        stmt = (
            select(Bot)
            .where(Bot.status == BotStatus.ACTIVE, Bot.bot_type == BotType.MEDIA_SEARCH)
            .limit(1)
        )
        bot_model = (await uow.session.execute(stmt)).scalar_one_or_none()
        if bot_model is None:
            return None, None
        return int(channel.telegram_id), bot_model.token
