from __future__ import annotations

import re
from typing import Iterable


COOKIE_ALLOW_TEXTS = (
    "accept all cookies",
    "accept all",
    "accept cookies",
    "allow all cookies",
    "allow cookies",
    "i accept",
    "i agree",
    "agree",
    "agree to all",
    "agree to necessary",
    "agree to required",
    "got it",
    "ok",
)

COOKIE_REJECT_TEXTS = (
    "reject all",
    "decline",
    "reject",
    "necessary only",
    "only necessary",
    "continue without accepting",
)

LOCATION_TEXTS = (
    "not now",
    "no thanks",
    "maybe later",
    "skip",
    "cancel",
    "deny",
)

SESSION_TEXTS = (
    "continue working",
    "discard",
)

CLOSE_SELECTORS = (
    "button[aria-label*='close' i]",
    "button[title*='close' i]",
    "[role='button'][aria-label*='close' i]",
    ".modal button.close",
    ".modal [class*='close']",
    "[class*='modal'] button[aria-label*='close' i]",
    "[class*='popup'] button[aria-label*='close' i]",
)


async def dismiss_common_popups(page) -> None:
    """Best-effort dismissal of non-essential overlays.

    This intentionally avoids clicking anything that looks like final submit.
    It is safe to call repeatedly after navigation.
    """
    await _dismiss_native_dialogs(page)
    await _click_text_buttons(page, COOKIE_REJECT_TEXTS)
    await _click_text_buttons(page, COOKIE_ALLOW_TEXTS)
    await _click_text_buttons(page, LOCATION_TEXTS)
    await _click_text_buttons(page, SESSION_TEXTS)
    await _click_close_buttons(page)
    await _remove_dead_overlays(page)


async def _dismiss_native_dialogs(page) -> None:
    page.on("dialog", lambda dialog: dialog.dismiss())


async def _click_text_buttons(page, texts: Iterable[str]) -> None:
    for text in texts:
        pattern = re.compile(rf"^\s*{re.escape(text)}\s*$", re.IGNORECASE)
        try:
            candidates = page.locator(
                "button, a, [role='button'], input[type='button'], input[type='submit']"
            ).filter(has_text=pattern)
            count = min(await candidates.count(), 5)
            for idx in range(count):
                candidate = candidates.nth(idx)
                if await candidate.is_visible():
                    await candidate.click(timeout=2000)
                    await page.wait_for_timeout(400)
                    return
        except Exception:
            continue


async def _click_close_buttons(page) -> None:
    for selector in CLOSE_SELECTORS:
        try:
            candidates = page.locator(selector)
            count = min(await candidates.count(), 5)
            for idx in range(count):
                candidate = candidates.nth(idx)
                if await candidate.is_visible():
                    await candidate.click(timeout=2000)
                    await page.wait_for_timeout(300)
                    return
        except Exception:
            continue


async def _remove_dead_overlays(page) -> None:
    try:
        await page.evaluate(
            """
            () => {
              const selectors = [
                '.modal-backdrop',
                '.overlay',
                '.cookie-overlay',
                '[class*="backdrop"]',
                '[class*="Overlay"]'
              ];
              for (const selector of selectors) {
                for (const el of document.querySelectorAll(selector)) {
                  const style = window.getComputedStyle(el);
                  const rect = el.getBoundingClientRect();
                  const looksBlocking = rect.width > innerWidth * 0.7 &&
                    rect.height > innerHeight * 0.7 &&
                    Number(style.zIndex || 0) >= 10;
                  if (looksBlocking && !el.querySelector('button, input, textarea, select')) {
                    el.style.pointerEvents = 'none';
                  }
                }
              }
            }
            """
        )
    except Exception:
        pass

