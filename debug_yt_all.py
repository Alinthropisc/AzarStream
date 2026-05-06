import yt_dlp
import json
import os

url = "https://www.youtube.com/watch?v=SdXx7Y8SzlA"
cookie_file = "storage/cookies/youtube.txt"

ydl_opts = {
    'quiet': False,
    'no_warnings': False,
    'cookiefile': cookie_file,
    'extractor_args': {
        'youtube': {
            'player_client': ['android', 'web', 'ios', 'mweb'],
        }
    }
}
try:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        formats = info.get('formats', [])
        for f in formats:
             print(f"ID: {f.get('format_id')}, Res: {f.get('height')}p, Ext: {f.get('ext')}, Vcodec: {f.get('vcodec')}")
except Exception as e:
    print(f"Error: {e}")
