from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..models import FieldEvidence
from ..storage import ROOT, SCREENSHOT_DIR
from .base import FillResult, VendorHandler


class CareersPageHandler(VendorHandler):
    def __init__(self, headless: bool = False):
        self.headless = headless

    def _safe_slug(self, value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
        return slug[:120] or "job"

    async def discover_fields(
        self, url: str
    ) -> tuple:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2000)

            apply_btn = page.locator("text=Apply now").first
            if await apply_btn.is_visible():
                await apply_btn.click()
                await page.wait_for_timeout(3000)

            form_url = page.url
            await page.wait_for_load_state("networkidle", timeout=10000)
            await page.wait_for_timeout(2000)

            fields = await self._extract_fields(page)
            slug = self._safe_slug(form_url)
            screenshot_path = str(SCREENSHOT_DIR / f"{slug}-handler.png")
            await page.screenshot(path=screenshot_path, full_page=True)
            await browser.close()

        return form_url, fields, screenshot_path

    async def fill_and_stop(
        self,
        url: str,
        field_mapping: Dict[str, Any],
        cv_path: Optional[str] = None,
    ) -> FillResult:
        from playwright.async_api import async_playwright

        result = FillResult(status="started")
        errors: List[str] = []
        p = None
        browser = None

        try:
            p = await async_playwright().start()
            browser = await p.chromium.launch(headless=self.headless)
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2000)

            apply_btn = page.locator("text=Apply now").first
            if await apply_btn.is_visible():
                await apply_btn.click()
                await page.wait_for_timeout(3000)

            form_url = page.url
            await page.wait_for_load_state("networkidle", timeout=10000)
            await page.wait_for_timeout(2000)

            live_fields = await self._extract_fields(page)
            live_by_idx = {f.field_idx: f for f in live_fields}

            field_contexts = {}
            for entry in field_mapping.get("fields", []):
                ctx = entry.get("field_context")
                fid = entry.get("field_id")
                if ctx and fid:
                    field_contexts[fid] = ctx

            filled = []
            for entry in field_mapping.get("fields", []):
                field_idx = entry.get("field_idx")
                field_id = entry.get("field_id")
                value = entry.get("value")
                if field_idx is None or not field_id or value is None:
                    continue

                match = live_by_idx.get(field_idx)
                if match is None:
                    errors.append(f"Field not found: idx={field_idx} id={field_id}")
                    continue

                el = page.locator("input, select, textarea").nth(match.field_idx)

                tag = match.tag_name
                input_type = match.input_type

                is_readonly_file = (
                    tag == "input"
                    and input_type in ("text", None)
                    and (
                        (match.placeholder and "attachment" in match.placeholder.lower())
                        or (match.nearby_text and any(
                            kw in match.nearby_text.lower()
                            for kw in ("upload", "cv", "resume", "attachment", "file")
                        ))
                    )
                )
                if is_readonly_file:
                    continue

                try:
                    if tag == "select":
                        await el.select_option(label=value)
                    elif input_type == "checkbox":
                        checked = await el.is_checked()
                        if (value is True or str(value).lower() == "on") and not checked:
                            await el.check()
                        elif (value is False or str(value).lower() == "off") and checked:
                            await el.uncheck()
                    elif input_type == "file":
                        if cv_path:
                            await el.set_input_files(cv_path)
                    else:
                        await el.fill(str(value))
                    filled.append({
                        "field_id": field_id,
                        "field_context": field_contexts.get(field_id) or entry.get("field_context"),
                        "value": value,
                    })
                except Exception as exc:
                    errors.append(f"Failed to fill {field_id}: {exc}")

            if cv_path:
                file_input = page.locator('input[type="file"]').first
                if await file_input.count():
                    try:
                        await file_input.set_input_files(cv_path)
                        filled.append({"field_id": "file_upload", "field_context": "cv_upload", "value": cv_path})
                    except Exception as exc:
                        errors.append(f"CV upload failed: {exc}")

            await page.wait_for_timeout(1000)
            slug = self._safe_slug(form_url)
            screenshot_path = str(SCREENSHOT_DIR / f"{slug}-filled.png")
            await page.screenshot(path=screenshot_path, full_page=True)

            result.status = "filled_awaiting_review" if not errors else "filled_with_errors"
            result.filled_fields = filled
            result.errors = errors
            result.screenshot_path = screenshot_path
            result.form_url = form_url

            if not self.headless:
                self._save_learned_answers(filled, form_url)
                job_slug = self._safe_slug(form_url)
                try:
                    from ..telegram_bot import send_message
                    await send_message(
                        f"Form filled for: {form_url}\n\n"
                        f"Browser is open. Your slug: {job_slug}\n"
                        "Edit fields in the browser, then send:\n"
                        f"  save {job_slug}\n"
                        "to capture adjustments as learned answers."
                    )
                except Exception:
                    pass
                print(f"\nBrowser open — job slug: {job_slug}")
                print(f"Send 'save {job_slug}' to Telegram when ready to capture.")
                token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
                chat_id = self._load_chat_id()
                last_update_id = 0
                while True:
                    await asyncio.sleep(3)
                    if not token or not chat_id:
                        continue
                    try:
                        msgs = self._poll_telegram(token, chat_id, last_update_id)
                        for msg_text, upd_id in msgs:
                            last_update_id = max(last_update_id, upd_id)
                            parts = msg_text.strip().lower().split(None, 1)
                            cmd = parts[0] if parts else ""
                            target = parts[1] if len(parts) > 1 else ""
                            if cmd in ("save", "capture", "learn") and (not target or target == job_slug):
                                current = await self._read_current_values(page, live_fields, field_contexts)
                                self._save_learned_answers(current, form_url)
                                self._reply_telegram(
                                    token, chat_id,
                                    f"Saved {len(current)} field values for {job_slug}. You can submit now."
                                )
                                print(f"Captured {len(current)} values — saved to learned_answers.json")
                    except Exception:
                        pass
            else:
                if browser and p:
                    await browser.close()
                    await p.stop()
        except Exception as exc:
            errors.append(str(exc))
            result.status = "filled_with_errors"
            result.errors = errors
        finally:
            if self.headless and browser and p:
                try:
                    await browser.close()
                except Exception:
                    pass
                try:
                    await p.stop()
                except Exception:
                    pass

        return result

    def _load_chat_id(self) -> Optional[int]:
        path = ROOT / "data" / "telegram_chat_id.txt"
        if path.exists():
            try:
                return int(path.read_text().strip())
            except (ValueError, OSError):
                return None
        return None

    def _telegram_api(self, method: str, data: dict) -> Optional[dict]:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            return None
        url = f"https://api.telegram.org/bot{token}/{method}"
        try:
            body = urllib.parse.urlencode(data).encode()
            req = urllib.request.Request(url, data=body)
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())
        except Exception:
            return None

    def _poll_telegram(
        self, token: str, chat_id: int, last_update_id: int
    ) -> List[tuple]:
        result = self._telegram_api("getUpdates", {
            "offset": last_update_id + 1,
            "timeout": 2,
            "allowed_updates": json.dumps(["message"]),
        })
        msgs = []
        if result and result.get("ok"):
            for update in result.get("result", []):
                upd_id = update.get("update_id", 0)
                msg = update.get("message", {})
                if msg.get("chat", {}).get("id") == chat_id:
                    text = msg.get("text", "")
                    msgs.append((text, upd_id))
        return msgs

    def _reply_telegram(self, token: str, chat_id: int, text: str) -> None:
        self._telegram_api("sendMessage", {
            "chat_id": chat_id,
            "text": text,
        })

    def _save_learned_answers(
        self, filled: List[Dict[str, Any]], form_url: str
    ) -> None:
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

    async def _read_current_values(
        self, page, live_fields: List[FieldEvidence], field_contexts: Optional[Dict[str, str]] = None
    ) -> List[Dict[str, Any]]:
        values = []
        locator = page.locator("input, select, textarea")
        for f in live_fields:
            el = locator.nth(f.field_idx)
            tag = f.tag_name
            input_type = f.input_type
            try:
                if tag == "select":
                    val = await el.evaluate("el => el.options[el.selectedIndex]?.text || ''")
                elif input_type == "checkbox":
                    val = "true" if await el.is_checked() else "false"
                else:
                    val = await el.input_value()
                ctx = (field_contexts or {}).get(f.field_id)
                values.append({"field_id": f.field_id, "field_context": ctx, "value": val})
            except Exception:
                pass
        return values

    async def _extract_fields(self, page) -> List[FieldEvidence]:
        locator = page.locator("input, select, textarea")
        count = await locator.count()
        raw = []
        seen = set()
        for i in range(count):
            el = locator.nth(i)
            tag_name = await el.evaluate("el => el.tagName.toLowerCase()")
            input_type = await el.evaluate("el => el.getAttribute('type')")
            if input_type == "hidden":
                continue
            visible = await el.is_visible()
            if not visible:
                continue
            el_id = await el.evaluate("el => el.id")
            el_name = await el.evaluate("el => el.getAttribute('name')")
            field_id = el_id or el_name or f"field_{i}"
            if field_id in seen:
                continue
            seen.add(field_id)
            label = await el.evaluate(
                "el => el.id ? document.querySelector('label[for=\"' + CSS.escape(el.id) + '\"]')?.innerText?.trim() || null : null"
            )
            placeholder = await el.evaluate("el => el.getAttribute('placeholder')")
            aria_label = await el.evaluate("el => el.getAttribute('aria-label')")
            required = await el.evaluate("el => Boolean(el.required || el.getAttribute('aria-required') === 'true')")
            options = []
            if tag_name == "select":
                options = await el.evaluate("el => Array.from(el.options).map(o => o.innerText.trim()).filter(Boolean)")
            nearby_text = await el.evaluate("""
                el => {
                    const parent = el.closest('.form-group, .field, [class*=form], [class*=field], div') || el.parentElement;
                    return (parent?.innerText || '').trim().slice(0, 300);
                }
            """)
            raw.append({
                "field_idx": i,
                "field_id": field_id,
                "tag_name": tag_name,
                "input_type": input_type,
                "label": label,
                "placeholder": placeholder,
                "aria_label": aria_label,
                "required": required,
                "options": options,
                "nearby_text": nearby_text,
            })

        return [
            FieldEvidence(
                field_idx=int(item["field_idx"]),
                field_id=str(item.get("field_id") or f"field_{i}"),
                tag_name=str(item.get("tag_name") or ""),
                input_type=item.get("input_type"),
                label=item.get("label"),
                placeholder=item.get("placeholder"),
                aria_label=item.get("aria_label"),
                required=bool(item.get("required")),
                visible=True,
                options=list(item.get("options") or []),
                nearby_text=item.get("nearby_text"),
            )
            for i, item in enumerate(raw)
        ]
