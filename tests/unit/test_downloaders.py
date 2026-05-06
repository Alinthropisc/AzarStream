import pytest
from pathlib import Path
from unittest.mock import patch

from services.downloaders.downloader import (
    download_service,
    MediaPlatform,
)
from services.media.youtube import YouTubeDownloader
from services.media.instagram import InstagramDownloader
from services.media.tiktok import TikTokDownloader
from services.media.pinterest import PinterestDownloader
from services.media.generic import (
    DailymotionDownloader,
    FacebookDownloader,
    GenericYtDlpDownloader,
    LikeeDownloader,
    RedditDownloader,
    SnapchatDownloader,
    SoundCloudDownloader,
    ThreadsDownloader,
    TumblrDownloader,
    TwitchDownloader,
    TwitterDownloader,
    VimeoDownloader,
)


class TestDownloadService:
    """Tests for DownloadService"""

    def test_detect_platform_youtube(self, sample_urls):
        """Test YouTube URL detection"""
        assert download_service.detect_platform(sample_urls["youtube"]) == MediaPlatform.YOUTUBE
        assert download_service.detect_platform(sample_urls["youtube_short"]) == MediaPlatform.YOUTUBE
        assert download_service.detect_platform(sample_urls["youtube_shorts"]) == MediaPlatform.YOUTUBE

    def test_detect_platform_instagram(self, sample_urls):
        """Test Instagram URL detection"""
        assert download_service.detect_platform(sample_urls["instagram_post"]) == MediaPlatform.INSTAGRAM
        assert download_service.detect_platform(sample_urls["instagram_reel"]) == MediaPlatform.INSTAGRAM

    def test_detect_platform_tiktok(self, sample_urls):
        """Test TikTok URL detection"""
        assert download_service.detect_platform(sample_urls["tiktok"]) == MediaPlatform.TIKTOK
        assert download_service.detect_platform(sample_urls["tiktok_short"]) == MediaPlatform.TIKTOK

    def test_detect_platform_pinterest(self, sample_urls):
        """Test Pinterest URL detection"""
        assert download_service.detect_platform(sample_urls["pinterest"]) == MediaPlatform.PINTEREST
        assert download_service.detect_platform(sample_urls["pinterest_short"]) == MediaPlatform.PINTEREST

    def test_detect_platform_unknown(self, sample_urls):
        """Test unknown URL detection"""
        assert download_service.detect_platform(sample_urls["invalid"]) == MediaPlatform.UNKNOWN

    def test_detect_platform_twitter(self, sample_urls):
        """Twitter / X URL detection"""
        assert download_service.detect_platform(sample_urls["twitter"]) == MediaPlatform.TWITTER
        assert download_service.detect_platform(sample_urls["twitter_x"]) == MediaPlatform.TWITTER

    def test_detect_platform_soundcloud(self, sample_urls):
        """SoundCloud URL detection (full + shortlink)"""
        assert download_service.detect_platform(sample_urls["soundcloud"]) == MediaPlatform.SOUNDCLOUD
        assert download_service.detect_platform(sample_urls["soundcloud_short"]) == MediaPlatform.SOUNDCLOUD

    def test_detect_platform_reddit(self, sample_urls):
        """Reddit URL detection (full + redd.it)"""
        assert download_service.detect_platform(sample_urls["reddit"]) == MediaPlatform.REDDIT
        assert download_service.detect_platform(sample_urls["reddit_short"]) == MediaPlatform.REDDIT

    def test_detect_platform_vimeo(self, sample_urls):
        """Vimeo URL detection"""
        assert download_service.detect_platform(sample_urls["vimeo"]) == MediaPlatform.VIMEO

    def test_detect_platform_facebook(self, sample_urls):
        assert download_service.detect_platform(sample_urls["facebook"]) == MediaPlatform.FACEBOOK
        assert download_service.detect_platform(sample_urls["facebook_short"]) == MediaPlatform.FACEBOOK

    def test_detect_platform_twitch(self, sample_urls):
        assert download_service.detect_platform(sample_urls["twitch"]) == MediaPlatform.TWITCH
        assert download_service.detect_platform(sample_urls["twitch_clips"]) == MediaPlatform.TWITCH

    def test_detect_platform_dailymotion(self, sample_urls):
        assert download_service.detect_platform(sample_urls["dailymotion"]) == MediaPlatform.DAILYMOTION
        assert download_service.detect_platform(sample_urls["dailymotion_short"]) == MediaPlatform.DAILYMOTION

    def test_detect_platform_tumblr(self, sample_urls):
        assert download_service.detect_platform(sample_urls["tumblr"]) == MediaPlatform.TUMBLR

    def test_detect_platform_threads(self, sample_urls):
        assert download_service.detect_platform(sample_urls["threads"]) == MediaPlatform.THREADS

    def test_detect_platform_snapchat(self, sample_urls):
        assert download_service.detect_platform(sample_urls["snapchat"]) == MediaPlatform.SNAPCHAT

    def test_detect_platform_likee(self, sample_urls):
        assert download_service.detect_platform(sample_urls["likee"]) == MediaPlatform.LIKEE

    def test_get_downloader(self):
        """Test getting downloader for platform"""
        assert isinstance(download_service.get_downloader(MediaPlatform.YOUTUBE), YouTubeDownloader)
        assert isinstance(download_service.get_downloader(MediaPlatform.INSTAGRAM), InstagramDownloader)
        assert isinstance(download_service.get_downloader(MediaPlatform.TIKTOK), TikTokDownloader)
        assert isinstance(download_service.get_downloader(MediaPlatform.PINTEREST), PinterestDownloader)
        assert isinstance(download_service.get_downloader(MediaPlatform.TWITTER), TwitterDownloader)
        assert isinstance(download_service.get_downloader(MediaPlatform.SOUNDCLOUD), SoundCloudDownloader)
        assert isinstance(download_service.get_downloader(MediaPlatform.REDDIT), RedditDownloader)
        assert isinstance(download_service.get_downloader(MediaPlatform.VIMEO), VimeoDownloader)
        assert isinstance(download_service.get_downloader(MediaPlatform.FACEBOOK), FacebookDownloader)
        assert isinstance(download_service.get_downloader(MediaPlatform.TWITCH), TwitchDownloader)
        assert isinstance(download_service.get_downloader(MediaPlatform.DAILYMOTION), DailymotionDownloader)
        assert isinstance(download_service.get_downloader(MediaPlatform.TUMBLR), TumblrDownloader)
        assert isinstance(download_service.get_downloader(MediaPlatform.THREADS), ThreadsDownloader)
        assert isinstance(download_service.get_downloader(MediaPlatform.SNAPCHAT), SnapchatDownloader)
        assert isinstance(download_service.get_downloader(MediaPlatform.LIKEE), LikeeDownloader)
        assert download_service.get_downloader(MediaPlatform.UNKNOWN) is None


class TestGenericDownloaderConfig:
    """Sanity-проверки конфигурации generic-загрузчиков (без сети)."""

    def test_twitter_config(self):
        d = TwitterDownloader()
        assert d.platform == MediaPlatform.TWITTER
        assert d.use_cookies is True
        assert d.audio_only is False
        # Поддерживаемые домены
        assert d.match_url("https://twitter.com/user/status/1")
        assert d.match_url("https://x.com/user/status/1")
        assert d.match_url("https://vxtwitter.com/user/status/1")
        assert not d.match_url("https://example.com/twitter/x")

    def test_soundcloud_config(self):
        d = SoundCloudDownloader()
        assert d.platform == MediaPlatform.SOUNDCLOUD
        assert d.audio_only is True   # mp3 only
        assert d.use_cookies is False
        assert d.match_url("https://soundcloud.com/artist/track")
        assert d.match_url("https://snd.sc/abc")
        assert not d.match_url("https://example.com/soundcloud")

    def test_reddit_config(self):
        d = RedditDownloader()
        assert d.platform == MediaPlatform.REDDIT
        assert d.use_cookies is False
        assert d.match_url("https://www.reddit.com/r/videos/comments/abc/")
        assert d.match_url("https://redd.it/abc")
        assert not d.match_url("https://example.com/reddit")

    def test_vimeo_config(self):
        d = VimeoDownloader()
        assert d.platform == MediaPlatform.VIMEO
        assert d.use_cookies is True
        assert d.match_url("https://vimeo.com/123456")
        assert d.match_url("https://player.vimeo.com/video/123")
        assert not d.match_url("https://example.com/vimeo")

    def test_generic_audio_format_for_audio_only(self):
        """audio_only=True → ydl_opts содержит bestaudio + FFmpegExtractAudio"""
        from services.downloaders.downloader import DownloadRequest

        d = SoundCloudDownloader()
        req = DownloadRequest(
            url="https://soundcloud.com/x/y",
            platform=MediaPlatform.SOUNDCLOUD,
            user_id=1, bot_id=1, chat_id=1, message_id=1,
        )
        opts = d._build_ydl_opts(str(d.temp_dir / "test.%(ext)s"), req)
        assert opts["format"] == "bestaudio/best"
        # Должен быть постпроцессор для извлечения mp3
        pps = opts.get("postprocessors", [])
        assert any(p.get("key") == "FFmpegExtractAudio" for p in pps)

    def test_generic_video_format_for_video_platforms(self):
        """audio_only=False → формат с видео-приоритетом"""
        from services.downloaders.downloader import DownloadRequest

        d = TwitterDownloader()
        req = DownloadRequest(
            url="https://twitter.com/x/status/1",
            platform=MediaPlatform.TWITTER,
            user_id=1, bot_id=1, chat_id=1, message_id=1,
        )
        opts = d._build_ydl_opts(str(d.temp_dir / "test.%(ext)s"), req)
        assert "bestvideo" in opts["format"]
        # Без extractAudio для видео
        pps = opts.get("postprocessors", [])
        assert not any(p.get("key") == "FFmpegExtractAudio" for p in pps)

    def test_facebook_config(self):
        d = FacebookDownloader()
        assert d.platform == MediaPlatform.FACEBOOK
        assert d.use_cookies is True
        assert d.match_url("https://www.facebook.com/watch/?v=1")
        assert d.match_url("https://fb.watch/abc")
        assert d.match_url("https://fb.com/x")

    def test_twitch_config(self):
        d = TwitchDownloader()
        assert d.platform == MediaPlatform.TWITCH
        assert d.use_cookies is False
        assert d.match_url("https://www.twitch.tv/streamer/clip/abc")
        assert d.match_url("https://clips.twitch.tv/abc")

    def test_dailymotion_config(self):
        d = DailymotionDownloader()
        assert d.platform == MediaPlatform.DAILYMOTION
        assert d.use_cookies is False
        assert d.match_url("https://www.dailymotion.com/video/x123")
        assert d.match_url("https://dai.ly/x123")

    def test_tumblr_config(self):
        d = TumblrDownloader()
        assert d.platform == MediaPlatform.TUMBLR
        assert d.use_cookies is True
        assert d.match_url("https://user.tumblr.com/post/1/title")

    def test_threads_config(self):
        d = ThreadsDownloader()
        assert d.platform == MediaPlatform.THREADS
        assert d.use_cookies is True
        assert d.match_url("https://www.threads.net/@user/post/abc")
        assert d.match_url("https://www.threads.com/@user/post/abc")

    def test_snapchat_config(self):
        d = SnapchatDownloader()
        assert d.platform == MediaPlatform.SNAPCHAT
        assert d.match_url("https://www.snapchat.com/spotlight/abc")

    def test_likee_config(self):
        d = LikeeDownloader()
        assert d.platform == MediaPlatform.LIKEE
        assert d.use_cookies is False
        assert d.match_url("https://likee.video/v/abc")
        assert d.match_url("https://l.likee.video/abc")

    def test_generic_aria2c_disabled_for_cookie_platforms(self):
        """aria2c НЕ должен включаться для платформ с use_cookies=True"""
        from services.downloaders.downloader import DownloadRequest

        d = TwitterDownloader()  # use_cookies=True
        req = DownloadRequest(
            url="https://twitter.com/x/status/1",
            platform=MediaPlatform.TWITTER,
            user_id=1, bot_id=1, chat_id=1, message_id=1,
        )
        opts = d._build_ydl_opts(str(d.temp_dir / "t.%(ext)s"), req)
        # Даже если aria2c установлен — для cookie-платформ его не должно быть
        assert "external_downloader" not in opts or opts.get("external_downloader") != "aria2c"


class TestYouTubeDownloader:
    """Tests for YouTubeDownloader"""

    def setup_method(self):
        self.downloader = YouTubeDownloader()

    def test_match_url_valid(self, sample_urls):
        """Test valid YouTube URL matching"""
        assert self.downloader.match_url(sample_urls["youtube"])
        assert self.downloader.match_url(sample_urls["youtube_short"])
        assert self.downloader.match_url(sample_urls["youtube_shorts"])

    def test_match_url_invalid(self, sample_urls):
        """Test invalid URL rejection"""
        assert not self.downloader.match_url(sample_urls["instagram_post"])
        assert not self.downloader.match_url(sample_urls["invalid"])

    def test_extract_id_regular(self):
        """Test extracting video ID from regular URL"""
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert self.downloader.extract_id(url) == "dQw4w9WgXcQ"

    def test_extract_id_short(self):
        """Test extracting video ID from short URL"""
        url = "https://youtu.be/dQw4w9WgXcQ"
        assert self.downloader.extract_id(url) == "dQw4w9WgXcQ"

    def test_extract_id_shorts(self):
        """Test extracting video ID from shorts URL"""
        url = "https://www.youtube.com/shorts/abc123xyz"
        assert self.downloader.extract_id(url) == "abc123xyz"

    def test_extract_id_invalid(self):
        """Test extracting ID from invalid URL"""
        url = "https://example.com/video"
        assert self.downloader.extract_id(url) is None

    @pytest.mark.asyncio
    async def test_get_video_info_mock(self):
        """Test getting video info with mock"""
        with patch.object(self.downloader, '_get_info_sync') as mock_info:
            mock_info.return_value = {
                "id": "test123",
                "title": "Test Video",
                "duration": 120,
                "formats": [
                    {"format_id": "22", "height": 720, "ext": "mp4", "filesize": 10000000},
                    {"format_id": "18", "height": 360, "ext": "mp4", "filesize": 5000000},
                ],
            }

            info = await self.downloader.get_video_info("https://youtube.com/watch?v=test123")

            assert info is not None
            assert info["id"] == "test123"
            assert info["title"] == "Test Video"
            assert len(info["formats"]) > 0

    def test_iter_cookie_modes_prefers_cookies_when_auth_cookiefile_ready(self):
        self.downloader._runtime_cookiefile = Path("/tmp/youtube-auth-cookies.txt")
        self.downloader._prefer_cookies = True

        assert self.downloader._iter_cookie_modes() == [True, False]

    def test_build_runtime_cookiefile_filters_noise(self, tmp_path):
        source = tmp_path / "youtube.txt"
        source.write_text(
            "\n".join(
                [
                    "# Netscape HTTP Cookie File",
                    ".youtube.com\tTRUE\t/\tFALSE\t1811328417\tSID\tsid-value",
                    ".youtube.com\tTRUE\t/\tTRUE\t1811328417\tSAPISID\tsapisid-value",
                    ".youtube.com\tTRUE\t/\tFALSE\t1777032140\tST-1ozca0d\tvery-large-state-cookie",
                    ".youtube.com\tTRUE\t/\tTRUE\t1811343661\tLOGIN_INFO\tlogin-info-value",
                ]
            )
            + "\n"
        )

        runtime = self.downloader._build_runtime_cookiefile(source)

        assert runtime is not None
        assert runtime.exists()
        content = runtime.read_text()
        assert "\tSID\t" in content
        assert "\tSAPISID\t" in content
        assert "\tLOGIN_INFO\t" in content
        assert "ST-1ozca0d" not in content


class TestInstagramDownloader:
    """Tests for InstagramDownloader"""

    def setup_method(self):
        self.downloader = InstagramDownloader()

    def test_match_url_post(self, sample_urls):
        """Test Instagram post URL matching"""
        assert self.downloader.match_url(sample_urls["instagram_post"])

    def test_match_url_reel(self, sample_urls):
        """Test Instagram reel URL matching"""
        assert self.downloader.match_url(sample_urls["instagram_reel"])

    def test_match_url_invalid(self, sample_urls):
        """Test invalid URL rejection"""
        assert not self.downloader.match_url(sample_urls["youtube"])

    def test_extract_id_post(self):
        """Test extracting shortcode from post URL"""
        url = "https://www.instagram.com/p/ABC123xyz/"
        assert self.downloader.extract_id(url) == "ABC123xyz"

    def test_extract_id_reel(self):
        """Test extracting shortcode from reel URL"""
        url = "https://www.instagram.com/reel/XYZ789abc/"
        assert self.downloader.extract_id(url) == "XYZ789abc"


class TestTikTokDownloader:
    """Tests for TikTokDownloader"""

    def setup_method(self):
        self.downloader = TikTokDownloader()

    def test_match_url_video(self, sample_urls):
        """Test TikTok video URL matching"""
        assert self.downloader.match_url(sample_urls["tiktok"])

    def test_match_url_short(self, sample_urls):
        """Test TikTok short URL matching"""
        assert self.downloader.match_url(sample_urls["tiktok_short"])

    def test_match_url_invalid(self, sample_urls):
        """Test invalid URL rejection"""
        assert not self.downloader.match_url(sample_urls["youtube"])

    def test_extract_id(self):
        """Test extracting video ID"""
        url = "https://www.tiktok.com/@user/video/1234567890123456789"
        assert self.downloader.extract_id(url) == "1234567890123456789"


class TestPinterestDownloader:
    """Tests for PinterestDownloader"""

    def setup_method(self):
        self.downloader = PinterestDownloader()

    def test_match_url_pin(self, sample_urls):
        """Test Pinterest pin URL matching"""
        assert self.downloader.match_url(sample_urls["pinterest"])

    def test_match_url_short(self, sample_urls):
        """Test Pinterest short URL matching"""
        assert self.downloader.match_url(sample_urls["pinterest_short"])

    def test_match_url_invalid(self, sample_urls):
        """Test invalid URL rejection"""
        assert not self.downloader.match_url(sample_urls["youtube"])

    def test_extract_id(self):
        """Test extracting pin ID"""
        url = "https://www.pinterest.com/pin/123456789012345678/"
        assert self.downloader.extract_id(url) == "123456789012345678"
