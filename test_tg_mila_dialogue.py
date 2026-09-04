#!/usr/bin/env python3
"""
ТЕСТ: Твой аккаунт ↔ Мила (через Telegram, pytdbot/TDLib)
"""

import asyncio
import json
import sys
import traceback
import os
from datetime import datetime
from typing import List, Dict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import settings
from core.llm.client import LLMClient

DEFAULT_ROUNDS = 5

TESTER_PROMPT = (
    "Ты — обычный парень {user_name}, пишешь девушке Миле в Telegram.\n"
    "Отвечай коротко (1-3 предлож.), неформально, развивай её тему.\n"
    "Не пиши как бот. Не знаешь, что Мила — ИИ.\n"
    "Напиши естественный ответ на последнее сообщение Милы."
)


class TesterLLM:
    def __init__(self):
        self.client = LLMClient()

    async def gen(self, history: List[Dict[str, str]], user_name: str) -> str:
        msgs = [{"role": m["role"], "content": m["content"]} for m in history[-6:]]
        msgs.append({"role": "user", "content": f"Ответь как {user_name}:"})
        try:
            r = await self.client.generate(
                messages=msgs,
                system_prompt=TESTER_PROMPT.format(user_name=user_name),
                max_tokens=256, temperature=0.85
            )
            return r["thoughts"].strip().strip('"').strip("'").strip()
        except Exception as e:
            print(f"  [TESTER] {e}")
            return "Расскажи ещё!"


class Tester:
    def __init__(self):
        self.client = None
        self.me = None
        self.target_id = None
        self.target_raw = None
        self.history: List[Dict[str, str]] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.name = "Пользователь"
        self.log_dir = None
        self.FILES_DIRECTORY = "data/tdlib_test"
        self.PAUSE = 2
        self._ready = asyncio.Event()

    async def connect(self):
        from pytdbot import Client, types
        from core.tg_auth import register_auth_handlers

        print("\n" + "=" * 50)
        phone = input("  Номер (+79123456789): ").strip()
        if not phone:
            sys.exit(1)

        self.client = Client(
            api_id=settings.API_ID,
            api_hash=settings.API_HASH,
            files_directory=self.FILES_DIRECTORY,
            database_encryption_key=settings.TD_DATABASE_ENCRYPTION_KEY,
            device_model=settings.DEVICE_MODEL,
            td_verbosity=settings.TD_VERBOSITY,
            user_bot=True,
        )
        register_auth_handlers(self.client, phone_hint=phone)

        @self.client.on_updateAuthorizationState()
        async def _ready_handler(c: Client, update: types.UpdateAuthorizationState):
            if isinstance(
                update.authorization_state, types.AuthorizationStateReady
            ):
                self._ready.set()

        await self.client.start(wait_login=False)
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=300)
        except asyncio.TimeoutError:
            print("  ✗ Login timed out.")
            await self.client.stop()
            sys.exit(1)

        self.me = await self.client.getMe()
        if isinstance(self.me, types.Error):
            print(f"  ✗ getMe failed: {self.me.message}")
            await self.client.stop()
            sys.exit(1)
        self.name = self.me.first_name or "Пользователь"
        username = ""
        if self.me.usernames and self.me.usernames.editable_username:
            username = f" (@{self.me.usernames.editable_username})"
        print(f"\n  ✓ Готов: {self.name}{username} ID={self.me.id}")
        print(f"  ✓ TDLib dir: {self.FILES_DIRECTORY}/")

    async def resolve(self):
        from pytdbot import types

        ident = input("\n  Получатель (@username или ID): ").strip()
        if not ident:
            await self.client.stop()
            sys.exit(1)
        self.target_raw = ident

        if ident.lstrip('-').isdigit():
            self.target_id = int(ident)
        else:
            clean = ident.lstrip('@')
            chat = await self.client.searchPublicChat(username=clean)
            if isinstance(chat, types.Error):
                print(f"  Не найден: {chat.message}")
                print("  (TDLib-тестер резолвит только @username и numeric ID)")
                await self.client.stop()
                sys.exit(1)
            self.target_id = chat.id

        chat = await self.client.getChat(chat_id=self.target_id)
        if isinstance(chat, types.Error):
            print(f"  Не найден: {chat.message}")
            await self.client.stop()
            sys.exit(1)
        print(f"  Цель: {chat.title} (ID={self.target_id})")

    async def on_msg(self, client, msg):
        try:
            print(f"  [DEBUG] chat={msg.chat_id} from={msg.from_id} "
                  f"me={self.me.id if self.me else '?'} out={msg.is_outgoing} "
                  f"text='{str(msg.text or '')[:80]}'")
        except Exception:
            pass

        if msg.is_outgoing:
            return
        if msg.from_id and self.me and msg.from_id == self.me.id:
            return
        if msg.chat_id != self.target_id:
            return

        text = (msg.text or "").strip()
        if not text:
            return

        await self.queue.put({"text": text, "mid": msg.id})
        print(f"\n  📩 {text[:120]}")

    async def run(self, rounds: int):
        from pytdbot import types

        folder = input("\n  Папка логов: ").strip() or f"test_{datetime.now():%Y%m%d_%H%M%S}"
        self.log_dir = os.path.join(settings.LOG_DIR, folder)
        os.makedirs(self.log_dir, exist_ok=True)

        # Replaces pyrogram MessageHandler(..., filters.private):
        # inner_object=True gives types.Message; private/text filtering
        # is done inside on_msg + checks below.
        self.client.add_handler(
            "updateNewMessage", self.on_msg, inner_object=True
        )
        print("  Обработчик → OK")

        llm = TesterLLM()
        print(f"\n{'='*60}")
        print(f"  ДИАЛОГ: {self.name} ↔ Мила ({rounds} раундов)")
        print(f"{'='*60}")

        turns = []

        async def _send(text: str) -> None:
            res = await self.client.sendTextMessage(
                chat_id=self.target_id, text=text
            )
            if isinstance(res, types.Error):
                raise RuntimeError(f"send failed: {res.code} {res.message}")

        # Раунд 1
        print(f"\n  РАУНД 1")
        await _send("Привет, как дела?")
        self.history.append({"role": "user", "content": "Привет, как дела?"})
        print("  → Привет, как дела?")

        try:
            data = await asyncio.wait_for(self.queue.get(), timeout=30)
            reply = data["text"]
            self.history.append({"role": "assistant", "content": reply})
            print(f"  Мила: {reply[:100]}")
            turns.append({"turn": 1, "user": "Привет, как дела?", "mila": reply})
        except asyncio.TimeoutError:
            print("  ❌ Нет ответа 30с. Тест прерван.")
            return turns

        # Раунды 2+
        for i in range(1, rounds):
            print(f"\n  РАУНД {i+1}")
            await asyncio.sleep(self.PAUSE)

            msg = await llm.gen(self.history, self.name)
            print(f"  {self.name}: {msg}")
            await _send(msg)
            self.history.append({"role": "user", "content": msg})

            try:
                data = await asyncio.wait_for(self.queue.get(), timeout=30)
                reply = data["text"]
                self.history.append({"role": "assistant", "content": reply})
                print(f"  Мила: {reply[:100]}")
                turns.append({"turn": i + 1, "user": msg, "mila": reply})
            except asyncio.TimeoutError:
                print("  ❌ Нет ответа. Тест прерван.")
                break

        return turns

    async def stop(self):
        if self.client:
            await self.client.stop()
            print("  ✓ Стоп")


async def main():
    print("=" * 60)
    print("  ТЕСТ: Твой → Мила (Telegram)")
    print(f"  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 60)

    r = input("\n  Раундов (Enter=5): ").strip()
    rounds = int(r) if r.isdigit() and 1 <= int(r) <= 15 else DEFAULT_ROUNDS

    t = Tester()
    try:
        await t.connect()
        await t.resolve()
        turns = await t.run(rounds)

        if not turns:
            print("  Нет данных")
            return

        # Стенограмма
        print(f"\n{'='*60}\n  СТЕНОГРАММА\n{'='*60}\n")
        for x in turns:
            print(f"── {x['turn']} ──\n  {t.name}: {x['user']}\n  Мила: {x['mila']}\n")

        # Анализ
        print(f"{'='*60}\n  АНАЛИЗ\n{'='*60}\n")
        tr = "\n".join(f"{t.name}: {x['user']}\nМила: {x['mila']}\n" for x in turns)
        try:
            ds = LLMClient()
            a = (await ds.generate(
                messages=[{"role": "user", "content": f"Анализ диалога:\n{tr}"}],
                system_prompt="Оцени человечность Милы. Что хорошо, что плохо. Вывод.",
                max_tokens=2048, temperature=0.5
            ))["thoughts"].strip()
        except Exception as e:
            a = f"Ошибка: {e}"
        print(a)

        log = os.path.join(t.log_dir, f"log_{datetime.now():%Y%m%d_%H%M%S}.json")
        with open(log, "w", encoding="utf-8") as f:
            json.dump({"user": t.name, "target": t.target_raw,
                       "turns": turns, "analysis": a}, f, ensure_ascii=False, indent=2)
        print(f"\n  ✓ {log}")

    except KeyboardInterrupt:
        print("\n  ⚡ Прервано")
    except Exception:
        traceback.print_exc()
    finally:
        if t.client and getattr(t.client, "is_running", False):
            await t.stop()


if __name__ == "__main__":
    asyncio.run(main())
