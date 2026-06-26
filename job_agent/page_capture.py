from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

from .config import SCREENSHOT_DIR, safe_slug
from .models import PageState

logger = logging.getLogger(__name__)

# Roles that are interactive and can be clicked/filled/selected
INTERACTIVE_ROLES = {
    'link', 'button', 'textbox', 'searchbox', 'combobox', 'checkbox',
    'radio', 'switch', 'slider', 'spinbutton', 'tab', 'menuitem',
    'option', 'textarea',
}

# Attributes mentioned in aria_snapshot
ATTRIBUTE_PATTERN = re.compile(r'\[(\w+)(?:=([^\]]+))?\]')


async def dismiss_popups(page) -> None:
    """Best-effort dismissal of cookie banners and non-essential overlays.

    Mechanical only — no decision-making. Safe to call repeatedly.
    """
    _popup_texts = [
        "reject all", "decline", "necessary only", "continue without accepting",
        "accept all", "got it", "agree", "ok",
    ]
    for text in _popup_texts:
        try:
            btn = page.locator(
                "button, a[role='button'], [role='button']"
            ).filter(has_text=text).first
            if await btn.is_visible(timeout=500):
                await btn.click(timeout=2000)
                await page.wait_for_timeout(300)
        except Exception:
            pass

    try:
        await page.evaluate("""
            () => {
              for (const sel of ['.modal-backdrop', '.overlay', '[class*="backdrop"]', '[class*="Overlay"]']) {
                for (const el of document.querySelectorAll(sel)) {
                  const r = el.getBoundingClientRect();
                  const style = getComputedStyle(el);
                  if (r.width > innerWidth * 0.7 && Number(style.zIndex || 0) >= 10) {
                    if (!el.querySelector('button, input, textarea, select')) {
                      el.style.pointerEvents = 'none';
                    }
                  }
                }
              }
            }
        """)
    except Exception:
        pass


def parse_snapshot(snapshot: str) -> List[Dict[str, Any]]:
    """Parse aria_snapshot output into a list of interactive elements.

    Each element gets a stable index within the page. The LLM references
    elements by their index in this list.

    Tracks group/section context so radio buttons and other grouped
    elements get their question/section text.

    Snapshot format:
        - group "Question text":
          - radio "Yes"
          - radio "No"
        - textbox "Full name" [required]
        - button "Submit"
    """
    elements: List[Dict[str, Any]] = []
    # Group context stack: index = depth level, value = group/section name
    group_stack: List[str] = []
    prev_depth = -1

    for line in snapshot.split('\n'):
        stripped = line.lstrip(' ')
        if not stripped.startswith('-'):
            continue

        # Calculate depth (each level = 2 spaces)
        leading_spaces = len(line) - len(line.lstrip(' '))
        depth = leading_spaces // 2

        # Trim group stack when going back up the tree
        if depth <= prev_depth and group_stack:
            group_stack = group_stack[:depth]
        prev_depth = depth

        if stripped.startswith('- /'):
            continue  # skip URL markers

        # Track group/section/heading context
        group_match = re.match(r'-\s+group\s+"([^"]+)"\s*:', stripped)
        if group_match:
            name = group_match.group(1)
            if depth < len(group_stack):
                group_stack[depth] = name
            else:
                group_stack.append(name)
            continue

        section_match = re.match(r'-\s+(?:section|heading)\s+"([^"]+)"', stripped)
        if section_match:
            name = section_match.group(1)
            if depth < len(group_stack):
                group_stack[depth] = name
            else:
                group_stack.append(name)
            continue

        # Parse: - role "name" [attributes]
        m = re.match(
            r'-\s+(\w+)(?:\s+"([^"]*)")?(?:\s+(\[[^\]]+\]))*\s*:?\s*$',
            stripped
        )
        if not m:
            # Try: - searchbox "Search" [required]
            m2 = re.match(r'-\s+(\w+)\s+"([^"]*)"(.*)', stripped)
            if m2:
                role = m2.group(1)
                name = m2.group(2)
                attrs_str = m2.group(3).strip()
            else:
                # Try: - role [required]  (no name)
                m3 = re.match(r'-\s+(\w+)\s+(.*)', stripped)
                if m3 and m3.group(1) in INTERACTIVE_ROLES:
                    role = m3.group(1)
                    name = ""
                    attrs_str = m3.group(2).strip()
                else:
                    continue
        else:
            role = m.group(1)
            name = m.group(2) or ""
            attrs_str = m.group(3) or ""

        if role not in INTERACTIVE_ROLES:
            continue

        # Parse attributes
        attrs: Dict[str, Any] = {}
        for attr_match in ATTRIBUTE_PATTERN.finditer(attrs_str):
            key = attr_match.group(1)
            value = attr_match.group(2)
            attrs[key] = value if value is not None else True

        # Context = nearest enclosing group/section name
        context = group_stack[-1] if group_stack else ""

        elements.append({
            'idx': len(elements),
            'role': role,
            'name': name,
            'required': 'required' in attrs,
            'disabled': 'disabled' in attrs,
            'readonly': 'readonly' in attrs,
            'checked': 'checked' in attrs,
            'selected': 'selected' in attrs,
            'expanded': 'expanded' in attrs,
            'context': context,
        })

    # Post-process: compute nth_within_group for disambiguation
    # When multiple elements share the same role+name (e.g., 7 "Yes" radios),
    # the LLM needs to know which one. We add nth_within_group:
    # 0 = first "Yes" radio, 1 = second "Yes" radio, etc.
    from collections import Counter
    role_name_counts: Counter = Counter()
    for el in elements:
        key = (el['role'], el['name'])
        role_name_counts[key] += 1

    # Only add nth when there are duplicates
    role_name_seen: Dict[tuple, int] = {}
    for el in elements:
        key = (el['role'], el['name'])
        if role_name_counts[key] > 1:
            nth = role_name_seen.get(key, 0)
            role_name_seen[key] = nth + 1
            el['nth'] = nth
        else:
            el['nth'] = 0

    return elements


async def capture_page_state(page) -> tuple[PageState, List[Dict[str, Any]]]:
    """Capture current page as a11y tree + screenshot + parsed elements.

    Returns (PageState, parsed_elements) where parsed_elements is the
    indexed list the LLM will reference.
    """
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    await page.wait_for_timeout(1500)

    await dismiss_popups(page)

    # Capture accessibility tree
    snapshot = await page.locator("body").aria_snapshot(timeout=10000) or ""

    # Parse into indexed elements
    elements = parse_snapshot(snapshot)

    # Capture screenshot
    slug = safe_slug(page.url)
    screenshot_path = str(SCREENSHOT_DIR / f"{slug}-{asyncio.get_event_loop().time():.0f}.png")
    await page.screenshot(path=screenshot_path, full_page=True)

    state = PageState(
        url=page.url,
        title=await page.title(),
        aria_snapshot=snapshot,
        screenshot_path=screenshot_path,
    )

    return state, elements


def format_elements_for_llm(elements: List[Dict[str, Any]]) -> str:
    """Format the parsed elements as a compact list for the LLM prompt.

    Includes group/section context when available, prefixed with ── to
    show which section each element belongs to.

    For duplicate role+name elements (e.g., multiple "Yes" radios),
    appends #N to disambiguate (the Nth occurrence of that role+name).
    """
    lines = []
    last_context = ""
    for el in elements:
        # Show context header when it changes
        ctx = el.get("context", "")
        if ctx and ctx != last_context:
            lines.append(f"\n  -- {ctx} --")
            last_context = ctx

        parts = [f"[{el['idx']}] {el['role']}"]
        name = el.get('name', '')
        if name:
            parts.append(f'"{name}"')
        # Add disambiguation index for duplicates
        if el.get('nth', 0) > 0:
            parts.append(f"#{el['nth']}")
        flags = []
        if el.get('required'):
            flags.append('required')
        if el.get('disabled'):
            flags.append('disabled')
        if el.get('checked'):
            flags.append('checked')
        if el.get('selected'):
            flags.append('selected')
        if flags:
            parts.append(f"[{','.join(flags)}]")
        lines.append("    " + " ".join(parts))
    return '\n'.join(lines)


async def setup_browser_page(url: str, *, headed: bool = True):
    """Launch browser, open URL, return (playwright, browser, context, page)."""
    from playwright.async_api import async_playwright

    p = await async_playwright().start()
    browser = await p.chromium.launch(headless=not headed)
    ctx = await browser.new_context(locale="en-US")
    page = await ctx.new_page()
    page.set_default_timeout(15000)
    page.on("dialog", lambda d: d.dismiss())

    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2000)
    await dismiss_popups(page)

    return p, browser, ctx, page
