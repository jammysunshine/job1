"""
DEPRECATED — v3 spec replaces per-vendor handlers with the generic
four-stage pipeline (GenericHandler). This file is retained for
reference only and is no longer imported or routed anywhere.

Use job_agent.handlers.generic_handler.GenericHandler instead.
"""


from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..models import FieldEvidence
from ..storage import ROOT, SCREENSHOT_DIR
from .base import FillResult, VendorHandler


class OracleRecruitingHandler(VendorHandler):
    def __init__(self, headless: bool = False):
        self.headless = headless

    def _safe_slug(self, value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
        return slug[:120] or "job"

    async def discover_fields(self, url: str) -> tuple:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            apply = page.locator("text=APPLY NOW").first
            if await apply.is_visible():
                pass

            fields = await self._extract_fields(page)
            slug = self._safe_slug(url)
            screenshot_path = str(SCREENSHOT_DIR / f"{slug}-handler.png")
            await page.screenshot(path=screenshot_path, full_page=True)
            await browser.close()

        return url, fields, screenshot_path

    async def _auth_flow(self, page, email: str) -> None:
        apply = page.locator("text=APPLY NOW").first
        if await apply.is_visible():
            await apply.click()
            await page.wait_for_timeout(5000)

        email_field = page.locator("#primary-email-0")
        if await email_field.count():
            await email_field.fill(email)
            await page.wait_for_timeout(500)

            agree = page.locator("text=I agree with the terms and conditions").first
            if await agree.count():
                await agree.click()
                await page.wait_for_timeout(500)

            next_btn = page.locator("text=NEXT").first
            if await next_btn.count() and await next_btn.is_visible():
                await next_btn.click()

    async def _wait_for_pin(self, page, job_slug: str) -> bool:
        for _ in range(20):
            pin_fields = page.locator("#pin-code-1")
            if await pin_fields.count():
                break
            form_fields = page.locator("#lastName-15, #firstName-16, #country-14")
            if await form_fields.count():
                return True
            await page.wait_for_timeout(1000)
        else:
            return True

        print(f"\nPIN verification required for {job_slug}")

        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = self._load_chat_id()
        use_telegram = bool(token and chat_id)

        if use_telegram:
            try:
                from ..telegram_bot import send_message as tg_send
                await tg_send(
                    f"Oracle form: PIN required for {job_slug}\n"
                    f"Send: pin {job_slug} 123456"
                )
                print("Telegram notification sent. Waiting for PIN via Telegram...")
            except Exception as exc:
                print(f"Telegram notification failed: {exc}")
                use_telegram = False

        if not use_telegram:
            print("Telegram not available. Enter PIN directly in terminal.")
            print(f"Type the 6-digit PIN for {job_slug} and press Enter.")

        last_update_id = 0
        timeout = 300
        loop = asyncio.get_event_loop()
        start = loop.time()

        def read_pin_from_stdin() -> Optional[str]:
            try:
                return input("PIN (6 digits): ").strip()
            except (EOFError, KeyboardInterrupt):
                return None

        while loop.time() - start < timeout:
            got_pin: Optional[str] = None

            if use_telegram:
                try:
                    msgs = self._poll_telegram(token, chat_id, last_update_id)
                    for msg_text, upd_id in msgs:
                        last_update_id = max(last_update_id, upd_id)
                        parts = msg_text.strip().split(None, 2)
                        if len(parts) >= 2 and parts[0].lower() == "pin":
                            target = parts[1]
                            code = parts[2] if len(parts) > 2 else ""
                            if target == job_slug and len(code) >= 6:
                                got_pin = code[:6]
                except Exception:
                    pass

            if got_pin is None:
                try:
                    fut = loop.run_in_executor(None, read_pin_from_stdin)
                    pin_input = await asyncio.wait_for(fut, timeout=1.0)
                    if pin_input and len(pin_input) >= 6:
                        got_pin = pin_input[:6]
                except (asyncio.TimeoutError, EOFError, KeyboardInterrupt):
                    pass

            if got_pin:
                for j in range(6):
                    pf = page.locator(f"#pin-code-{j+1}")
                    if await pf.count():
                        await pf.fill(got_pin[j])
                await page.wait_for_timeout(1000)
                try:
                    from ..telegram_bot import send_message as tg_send
                    await tg_send(f"PIN entered for {job_slug}. Filling form...")
                except Exception:
                    pass
                return True

            await asyncio.sleep(0.5)

        return False

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
            page.set_default_timeout(15000)
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            await self._auth_flow(page, "mohit.mendiratta@gmail.com")

            job_slug = self._safe_slug(url)
            pin_ok = await self._wait_for_pin(page, job_slug)
            if not pin_ok:
                result.status = "pin_timeout"
                result.errors = ["PIN not received within 5 minutes"]
                return result

            await page.wait_for_timeout(3000)
            form_url = page.url

            import yaml as _yaml
            _profile = _yaml.safe_load((ROOT / "config" / "profile.yaml").read_text())
            _personal = _profile.get("personal", {})

            live_fields = await self._extract_fields(page)

            mapping = field_mapping
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
            used_field_ids: set = set()

            for entry in mapping.get("fields", []):
                field_idx = entry.get("field_idx")
                field_id = entry.get("field_id")
                value = entry.get("value")
                if field_idx is None or not field_id or value is None:
                    continue
                match = None
                for f in live_fields:
                    if f.field_idx == field_idx:
                        match = f
                        break
                if match is None:
                    continue

                el = page.locator("input, select, textarea").nth(match.field_idx)
                is_readonly = await el.evaluate("el => el.hasAttribute('readonly')")
                if is_readonly:
                    continue

                tag = match.tag_name
                input_type = match.input_type
                try:
                    if tag == "select":
                        val_str = str(value)
                        try:
                            await el.select_option(label=val_str)
                        except Exception:
                            try:
                                await el.select_option(value=val_str)
                            except Exception:
                                try:
                                    await el.select_option(index=0)
                                except Exception:
                                    pass
                    elif input_type == "checkbox":
                        checked = await el.is_checked()
                        if (value is True or str(value).lower() == "on") and not checked:
                            await el.check()
                        elif (value is False or str(value).lower() == "off") and checked:
                            await el.uncheck()
                    elif input_type == "radio":
                        val_str = str(value)
                        opt = page.locator(f"label:has-text('{val_str}')").first
                        if await opt.count() and await opt.is_visible():
                            await opt.click()
                        else:
                            await el.evaluate("el => el.checked = true")
                    elif input_type == "file":
                        if cv_path:
                            cv_abs = Path(cv_path)
                            if not cv_abs.is_absolute():
                                cv_abs = ROOT / cv_path
                            await el.set_input_files(str(cv_abs))
                    else:
                        await el.fill(str(value))
                    used_field_ids.add(field_id)
                    filled.append({
                        "field_id": field_id,
                        "field_context": field_contexts.get(field_id) or entry.get("field_context"),
                        "value": value,
                    })
                except Exception as exc:
                    errors.append(f"Failed to fill {field_id}: {exc}")

            PROFILE_MAP = {
                "lastname": _personal.get("full_name", "").split()[-1] if _personal.get("full_name") else "",
                "firstname": _personal.get("full_name", "").split()[0] if _personal.get("full_name") else "",
                "email": _personal.get("email", ""),
                "phone": _personal.get("phone", ""),
                "addressline1": _personal.get("location", "Dubai, UAE").split(",")[0].strip() if "," in _personal.get("location", "") else _personal.get("location", ""),
                "city": _personal.get("location", "").split(",")[0].strip() if _personal.get("location") else "",
                "nationality": _personal.get("nationality", "British"),
                "country": "Saudi Arabia",
            }

            for nf in live_fields:
                if nf.field_id in used_field_ids:
                    continue
                el = page.locator("input, select, textarea").nth(nf.field_idx)
                is_readonly = await el.evaluate("el => el.hasAttribute('readonly')")
                if is_readonly:
                    continue
                tag = nf.tag_name
                input_type = nf.input_type
                id_lower = (nf.field_id or "").lower()
                label_lower = (nf.label or nf.nearby_text or "").lower()

                matched = False
                value = None
                ctx = None

                if input_type == "file":
                    if cv_path and "resume" in label_lower or "cv" in label_lower or "upload" in label_lower:
                        cv_abs = Path(cv_path)
                        if not cv_abs.is_absolute():
                            cv_abs = ROOT / cv_path
                        try:
                            await el.set_input_files(str(cv_abs))
                            matched = True
                            ctx = "cv_upload"
                            value = str(cv_abs)
                        except Exception:
                            pass
                elif "lastname" in id_lower or ("last" in label_lower and "name" in label_lower):
                    value = PROFILE_MAP["lastname"]
                    ctx = "last_name"
                elif "firstname" in id_lower or ("first" in label_lower and "name" in label_lower):
                    value = PROFILE_MAP["firstname"]
                    ctx = "first_name"
                elif "email" in id_lower or "email" in label_lower:
                    value = PROFILE_MAP["email"]
                    ctx = "email"
                elif "phone" in label_lower and "number" in label_lower:
                    raw = PROFILE_MAP["phone"]
                    value = re.sub(r"[^0-9]", "", raw)
                    ctx = "phone_number"
                elif "country" in id_lower or "country" in label_lower:
                    if tag == "select":
                        try:
                            await el.select_option(label=PROFILE_MAP["country"])
                            matched = True
                            ctx = "country"
                            value = PROFILE_MAP["country"]
                        except Exception:
                            pass
                elif "nationality" in id_lower or "nationality" in label_lower:
                    if tag == "select":
                        try:
                            await el.select_option(label=PROFILE_MAP["nationality"])
                            matched = True
                            ctx = "nationality"
                            value = PROFILE_MAP["nationality"]
                        except Exception:
                            pass
                elif "addressline1" in id_lower or "address" in label_lower:
                    value = PROFILE_MAP["addressline1"]
                    ctx = "address"
                elif "city" in id_lower or "city" in label_lower:
                    value = PROFILE_MAP["city"]
                    ctx = "city"
                elif "dateofbirth" in id_lower or "date_of_birth" in id_lower or "dob" in id_lower:
                    dob = _personal.get("date_of_birth", "")
                    parts = dob.replace(",", "").split()
                    month_map = {"jan": "1","feb": "2","mar": "3","apr": "4","may": "5","jun": "6","jul": "7","aug": "8","sep": "9","oct": "10","nov": "11","dec": "12"}
                    if len(parts) >= 3:
                        m = month_map.get(parts[0].strip().lower()[:3], "1")
                        d = re.sub(r"[^0-9]", "", parts[1])
                        y = re.sub(r"[^0-9]", "", parts[2])
                        if "month" in id_lower:
                            if tag == "select":
                                try:
                                    await el.select_option(value=m)
                                    matched = True; ctx = "date_of_birth_month"; value = m
                                except Exception:
                                    pass
                        elif "day" in id_lower:
                            if tag == "select":
                                try:
                                    await el.select_option(value=d)
                                    matched = True; ctx = "date_of_birth_day"; value = d
                                except Exception:
                                    pass
                        elif "year" in id_lower:
                            if tag == "select":
                                try:
                                    await el.select_option(value=y)
                                    matched = True; ctx = "date_of_birth_year"; value = y
                                except Exception:
                                    pass
                elif "gender" in id_lower or "gender" in label_lower:
                    if tag == "select":
                        try:
                            await el.select_option(label="Male")
                            matched = True; ctx = "gender"; value = "Male"
                        except Exception:
                            pass
                elif "marital" in id_lower or "marital" in label_lower:
                    status = _profile.get("personal_details", {}).get("marital_status", "")
                    if status and tag == "select":
                        try:
                            await el.select_option(label=status)
                            matched = True; ctx = "marital_status"; value = status
                        except Exception:
                            pass
                elif "native" in label_lower or "residing" in label_lower or "inside" in label_lower:
                    value = "No"
                    ctx = "residing_in_ksa"
                    opt = page.locator("label:has-text('No')").first
                    if await opt.count() and await opt.is_visible():
                        await opt.click()
                        matched = True
                elif "notice" in label_lower:
                    value = "0"
                    ctx = "notice_period"
                elif "signature" in label_lower or "fullname" in id_lower:
                    value = _personal.get("full_name", "")
                    ctx = "full_name"

                if value is not None and not matched:
                    try:
                        if tag == "select":
                            try:
                                await el.select_option(label=str(value))
                            except Exception:
                                try:
                                    await el.select_option(value=str(value))
                                except Exception:
                                    pass
                        else:
                            await el.fill(str(value))
                        matched = True
                    except Exception:
                        pass

                if matched:
                    filled.append({"field_id": nf.field_id, "field_context": ctx, "value": value})
                    used_field_ids.add(nf.field_id)

            await page.wait_for_timeout(1000)
            slug = self._safe_slug(url)
            screenshot_path = str(SCREENSHOT_DIR / f"{slug}-filled.png")
            await page.screenshot(path=screenshot_path, full_page=True)

            result.status = "filled_awaiting_review" if not errors else "filled_with_errors"
            result.filled_fields = filled
            result.errors = errors
            result.screenshot_path = screenshot_path
            result.form_url = form_url

            if not self.headless:
                try:
                    from ..telegram_bot import send_message
                    await send_message(
                        f"Oracle form filled for: {url}\n\n"
                        f"Browser is open. Slug: {job_slug}\n"
                        "Review fields, then send:\n"
                        f"  save {job_slug}\n"
                        "to capture adjustments."
                    )
                except Exception:
                    pass
                print(f"\nBrowser open — job slug: {job_slug}")
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
                                current = await self._read_current_values(page, live_fields)
                                self._save_learned_answers(current, url)
                                self._reply_telegram(token, chat_id, f"Saved {len(current)} field values for {job_slug}. You can submit now.")
                                print(f"Captured {len(current)} values — saved to learned_answers.json")
                    except Exception:
                        pass

        except Exception as exc:
            result.status = "error"
            result.errors = [str(exc)]
            import traceback
            traceback.print_exc()
        finally:
            if self.headless and browser:
                await browser.close()
                if p:
                    await p.stop()

        return result

    async def _extract_fields(self, page) -> List[FieldEvidence]:
        locator = page.locator("input, select, textarea")
        count = await locator.count()
        raw: List[FieldEvidence] = []
        seen = set()
        for i in range(count):
            el = locator.nth(i)
            tag_name = await el.evaluate("el => el.tagName.toLowerCase()")
            input_type = await el.evaluate("el => el.getAttribute('type')")
            if input_type == "hidden":
                continue
            visible = await el.is_visible()
            if not visible and input_type != "file":
                continue
            el_id = await el.evaluate("el => el.id")
            el_name = await el.evaluate("el => el.getAttribute('name')")
            field_id = el_id or el_name or f"field_{i}"
            if field_id in seen:
                continue
            seen.add(field_id)
            placeholder = await el.evaluate("el => el.getAttribute('placeholder')")
            aria_label = await el.evaluate("el => el.getAttribute('aria-label')")
            required = await el.evaluate("el => Boolean(el.required || el.getAttribute('aria-required') === 'true')")
            nearby_text = await el.evaluate("""el => {
                const p = el.closest('[class*=field], .row, .form-group, section, div');
                if (!p) return '';
                const label = p.querySelector('label, .label, .field-label');
                if (label) return label.innerText.trim().slice(0, 200);
                return p.innerText.replace(el.value || '', '').trim().slice(0, 200);
            }""")
            options: List[str] = []
            if tag_name == "select":
                options = await el.evaluate("el => Array.from(el.options).map(o => o.text.trim()).filter(Boolean)")
            raw.append(FieldEvidence(
                field_idx=i, field_id=field_id, tag_name=tag_name,
                input_type=input_type, label=nearby_text[:100] if nearby_text else None,
                placeholder=placeholder, aria_label=aria_label,
                required=required, visible=visible, options=options,
                nearby_text=nearby_text,
            ))
        return raw

    def _save_learned_answers(self, filled: List[Dict[str, Any]], form_url: str) -> None:
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

    async def _read_current_values(self, page, live_fields: List[FieldEvidence]) -> List[Dict[str, Any]]:
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
                values.append({"field_id": f.field_id, "value": val})
            except Exception:
                pass
        return values

    def _load_chat_id(self) -> Optional[int]:
        path = ROOT / "data" / "telegram_chat_id.txt"
        if path.exists():
            try:
                return int(path.read_text().strip())
            except (ValueError, OSError):
                return None
        return None

    def _poll_telegram(self, token: str, chat_id: int, last_update_id: int) -> List[Tuple[str, int]]:
        import ssl
        ctx = ssl._create_unverified_context()
        url = f"https://api.telegram.org/bot{token}/getUpdates?offset={last_update_id + 1}&timeout=5"
        req = urllib.request.Request(url, headers={"User-Agent": "python"})
        resp = urllib.request.urlopen(req, timeout=10, context=ctx)
        data = json.loads(resp.read())
        messages = []
        for update in data.get("result", []):
            upd_id = update.get("update_id", 0)
            msg = update.get("message", {}) or update.get("edited_message", {})
            text = msg.get("text", "")
            cid = msg.get("chat", {}).get("id")
            if cid == chat_id and text:
                messages.append((text, upd_id))
        return messages

    def _reply_telegram(self, token: str, chat_id: int, text: str) -> None:
        import ssl
        ctx = ssl._create_unverified_context()
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        req = urllib.request.Request(url, data=data, headers={"User-Agent": "python"})
        urllib.request.urlopen(req, timeout=10, context=ctx)
