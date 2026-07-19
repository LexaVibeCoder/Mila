#!/usr/bin/env python3
"""
Скрипт для первичной авторизации Pyrogram (userbot).
Запускается один раз на хосте (не в Docker).
Создаёт файл mila_session.session, который потом используется в Docker.

Использование:
    python create_session.py

После запуска введи номер телефона и код из Telegram.
"""
import asyncio
import sys

# Python 3.14+ requires explicit event loop in main thread
if sys.version_info >= (3, 14):
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

from pyrogram import Client
from config import settings


async def main():
    print("=== Создание сессии Pyrogram ===")
    print(f"API_ID: {settings.API_ID}")
    print(f"API_HASH: {settings.API_HASH[:8]}...")
    print(f"PHONE_NUMBER: {settings.PHONE_NUMBER or 'не указан (будет запрошен)'}")
    print()

    app = Client(
        "mila_session",
        api_id=settings.API_ID,
        api_hash=settings.API_HASH,
        phone_number=settings.PHONE_NUMBER or None,
        workdir="."
    )

    async with app:
        me = await app.get_me()
        print(f"\n✓ Сессия создана! Аккаунт: {me.first_name} (@{me.username})")
        print(f"  Файл: mila_session.session")
        print(f"  ID: {me.id}")


if __name__ == "__main__":
    asyncio.run(main())