import yt_dlp
import json
import os

url = "https://www.youtube.com/watch?v=SdXx7Y8SzlA"
cookie_file = "storage/cookies/youtube.txt"

clients_to_test = [
    ["ios"],
    ["android"],
    ["web"],
    ["mweb"],
    ["tv"]
]

for clients in clients_to_test:
    print(f"\n--- Testing clients: {clients} WITH COOKIES ---")
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'cookiefile': cookie_file,
        'extractor_args': {
            'youtube': {
                'player_client': clients,
            }
        }
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get('formats', [])
            heights = sorted(list(set([f.get('height') for f in formats if f.get('height')])))
            print(f"Found {len(formats)} formats. Available heights: {heights}")
    except Exception as e:
        print(f"Error: {e}")
