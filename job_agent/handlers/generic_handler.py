from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..models import FieldEvidence
from ..storage import ROOT, SCREENSHOT_DIR
from ._shared import (
    discover_chat_id,
    load_chat_id,
    poll_telegram,
    reply_telegram,
    safe_slug,
    save_learned_answers,
    send_telegram,
)
from .base import FillResult, VendorHandler

logger = logging.getLogger(__name__)

CONTINUE_LABELS = {"continue", "next", "next step", "proceed", "save and continue", "save & continue"}
SUBMIT_LABELS = {"submit", "submit application", "apply", "done", "apply now"}
MAX_PAGES = 10
PIN_INPUT_SELECTOR = "input[type='tel'], input[type='text']"


class GenericHandler(VendorHandler):
    def __init__(self, headless: bool = False):
        self.headless = headless

    async def _make_context(self, browser, **kwargs):
        return await browser.new_context(
            permissions=[],
            geolocation=None,
            locale="en-US",
            **kwargs,
        )

    async def _setup_page(self, browser, url: str):
        ctx = await self._make_context(browser)
        page = await ctx.new_page()
        page.set_default_timeout(15000)
        page.on("dialog", lambda d: d.dismiss())
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)
        return page, ctx

    async def discover_fields(self, url: str) -> tuple:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)
            page, ctx = await self._setup_page(browser, url)

            await self._dismiss_popups(page)
            await self._click_apply(page)

            if await self._is_auth_gate(page):
                pass

            all_fields = []
            for page_num in range(MAX_PAGES):
                fields = await self._extract_fields(page)
                self._tag_page(page_num, fields)
                all_fields.extend(fields)
                btn = await self._find_continue(page)
                if btn is None:
                    break
                try:
                    await btn.click()
                    await page.wait_for_timeout(3000)
                    await self._dismiss_popups(page)
                except Exception:
                    break

            slug = safe_slug(url)
            screenshot_path = str(SCREENSHOT_DIR / f"{slug}-handler.png")
            try:
                await page.screenshot(path=screenshot_path, full_page=True)
            except Exception:
                pass
            await ctx.close()
            await browser.close()

        return url, all_fields, screenshot_path

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
            page, ctx = await self._setup_page(browser, url)

            await self._dismiss_popups(page)
            await self._click_apply(page)
            await self._dismiss_popups(page)
            await self._handle_auth_gate(page)

            mapping = field_mapping
            live_fields = await self._extract_fields(page)
            if not mapping.get("fields") and live_fields:
                from ..decision_engine import map_fields as _map_fields
                jd_text = None
                try:
                    t = await page.evaluate("() => document.body.innerText")
                    jd_text = t[:6000]
                except Exception:
                    pass
                job_ctx = {"job_url": url, "form_url": page.url}
                if jd_text:
                    job_ctx["job_description"] = jd_text
                mapping = _map_fields(live_fields, job_context=job_ctx)
                if not mapping.get("fields"):
                    mapping["fields"] = [
                        {"field_idx": f.field_idx, "field_id": f"auto_{f.field_idx}",
                         "value": "", "field_context": "text"}
                        for f in live_fields
                    ]

            field_contexts: Dict[str, str] = {}
            for entry in mapping.get("fields", []):
                ctx = entry.get("field_context")
                fid = entry.get("field_id")
                if ctx and fid:
                    field_contexts[fid] = ctx

            filled: List[Dict[str, Any]] = []

            for page_num in range(MAX_PAGES):
                live = await self._extract_fields(page)
                if not live:
                    btn = await self._find_continue(page)
                    if btn:
                        try:
                            await btn.click()
                            await page.wait_for_timeout(3000)
                            continue
                        except Exception:
                            break
                    break

                page_mapping = [
                    e for e in mapping.get("fields", [])
                    if e.get("page") is None or e.get("page") == page_num
                ]

                page_filled = 0
                for entry in page_mapping:
                    field_idx = entry.get("field_idx")
                    field_id = entry.get("field_id")
                    value = entry.get("value")
                    if field_idx is None or not field_id or value is None:
                        continue
                    match = None
                    for f in live:
                        if f.field_id == field_id:
                            match = f
                            break
                    if match is None:
                        pidx = entry.get("page_idx", field_idx)
                        for f in live:
                            if f.page_idx == pidx or f.field_idx == field_idx:
                                match = f
                                break
                    if match is None:
                        continue
                    try:
                        ok = await self._fill_field(page, match, value, cv_path)
                        if ok:
                            filled.append({
                                "field_id": field_id,
                                "field_context": field_contexts.get(field_id),
                                "value": value,
                            })
                            page_filled += 1
                    except Exception as exc:
                        errors.append(f"Field {field_id}: {exc}")

                cv_done = await self._try_cv_upload(page, cv_path)
                if cv_done:
                    filled.append({"field_id": "_cv", "field_context": "cv", "value": cv_path or "uploaded"})

                result.filled_fields = filled

                btn = await self._find_continue(page)
                if btn is None:
                    break
                try:
                    await btn.click()
                    await page.wait_for_timeout(3000)
                    await self._dismiss_popups(page)
                except Exception:
                    break

            result.status = "filled_awaiting_review" if not errors else "filled_with_errors"
            result.errors = errors
            slug = safe_slug(url)
            screenshot_path = str(SCREENSHOT_DIR / f"{slug}-filled.png")
            try:
                await page.screenshot(path=screenshot_path, full_page=True)
                result.screenshot_path = screenshot_path
            except Exception:
                pass
            result.form_url = page.url

            if not self.headless:
                save_learned_answers(filled, page.url)
                await self._poll_commands(page, slug, live, field_contexts)

        except Exception as exc:
            errors.append(str(exc))
            result.status = "filled_with_errors"
            result.errors = errors
        finally:
            if self.headless and browser and p:
                try:
                    await ctx.close()
                except Exception:
                    pass
                try:
                    await browser.close()
                except Exception:
                    pass
                try:
                    await p.stop()
                except Exception:
                    pass

        return result

    async def _extract_fields(self, page) -> List[FieldEvidence]:
        locator = page.locator("input, select, textarea")
        count = await locator.count()
        raw = []
        seen = set()
        for i in range(count):
            el = locator.nth(i)
            try:
                tag_name = await el.evaluate("el => el.tagName.toLowerCase()")
                input_type = await el.evaluate("el => el.getAttribute('type')")
            except Exception:
                continue
            if input_type == "hidden":
                continue
            try:
                visible = await el.is_visible()
            except Exception:
                continue
            el_id = await el.evaluate("el => el.id || ''")
            el_name = await el.evaluate("el => el.getAttribute('name') || ''")
            field_id = el_id or el_name or f"field_{i}"
            if field_id in seen:
                continue
            seen.add(field_id)
            label = await el.evaluate(
                "el => el.id ? document.querySelector('label[for=\"' + CSS.escape(el.id) + '\"]')?.innerText?.trim() || null : null"
            )
            placeholder = await el.evaluate("el => el.getAttribute('placeholder') || null")
            aria_label = await el.evaluate("el => el.getAttribute('aria-label') || null")
            required = await el.evaluate("el => Boolean(el.required || el.getAttribute('aria-required') === 'true')")
            options = []
            if tag_name == "select":
                options = await el.evaluate("el => Array.from(el.options).map(o => o.innerText.trim()).filter(Boolean)")
            nearby_text = await el.evaluate("""
                el => {
                    const parent = el.closest('.form-group, .field, [class*=form], [class*=field], div, li, td') || el.parentElement;
                    return (parent?.innerText || '').trim().slice(0, 300);
                }
            """)
            raw.append(FieldEvidence(
                field_idx=i, field_id=field_id, tag_name=tag_name,
                input_type=input_type or "text", label=label or "",
                placeholder=placeholder, aria_label=aria_label,
                required=required, visible=visible, options=options,
                nearby_text=nearby_text,
            ))
        return raw

    def _tag_page(self, page_num: int, fields: List[FieldEvidence]) -> None:
        for idx, f in enumerate(fields):
            f.page_idx = idx

    async def _fill_field(self, page, field: FieldEvidence, value: str, cv_path: Optional[str]) -> bool:
        el = None
        fid = field.field_id
        if fid:
            escaped_id = CSS.escape(fid)
            el = page.locator(f"#{escaped_id}").first
            if not await el.count():
                el = page.locator(f"[name=\"{fid}\"]").first
        if el is None or not await el.count():
            locator = page.locator("input, select, textarea")
            el = locator.nth(field.field_idx)
        if not await el.count():
            return False
        tag = field.tag_name
        inp_type = field.input_type or "text"

        if tag == "select":
            try:
                await el.select_option(label=value)
                return True
            except Exception:
                try:
                    await el.select_option(value=value)
                    return True
                except Exception:
                    opts = field.options or []
                    for o in opts:
                        if value.lower() in o.lower():
                            try:
                                await el.select_option(label=o)
                                return True
                            except Exception:
                                pass
                    return False

        if inp_type == "file":
            file_path = value if value and Path(value).exists() else (cv_path if cv_path and Path(cv_path).exists() else None)
            if file_path:
                try:
                    await el.set_input_files(file_path)
                    return True
                except Exception:
                    return False
            return False

        if inp_type == "radio":
            try:
                label_loc = page.locator(f"label:text-is('{value}')").first
                if await label_loc.count():
                    radio_id = await label_loc.get_attribute("for")
                    if radio_id:
                        r = page.locator(f"#{CSS.escape(radio_id)}")
                        if await r.count():
                            await r.click()
                            return True
                await el.click()
                return True
            except Exception:
                return False

        if inp_type == "checkbox":
            checked = await el.is_checked()
            should_check = value.lower() in ("yes", "true", "1", "on")
            if should_check != checked:
                try:
                    label_id = await el.get_attribute("id")
                    if label_id:
                        lbl = page.locator(f"label[for='{label_id}']").first
                        if await lbl.count():
                            await lbl.click()
                            return True
                    await el.click()
                except Exception:
                    pass
            return True

        if inp_type == "tel" or inp_type == "number":
            cleaned = re.sub(r"[^\d+]", "", value)
            await el.fill(cleaned)
            return True

        if inp_type in ("email", "url"):
            await el.fill(value.strip())
            return True

        await el.fill(value)
        return True

    async def _try_cv_upload(self, page, cv_path: Optional[str]) -> bool:
        if not cv_path or not Path(cv_path).exists():
            return False
        try:
            file_input = page.locator("input[type='file']").first
            if await file_input.count():
                await file_input.set_input_files(cv_path)
                return True
        except Exception:
            pass
        try:
            hidden = page.locator("input[type='file']").first
            if await hidden.count():
                await hidden.set_input_files(cv_path)
                return True
        except Exception:
            pass
        return False

    async def _find_continue(self, page):
        buttons = page.locator("button, a[role='button'], input[type='submit'], input[type='button'], [class*='btn'], [class*='button']")
        count = await buttons.count()
        best = None
        for i in range(count):
            b = buttons.nth(i)
            try:
                text = (await b.inner_text()).strip().lower()
            except Exception:
                try:
                    text = (await b.get_attribute("value") or "").strip().lower()
                except Exception:
                    continue
            if not text:
                continue
            if text in SUBMIT_LABELS or any(s in text for s in SUBMIT_LABELS):
                if best is None:
                    best = ("submit", b)
                continue
            if text in CONTINUE_LABELS or any(c in text for c in CONTINUE_LABELS):
                return b
        if best and best[0] == "submit":
            return None
        return None

    async def _dismiss_popups(self, page) -> None:
        try:
            cookie_texts = ["accept all cookies", "accept all", "accept cookies", "allow all cookies",
                            "accept", "i agree", "got it", "consent", "allow cookies",
                            "accept all cookies and continue"]
            for text in cookie_texts:
                try:
                    btn = page.locator(f"button, a, [role='button']").filter(has_text=re.compile(re.escape(text), re.IGNORECASE)).first
                    if await btn.count() and await btn.is_visible():
                        await btn.click()
                        await page.wait_for_timeout(500)
                        break
                except Exception:
                    pass
        except Exception:
            pass

    async def _click_apply(self, page) -> None:
        try:
            apply = page.locator("a, button, [role='button']").filter(has_text=re.compile(r"apply\s*now|apply\s*here|start\s*application", re.IGNORECASE)).first
            if await apply.count() and await apply.is_visible():
                await apply.click()
                await page.wait_for_timeout(4000)
        except Exception:
            pass

    async def _is_auth_gate(self, page) -> bool:
        try:
            locator = page.locator("input, select, textarea")
            count = await locator.count()
            if count > 5:
                return False
            types = set()
            for i in range(count):
                t = await locator.nth(i).get_attribute("type")
                if t:
                    types.add(t)
            login_shaped = types.issubset({"email", "password", "tel", "text", "checkbox"})
            return login_shaped and count <= 4
        except Exception:
            return False

    async def _handle_auth_gate(self, page) -> None:
        if not await self._is_auth_gate(page):
            return
        slug = safe_slug(await page.evaluate("() => window.location.href"))
        print(f"\nAuth gate detected for {slug}")
        await send_telegram(
            f"Auth required for {slug}\n"
            f"Complete the login/verification in the browser, then send: ready {slug}"
        )
        print("Auth gate: check Telegram or complete in browser manually.")
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = load_chat_id()
        last_update_id = 0
        timeout = 300
        start = asyncio.get_event_loop().time()
        loop = asyncio.get_event_loop()

        def read_stdin() -> str:
            try:
                return input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                return ""

        ready = False
        while not ready and (asyncio.get_event_loop().time() - start) < timeout:
            if token and chat_id:
                try:
                    msgs = poll_telegram(token, chat_id, last_update_id)
                    for msg_text, upd_id in msgs:
                        last_update_id = max(last_update_id, upd_id)
                        parts = msg_text.strip().lower().split(None, 1)
                        if parts and parts[0] == "ready" and (len(parts) < 2 or parts[1] == slug):
                            ready = True
                except Exception:
                    pass
            try:
                fut = loop.run_in_executor(None, read_stdin)
                line = await asyncio.wait_for(fut, timeout=2.0)
                if line.lower() == f"ready {slug}" or line.lower() == "ready":
                    ready = True
            except asyncio.TimeoutError:
                pass
            if ready:
                break
            await page.wait_for_timeout(1000)
            if not await self._is_auth_gate(page):
                ready = True
        await page.wait_for_timeout(2000)

    async def _poll_commands(
        self,
        page,
        slug: str,
        live_fields: List[FieldEvidence],
        field_contexts: Dict[str, str],
    ) -> None:
        from ._shared import read_current_values as _read_current

        print(f"\nBrowser open — job slug: {slug}")
        print(f"Send 'save {slug}' to Telegram or type in terminal to capture.")
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = load_chat_id()
        telegram_ok = bool(token and chat_id)
        if not telegram_ok:
            print(f"Telegram not available. Type: save {slug}")
        last_update_id = 0
        loop = asyncio.get_event_loop()

        def read_stdin() -> str:
            try:
                return input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                return ""

        while True:
            cmd = ""
            target = ""
            if telegram_ok:
                try:
                    msgs = poll_telegram(token, chat_id, last_update_id)
                    for msg_text, upd_id in msgs:
                        last_update_id = max(last_update_id, upd_id)
                        parts = msg_text.strip().lower().split(None, 1)
                        c = parts[0] if parts else ""
                        t = parts[1] if len(parts) > 1 else ""
                        if c in ("save", "capture", "learn") and (not t or t == slug):
                            cmd = c
                            target = t
                except Exception:
                    pass
            if not cmd:
                try:
                    fut = loop.run_in_executor(None, read_stdin)
                    line = await asyncio.wait_for(fut, timeout=3.0)
                    if line:
                        parts = line.lower().split(None, 1)
                        c = parts[0] if parts else ""
                        t = parts[1] if len(parts) > 1 else ""
                        if c in ("save", "capture", "learn") and (not t or t == slug):
                            cmd = c
                            target = t
                except asyncio.TimeoutError:
                    pass
                except (EOFError, KeyboardInterrupt):
                    break
            if cmd:
                current = await _read_current(page, live_fields, field_contexts)
                save_learned_answers(current, page.url)
                msg = f"Saved {len(current)} field values for {slug}. You can submit now."
                if telegram_ok:
                    try:
                        reply_telegram(token, chat_id, msg)
                    except Exception:
                        pass
                print(msg)
