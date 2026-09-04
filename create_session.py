"""
First-time setup script for pytdbot (TDLib userbot).
Runs once on the host.
Creates the TDLib database directory, which is then used later.

Usage:
    python create_session.py

After running, enter your phone number and the code received from Telegram.
"""
import asyncio
import sys

# Python 3.14+ requires explicit event loop in main thread
if sys.version_info >= (3, 14):
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

from pytdbot import Client, types
from config import settings
from core.tg_auth import register_auth_handlers


async def main():
    print("=== Creating TDLib Session (pytdbot) ===")
    print(f"API_ID: {settings.API_ID}")
    print(f"API_HASH: {settings.API_HASH[:8]}...")
    print(f"PHONE_NUMBER: {settings.PHONE_NUMBER or 'not specified (will be requested)'}")
    print(f"TD_FILES_DIRECTORY: {settings.TD_FILES_DIRECTORY}")
    print()

    app = Client(
        api_id=settings.API_ID,
        api_hash=settings.API_HASH,
        files_directory=settings.TD_FILES_DIRECTORY,
        database_encryption_key=settings.TD_DATABASE_ENCRYPTION_KEY,
        device_model=settings.DEVICE_MODEL,
        td_verbosity=settings.TD_VERBOSITY,
        user_bot=True,
    )
    register_auth_handlers(app, phone_hint=settings.PHONE_NUMBER or "")

    ready = asyncio.Event()

    @app.on_updateAuthorizationState()
    async def _ready_handler(c: Client, update: types.UpdateAuthorizationState):
        if isinstance(update.authorization_state, types.AuthorizationStateReady):
            ready.set()

    await app.start(wait_login=False)
    try:
        await asyncio.wait_for(ready.wait(), timeout=300)
    except asyncio.TimeoutError:
        print("\n✗ Login timed out.")
        await app.stop()
        return

    me = await app.getMe()
    if isinstance(me, types.Error):
        print(f"\n✗ getMe failed: {me.message}")
    else:
        username = ""
        if me.usernames and me.usernames.editable_username:
            username = f" (@{me.usernames.editable_username})"
        print(f"\n✓ Session created! Account: {me.first_name}{username}")
        print(f"  Directory: {settings.TD_FILES_DIRECTORY}/")
        print(f"  ID: {me.id}")

    await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
