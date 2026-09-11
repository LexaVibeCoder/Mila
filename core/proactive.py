"""Proactive scheduler — state_agent every N min of global silence.

Flow:
- tick every `proactive.check_interval_seconds` (default 60);
- if global idle (any side, all chats) >= `proactive.idle_minutes` (default 27)
  and presence is rest/working and Mila is not sleeping → run state_agent;
- `write_to_user` → one proactive LLM turn with chat list (IDs) + optional
  `peek_recent_chats` tool (all chats at once, old->new, no truncation);
- `sleep` → sleep `proactive.sleep_minutes` (default 480 = 8h);
- `wait` / `plan_day` → stub: log only.

Chat ordering: oldest activity first, freshest last (bottom of context).
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from datetime import datetime, timezone
from typing import Any

_LEADING_TS_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?\]\s*")

_proactive_task: asyncio.Task | None = None
_last_run_ts: float = 0.0


def _cfg(key: str, default: Any) -> Any:
    try:
        from Mila_observatory.runtime import runtime_config
        return runtime_config.get(f"proactive.{key}", default)
    except Exception:
        return default


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        s = str(raw).strip()
        if not s:
            return None
        # SQLite/CONTEXT formats: "2026-09-11T15:27:52..." or "2026-09-11 15:27:52"
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def get_global_idle_seconds() -> float:
    """Seconds since the last chunk in ANY chat (global silence)."""
    try:
        from core.db import get_global_last_activity
        last = get_global_last_activity()
    except Exception:
        return 86400.0
    dt = _parse_ts(last)
    if dt is None:
        return 86400.0
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())


def format_relative_ru(last_at: str | None) -> str:
    dt = _parse_ts(last_at)
    if dt is None:
        return "давно"
    secs = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    if secs < 60:
        return "только что"
    mins = secs // 60
    if mins < 60:
        return f"{mins} мин назад"
    hours = mins // 60
    mins = mins % 60
    if hours < 24:
        return f"{hours} ч {mins} мин назад" if mins else f"{hours} ч назад"
    days = hours // 24
    hours = hours % 24
    return f"{days} дн {hours} ч назад" if hours else f"{days} дн назад"


def build_proactive_block(chats: list[dict]) -> str:
    """Top-of-context instructions + chat list old->new (no messages here)."""
    lines = [
        "Ты хочешь кому-то написать. Это твоё собственное желание после тишины, "
        "а не ответ на уведомление.",
        "",
        "Доступные чаты (от старых к свежим, свежие внизу):",
    ]
    for ch in chats:
        cid = ch.get("chat_id", "?")
        name = ch.get("chat_name") or cid[:8]
        rel = format_relative_ru(ch.get("last_at"))
        lines.append(f"Чат «{name}» ({cid}) — последнее сообщение {rel}")
    lines += [
        "",
        "Как действовать:",
        "1. Можешь СРАЗУ выбрать один чат и вызвать enter_dialog с его chat_id "
        "(тебя позовут в диалог, там и напишешь), либо отправить одну короткую "
        "записку через send_message (chat_id + text).",
        "2. Можешь СНАЧАЛА вызвать peek_recent_chats (можно без параметров — "
        "тогда вернутся последние сообщения из каждого чата одновременно), "
        "посмотреть их и только потом решить кому писать. limit_per_chat ставишь "
        "сама (по умолчанию 10). Сообщения не обрезаются, чаты придут от старых "
        "к свежим.",
        "3. Если никому писать не хочешь — верни пустой ответ без тулов.",
        "Хочешь спать — вызови start_sleep с duration_minutes 480.",
        "Пиши только в ОДИН чат за этот ход.",
    ]
    return "\n".join(lines)


async def run_proactive_turn(bot) -> dict:
    """One proactive LLM turn. Returns pipeline result dict (may be empty)."""
    from core.db import get_chats_by_last_activity, get_global_last_activity

    max_chats = int(_cfg("max_chats", 20) or 20)
    chats = get_chats_by_last_activity(limit=max_chats)
    if not chats:
        print("[Proactive] write_to_user but no chats — skip")
        return {"success": False, "error": "no chats"}

    before = get_global_last_activity()
    block = build_proactive_block(chats)
    print(f"[Proactive] write_to_user: {len(chats)} чатов, запуск хода")

    reply_texts: list[str] = []
    reply_chat_id = ""
    reply_via = ""
    silence_reason = ""
    all_actions: list[dict] = []
    entered_dialog_chat = ""

    try:
        async for ev in bot.pipeline.process_message_stream(
            user_message="",
            user_visible_history=[],
            agent_internal_history=[],
            user_profile=None,
            media={"chat_id": "proactive"},
            request_user_id="proactive",
            request_chat_id="proactive",
            enable_peek=True,
            proactive_block=block,
        ):
            etype = ev.get("type")
            if etype == "thought":
                preview = (ev.get("thoughts") or "")[:120].replace("\n", " ")
                if preview.strip():
                    print(f"  [Proactive] {preview}...")
            elif etype == "action":
                a = ev.get("action", {})
                all_actions.append(a)
                at = a.get("type")
                if at == "peek_recent_chats":
                    print(f"  [Proactive] peek: {len(a.get('chats') or [])} чатов")
                elif at in ("enter_dialog", "enter_rest", "enter_working"):
                    print(f"  [Proactive] {at} → {a.get('chat_id', a.get('state', ''))}")
                    if at == "enter_dialog" and a.get("status") == "switched":
                        entered_dialog_chat = str(a.get("chat_id") or "")
                elif at == "start_sleep":
                    print(f"  [Proactive] sleep started for {a.get('duration_minutes')} мин")
                elif at == "send_message":
                    print(f"  [Proactive] send_message ready: {(a.get('text') or '')[:80]}")
            elif etype == "reply":
                reply_texts = list(ev.get("texts") or [])
                reply_chat_id = str(ev.get("chat_id", "") or "")
                reply_via = ev.get("via", "")
                break
            elif etype == "silence":
                silence_reason = ev.get("reason", "")
                print(f"  [Proactive] silence: {silence_reason}")
                break
            elif etype == "error":
                print(f"  [Proactive] pipeline error: {ev.get('error')}")
                break
            elif etype == "done":
                res = ev.get("result", {})
                if not reply_texts and res.get("reply_texts"):
                    reply_texts = list(res.get("reply_texts") or [])
                    reply_chat_id = str(res.get("reply_chat_id", "") or "")
                    reply_via = res.get("reply_via", "")
                if not silence_reason:
                    silence_reason = res.get("silence_reason", "")
                break
    except Exception as e:
        print(f"[Proactive] turn failed: {e}")
        return {"success": False, "error": str(e)}

    # No reply → leave presence clean if we entered a dialog speculatively.
    if not reply_texts:
        if entered_dialog_chat:
            try:
                from core.presence import get_presence, enter_rest
                p = get_presence()
                if p.get("state") == "dialog" and str(p.get("focus_chat_id")) == entered_dialog_chat:
                    enter_rest("proactive: решила не писать")
            except Exception:
                pass
        try:
            from Mila_observatory.api.dashboard import add_event
            add_event("proactive", f"write_to_user → silence ({silence_reason or 'no reply'})")
        except Exception:
            pass
        return {"success": True, "reply_texts": [], "silence_reason": silence_reason,
                "actions": all_actions}

    # Guard: new incoming message during generation → abort (reactive worker owns it).
    try:
        after = get_global_last_activity()
        if after != before:
            print("[Proactive] new message arrived during generation — abort send")
            try:
                from core.presence import get_presence, enter_rest
                p = get_presence()
                if p.get("state") == "dialog" and entered_dialog_chat \
                        and str(p.get("focus_chat_id")) == entered_dialog_chat:
                    enter_rest("proactive: прервано новым сообщением")
            except Exception:
                pass
            return {"success": False, "error": "interrupted by new message"}
    except Exception:
        pass

    if not reply_chat_id or reply_chat_id == "proactive":
        print("[Proactive] reply without target chat — skip send")
        return {"success": False, "error": "no target chat"}

    # Guard: peer typing in target → don't interrupt.
    try:
        if bot.messages.is_user_typing(reply_chat_id):
            print("[Proactive] peer typing in target — abort send")
            return {"success": False, "error": "peer typing"}
    except Exception:
        pass

    # Guard: don't send while sleeping.
    try:
        from core.sleep.manager import is_sleeping
        if is_sleeping():
            print("[Proactive] fell asleep mid-turn — abort send")
            return {"success": False, "error": "sleeping"}
    except Exception:
        pass

    cleans = [_LEADING_TS_RE.sub("", (t or "").strip()) for t in reply_texts]
    cleans = [c for c in cleans if c]
    if not cleans:
        return {"success": True, "reply_texts": [], "silence_reason": "empty reply"}

    print(f"[Proactive] SENDING via {reply_via} {len(cleans)} bubble(s) → {reply_chat_id}")
    try:
        from core.tg_adapter import send_text, send_typing
        from core.db import add_chunk
        from core.context.buffer import push_message
        try:
            await send_typing(bot.app, int(reply_chat_id))
        except Exception:
            pass
        await asyncio.sleep(random.uniform(0.3, 1.0))
        for i, clean in enumerate(cleans):
            if i > 0:
                try:
                    await send_typing(bot.app, int(reply_chat_id))
                except Exception:
                    pass
                await asyncio.sleep(random.uniform(0.3, 1.0))
            await send_text(bot.app, int(reply_chat_id), clean)
            try:
                chunk_id = add_chunk(reply_chat_id, str(bot.me.id), content=clean,
                                     token_count=0, type="raw")
            except Exception:
                chunk_id = None
            try:
                push_message(reply_chat_id, clean, source_chunk_id=chunk_id,
                             role="assistant")
            except Exception:
                pass
            print(f"[Proactive] ✓ bubble {i + 1}/{len(cleans)} sent")
        try:
            from Mila_observatory.api.dashboard import add_event
            add_event("proactive", f"Написала первой в чат {reply_chat_id} ({len(cleans)} сообщ.)")
        except Exception:
            pass
        return {"success": True, "reply_texts": cleans,
                "reply_chat_id": reply_chat_id, "via": reply_via,
                "actions": all_actions}
    except Exception as e:
        print(f"[Proactive] send failed: {e}")
        return {"success": False, "error": str(e)}


async def _tick(bot) -> None:
    global _last_run_ts
    try:
        if not bool(_cfg("enabled", True)):
            return
        # Sleeping/committing → skip (sleep manager owns the state).
        try:
            from core.sleep.manager import is_sleeping, is_committing
            if is_sleeping() or is_committing():
                return
        except Exception:
            pass
        # Only from rest/working.
        try:
            from core.presence import get_presence
            st = get_presence().get("state", "rest")
        except Exception:
            st = "rest"
        if st not in ("rest", "working"):
            return

        idle_need = float(_cfg("idle_minutes", 27) or 27) * 60.0
        idle = get_global_idle_seconds()
        if idle < idle_need:
            return

        cooldown = float(_cfg("cooldown_minutes", 27) or 27) * 60.0
        now = time.monotonic()
        if now - _last_run_ts < cooldown:
            return
        _last_run_ts = now

        # State agent inference (global needs, no chat).
        try:
            from core.needs.tracker import get_tracker
            tracker = get_tracker()
            needs = tracker.get_needs(force=True)
            result = tracker.run_inference(needs)
        except Exception as e:
            print(f"[Proactive] state_agent failed: {e}")
            return

        action = (result or {}).get("action", "wait")
        probs = (result or {}).get("probabilities", {})
        print(f"[Proactive] idle {idle:.0f}s → state_agent: {action} {probs}")

        if action == "write_to_user":
            try:
                from Mila_observatory.api.dashboard import add_event
                add_event("proactive", f"state_agent → write_to_user (idle {idle:.0f}s)")
            except Exception:
                pass
            await run_proactive_turn(bot)
        elif action == "sleep":
            minutes = int(_cfg("sleep_minutes", 480) or 480)
            print(f"[Proactive] state_agent → sleep {minutes} мин")
            try:
                from core.presence import enter_sleep_note
                enter_sleep_note(f"state_agent: {minutes} min")
                from core.sleep.manager import start_sleep
                await start_sleep(duration_minutes=minutes,
                                  triggered_by="state_agent")
                try:
                    from Mila_observatory.api.dashboard import add_event
                    add_event("sleep", f"Proactive sleep {minutes} мин (state_agent)")
                except Exception:
                    pass
            except Exception as e:
                print(f"[Proactive] sleep failed: {e}")
        else:
            # wait / plan_day → stub for now.
            print(f"[Proactive] state_agent → {action} (stub, nothing happens)")
            try:
                from Mila_observatory.api.dashboard import add_event
                add_event("proactive", f"state_agent → {action} (stub)")
            except Exception:
                pass
    except Exception as e:
        print(f"[Proactive] tick error: {e}")


async def _loop(bot) -> None:
    interval = float(_cfg("check_interval_seconds", 60) or 60)
    interval = max(10.0, min(interval, 3600.0))
    print(f"[Proactive] scheduler started "
          f"(idle {_cfg('idle_minutes', 27)} мин, check every {interval:.0f}s)")
    while True:
        try:
            await _tick(bot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[Proactive] loop error: {e}")
        try:
            interval = float(_cfg("check_interval_seconds", 60) or 60)
            interval = max(10.0, min(interval, 3600.0))
        except Exception:
            pass
        await asyncio.sleep(interval)


def start_proactive_scheduler(bot) -> None:
    global _proactive_task
    if _proactive_task is not None and not _proactive_task.done():
        return
    _proactive_task = asyncio.create_task(_loop(bot))


def stop_proactive_scheduler() -> None:
    global _proactive_task
    if _proactive_task is not None and not _proactive_task.done():
        _proactive_task.cancel()
    _proactive_task = None
