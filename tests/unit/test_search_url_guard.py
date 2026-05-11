"""Тесты что Media Search не принимает ссылки как поисковые запросы."""

import pytest

from bot.search_handler import _URL_LIKE_RE


class TestUrlGuard:
    @pytest.mark.parametrize("query", [
        "https://youtube.com/playlist?list=PLVikmjgplfv3lcOEBe02Dsb14hu7-iQcb",
        "http://example.com",
        "youtube.com/watch?v=abc",
        "www.example.com",
        "t.me/somebot",
        "youtu.be/abc",
        "soundcloud.com/artist/track",
        "open.spotify.com/track/xxx",
        "INSTAGRAM.com/reel/abc",
    ])
    def test_url_like_detected(self, query):
        assert _URL_LIKE_RE.search(query) is not None, f"Should detect URL in: {query}"

    @pytest.mark.parametrize("query", [
        "sami yusuf you came to me",
        "maher zain",
        "коран бакара",
        "سامي يوسف",
        "huntrix golden",
        "Track 1.1 version",  # dot, но не TLD
        "song (remix)",
        "Hello World",
        "AC/DC",
        "Mozart's symphony No. 9",
    ])
    def test_plain_text_passes(self, query):
        assert _URL_LIKE_RE.search(query) is None, f"Should NOT match plain text: {query}"
