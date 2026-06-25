from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..models import FieldEvidence
from ..storage import ROOT, SCREENSHOT_DIR
from ._shared import (
    load_chat_id,
    poll_telegram,
    read_current_values,
    reply_telegram,
    safe_slug,
    save_learned_answers,
    send_telegram,
)
from .base import FillResult, VendorHandler
from .popups import dismiss_common_popups

logger = logging.getLogger(__name__)

CONTINUE_LABELS = (
    "continue",
    "next",
    "next step",
    "proceed",
    "save and continue",
    "save & continue",
)
FINAL_SUBMIT_LABELS = (
    "submit",
    "submit application",
    "send application",
    "finish application",
)
MAX_PAGES = 10


class GenericHandler(VendorHandler):
    """V3 four-stage pipeline form runner.

    Implements the generic pipeline from the v3 spec. Every form —
    known ATS or fully custom — runs through the same four stages:

      Stage A (Extractor)    — inventory visible DOM fields (no LLM)
      Stage B (Interpreter)  — resolve what each field means (1 LLM call)
      Stage C (Mapper)       — decide what value goes in each field (1 LLM call)
      Stage D (Filler)       — mechanically execute fills (no LLM)

    The pipeline loops for multi-step forms. Never clicks final submit.
    """

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
        page.on("dialog", lambda dialog: dialog.dismiss())
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2000)
        await dismiss_common_popups(page)
        return page, ctx

    async def discover_fields(self, url: str) -> tuple:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)
            page, ctx = await self._setup_page(browser, url)
            await self._click_apply(page)
            await dismiss_common_popups(page)
            fields = await self._extract_fields(page)
            slug = safe_slug(page.url or url)
            screenshot_path = str(SCREENSHOT_DIR / f"{slug}-handler.png")
            await page.screenshot(path=screenshot_path, full_page=True)
            await ctx.close()
            await browser.close()

        return url, fields, screenshot_path

    async def fill_and_stop(
        self,
        url: str,
        field_mapping: Dict[str, Any],
        cv_path: Optional[str] = None,
    ) -> FillResult:
        from playwright.async_api import async_playwright

        result = FillResult(status="started")
        errors: List[str] = []
        filled: List[Dict[str, Any]] = []
        page_snapshots: List[Dict[str, Any]] = []
        p = None
        browser = None
        ctx = None

        try:
            p = await async_playwright().start()
            browser = await p.chromium.launch(headless=self.headless)
            page, ctx = await self._setup_page(browser, url)

            await self._click_apply(page)
            await dismiss_common_popups(page)
            await self._handle_auth_gate(page)

            cv_path = self._resolve_path(cv_path)
            prior_signatures = set()

            for page_num in range(MAX_PAGES):
                await dismiss_common_popups(page)
                if page_num > 0:
                    await self._click_apply(page)
                    await dismiss_common_popups(page)
                    await self._handle_auth_gate(page)

                # Stage A: Extractor — deterministic DOM inventory
                live_fields = await self._extract_fields(page)
                signature = self._field_signature(page.url, live_fields)
                if signature in prior_signatures and page_num > 0:
                    errors.append("Stopped because the form did not advance after clicking continue.")
                    break
                prior_signatures.add(signature)

                if live_fields:
                    # Stage B: Field Interpreter — one LLM call, reasons about the FORM
                    interpreted = await self._interpret_fields(page, live_fields)

                    # Stage C: Mapper — one LLM call, matches PROFILE data to field needs
                    mapping = await self._map_fields(page, url, live_fields, interpreted, page_num)

                    # Stage D: Filler — deterministic execution
                    page_filled, page_errors = await self._fill_fields(
                        page,
                        live_fields,
                        mapping,
                        cv_path=cv_path,
                    )
                    filled.extend(page_filled)
                    errors.extend(page_errors)
                    page_snapshots.append({
                        "page": page_num,
                        "url": page.url,
                        "field_count": len(live_fields),
                        "filled_count": len(page_filled),
                    })

                if await self._final_submit_visible(page):
                    break

                continue_btn = await self._find_continue(page)
                if continue_btn is None:
                    break

                before_url = page.url
                try:
                    await continue_btn.click()
                    await page.wait_for_timeout(2500)
                    await dismiss_common_popups(page)
                    if page.url == before_url:
                        await page.wait_for_timeout(1000)
                except Exception as exc:
                    errors.append(f"Could not continue to next page: {exc}")
                    break

            result.status = "filled_awaiting_review" if not errors else "filled_with_errors"
            result.filled_fields = filled
            result.errors = errors
            result.form_url = page.url
            slug = safe_slug(url)
            screenshot_path = str(SCREENSHOT_DIR / f"{slug}-filled.png")
            await page.screenshot(path=screenshot_path, full_page=True)
            result.screenshot_path = screenshot_path

            if not self.headless:
                await self._final_review_checkpoint(page, slug, filled)
            else:
                await ctx.close()
                await browser.close()
                await p.stop()

        except Exception as exc:
            errors.append(str(exc))
            result.status = "filled_with_errors"
            result.errors = errors
        finally:
            if self.headless:
                try:
                    if ctx:
                        await ctx.close()
                except Exception:
                    pass
                try:
                    if browser:
                        await browser.close()
                except Exception:
                    pass
                try:
                    if p:
                        await p.stop()
                except Exception:
                    pass

        return result

    async def _interpret_fields(
        self,
        page,
        fields: List[FieldEvidence],
    ) -> Optional[Dict[str, Any]]:
        from ..field_interpreter import interpret_fields

        text = ""
        try:
            text = (await page.locator("body").inner_text(timeout=5000))[:6000]
        except Exception:
            pass

        return interpret_fields(fields, page_text=text)

    async def _map_fields(
        self,
        page,
        job_url: str,
        fields: List[FieldEvidence],
        interpreted: Optional[Dict[str, Any]],
        page_num: int,
    ) -> Dict[str, Any]:
        from ..decision_engine import map_fields

        text = ""
        try:
            text = (await page.locator("body").inner_text(timeout=5000))[:6000]
        except Exception:
            pass

        job_context = {
            "job_url": job_url,
            "form_url": page.url,
            "page_number": page_num,
            "visible_page_text": text,
            "mapping_scope": "current_page_only",
            "stage_b_annotations": interpreted,
        }
        return map_fields(fields, job_context=job_context)

    async def _fill_fields(
        self,
        page,
        live_fields: List[FieldEvidence],
        mapping: Dict[str, Any],
        *,
        cv_path: Optional[str],
    ) -> tuple[List[Dict[str, Any]], List[str]]:
        filled: List[Dict[str, Any]] = []
        errors: List[str] = []
        live_by_idx = {field.field_idx: field for field in live_fields}
        live_by_id = {field.field_id: field for field in live_fields if field.field_id}

        for entry in mapping.get("fields", []):
            value = entry.get("value")
            if value is None:
                continue
            if isinstance(value, str) and value.strip().lower() in ("null", "none", ""):
                continue
            field = None
            if entry.get("field_idx") is not None:
                field = live_by_idx.get(int(entry["field_idx"]))
            if field is None and entry.get("field_id"):
                field = live_by_id.get(entry["field_id"])
            if field is None:
                errors.append(f"Mapped field not found on current page: {entry.get('field_id')}")
                continue

            try:
                ok = await self._fill_field(page, field, value, cv_path)
                if ok:
                    verified = await self._verify_field_value(page, field, value)
                    filled.append({
                        "field_id": field.field_id,
                        "field_idx": field.field_idx,
                        "field_context": entry.get("field_context"),
                        "value": value,
                        "verified": verified,
                    })
                    if not verified and field.input_type not in ("file", "checkbox", "radio"):
                        errors.append(f"Could not verify value for {field.field_id}")
                else:
                    errors.append(f"Could not fill {field.field_id}")
            except Exception as exc:
                errors.append(f"Field {field.field_id}: {exc}")

        if cv_path:
            try:
                uploaded = await self._try_cv_upload(page, cv_path)
                if uploaded:
                    filled.append({
                        "field_id": "_cv",
                        "field_context": "cv_upload",
                        "value": cv_path,
                        "verified": True,
                    })
            except Exception as exc:
                errors.append(f"CV upload failed: {exc}")

        return filled, errors

    async def _extract_fields(self, page) -> List[FieldEvidence]:
        raw: List[FieldEvidence] = []

        locator = page.locator("input, select, textarea")
        count = await locator.count()
        seen_keys = set()

        for idx in range(count):
            el = locator.nth(idx)
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
                visible = False
            if not visible and input_type != "file":
                continue

            input_type_attr = input_type
            meta = await el.evaluate(
                f"""
                el => {{
                  const labelFor = el.id
                    ? document.querySelector(`label[for="${{CSS.escape(el.id)}}"]`)?.innerText?.trim()
                    : null;
                  const wrappingLabel = el.closest('label')?.innerText?.trim() || null;
                  let parent = el.closest('fieldset, .form-group, .field, [class*=form], [class*=field], div, li, td') || el.parentElement;
                  const isChoice = {('true' if input_type_attr in ('radio', 'checkbox') else 'false')};
                  if (isChoice) {{
                    for (let p = parent; p && p !== document.body; p = p.parentElement) {{
                      const text = (p.innerText || '').trim();
                      if (text.length > 60 || p.querySelectorAll('input[type=radio],input[type=checkbox]').length >= 2) {{
                        parent = p;
                        break;
                      }}
                    }}
                  }}
                  const section = el.closest('section, fieldset, form');
                  return {{
                    id: el.id || '',
                    name: el.getAttribute('name') || '',
                    placeholder: el.getAttribute('placeholder'),
                    ariaLabel: el.getAttribute('aria-label'),
                    required: Boolean(el.required || el.getAttribute('aria-required') === 'true'),
                    label: labelFor || wrappingLabel || null,
                    nearbyText: (parent?.innerText || '').trim().slice(0, 600),
                    sectionText: (section?.querySelector('legend,h1,h2,h3,h4')?.innerText || '').trim(),
                    value: el.value || ''
                  }};
                }}
                """
            )
            field_id = meta.get("id") or meta.get("name") or f"field_{idx}"
            stable_key = (field_id, idx)
            if stable_key in seen_keys:
                continue
            seen_keys.add(stable_key)

            options: List[str] = []
            if tag_name == "select":
                options = await el.evaluate(
                    "el => Array.from(el.options).map(o => o.innerText.trim()).filter(Boolean)"
                )
            elif input_type in ("radio", "checkbox"):
                options = await self._nearby_choice_options(el)

            nearby_parts = [
                meta.get("sectionText") or "",
                meta.get("nearbyText") or "",
            ]
            raw.append(FieldEvidence(
                field_idx=idx,
                field_id=field_id,
                tag_name=tag_name,
                input_type=input_type or "text",
                label=meta.get("label") or "",
                placeholder=meta.get("placeholder"),
                aria_label=meta.get("ariaLabel"),
                required=bool(meta.get("required")),
                visible=visible,
                options=options,
                nearby_text=" ".join(part for part in nearby_parts if part).strip()[:800],
            ))

        iframe_fields = await self._extract_fields_from_iframes(page)
        raw.extend(iframe_fields)

        return raw

    async def _extract_fields_from_iframes(self, page) -> List[FieldEvidence]:
        raw: List[FieldEvidence] = []
        try:
            await page.wait_for_selector("iframe[src]", state="attached", timeout=3000)
        except Exception:
            pass
        iframe_count = await page.locator("iframe[src]").count()
        for i in range(iframe_count):
            iframe_el = page.locator("iframe[src]").nth(i)
            src = (await iframe_el.get_attribute("src")) or ""
            try:
                handle = await iframe_el.element_handle()
                if not handle:
                    continue
                    frame = await handle.content_frame()
                    if not frame:
                        continue
                    try:
                        await frame.wait_for_selector("input, select, textarea", state="attached", timeout=5000)
                    except Exception:
                        continue
            except Exception:
                continue
            locator = frame.locator("input, select, textarea")
            count = await locator.count()
            for idx in range(count):
                el = locator.nth(idx)
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
                    visible = False
                if not visible and input_type != "file":
                    continue
                meta = await el.evaluate(
                    """
                    el => {
                      const labelFor = el.id
                        ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`)?.innerText?.trim()
                        : null;
                      const wrappingLabel = el.closest('label')?.innerText?.trim() || null;
                      let parent = el.closest('fieldset, .form-group, .field, div, li, td') || el.parentElement;
                      const section = el.closest('section, fieldset, form');
                      return {
                        id: el.id || '',
                        name: el.getAttribute('name') || '',
                        placeholder: el.getAttribute('placeholder'),
                        ariaLabel: el.getAttribute('aria-label'),
                        required: Boolean(el.required || el.getAttribute('aria-required') === 'true'),
                        label: labelFor || wrappingLabel || null,
                        nearbyText: (parent?.innerText || '').trim().slice(0, 600),
                        sectionText: (section?.querySelector('legend,h1,h2,h3,h4')?.innerText || '').trim(),
                      };
                    }
                    """
                )
                field_id = meta.get("id") or meta.get("name") or f"iframe_field_{len(raw)}"
                options: List[str] = []
                if tag_name == "select":
                    options = await el.evaluate(
                        "el => Array.from(el.options).map(o => o.innerText.trim()).filter(Boolean)"
                    )
                elif input_type in ("radio", "checkbox"):
                    options = await self._nearby_choice_options(el)
                nearby_parts = [
                    meta.get("sectionText") or "",
                    meta.get("nearbyText") or "",
                ]
                raw.append(FieldEvidence(
                    field_idx=len(raw),
                    field_id=field_id,
                    tag_name=tag_name,
                    input_type=input_type or "text",
                    label=meta.get("label") or "",
                    placeholder=meta.get("placeholder"),
                    aria_label=meta.get("ariaLabel"),
                    required=bool(meta.get("required")),
                    visible=visible,
                    options=options,
                    nearby_text=" ".join(part for part in nearby_parts if part).strip()[:800],
                    iframe_id=src[:120],
                ))
        return raw

    async def _nearby_choice_options(self, el) -> List[str]:
        try:
            return await el.evaluate(
                """
                el => {
                  const group = el.closest('fieldset, [role=radiogroup], [class*=radio], [class*=checkbox], div') || el.parentElement;
                  let labels = Array.from(group.querySelectorAll('label'))
                    .map(label => label.innerText.trim())
                    .filter(Boolean);
                  if (labels.length === 0) {
                    const inputs = group.querySelectorAll('input[type=radio], input[type=checkbox]');
                    labels = Array.from(inputs)
                      .map(inp => {
                        const lbl = inp.closest('label');
                        if (lbl) return lbl.innerText.trim();
                        const forLabel = inp.id && document.querySelector('label[for=\"' + CSS.escape(inp.id) + '\"]');
                        if (forLabel) return forLabel.innerText.trim();
                        const parent = inp.parentElement;
                        const text = (parent?.innerText || '').trim().replace(inp.value || '', '').trim();
                        return text || inp.value || inp.nextSibling?.textContent?.trim() || '';
                      })
                      .filter(Boolean);
                  }
                  return [...new Set(labels)].slice(0, 20);
                }
                """
            )
        except Exception:
            return []

    async def _fill_field(self, page, field: FieldEvidence, value: Any, cv_path: Optional[str]) -> bool:
        el = await self._locator_for_field(page, field)
        if el is None or not await el.count():
            return False

        tag = field.tag_name
        input_type = field.input_type or "text"
        value_text = str(value)

        readonly = False
        try:
            readonly = await el.evaluate("el => Boolean(el.readOnly || el.disabled)")
        except Exception:
            pass
        if readonly and input_type != "file":
            return False

        if tag == "select":
            return await self._select_option(el, field, value_text)

        if input_type == "file":
            file_path = self._resolve_path(value_text)
            if file_path and Path(file_path).exists():
                await el.set_input_files(file_path)
                await self._dispatch_input_events(el)
                return True
            if len(value_text) > 50:
                tmp = ROOT / "data" / f"tmp_{field.field_idx}_{field.field_id.replace('/', '_')[:40]}.txt"
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(value_text, encoding="utf-8")
                await el.set_input_files(str(tmp))
                await self._dispatch_input_events(el)
                return True
            if cv_path and Path(cv_path).exists():
                await el.set_input_files(cv_path)
                await self._dispatch_input_events(el)
                return True
            return False

        if input_type == "radio":
            return await self._choose_radio(page, field, value_text)

        if input_type == "checkbox":
            should_check = str(value).strip().lower() in ("yes", "true", "1", "on", "checked", "agree")
            checked = await el.is_checked()
            if should_check != checked:
                await el.click()
            return True

        if input_type in ("tel", "number"):
            value_text = re.sub(r"[^\d+]", "", value_text)

        await el.fill(value_text)
        await self._dispatch_input_events(el)
        return True

    async def _locator_for_field(self, page, field: FieldEvidence):
        target = page
        if field.iframe_id:
            frame = await self._find_iframe_by_src(page, field.iframe_id)
            if frame:
                target = frame

        if field.field_id:
            for selector in (
                f"[id={json.dumps(field.field_id)}]",
                f"[name={json.dumps(field.field_id)}]",
            ):
                loc = target.locator(selector).first
                try:
                    if await loc.count():
                        return loc
                except Exception:
                    pass
        loc = target.locator("input, select, textarea").nth(field.field_idx)
        if await loc.count():
            return loc
        return None

    async def _find_iframe_by_src(self, page, src_substring: str) -> Any:
        iframes = page.locator("iframe")
        count = await iframes.count()
        for i in range(count):
            try:
                actual_src = await iframes.nth(i).get_attribute("src")
                if actual_src and src_substring in actual_src:
                    handle = await iframes.nth(i).element_handle()
                    if handle:
                        frame = await handle.content_frame()
                        if frame:
                            return frame
            except Exception:
                continue
        return None

    async def _select_option(self, el, field: FieldEvidence, value: str) -> bool:
        attempts = [value]
        attempts.extend(option for option in field.options if value.lower() in option.lower())
        for attempt in attempts:
            try:
                await el.select_option(label=attempt)
                await self._dispatch_input_events(el)
                return True
            except Exception:
                try:
                    await el.select_option(value=attempt)
                    await self._dispatch_input_events(el)
                    return True
                except Exception:
                    continue
        return False

    async def _choose_radio(self, page, field: FieldEvidence, value: str) -> bool:
        el = await self._locator_for_field(page, field)
        if el is None or not await el.count():
            return False

        try:
            clicked = await el.evaluate(
                f"""
                el => {{
                    const targetValue = {json.dumps(value)};
                    const parent = el.closest('fieldset, [role=radiogroup], [class*=radio], .form-group, div')
                        || el.parentElement;
                    const labels = parent.querySelectorAll('label');
                    for (const label of labels) {{
                        if (label.innerText.trim().toLowerCase() === targetValue.toLowerCase()) {{
                            const input = label.querySelector('input[type=radio]');
                            if (input) {{ input.click(); return true; }}
                            label.click(); return true;
                        }}
                    }}
                    const inputs = parent.querySelectorAll('input[type=radio]');
                    for (const input of inputs) {{
                        const lbl = parent.querySelector('label[for=\"' + input.id + '\"]');
                        if ((lbl && lbl.innerText.trim().toLowerCase() === targetValue.toLowerCase())) {{
                            input.click(); return true;
                        }}
                    }}
                    return false;
                }}
                """
            )
            if clicked:
                return True
        except Exception:
            pass

        await el.click()
        return True

    async def _verify_field_value(self, page, field: FieldEvidence, expected: Any) -> bool:
        el = await self._locator_for_field(page, field)
        if el is None or not await el.count():
            return False
        try:
            if field.tag_name == "select":
                actual = await el.evaluate("el => el.options[el.selectedIndex]?.text || el.value || ''")
            elif field.input_type == "checkbox":
                return True
            elif field.input_type == "radio":
                return True
            elif field.input_type == "file":
                return True
            else:
                actual = await el.input_value()
            return str(expected).strip().lower() in str(actual).strip().lower() or str(actual).strip().lower() in str(expected).strip().lower()
        except Exception:
            return False

    async def _try_cv_upload(self, page, cv_path: Optional[str]) -> bool:
        if not cv_path or not Path(cv_path).exists():
            return False
        inputs = page.locator("input[type='file']")
        count = await inputs.count()
        for idx in range(count):
            try:
                el = inputs.nth(idx)
                await el.set_input_files(cv_path)
                await self._dispatch_input_events(el)
                return True
            except Exception:
                continue
        return False

    async def _dispatch_input_events(self, el) -> None:
        try:
            await el.evaluate(
                "el => { el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }"
            )
        except Exception:
            pass

    async def _click_apply(self, page) -> None:
        patterns = (
            r"apply\s*now",
            r"apply\s*here",
            r"apply\s*for\s*this\s*role",
            r"apply\s*for\s*this\s*job",
            r"apply\s*to\s*this",
            r"start\s*application",
            r"quick\s*apply",
            r"apply\s*$",
        )
        for pattern in patterns:
            try:
                apply = page.locator("a, button, [role='button']").filter(
                    has_text=re.compile(pattern, re.IGNORECASE)
                ).first
                if await apply.count() and await apply.is_visible():
                    await apply.click()
                    await page.wait_for_timeout(3000)
                    return
            except Exception:
                continue

    async def _find_continue(self, page):
        buttons = page.locator(
            "button, a[role='button'], input[type='submit'], input[type='button'], [class*='btn'], [class*='button']"
        )
        count = await buttons.count()
        for idx in range(count):
            button = buttons.nth(idx)
            try:
                text = (await button.inner_text()).strip().lower()
            except Exception:
                text = (await button.get_attribute("value") or "").strip().lower()
            if not text:
                continue
            if any(label == text or label in text for label in FINAL_SUBMIT_LABELS):
                continue
            if any(label == text or label in text for label in CONTINUE_LABELS):
                try:
                    if await button.is_visible():
                        return button
                except Exception:
                    continue
        return None

    async def _final_submit_visible(self, page) -> bool:
        buttons = page.locator("button, input[type='submit'], [role='button']")
        count = await buttons.count()
        for idx in range(count):
            button = buttons.nth(idx)
            try:
                text = (await button.inner_text()).strip().lower()
            except Exception:
                text = (await button.get_attribute("value") or "").strip().lower()
            if text and any(label == text or label in text for label in FINAL_SUBMIT_LABELS):
                try:
                    if await button.is_visible():
                        return True
                except Exception:
                    pass
        return False

    async def _handle_auth_gate(self, page) -> None:
        if not await self._is_auth_gate(page):
            return
        slug = safe_slug(page.url)
        await send_telegram(
            f"Auth or verification required for {slug}\n"
            f"Complete it in the browser, then send: ready {slug}"
        )
        print(f"\nAuth/verification gate detected. Send 'ready {slug}' or type 'ready'.")

        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = load_chat_id()
        last_update_id = 0
        loop = asyncio.get_event_loop()
        start = loop.time()

        while loop.time() - start < 300:
            if token and chat_id:
                for msg_text, upd_id in poll_telegram(token, chat_id, last_update_id):
                    last_update_id = max(last_update_id, upd_id)
                    parts = msg_text.strip().lower().split(None, 1)
                    if parts and parts[0] in ("ready", "done") and (len(parts) == 1 or parts[1] == slug):
                        await page.wait_for_timeout(1500)
                        return
            if not await self._is_auth_gate(page):
                await page.wait_for_timeout(1500)
                return
            await page.wait_for_timeout(1000)

    async def _is_auth_gate(self, page) -> bool:
        try:
            text = (await page.locator("body").inner_text(timeout=3000)).lower()
            if any(token in text for token in ("verification code", "pin code", "one-time", "otp", "sign in", "login")):
                fields = page.locator("input, select, textarea")
                return await fields.count() <= 6
        except Exception:
            pass
        return False

    async def _final_review_checkpoint(
        self,
        page,
        slug: str,
        filled: List[Dict[str, Any]],
    ) -> None:
        await send_telegram(
            f"Form filled for review: {page.url}\n\n"
            f"Review/edit in the browser. Then send:\n"
            f"learn {slug} — capture reviewed values\n"
            f"done {slug} — finish without learning\n"
            "The agent will not submit."
        )
        print(f"\nBrowser open for review — job slug: {slug}")
        print(f"Send/type 'learn {slug}' to capture reviewed values, or 'done {slug}' to finish.")

        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = load_chat_id()
        last_update_id = 0
        loop = asyncio.get_event_loop()
        start = loop.time()

        while loop.time() - start < 1800:
            command = await self._next_review_command(token, chat_id, last_update_id, slug)
            if command:
                cmd, last_update_id = command
                if cmd == "learn":
                    live_fields = await self._extract_fields(page)
                    contexts = {item.get("field_id"): item.get("field_context") for item in filled}
                    current = await read_current_values(page, live_fields, contexts)
                    save_learned_answers(current, page.url)
                    message = f"Captured {len(current)} reviewed values for {slug}. You can submit manually now."
                    if token and chat_id:
                        reply_telegram(token, chat_id, message)
                    print(message)
                return
            await page.wait_for_timeout(1000)

    async def _next_review_command(
        self,
        token: str,
        chat_id: Optional[int],
        last_update_id: int,
        slug: str,
    ) -> Optional[tuple[str, int]]:
        if token and chat_id:
            try:
                for msg_text, upd_id in poll_telegram(token, chat_id, last_update_id):
                    last_update_id = max(last_update_id, upd_id)
                    parsed = self._parse_command(msg_text, slug)
                    if parsed:
                        return parsed, last_update_id
            except Exception:
                pass

        try:
            loop = asyncio.get_event_loop()
            line = await asyncio.wait_for(loop.run_in_executor(None, input, "> "), timeout=0.2)
            parsed = self._parse_command(line, slug)
            if parsed:
                return parsed, last_update_id
        except (asyncio.TimeoutError, EOFError, KeyboardInterrupt):
            pass
        return None

    def _parse_command(self, text: str, slug: str) -> Optional[str]:
        parts = text.strip().lower().split(None, 1)
        if not parts:
            return None
        cmd = parts[0]
        target = parts[1] if len(parts) > 1 else ""
        if cmd in ("learn", "save", "capture") and (not target or target == slug):
            return "learn"
        if cmd in ("done", "skip", "finish") and (not target or target == slug):
            return "done"
        return None

    def _field_signature(self, url: str, fields: List[FieldEvidence]) -> str:
        ids = "|".join(f"{field.field_idx}:{field.field_id}:{field.label}" for field in fields)
        return f"{url}|{ids}"

    def _resolve_path(self, maybe_path: Optional[str]) -> Optional[str]:
        if not maybe_path:
            return None
        path = Path(maybe_path)
        if not path.is_absolute():
            path = ROOT / path
        return str(path)

