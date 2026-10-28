"""Shared TDLib authorization flow for userbots (pytdbot).

Used by both the main bot and create_session.py / test scripts.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from typing import Any

from pytdbot import types


class _NoTtyError(RuntimeError):
    """Raised when interactive input is impossible (detached container)."""


_tty_warned = False

# States TDLib emits as part of normal start/stop; no user input needed.
_INTERNAL_STATES = {
    "AuthorizationStateWaitTdlibParameters",
    "AuthorizationStateWaitEncryptionKey",
    "AuthorizationStateLoggingOut",
    "AuthorizationStateClosing",
    "AuthorizationStateClosed",
}

_CODE_DELIVERY = {
    "AuthenticationCodeTypeTelegramMessage": (
        "код отправлен сообщением в Telegram — смотрите чат «Telegram» (777000) "
        "в уже авторизованном приложении; это НЕ SMS"
    ),
    "AuthenticationCodeTypeSms": "код отправлен по SMS",
    "AuthenticationCodeTypeSmsWord": "SMS содержит слово из кода",
    "AuthenticationCodeTypeSmsPhrase": "SMS содержит фразу из кода",
    "AuthenticationCodeTypeCall": "код продиктуют голосовым звонком",
    "AuthenticationCodeTypeFlashCall": "код придёт во flash-call (номер входящего звонка)",
    "AuthenticationCodeTypeMissedCall": "код — последние цифры пропущенного звонка",
    "AuthenticationCodeTypeFragment": "код придёт через Fragment",
    "AuthenticationCodeTypeFirebaseAndroid": "код придёт через Firebase (Android)",
    "AuthenticationCodeTypeFirebaseIos": "код придёт через Firebase (iOS)",
}

_RESEND_WORDS = {"resend", "повтор", "повторить", "отправить", "ещё", "еще"}
_QR_WORDS = {"qr", "qr-код", "qrкод", "куар"}


_stdin_buffer = b""


def _read_line_console(prompt: str) -> str:
    """Read a line from a Windows console without the buffered-reader lock.

    ``input()`` on a console thread keeps the ``sys.stdin`` lock, so a
    blocked daemon thread makes CPython abort on shutdown. ``msvcrt``
    reads wide chars directly and keeps Unicode (2FA passwords) intact.
    """
    import _thread
    import msvcrt

    sys.stdout.write(prompt)
    sys.stdout.flush()

    chars: list[str] = []
    while True:
        ch = msvcrt.getwch()
        if ch in ("\r", "\n"):
            sys.stdout.write("\n")
            sys.stdout.flush()
            break
        if ch == "\x03":  # Ctrl+C: raise in the main thread, stop reading here
            _thread.interrupt_main()
            raise EOFError("interrupted")
        if ch == "\x1a":  # Ctrl+Z
            raise EOFError("stdin closed")
        if ch == "\b":
            if chars:
                chars.pop()
                sys.stdout.write("\b \b")
                sys.stdout.flush()
            continue
        if ch in ("\x00", "\xe0"):  # arrows / function keys: skip pair
            msvcrt.getwch()
            continue
        chars.append(ch)
        sys.stdout.write(ch)
        sys.stdout.flush()
    return "".join(chars)


def _read_line_fd(prompt: str) -> str:
    """Read one line from fd 0 without taking the ``sys.stdin`` buffer lock.

    ``input()``/``sys.stdin.readline()`` lock the buffered reader while
    waiting. If such a daemon thread is still blocked on exit, CPython
    aborts with ``_enter_buffered_busy``. Reading the raw fd in canonical
    mode (line editing and echo are done by the terminal) avoids that.
    """
    global _stdin_buffer
    sys.stdout.write(prompt)
    sys.stdout.flush()

    while b"\n" not in _stdin_buffer:
        chunk = os.read(0, 4096)
        if not chunk:
            if not _stdin_buffer:
                raise EOFError("stdin closed")
            break
        _stdin_buffer += chunk

    if b"\n" in _stdin_buffer:
        line, _, _stdin_buffer = _stdin_buffer.partition(b"\n")
    else:
        line, _stdin_buffer = _stdin_buffer, b""
    return line.decode(errors="replace").rstrip("\r")


def _read_line_blocking(prompt: str) -> str:
    if sys.platform == "win32":
        try:
            is_tty = sys.stdin is not None and sys.stdin.isatty()
        except (ValueError, OSError):
            is_tty = False
        if is_tty:
            return _read_line_console(prompt)
    return _read_line_fd(prompt)


async def _ainput(prompt: str = "") -> str:
    """Read a line from stdin in a daemon thread (cancellable on shutdown)."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def _deliver(result: str | None = None, exc: BaseException | None = None) -> None:
        if future.done():
            return
        if exc is not None:
            future.set_exception(exc)
        else:
            future.set_result(result or "")

    def _read() -> None:
        try:
            line = _read_line_blocking(prompt)
        except (EOFError, OSError) as e:
            outcome: dict[str, Any] = {"exc": _NoTtyError(str(e))}
        else:
            outcome = {"result": line}
        try:
            if "exc" in outcome:
                loop.call_soon_threadsafe(_deliver, None, outcome["exc"])
            else:
                loop.call_soon_threadsafe(_deliver, outcome["result"])
        except RuntimeError:
            # Event loop is already closed (timeout / Ctrl+C) - nothing to do.
            pass

    threading.Thread(target=_read, daemon=True).start()
    return await future


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
        "      docker compose up -d bot",
        flush=True,
    )


def _type_name(obj: Any) -> str:
    if obj is None:
        return ""
    if isinstance(obj, dict):
        return str(obj.get("@type", ""))
    return type(obj).__name__


def _describe_code(info: Any) -> None:
    """Print where the code was sent and whether resend is possible."""
    if info is None:
        return
    if isinstance(info, dict):
        code_type = info.get("type") or {}
        name = str(code_type.get("@type", ""))
        length = code_type.get("length")
        next_type = info.get("next_type")
        timeout = info.get("timeout") or 0
    else:
        code_type = getattr(info, "type", None)
        name = _type_name(code_type)
        length = getattr(code_type, "length", None)
        next_type = getattr(info, "next_type", None)
        timeout = getattr(info, "timeout", 0) or 0

    hint = _CODE_DELIVERY.get(name)
    print(f"  {hint}" if hint else f"  Способ доставки: {name or 'неизвестен'}")
    if length:
        print(f"  Длина кода: {length}")

    next_name = (
        str(next_type.get("@type", "")) if isinstance(next_type, dict) else _type_name(next_type)
    )
    if not next_name:
        print(
            "  Резервной доставки (SMS/звонок) нет: Telegram не предлагает "
            "переотправку для этого входа."
        )
    else:
        next_hint = _CODE_DELIVERY.get(next_name, next_name)
        when = f" через {timeout} с" if timeout else ""
        print(f"  Альтернатива{when}: {next_hint}. Введите 'resend', чтобы запросить.")


def register_auth_handlers(
    client, phone_hint: str = "", use_qr: bool = False
) -> None:
    """Register on_updateAuthorizationState handler for interactive login.

    If phone_hint is provided (settings.PHONE_NUMBER), it is tried first
    without prompting. If use_qr is true, QR login is requested instead of
    sending a phone number (no code needed; confirm on another device).
    """
    sent_phone_this_run = False

    @client.on_updateAuthorizationState()
    async def _auth_handler(c, update: types.UpdateAuthorizationState) -> None:
        nonlocal sent_phone_this_run
        state = update.authorization_state
        name = _type_name(state)

        if isinstance(state, types.AuthorizationStateWaitPhoneNumber):
            if use_qr:
                res = await c.requestQrCodeAuthentication()
                if not isinstance(res, types.Error):
                    return
                print(f"  QR-вход недоступен: {res.message}. Пробую по номеру.")

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
            else:
                sent_phone_this_run = True
                print(f"  Запрос кода отправлен для {phone}.")
            return

        if isinstance(state, types.AuthorizationStateWaitCode):
            _describe_code(getattr(state, "code_info", None))
            if not sent_phone_this_run:
                print(
                    "  ⚠ TDLib восстановил ожидание кода из прошлой попытки: "
                    "новый код НЕ отправлялся."
                )
                print(
                    "    Введите код, который уже пришёл, либо удалите data/tdlib "
                    "и запустите заново, чтобы запросить новый."
                )
            try:
                code = (await _ainput("  Код из Telegram (или 'resend' / 'qr'): ")).strip()
            except _NoTtyError:
                _warn_no_tty("код из Telegram")
                return
            if not code:
                print("  Код не введён.")
                return
            if code.lower() in _QR_WORDS:
                res = await c.requestQrCodeAuthentication()
                if isinstance(res, types.Error):
                    print(f"  QR-вход недоступен: {res.message}")
                    print(
                        "    Удалите каталог TDLib (обычно data/tdlib) и запустите:\n"
                        "      ./start/create_session.sh --qr"
                    )
                return
            if code.lower() in _RESEND_WORDS:
                res = await c.resendAuthenticationCode()
                if isinstance(res, types.Error):
                    print(f"  Переотправка недоступна: {res.message}")
                    print(
                        "  Telegram не даёт для этой попытки резервный канал, "
                        "поэтому новый код не отправить."
                    )
                    print(
                        "  Варианты:\n"
                        "    • посмотреть код в чате «Telegram» (777000) в приложении;\n"
                        "    • удалить каталог TDLib (обычно data/tdlib) и запросить код заново;\n"
                        "    • войти по QR: введите 'qr' здесь же либо перезапустите с --qr"
                    )
                else:
                    print("  Код запрошен повторно.")
                return
            res = await c.checkAuthenticationCode(code=code)
            if isinstance(res, types.Error):
                print(f"  Ошибка кода: {res.message}")
            return

        if isinstance(state, types.AuthorizationStateWaitEmailAddress):
            print("  Telegram просит email для подтверждения входа.")
            try:
                email = (await _ainput("  Email: ")).strip()
            except _NoTtyError:
                _warn_no_tty("email")
                return
            if not email:
                print("  Email не введён.")
                return
            res = await c.setAuthenticationEmailAddress(email_address=email)
            if isinstance(res, types.Error):
                print(f"  Ошибка email: {res.message}")
            return

        if isinstance(state, types.AuthorizationStateWaitEmailCode):
            info = getattr(state, "code_info", None)
            pattern = getattr(info, "email_address_pattern", "") if info else ""
            length = getattr(info, "length", 0) if info else 0
            print(f"  Код отправлен на email {pattern or '(адрес скрыт)'}")
            if length:
                print(f"  Длина кода: {length}")
            try:
                code = (await _ainput("  Код из письма (или 'resend'): ")).strip()
            except _NoTtyError:
                _warn_no_tty("код из письма")
                return
            if not code:
                print("  Код не введён.")
                return
            if code.lower() in _RESEND_WORDS:
                res = await c.resendAuthenticationCode()
                if isinstance(res, types.Error):
                    print(f"  Переотправка недоступна: {res.message}")
                else:
                    print("  Код запрошен повторно.")
                return
            res = await c.checkAuthenticationEmailCode(
                code=types.EmailAddressAuthenticationCode(code=code)
            )
            if isinstance(res, types.Error):
                print(f"  Ошибка кода: {res.message}")
            return

        if isinstance(state, types.AuthorizationStateWaitRegistration):
            print("  Это регистрация нового аккаунта Telegram.")
            try:
                first_name = (await _ainput("  Имя: ")).strip()
                last_name = (await _ainput("  Фамилия (можно пусто): ")).strip()
            except _NoTtyError:
                _warn_no_tty("имя для регистрации")
                return
            if not first_name:
                print("  Имя не введено.")
                return
            res = await c.registerUser(
                first_name=first_name, last_name=last_name
            )
            if isinstance(res, types.Error):
                print(f"  Ошибка регистрации: {res.message}")
            return

        if isinstance(state, types.AuthorizationStateWaitPremiumPurchase):
            print(
                "  ✗ Telegram требует Telegram Premium для входа с этого устройства.\n"
                "    Войдите через официальное приложение Telegram и повторите позже."
            )
            return

        if isinstance(state, types.AuthorizationStateWaitOtherDeviceConfirmation):
            link = getattr(state, "link", "")
            print(
                "  Подтвердите вход: откройте ссылку на телефоне, где Telegram уже\n"
                "  авторизован в этом аккаунте (ссылка периодически обновляется):"
            )
            print(f"    {link}")
            return

        if isinstance(state, types.AuthorizationStateReady):
            me = await c.getMe()
            if not isinstance(me, types.Error):
                username = ""
                if me.usernames and me.usernames.editable_username:
                    username = f" (@{me.usernames.editable_username})"
                print(f"  ✓ TDLib login: {me.first_name}{username} ID={me.id}")
            return

        if name not in _INTERNAL_STATES:
            print(f"  [auth] Состояние {name} не обрабатывается скриптом.")
