from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


# Путь к storage/cookies/
COOKIES_DIR = Path(__file__).resolve().parent.parent / "storage" / "cookies"


def generate_tokens() -> tuple[str, str] | None:
    """Генерирует po_token и visitor_data через Node.js."""
    try:
        result = subprocess.run(
            ["npx", "--yes", "@iv-org/youtube-po-token-generator", "--json"],
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            print(f"[ERROR] Generator failed: {result.stderr}", file=sys.stderr)
            return None

        # Парсим JSON вывод
        data = json.loads(result.stdout.strip())
        po_token = data.get("poToken") or data.get("po_token")
        visitor_data = data.get("visitorData") or data.get("visitor_data")

        if not po_token or not visitor_data:
            print(f"[ERROR] Unexpected output: {result.stdout}", file=sys.stderr)
            return None

        return po_token, visitor_data

    except subprocess.TimeoutExpired:
        print("[ERROR] Token generation timed out", file=sys.stderr)
        return None
    except json.JSONDecodeError as exc:
        print(f"[ERROR] Invalid JSON: {exc}", file=sys.stderr)
        return None
    except FileNotFoundError:
        print("[ERROR] npx not found. Install Node.js first.", file=sys.stderr)
        return None


def save_tokens(po_token: str, visitor_data: str) -> bool:
    """Сохраняет токены в файлы."""
    COOKIES_DIR.mkdir(parents=True, exist_ok=True)

    try:
        (COOKIES_DIR / "po_token.txt").write_text(po_token.strip(), encoding="utf-8")
        (COOKIES_DIR / "visitor_data.txt").write_text(visitor_data.strip(), encoding="utf-8")
        print(f"[OK] Tokens saved to {COOKIES_DIR}")
        print(f"     po_token:      {po_token[:20]}...")
        print(f"     visitor_data:  {visitor_data[:20]}...")
        return True
    except OSError as exc:
        print(f"[ERROR] Cannot save tokens: {exc}", file=sys.stderr)
        return False


def main() -> int:
    print("[*] Generating YouTube PO Token...")
    tokens = generate_tokens()

    if not tokens:
        print("[FAIL] Could not generate tokens", file=sys.stderr)
        return 1

    po_token, visitor_data = tokens

    if not save_tokens(po_token, visitor_data):
        return 1

    print("[DONE] Tokens updated successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())