import asyncio
import logging
import os
import signal
from config import settings
from core.db import init_db
from core.bot import MilaUserbot


def _setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Quiet noisy libs; our prints stay for now (migrated incrementally).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def main():
    _setup_logging()
    os.makedirs(settings.PHOTOS_DIR, exist_ok=True)
    os.makedirs(getattr(settings, "MEDIA_DIR", "data/media"), exist_ok=True)
    os.makedirs(settings.DAILY_REPORTS, exist_ok=True)
    os.makedirs(settings.LOG_DIR, exist_ok=True)

    init_db()
    bot = MilaUserbot()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop(*_args):
        stop_event.set()

    # SIGTERM (docker stop) + SIGINT (Ctrl-C) → graceful shutdown.
    # add_signal_handler is Unix-only; fall back to signal.signal on Windows.
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _request_stop)
    except (NotImplementedError, RuntimeError):
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, lambda *_a: loop.call_soon_threadsafe(stop_event.set))
            except (OSError, ValueError):
                pass

    await bot.start()
    try:
        await stop_event.wait()
    finally:
        await bot.stop()

if __name__ == "__main__":
    asyncio.run(main())
