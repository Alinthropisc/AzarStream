from __future__ import annotations

import os
import sys


def run_server() -> None:
    import granian
    from granian.constants import Interfaces

    # Логирование настраивается здесь — один раз, до старта сервера
    from app.logging import setup_logging
    setup_logging()

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 8000))
    
    # Автоматически ставим 2-4 workers если не указано
    # Для веба нужно минимум 2 workers чтобы обрабатывать concurrent запросы
    workers = int(os.environ.get("WORKERS", 1))

    debug = os.environ.get("DEBUG", "false").lower() in ("1", "true", "yes")

    kwargs = dict(
        target="app.lifecycle:app",
        address=host,
        port=port,
        interface=Interfaces.ASGI,
        workers=workers,
        reload=debug,  # Reload только в debug mode
        websockets=False,
    )

    granian.Granian(**kwargs).serve()


def run_worker() -> None:
    import asyncio
    from arq import run_worker as arq_run_worker
    from workers.worker import WorkerSettings

    # Для Python 3.10+ нужно явно создать event loop ДО установки uvloop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # uvloop даёт ~20% прироста на Linux, но не обязателен
    if sys.platform != "win32":
        try:
            import uvloop
            uvloop.install()
            # Пересоздаем loop с uvloop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        except Exception as e:
            print(f"⚠️  uvloop не установлен, используем стандартный asyncio: {e}")

    from app.logging import setup_logging
    setup_logging()

    arq_run_worker(WorkerSettings)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="MediaFlow entrypoint")
    parser.add_argument(
        "command",
        choices=["server", "worker"],
        default="server",
        nargs="?",
        help="What to run (default: server)",
    )
    args = parser.parse_args()

    match args.command:
        case "worker":
            run_worker()
        case _:
            run_server()


if __name__ == "__main__":
    main()