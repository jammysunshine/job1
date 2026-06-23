from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .models import FieldEvidence, PageEvidence
from .storage import EVIDENCE_DIR, SCREENSHOT_DIR, write_json


def cheap_ats_signals(url: str, title: str = "", scripts_text: str = "") -> Dict[str, Any]:
    haystack = " ".join([url, title, scripts_text]).lower()
    candidates = {
        "oracle_taleo": ["taleo", "oraclecloud", "oracle recruiting", "fa-ext"],
        "workday": ["myworkdayjobs", "workday"],
        "greenhouse": ["greenhouse.io", "greenhouse"],
        "lever": ["lever.co", "lever"],
        "ashby": ["ashbyhq", "ashby"],
        "successfactors": ["successfactors", "sapsf"],
        "smartrecruiters": ["smartrecruiters"],
    }
    matches = {
        ats: [token for token in tokens if token in haystack]
        for ats, tokens in candidates.items()
    }
    matches = {ats: tokens for ats, tokens in matches.items() if tokens}
    return {
        "host": urlparse(url).netloc,
        "candidate_matches": matches,
        "source": "cheap_signals_only_llm_must_confirm",
    }


async def capture_page_evidence(url: str, *, headed: bool = True) -> PageEvidence:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run: pip install -r requirements.txt && "
            "python -m playwright install chromium"
        ) from exc

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not headed)
        page = await browser.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1500)

        title = await page.title()
        final_url = page.url
        visible_text = await _visible_text_sample(page)
        scripts_text = await _scripts_text_sample(page)
        fields = await _extract_fields(page)
        buttons = await _extract_buttons(page)

        slug = _safe_slug(final_url)
        screenshot_path = SCREENSHOT_DIR / f"{slug}.png"
        await page.screenshot(path=str(screenshot_path), full_page=True)
        await browser.close()

    evidence = PageEvidence(
        job_url=url,
        final_url=final_url,
        title=title,
        visible_text_sample=visible_text,
        ats_signals=cheap_ats_signals(final_url, title, scripts_text),
        fields=fields,
        buttons=buttons,
        screenshot_path=str(screenshot_path),
    )
    evidence_path = EVIDENCE_DIR / f"{_safe_slug(final_url)}.json"
    write_json(evidence_path, evidence)
    return evidence


async def _visible_text_sample(page: Any) -> str:
    text = await page.locator("body").inner_text(timeout=5000)
    return _compact(text)[:8000]


async def _scripts_text_sample(page: Any) -> str:
    return await page.evaluate(
        """
        () => Array.from(document.scripts)
          .map((s) => [s.src, s.textContent || ""].join(" "))
          .join(" ")
          .slice(0, 12000)
        """
    )


async def _extract_buttons(page: Any) -> List[str]:
    labels = await page.locator("button, input[type=button], input[type=submit], a").evaluate_all(
        """
        els => els
          .filter(el => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
          })
          .map(el => (el.innerText || el.value || el.getAttribute('aria-label') || '').trim())
          .filter(Boolean)
          .slice(0, 80)
        """
    )
    return [_compact(label) for label in labels]


async def _extract_fields(page: Any) -> List[FieldEvidence]:
    raw_fields = await page.locator("input, textarea, select").evaluate_all(
        """
        els => els.map((el, index) => {
          const id = el.id || el.name || `field_${index}`;
          const labelEl = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
          const parentText = (el.closest('label, div, section, fieldset')?.innerText || '').trim();
          const options = el.tagName.toLowerCase() === 'select'
            ? Array.from(el.options).map(o => o.innerText.trim()).filter(Boolean)
            : [];
          const rect = el.getBoundingClientRect();
          return {
            field_id: id,
            tag_name: el.tagName.toLowerCase(),
            input_type: el.getAttribute('type'),
            label: labelEl ? labelEl.innerText.trim() : null,
            placeholder: el.getAttribute('placeholder'),
            aria_label: el.getAttribute('aria-label'),
            required: Boolean(el.required || el.getAttribute('aria-required') === 'true'),
            visible: rect.width > 0 && rect.height > 0,
            options,
            nearby_text: parentText.slice(0, 500)
          };
        }).slice(0, 120)
        """
    )
    return [
        FieldEvidence(
            field_idx=idx,
            field_id=str(item.get("field_id") or f"field_{idx}"),
            tag_name=str(item.get("tag_name") or ""),
            input_type=item.get("input_type"),
            label=item.get("label"),
            placeholder=item.get("placeholder"),
            aria_label=item.get("aria_label"),
            required=bool(item.get("required")),
            visible=bool(item.get("visible")),
            options=list(item.get("options") or []),
            nearby_text=item.get("nearby_text"),
        )
        for idx, item in enumerate(raw_fields)
    ]


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return slug[:120] or "job"


def _compact(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", value or "").strip()

