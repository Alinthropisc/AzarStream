"""
Handler для Media Search ботов: пользователь шлёт текст — бот ищет в библиотеке
треков и отдаёт 10 результатов с кнопками 1..10 + ◀ ▶ для пагинации. По нажатию
на номер бот шлёт кешированное аудио через file_id.
"""
from __future__ import annotations

import re

from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

# Урла, домен-подобная штука, t.me-ссылка — всё это не должно идти в поиск.
_URL_LIKE_RE = re.compile(
    r"(https?://|www\.|t\.me/|"
    r"\b[\w-]+\.(com|net|org|io|me|ru|uz|tv|fm|gg|app|link|to|be|co|info|biz|live|xyz)\b)",
    re.IGNORECASE,
)

from app.logging import get_logger
from i18n.lang import MESSAGES
from services.track import SearchPage, TrackService

log = get_logger("bot.search")


def _t(key: str, language: str, **fmt) -> str:
    entry = MESSAGES.get(key, {})
    text = entry.get(language) or entry.get("en") or ""
    return text.format(**fmt) if fmt else text


# ──────────────────────────────────────────────────────────────────────────────
# Public entry points (called from UpdateProcessor)
# ──────────────────────────────────────────────────────────────────────────────


async def handle_welcome(bot, chat_id: int, language: str, reply_to: int | None = None) -> None:
    """Стандартное приветствие после выбора языка / для существующих пользователей."""
    text = _t("search_welcome", language)
    await bot.send_message(chat_id, text, parse_mode="HTML", reply_to_message_id=reply_to)


async def handle_query(
    bot,
    chat_id: int,
    bot_id: int,
    user_id: int,
    query: str,
    language: str = "en",
) -> None:
    query = query.strip()
    if len(query) < 2:
        await bot.send_message(chat_id, _t("search_too_short", language))
        return

    if _URL_LIKE_RE.search(query):
        await bot.send_message(chat_id, _t("search_no_links", language), parse_mode="HTML")
        return

    page = await TrackService.search(query=query, bot_id=bot_id, user_telegram_id=user_id, page=1)
    await _render(bot, chat_id, page, language=language, edit_message_id=None)


async def handle_pagination(
    bot,
    callback: CallbackQuery,
    search_query_id: int,
    page_num: int,
    language: str = "en",
) -> None:
    page = await TrackService.paginate(search_query_id, page_num)
    if page is None:
        await bot.answer_callback_query(callback.id, _t("search_query_expired", language))
        return
    chat_id = callback.message.chat.id if callback.message else None
    msg_id = callback.message.message_id if callback.message else None
    if chat_id is None or msg_id is None:
        await bot.answer_callback_query(callback.id)
        return
    await _render(bot, chat_id, page, language=language, edit_message_id=msg_id)
    await bot.answer_callback_query(callback.id)


async def handle_play(
    bot,
    callback: CallbackQuery,
    track_id: int,
    language: str = "en",
    bot_username: str | None = None,
) -> bool:
    """
    Шлёт пользователю аудио из кеш-канала по track_id + подпись + кнопки лайк/дизлайк.

    Возвращает True если трек отправлен — тогда вызывающий код может слать
    post-download ad. False — ошибка / трек не найден / send_audio упал.
    """
    track = await TrackService.get(track_id)
    if track is None:
        await bot.answer_callback_query(callback.id, _t("search_track_not_found", language))
        return False

    chat_id = callback.from_user.id  # отправим в личку
    caption = _build_track_caption(track, language=language, bot_username=bot_username)
    keyboard = _build_vote_keyboard(track.id, track.likes_count, track.dislikes_count)

    try:
        await bot.send_audio(
            chat_id,
            audio=track.file_id,
            title=track.title,
            performer=track.artist,
            caption=caption,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception as exc:
        log.warning("Failed to send cached audio", track_id=track_id, error=str(exc))
        await bot.answer_callback_query(callback.id, _t("search_send_failed", language))
        return False

    await TrackService.increment_play(track_id)
    await bot.answer_callback_query(callback.id, "▶")
    return True


async def handle_vote(
    bot,
    callback: CallbackQuery,
    track_id: int,
    value: int,
    language: str = "en",
) -> None:
    """Принять лайк/дизлайк, обновить inline-клавиатуру у сообщения с аудио."""
    if value not in (1, -1):
        await bot.answer_callback_query(callback.id)
        return

    user_id = callback.from_user.id
    result = await TrackService.cast_vote(track_id, user_id, value)
    if result is None:
        await bot.answer_callback_query(callback.id, _t("search_track_not_found", language))
        return

    likes, dislikes, effective = result
    if effective > 0:
        toast = _t("search_vote_liked", language)
    elif effective < 0:
        toast = _t("search_vote_disliked", language)
    else:
        toast = _t("search_vote_removed", language)

    new_kb = _build_vote_keyboard(track_id, likes, dislikes)
    msg = callback.message
    if msg is not None:
        try:
            await bot.edit_message_reply_markup(
                chat_id=msg.chat.id,
                message_id=msg.message_id,
                reply_markup=new_kb,
            )
        except Exception:
            pass

    await bot.answer_callback_query(callback.id, toast)


# ──────────────────────────────────────────────────────────────────────────────
# Rendering
# ──────────────────────────────────────────────────────────────────────────────


def _format_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _render_text(page: SearchPage, language: str) -> str:
    if not page.items:
        return _t("search_not_found", language, query=_escape(page.query))

    header = _t(
        "search_header",
        language,
        query=_escape(page.query),
        page=page.page,
        total_pages=page.total_pages,
        total=page.total,
    )
    lines = [header, ""]
    start = (page.page - 1) * 10
    for i, tr in enumerate(page.items, start=1):
        dur = _format_duration(tr.duration_sec)
        dur_str = f"  <code>{dur}</code>" if dur else ""
        title = _escape(tr.title)
        artist = f"<i>{_escape(tr.artist)}</i> — " if tr.artist else ""
        lines.append(f"<b>{start + i}.</b> {artist}{title}{dur_str}")
    return "\n".join(lines)


def _render_keyboard(page: SearchPage) -> InlineKeyboardMarkup | None:
    if not page.items:
        return None

    number_row = [
        InlineKeyboardButton(text=str(i), callback_data=f"t:{tr.id}")
        for i, tr in enumerate(page.items, start=1)
    ]
    if len(number_row) > 5:
        nav_rows = [number_row[:5], number_row[5:]]
    else:
        nav_rows = [number_row]

    sq_id = page.search_query_id
    pag_row: list[InlineKeyboardButton] = []
    if page.has_prev:
        pag_row.append(InlineKeyboardButton(text="◀", callback_data=f"sq:{sq_id}:{page.page - 1}"))
    pag_row.append(InlineKeyboardButton(
        text=f"{page.page}/{page.total_pages}", callback_data="noop"
    ))
    if page.has_next:
        pag_row.append(InlineKeyboardButton(text="▶", callback_data=f"sq:{sq_id}:{page.page + 1}"))

    return InlineKeyboardMarkup(inline_keyboard=[*nav_rows, pag_row])


async def _render(
    bot,
    chat_id: int,
    page: SearchPage,
    language: str,
    edit_message_id: int | None,
) -> None:
    text = _render_text(page, language)
    keyboard = _render_keyboard(page)

    if edit_message_id is not None:
        try:
            await bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=edit_message_id,
                reply_markup=keyboard,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return
        except Exception:
            pass

    await bot.send_message(
        chat_id,
        text=text,
        reply_markup=keyboard,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


def _escape(s: str | None) -> str:
    if not s:
        return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _build_track_caption(track, language: str, bot_username: str | None) -> str:
    title = _escape(track.title)
    artist = _escape(track.artist) if track.artist else ""
    head = f"🎵 <b>{artist} — {title}</b>" if artist else f"🎵 <b>{title}</b>"
    parts = [head]
    dur = _format_duration(track.duration_sec)
    if dur:
        parts.append(f"⏱ <code>{dur}</code>")
    if bot_username:
        parts.append(f"🤖 @{bot_username}")
    parts.append("")
    parts.append(f"<i>{_t('search_rate_prompt', language)}</i>")
    return "\n".join(parts)


def _build_vote_keyboard(track_id: int, likes: int, dislikes: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"👍 {likes}", callback_data=f"tv:{track_id}:1"),
        InlineKeyboardButton(text=f"👎 {dislikes}", callback_data=f"tv:{track_id}:-1"),
    ]])
