from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import urllib.parse
import urllib.request
from typing import List, Optional, Tuple

from .config import CHAT_ID_PATH

logger = logging.getLogger(__name__)

_SSL_CTX = ssl._create_unverified_context()


def _ensure_env() -> None:
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_path):
        for line in open(env_path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get_chat_id() -> Optional[int]:
    if CHAT_ID_PATH.exists():
        try:
            return int(CHAT_ID_PATH.read_text().strip())
        except (ValueError, OSError):
            return None
    return None


def save_chat_id(chat_id: int) -> None:
    CHAT_ID_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHAT_ID_PATH.write_text(str(chat_id))


def _tg_api(method: str, data: dict) -> Optional[dict]:
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
    except Exception as exc:
        logger.error("Telegram API %s failed: %s", method, exc)
        return None


async def send_telegram(text: str) -> bool:
    _ensure_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = get_chat_id()
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
        logger.error("Failed to send Telegram: %s", exc)
        return False


def poll_telegram(token: str, chat_id: int, last_update_id: int) -> List[Tuple[str, int]]:
    result = _tg_api("getUpdates", {
        "offset": last_update_id + 1,
        "timeout": 2,
        "allowed_updates": json.dumps(["message"]),
    })
    msgs: List[Tuple[str, int]] = []
    if result and result.get("ok"):
        for update in result.get("result", []):
            msg = update.get("message", {}) or update.get("edited_message", {})
            cid = msg.get("chat", {}).get("id")
            text = msg.get("text", "")
            if cid == chat_id and text:
                msgs.append((text, update.get("update_id", 0)))
    return msgs


async def wait_for_ready_signal(slug: str, timeout: int = 300) -> bool:
    """Wait for user to send 'ready {slug}' or 'ready' via Telegram or stdin."""
    _ensure_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = get_chat_id()
    last_update_id = 0
    loop = asyncio.get_event_loop()
    start = loop.time()

    while loop.time() - start < timeout:
        if token and chat_id:
            for msg_text, upd_id in poll_telegram(token, chat_id, last_update_id):
                last_update_id = max(last_update_id, upd_id)
                parts = msg_text.strip().lower().split(None, 1)
                if parts and parts[0] == "ready":
                    if len(parts) == 1 or parts[1] == slug:
                        return True
        # Also check stdin
        try:
            line = await asyncio.wait_for(
                loop.run_in_executor(None, input, "> "),
                timeout=0.5,
            )
            parts = line.strip().lower().split(None, 1)
            if parts and parts[0] == "ready":
                if len(parts) == 1 or parts[1] == slug:
                    return True
        except (asyncio.TimeoutError, EOFError, KeyboardInterrupt):
            pass

    return False
