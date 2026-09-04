"""Shared TDLib authorization flow for userbots (pytdbot).

Used by both the main bot and create_session.py / test scripts.
"""

from __future__ import annotations

import asyncio

from pytdbot import types


class _NoTtyError(RuntimeError):
    """Raised when interactive input is impossible (detached container)."""


_tty_warned = False


async def _ainput(prompt: str = "") -> str:
    try:
        return await asyncio.to_thread(input, prompt)
    except (EOFError, OSError) as e:
        raise _NoTtyError(str(e))


def _warn_no_tty(what: str) -> None:
    global _tty_warned
    if _tty_warned:
        return
    _tty_warned = True
    print(
        f"  ✗ TDLib ждёт {what}, но ввода нет (контейнер без TTY).\n"
        "    Залогинься отдельно и перезапусти бота:\n"
        "      docker compose stop bot\n"
        "      docker compose run --rm -it bot python create_session.py\n"
        "      docker compose up -d bot"
    )


def register_auth_handlers(client, phone_hint: str = "") -> None:
    """Register on_updateAuthorizationState handler for interactive login.

    If phone_hint is provided (settings.PHONE_NUMBER), it is tried first
    without prompting.
    """

    @client.on_updateAuthorizationState()
    async def _auth_handler(c, update: types.UpdateAuthorizationState) -> None:
        state = update.authorization_state

        if isinstance(state, types.AuthorizationStateWaitPhoneNumber):
            phone = (phone_hint or "").strip()
            if not phone:
                try:
                    phone = (await _ainput("  Номер (+79123456789): ")).strip()
                except _NoTtyError:
                    _warn_no_tty("номер телефона")
                    return
                if not phone:
                    print("  Номер не введён.")
                    return
            res = await c.setAuthenticationPhoneNumber(phone_number=phone)
            if isinstance(res, types.Error):
                print(f"  Ошибка номера: {res.message}")
            return

        if isinstance(state, types.AuthorizationStateWaitCode):
            try:
                code = (await _ainput("  Код из Telegram: ")).strip()
            except _NoTtyError:
                _warn_no_tty("код из Telegram")
                return
            if not code:
                print("  Код не введён.")
                return
            res = await c.checkAuthenticationCode(code=code)
            if isinstance(res, types.Error):
                print(f"  Ошибка кода: {res.message}")
            return

        if isinstance(state, types.AuthorizationStateWaitPassword):
            hint = getattr(state, "password_hint", "")
            try:
                pw = (await _ainput(f"  Пароль 2FA (hint: {hint}): ")).strip()
            except _NoTtyError:
                _warn_no_tty("пароль 2FA")
                return
            if not pw:
                print("  Пароль не введён.")
                return
            res = await c.checkAuthenticationPassword(password=pw)
            if isinstance(res, types.Error):
                print(f"  Ошибка 2FA: {res.message}")
            return

        if isinstance(state, types.AuthorizationStateReady):
            me = await c.getMe()
            if not isinstance(me, types.Error):
                username = ""
                if me.usernames and me.usernames.editable_username:
                    username = f" (@{me.usernames.editable_username})"
                print(f"  ✓ TDLib login: {me.first_name}{username} ID={me.id}")
