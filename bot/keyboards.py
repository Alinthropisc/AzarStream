from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

_YOUTUBE_QUALITY_ORDER = {
    "360p": 0,
    "480p": 1,
    "720p": 2,
    "1080p": 3,
    "1440p": 4,
    "2160p": 5,
}


def get_language_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора языка"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇷🇺 Русский", callback_data="set_language:ru"),
            InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data="set_language:uz"),
            InlineKeyboardButton(text="🇺🇸 English", callback_data="set_language:en"),
        ]
    ])





def get_youtube_formats_keyboard_v2(
    formats: list[dict],
    video_id: str,
    cache_status: dict | None = None,
) -> InlineKeyboardMarkup:
    """
    Клавиатура с форматами + аудио кнопка
    formats: [{"format_id": "...", "quality": "720p", "filesize_str": "10.5 MB"}, ...]
    cache_status:
      {
        "360p": True,                       # backward-compatible bool
        "720p": {"cached": True, "size_str": "8.5MB"},  # extended format
      }
    """
    buttons = []
    cache_status = cache_status or {}

    # Видео форматы
    video_formats = [
        fmt for fmt in formats
        if fmt.get("quality") in _YOUTUBE_QUALITY_ORDER
    ]
    video_formats.sort(key=lambda fmt: _YOUTUBE_QUALITY_ORDER.get(fmt.get("quality", ""), 999))

    for fmt in video_formats:
        quality = fmt.get("quality", "")
        format_id = fmt.get("format_id", "")
        size_str = fmt.get("filesize_str", "")

        cache_meta = cache_status.get(quality)
        is_cached = bool(cache_meta) if isinstance(cache_meta, bool) else bool((cache_meta or {}).get("cached"))
        cached_size = "" if isinstance(cache_meta, bool) else (cache_meta or {}).get("size_str", "")
        # Определяем эмодзи: ⚡️ если в кеше, 💢 если нет
        emoji = "⚡️" if is_cached else "💢"
        is_exact = bool(fmt.get("filesize_exact", False))

        # Если формат уже в кеше и размер известен, показываем cached size как самый точный.
        if cached_size:
            button_text = f"{emoji} 📹 {quality} - 💾 {cached_size}"
        elif size_str:
            # Для оценочных размеров ставим "~", чтобы не вводить в заблуждение.
            size_prefix = "" if is_exact else "~"
            button_text = f"{emoji} 📹 {quality} - 💾 {size_prefix}{size_str}"
        else:
            button_text = f"{emoji} 📹 {quality}"

        if len(button_text) > 64:
            button_text = button_text[:61] + "..."

        buttons.append([
            InlineKeyboardButton(
                text=button_text,
                callback_data=f"yt_fmt:{format_id}:{quality}:{video_id}",
            )
        ])

    # Кнопка аудио
    audio_fmt = next((f for f in formats if f.get("quality") == "audio"), None)
    audio_size = (audio_fmt or {}).get("filesize_str", "")
    audio_exact = bool((audio_fmt or {}).get("filesize_exact", False))
    audio_cache_meta = cache_status.get("audio")
    audio_in_cache = bool(audio_cache_meta) if isinstance(audio_cache_meta, bool) else bool((audio_cache_meta or {}).get("cached"))
    audio_cached_size = "" if isinstance(audio_cache_meta, bool) else (audio_cache_meta or {}).get("size_str", "")
    audio_emoji = "⚡️" if audio_in_cache else "💢"

    if audio_cached_size:
        audio_text = f"{audio_emoji} 🔊 Audio - 💾 {audio_cached_size}"
    elif audio_size:
        audio_prefix = "" if audio_exact else "~"
        audio_text = f"{audio_emoji} 🔊 Audio - 💾 {audio_prefix}{audio_size}"
    else:
        audio_text = f"{audio_emoji} 🔊 Audio (MP3)"

    buttons.append([
        InlineKeyboardButton(
            text=audio_text,
            callback_data=f"yt_audio:{video_id}",
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_cancel_keyboard(language: str = "en") -> InlineKeyboardMarkup:
    """Клавиатура отмены"""
    text = "❌ Отмена" if language == "ru" else "❌ Cancel"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data="cancel")]
    ])
