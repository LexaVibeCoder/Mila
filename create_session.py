"""
First-time setup script for pytdbot (TDLib userbot).
Runs once on the host.
Creates the TDLib database directory, which is then used later.

Usage:
    python create_session.py          # вход по коду из Telegram
    python create_session.py --qr     # вход по QR-ссылке (без кода)

After running, enter your phone number and the code received from Telegram,
or confirm the printed tg://login link on an already authorized device.
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
    if "--qr" in sys.argv[1:]:
        print("Режим: QR-вход (код не нужен, подтвердите ссылку на телефоне)")
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
    use_qr = "--qr" in sys.argv[1:]
    register_auth_handlers(
        app, phone_hint=settings.PHONE_NUMBER or "", use_qr=use_qr
    )

    ready = asyncio.Event()

    @app.on_updateAuthorizationState()
    async def _ready_handler(c: Client, update: types.UpdateAuthorizationState):
        if isinstance(update.authorization_state, types.AuthorizationStateReady):
            ready.set()

    await app.start(wait_login=False)
    try:
        await asyncio.wait_for(ready.wait(), timeout=300)
    except asyncio.TimeoutError:
        print("\n✗ Login timed out (5 minutes).")
        print("  Если код так и не пришёл:")
        print("    • Telegram мог отправить его в чат «Telegram» (777000) в уже")
        print("      авторизованном приложении — это не обязательно SMS;")
        print("    • повторный запуск не отправляет новый код: удалите")
        print(f"      {settings.TD_FILES_DIRECTORY}/ и запустите заново;")
        print("    • частые запросы кода Telegram временно блокирует — подождите")
        print("      несколько часов, прежде чем пробовать снова;")
        print("    • можно войти по QR без кода: введите 'qr' на запросе кода")
        print(f"      или: rm -rf {settings.TD_FILES_DIRECTORY} && ./start/create_session.sh --qr")
        await app.stop()
        sys.exit(1)

    me = await app.getMe()
    if isinstance(me, types.Error):
        print(f"\n✗ getMe failed: {me.message}")
        await app.stop()
        sys.exit(1)

    username = ""
    if me.usernames and me.usernames.editable_username:
        username = f" (@{me.usernames.editable_username})"
    print(f"\n✓ Session created! Account: {me.first_name}{username}")
    print(f"  Directory: {settings.TD_FILES_DIRECTORY}/")
    print(f"  ID: {me.id}")

    await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
