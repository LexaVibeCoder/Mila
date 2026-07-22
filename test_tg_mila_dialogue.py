#!/usr/bin/env python3
"""
ТЕСТ: Твой аккаунт ↔ Мила (через Telegram)
"""

import asyncio
import json
import sys
import traceback
import os
from datetime import datetime
from typing import List, Dict, Optional
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
        self.SESSION = "mila_test"
        self.PAUSE = 2

    async def connect(self):
        from pyrogram import Client

        print("\n" + "=" * 50)
        phone = input("  Номер (+79123456789): ").strip()
        if not phone:
            sys.exit(1)

        self.client = Client(
            self.SESSION, api_id=settings.API_ID,
            api_hash=settings.API_HASH, workdir="."
        )

        # Сначала пробуем start() — он сам соединится, авторизуется (если есть сессия) и запустит обновления
        try:
            await self.client.start()
            self.me = await self.client.get_me()
            self.name = self.me.first_name or "Пользователь"
            print(f"\n  ✓ Сессия есть: {self.name} (@{self.me.username})")
            return
        except Exception:
            # Сессии нет, start() не смог — подключаемся вручную
            pass

        await self.client.connect()
        try:
            self.me = await self.client.get_me()
            self.name = self.me.first_name or "Пользователь"
            await self.client.initialize()
            print(f"\n  ✓ Сессия активна: {self.name}")
            return
        except Exception:
            pass  # Не авторизованы

        sent = await self.client.send_code(phone)
        code = input("  Код: ").strip()
        if not code:
            await self.client.disconnect()
            sys.exit(1)

        try:
            await self.client.sign_in(phone, sent.phone_code_hash, code)
        except Exception as e:
            if "PASSWORD" in str(e).upper() or "2FA" in str(e).upper():
                pw = input("  Пароль 2FA: ")
                await self.client.check_password(pw)
            else:
                raise

        # Запускаем приём сообщений
        await self.client.initialize()

        self.me = await self.client.get_me()
        self.name = self.me.first_name or "Пользователь"
        print(f"\n  ✓ Готов: {self.name} (@{self.me.username}) ID={self.me.id}")
        print(f"  ✓ {self.SESSION}.session сохранён")

    async def resolve(self):
        ident = input("\n  Получатель (@username, +79... или ID): ").strip()
        if not ident:
            await self.client.disconnect()
            sys.exit(1)
        self.target_raw = ident

        if ident.lstrip('-').isdigit():
            self.target_id = int(ident)
        else:
            clean = ident.lstrip('@')
            try:
                u = await self.client.get_users(clean)
                self.target_id = u.id
            except Exception:
                from pyrogram.raw.functions.contacts import ResolvePhone
                r = await self.client.invoke(ResolvePhone(phone=clean.replace("+", "")))
                if r.users:
                    self.target_id = r.users[0].id
                else:
                    print("  Не найден")
                    await self.client.disconnect()
                    sys.exit(1)

        chat = await self.client.get_chat(self.target_id)
        cn = getattr(chat, 'first_name', getattr(chat, 'title', 'Чат'))
        print(f"  Цель: {cn} (ID={self.target_id})")

    async def on_msg(self, client, msg):
        try:
            print(f"  [DEBUG] chat={msg.chat.id} from={msg.from_user.id if msg.from_user else '?'} "
                  f"me={self.me.id if self.me else '?'} out={msg.outgoing} "
                  f"text='{str(msg.text or msg.caption or '')[:80]}'")
        except Exception:
            pass

        if msg.outgoing:
            return
        if msg.from_user and self.me and msg.from_user.id == self.me.id:
            return
        if msg.chat.id != self.target_id:
            return

        text = (msg.text or msg.caption or "").strip()
        if not text:
            return

        await self.queue.put({"text": text, "mid": msg.id})
        print(f"\n  📩 {text[:120]}")

    async def run(self, rounds: int):
        folder = input("\n  Папка логов: ").strip() or f"test_{datetime.now():%Y%m%d_%H%M%S}"
        self.log_dir = os.path.join(settings.LOG_DIR, folder)
        os.makedirs(self.log_dir, exist_ok=True)

        from pyrogram import filters
        from pyrogram.handlers import MessageHandler
        self.client.add_handler(MessageHandler(self.on_msg, filters.private), group=0)
        print("  Обработчик → OK")

        llm = TesterLLM()
        print(f"\n{'='*60}")
        print(f"  ДИАЛОГ: {self.name} ↔ Мила ({rounds} раундов)")
        print(f"{'='*60}")

        turns = []

        # Раунд 1
        print(f"\n  РАУНД 1")
        await self.client.send_message(self.target_id, "Привет, как дела?")
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
            await self.client.send_message(self.target_id, msg)
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
    except Exception as e:
        traceback.print_exc()
    finally:
        if t.client and t.client.is_connected:
            await t.stop()


if __name__ == "__main__":
    asyncio.run(main())