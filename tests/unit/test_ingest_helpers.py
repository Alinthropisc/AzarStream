"""Тесты для helper-функций воркера ingest_audio: парсинг title/artist + лимит upload."""

import pytest

from workers.ingest import _parse_title_artist, _upload_size_limit


class TestParseTitleArtist:
    def test_explicit_track_and_artist(self):
        entry = {"track": "Golden", "artist": "HUNTR/X", "title": "ignored", "uploader": "ignored"}
        assert _parse_title_artist(entry, "stem") == ("Golden", "HUNTR/X")

    def test_explicit_track_no_artist(self):
        entry = {"track": "Golden", "title": "ignored", "uploader": "Some Channel"}
        # uploader НЕ должен попасть в artist
        assert _parse_title_artist(entry, "stem") == ("Golden", None)

    def test_parse_artist_dash_title(self):
        entry = {"title": "HUNTR/X - Golden (Lyrics)", "uploader": "Unique Sound"}
        title, artist = _parse_title_artist(entry, "stem")
        assert title == "Golden (Lyrics)"
        assert artist == "HUNTR/X"

    def test_parse_em_dash(self):
        entry = {"title": "Maher Zain — Insha Allah", "uploader": "ignored"}
        title, artist = _parse_title_artist(entry, "stem")
        assert (title, artist) == ("Insha Allah", "Maher Zain")

    def test_parse_en_dash(self):
        entry = {"title": "Sami Yusuf – You Came To Me", "uploader": "ignored"}
        title, artist = _parse_title_artist(entry, "stem")
        assert (title, artist) == ("You Came To Me", "Sami Yusuf")

    def test_no_separator_uses_whole_title(self):
        entry = {"title": "Just a song name", "uploader": "Unique Sound"}
        title, artist = _parse_title_artist(entry, "stem")
        # artist должен быть None, а НЕ "Unique Sound"
        assert title == "Just a song name"
        assert artist is None

    def test_explicit_artist_wins_over_dash_parse(self):
        entry = {"title": "Foo - Bar", "artist": "Real Artist", "uploader": "ignored"}
        title, artist = _parse_title_artist(entry, "stem")
        assert artist == "Real Artist"
        # title в этом случае остаётся как есть, потому что artist уже определён
        assert title == "Foo - Bar"

    def test_empty_title_falls_back_to_stem(self):
        entry = {}
        title, artist = _parse_title_artist(entry, "abc_stem")
        assert title == "abc_stem"
        assert artist is None

    def test_uploader_never_becomes_artist(self):
        # Главный regression-кейс: «Unique Sound» в кеш-канале
        entry = {
            "title": "Huntrix - Golden (Lyrics) KPop Demon Hunters",
            "uploader": "Unique Sound",
            "creator": "Unique Sound",
            "channel": "Unique Sound",
        }
        title, artist = _parse_title_artist(entry, "stem")
        assert artist == "Huntrix"
        assert "Unique Sound" not in (artist or "")


class TestUploadSizeLimit:
    def test_default_is_49mb(self, monkeypatch):
        from workers import ingest as ingest_mod

        class _S:
            telegram_api_local = False
            telegram_api_server = ""

        monkeypatch.setattr(ingest_mod, "settings", _S)
        assert _upload_size_limit() == 49 * 1024 * 1024

    def test_local_api_raises_limit(self, monkeypatch):
        from workers import ingest as ingest_mod

        class _S:
            telegram_api_local = True
            telegram_api_server = "http://localhost:8081"

        monkeypatch.setattr(ingest_mod, "settings", _S)
        assert _upload_size_limit() == 1900 * 1024 * 1024

    def test_local_flag_without_url_keeps_cloud_limit(self, monkeypatch):
        from workers import ingest as ingest_mod

        class _S:
            telegram_api_local = True
            telegram_api_server = ""  # URL пустой = всё равно cloud

        monkeypatch.setattr(ingest_mod, "settings", _S)
        assert _upload_size_limit() == 49 * 1024 * 1024
