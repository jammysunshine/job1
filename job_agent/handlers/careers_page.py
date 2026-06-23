from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from ..models import FieldEvidence
from ..storage import SCREENSHOT_DIR
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

            live_fields = await self._extract_fields(page)
            live_by_idx = {f.field_idx: f for f in live_fields}

            filled = []
            for entry in field_mapping.get("fields", []):
                field_idx = entry.get("field_idx")
                field_id = entry.get("field_id")
                value = entry.get("value")
                if field_idx is None or not field_id or value is None:
                    continue

                match = live_by_idx.get(field_idx)
                if match is None:
                    errors.append(f"Field not found: idx={field_id} id={field_id}")
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
                    filled.append({"field_id": field_id, "value": value})
                except Exception as exc:
                    errors.append(f"Failed to fill {field_id}: {exc}")

            if cv_path:
                file_input = page.locator('input[type="file"]').first
                if await file_input.count():
                    try:
                        await file_input.set_input_files(cv_path)
                        filled.append({"field_id": "file_upload", "value": cv_path})
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

        return result

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
