import html
import re
from html.parser import HTMLParser
from urllib.parse import urlparse
from html import unescape


_MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(((?:https?|tg|mailto):[^\s)]+)\)")
_SPOILER_RE = re.compile(r"\|\|(.+?)\|\|", re.DOTALL)
_BLOCKQUOTE_TAG_RE = re.compile(r"<blockquote(?:\s+expandable)?>(.*?)</blockquote>", re.DOTALL | re.IGNORECASE)


def _is_safe_href(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme in {"http", "https", "tg", "mailto"}

_TELEGRAM_ALLOWED_TAGS = {
    "b", "strong",
    "i", "em",
    "u", "ins",
    "s", "strike", "del",
    "code", "pre",
    "a",
    "blockquote",  # ← поддерживается с Bot API 7.0+
    "tg-spoiler",
}



def _convert_blockquote_shortcuts(text: str) -> str:
    lines: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith(">"):
            quote = stripped.lstrip(">").strip()
            lines.append(f"<blockquote>{quote}</blockquote>" if quote else "")
            continue
        if len(stripped) > 4 and stripped.startswith("__") and stripped.endswith("__"):
            quote = stripped[2:-2].strip()
            lines.append(f"<blockquote>{quote}</blockquote>" if quote else "")
            continue
        lines.append(line)
    return "\n".join(lines)


def _convert_markdown_links(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        label = match.group(1)
        href = match.group(2).strip()
        if not _is_safe_href(href):
            return match.group(0)
        return f'<a href="{html.escape(href, quote=True)}">{label}</a>'

    return _MARKDOWN_LINK_RE.sub(repl, text)


def _convert_spoilers(text: str) -> str:
    return _SPOILER_RE.sub(r"<tg-spoiler>\1</tg-spoiler>", text)


def _apply_shortcuts(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _convert_blockquote_shortcuts(normalized)
    normalized = _convert_markdown_links(normalized)
    normalized = _convert_spoilers(normalized)
    return normalized


class _TelegramHTMLSanitizer(HTMLParser):
    _ALLOWED_TAGS = {
        "b", "strong",
    "i", "em",
    "u", "ins",
    "s", "strike", "del",
    "code", "pre",
    "a",
    "blockquote",  # ← поддерживается с Bot API 7.0+
    "tg-spoiler",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self.open_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()

        if tag == "br":
            self.parts.append("\n")
            return

        if tag in {"p", "div"}:
            if self.parts and not self.parts[-1].endswith("\n"):
                self.parts.append("\n")
            return

        if tag not in self._ALLOWED_TAGS:
            return

        attr_bits = ""
        attr_map = {name.lower(): (value or "") for name, value in attrs}

        if tag == "a":
            href = attr_map.get("href", "").strip()
            if not _is_safe_href(href):
                return
            attr_bits = f' href="{html.escape(href, quote=True)}"'
        elif tag == "pre":
            language = attr_map.get("language", "").strip()
            if language:
                attr_bits = f' language="{html.escape(language, quote=True)}"'
        elif tag == "blockquote":
            if "expandable" in attr_map or any(name.lower() == "expandable" for name, _ in attrs):
                attr_bits = " expandable"

        self.parts.append(f"<{tag}{attr_bits}>")
        self.open_tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag not in self._ALLOWED_TAGS or tag not in self.open_tags:
            return

        while self.open_tags:
            current = self.open_tags.pop()
            self.parts.append(f"</{current}>")
            if current == tag:
                break

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "br":
            self.parts.append("\n")
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        self.parts.append(html.escape(data))

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def get_html(self) -> str:
        while self.open_tags:
            self.parts.append(f"</{self.open_tags.pop()}>")
        joined = "".join(self.parts)
        joined = re.sub(r"\n{3,}", "\n\n", joined)
        return joined.strip()


def prepare_telegram_html(raw: str | None) -> str:
    """
    Конвертирует HTML из админки в HTML совместимый с Telegram Bot API.

    Что делает:
    - Убирает <span>, <div>, <p> — заменяет на их содержимое
    - Сохраняет <blockquote>, <b>, <i>, <a>, <code>, <pre> и т.д.
    - Схлопывает лишние пустые строки
    """
    if not raw:
        return ""

    text = raw

    # 1. Убираем <span ...> и </span> — просто вырезаем тег, содержимое оставляем
    text = re.sub(r"<span[^>]*>", "", text)
    text = re.sub(r"</span>", "", text)

    # 2. <div> и <p> → перенос строки
    text = re.sub(r"<div[^>]*>", "", text)
    text = re.sub(r"</div>", "\n", text)
    text = re.sub(r"<p[^>]*>", "", text)
    text = re.sub(r"</p>", "\n", text)

    # 3. <br> → перенос строки
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)

    # 4. Убираем все остальные неподдерживаемые теги (кроме разрешённых)
    def _strip_unknown_tags(m: re.Match) -> str:
        full_tag = m.group(0)
        # Извлекаем имя тега
        tag_name = re.match(r"</?([a-zA-Z][a-zA-Z0-9]*)", full_tag)
        if not tag_name:
            return full_tag
        name = tag_name.group(1).lower()
        if name in _TELEGRAM_ALLOWED_TAGS:
            return full_tag  # оставляем
        return ""  # убираем

    text = re.sub(r"<[^>]+>", _strip_unknown_tags, text)

    # 5. Схлопываем 3+ переносов строк → 2
    text = re.sub(r"\n{3,}", "\n\n", text)

    # 6. Убираем пробелы в начале/конце
    text = text.strip()

    return text


def prepare_telegram_compat_html(raw: str | None) -> str:
    """
    Более агрессивная версия — убирает blockquote тоже.
    Для старых Bot API или если blockquote не работает.
    Заменяет <blockquote> на вертикальную черту ▎
    """
    if not raw:
        return ""

    text = prepare_telegram_html(raw)

    # Заменяем blockquote на визуальную имитацию
    def _blockquote_to_lines(m: re.Match) -> str:
        content = m.group(1).strip()
        lines = content.split("\n")
        return "\n".join(f"▎ {line}" for line in lines if line.strip())

    text = re.sub(
        r"<blockquote[^>]*>(.*?)</blockquote>",
        _blockquote_to_lines,
        text,
        flags=re.DOTALL,
    )

    return text.strip()

def strip_telegram_markup(raw: str | None) -> str:
    """Полностью убирает все HTML теги — plain text."""
    if not raw:
        return ""
    # Сначала заменяем блочные теги на переносы
    text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|blockquote|pre)>", "\n", text, flags=re.IGNORECASE)
    # Убираем все теги
    text = re.sub(r"<[^>]+>", "", text)
    # Декодируем HTML entities (&amp; → & и т.д.)
    text = unescape(text)
    # Схлопываем переносы
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()