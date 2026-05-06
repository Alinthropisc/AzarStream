import yt_dlp
import json

def test_clients(url):
    cookie_path = '/home/sayavdera/Desktop/projects/TelegramBots/MediaFlow/storage/cookies/youtube.txt'
    
    test_cases = [
        {"name": "No cookies, Mobile clients", "cookies": False, "clients": ["ios", "android", "mweb"]},
        {"name": "With cookies, Desktop clients", "cookies": True, "clients": ["web", "tv"]},
        {"name": "With cookies, All clients", "cookies": True, "clients": ["ios", "android", "mweb", "web", "tv"]},
        {"name": "No cookies, All clients", "cookies": False, "clients": ["ios", "android", "mweb", "web", "tv"]},
    ]

    for case in test_cases:
        print(f"\n--- Testing: {case['name']} ---")
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extractor_args': {
                'youtube': {
                    'player_client': case['clients'],
                }
            },
        }
        if case['cookies']:
            ydl_opts['cookiefile'] = cookie_path

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                formats = info.get('formats', [])
                resolutions = sorted(list(set([f.get('height') for f in formats if f.get('height')])))
                print(f"Success! Found resolutions: {resolutions}")
            except Exception as e:
                print(f"Failed: {str(e)[:100]}")

if __name__ == "__main__":
    url = "https://www.youtube.com/watch?v=V7ZLyfIb0Z0"
    test_clients(url)
