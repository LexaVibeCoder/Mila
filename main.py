import asyncio
import os
from config import settings
from core.db import init_db
from core.bot import MilaUserbot

async def main():
    os.makedirs(settings.PHOTOS_DIR, exist_ok=True)
    os.makedirs(settings.DAILY_REPORTS, exist_ok=True)
    os.makedirs(settings.LOG_DIR, exist_ok=True)

    init_db()
    bot = MilaUserbot()
    try:
        await bot.start()
        await asyncio.Future()
    except KeyboardInterrupt:
        await bot.stop()

if __name__ == "__main__":
    asyncio.run(main())