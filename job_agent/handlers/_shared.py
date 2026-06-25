from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..storage import ROOT

logger = logging.getLogger(__name__)

_SSL_CTX = ssl._create_unverified_context()
_ENV_LOADED = False


def _ensure_env() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    env_path = ROOT / ".env"
    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    _ENV_LOADED = True


def load_chat_id() -> Optional[int]:
    path = ROOT / "data" / "telegram_chat_id.txt"
    if path.exists():
        try:
            return int(path.read_text().strip())
        except (ValueError, OSError):
            return None
    return None


def save_chat_id(chat_id: int) -> None:
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "telegram_chat_id.txt").write_text(str(chat_id))


def telegram_api(method: str, data: dict) -> Optional[dict]:
    _ensure_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(url, data=body, headers={"User-Agent": "python"})
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def poll_telegram(token: str, chat_id: int, last_update_id: int) -> List[Tuple[str, int]]:
    result = telegram_api("getUpdates", {
        "offset": last_update_id + 1,
        "timeout": 2,
        "allowed_updates": json.dumps(["message"]),
    })
    msgs = []
    if result and result.get("ok"):
        for update in result.get("result", []):
            upd_id = update.get("update_id", 0)
            msg = update.get("message", {}) or update.get("edited_message", {})
            cid = msg.get("chat", {}).get("id")
            if cid == chat_id:
                text = msg.get("text", "")
                if text:
                    msgs.append((text, upd_id))
    return msgs


def reply_telegram(token: str, chat_id: int, text: str) -> None:
    telegram_api("sendMessage", {
        "chat_id": chat_id,
        "text": text,
    })


async def send_telegram(text: str) -> bool:
    _ensure_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = load_chat_id()
    if not token or not chat_id:
        return False
    loop = asyncio.get_event_loop()
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        req = urllib.request.Request(url, data=data, headers={"User-Agent": "python"})
        await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=10, context=_SSL_CTX))
        return True
    except Exception as exc:
        logger.error("Failed to send Telegram message: %s", exc)
        return False


def save_learned_answers(filled: List[Dict[str, Any]], form_url: str) -> None:
    path = ROOT / "data" / "learned_answers.json"
    existing = {}
    if path.exists():
        existing = json.loads(path.read_text())
    answers = existing.get("answers", [])
    seen_contexts = {a.get("field_context") for a in answers if a.get("field_context")}
    for f in filled:
        ctx = f.get("field_context")
        if not ctx or ctx in seen_contexts:
            continue
        answers.append({
            "field_context": ctx,
            "question": f"Value for field: {ctx}",
            "answer": str(f["value"]),
            "source_url": form_url,
        })
        seen_contexts.add(ctx)
    path.write_text(json.dumps({"answers": answers}, indent=2))


async def read_current_values(page, live_fields: List, field_contexts: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    from ..models import FieldEvidence
    values = []
    locator = page.locator("input, select, textarea")
    for f in live_fields:
        el = locator.nth(f.field_idx)
        try:
            if f.tag_name == "select":
                val = await el.evaluate("el => el.options[el.selectedIndex]?.text || ''")
            elif f.input_type == "checkbox":
                val = "true" if await el.is_checked() else "false"
            elif f.input_type == "radio":
                checked = await el.is_checked()
                if not checked:
                    continue
                parent_text = await el.evaluate("el => { const l = el.closest('label'); return l ? l.innerText.trim() : el.value; }")
                val = parent_text or "selected"
            else:
                val = await el.input_value()
            ctx = (field_contexts or {}).get(f.field_id)
            values.append({"field_id": f.field_id, "field_context": ctx, "value": val})
        except Exception:
            pass
    return values


def safe_slug(value: str) -> str:
    import re
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return slug[:120] or "job"


def discover_chat_id() -> Optional[int]:
    _ensure_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return None
    try:
        url = f"https://api.telegram.org/bot{token}/getUpdates?timeout=2"
        req = urllib.request.Request(url, headers={"User-Agent": "python"})
        resp = urllib.request.urlopen(req, timeout=10, context=_SSL_CTX)
        data = json.loads(resp.read())
        chats = {}
        for update in data.get("result", []):
            msg = update.get("message", {}) or update.get("edited_message", {})
            cid = msg.get("chat", {}).get("id")
            date = msg.get("date", 0)
            if cid:
                chats[cid] = max(chats.get(cid, 0), date)
        if chats:
            latest = max(chats, key=chats.get)
            save_chat_id(latest)
            logger.info("Discovered chat_id %s from Telegram updates", latest)
            return latest
    except Exception as exc:
        logger.warning("Failed to discover chat_id: %s", exc)
    return None
