from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from .config import (
    ensure_dirs,
    load_cover_letter_template,
    load_cv_variants,
    load_learned_answers,
    load_profile,
    safe_slug,
)
from .llm import call_llm_json
from .models import JobRecord, Plan, PlanStep, PageState
from .page_capture import (
    capture_page_state,
    format_elements_for_llm,
    setup_browser_page,
)
from .telegram import send_telegram, wait_for_ready_signal

logger = logging.getLogger(__name__)

MAX_STEPS = 50
MAX_PAGES = 10


def _build_candidate_summary() -> Dict[str, Any]:
    """Build a compact dict of candidate data for the LLM prompt."""
    profile = load_profile()
    personal = profile.get("personal", {})
    prefs = profile.get("preferences", {})

    return {
        "full_name": personal.get("full_name", ""),
        "first_name": personal.get("first_name", ""),
        "last_name": personal.get("last_name", ""),
        "email": personal.get("email", ""),
        "phone": personal.get("phone", ""),
        "location": personal.get("location", ""),
        "nationality": personal.get("nationality", ""),
        "date_of_birth": personal.get("date_of_birth", ""),
        "linkedin": personal.get("linkedin", ""),
        "visa_status": personal.get("visa_status", ""),
        "gender": profile.get("personal_details", {}).get("gender", ""),
        "marital_status": profile.get("personal_details", {}).get("marital_status", ""),
        "ethnicity": profile.get("personal_details", {}).get("ethnicity", ""),
        "religion": profile.get("personal_details", {}).get("religion", ""),
        "disability": profile.get("personal_details", {}).get("disability", ""),
        "preferred_pronouns": profile.get("personal_details", {}).get("preferred_pronouns", ""),
        "sexual_orientation": profile.get("personal_details", {}).get("sexual_orientation", ""),
        "gender_identity": profile.get("personal_details", {}).get("gender_identity", ""),
        "willing_to_relocate": prefs.get("willing_to_relocate", True),
        "notice_period_days": prefs.get("notice_period_days", 0),
        "current_salary": prefs.get("current_salary", {}),
        "expected_salary": prefs.get("expected_salary", {}),
        "requires_manual_confirmation_for": prefs.get("requires_manual_confirmation_for", []),
    }


def _build_system_prompt() -> str:
    return """You are a job application assistant. You see a web page's interactive elements listed by index and decide what actions to take to fill out the application form.

Your goal: fill ALL form fields accurately using the candidate's profile data. Stop before the final submit button for the user to review.

CRITICAL RULES:
1. NEVER click the final submit/send/finish/apply button. If you see one, set stop_for_review=true and do NOT include it in steps.
2. If you are unsure about any field, set ask_user to your question — do NOT guess.
3. Return ONLY valid JSON matching the output schema.
4. Reference elements by their index number (e.g., idx=5 for the 6th element).
5. For select/combobox fields, use action="select" with the exact visible option text as value.
6. For file uploads, use action="upload" with the file path as value.
7. For checkboxes, use action="checkbox" with value "true" (check) or "false" (uncheck).
8. For radio buttons, use action="click" on the specific option index you want selected.
9. For text fields, use action="fill" with the exact text value.
10. For buttons/links, use action="click".
11. For legal/conflict-of-interest questions, default to "No" unless profile clearly indicates "Yes".
12. For D&I fields, use profile values if available; otherwise ask user.

Output JSON schema:
{"steps":[{"action":"click|fill|select|upload|checkbox","idx":5,"value":"optional","reason":"why"}],"cv_variant":"name of best CV variant or null","notes":"brief description","ask_user":"question or null","stop_for_review":false}"""


def _build_user_context() -> str:
    """Build candidate + CV context string for the user prompt."""
    cv_variants = load_cv_variants()
    variants_json = _to_compact_json([{
        "name": v["name"],
        "tags": v.get("tags", []),
    } for v in cv_variants])

    candidate_json = _to_compact_json(_build_candidate_summary())

    return f"""Candidate info:
{candidate_json}

CV variants (choose the best match for this job):
{variants_json}"""


def _to_compact_json(obj: Any) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _build_user_prompt(
    page_state: PageState,
    elements: List[Dict[str, Any]],
    history: List[Dict[str, Any]],
    job_description: Optional[str] = None,
    job_url: Optional[str] = None,
) -> str:
    parts = [_build_user_context()]
    parts.append(f"\nCurrent page URL: {page_state.url}")
    parts.append(f"Page title: {page_state.title}")

    if job_url and job_url != page_state.url:
        parts.append(f"Original job URL: {job_url}")

    if job_description:
        parts.append(f"\nJob description:\n{job_description[:4000]}")

    if history:
        parts.append(f"\nRecent actions ({len(history)} total):")
        for i, h in enumerate(history[-5:], 1):
            parts.append(f"  {i}. {h['action']} idx={h.get('idx', '?')} -> {h.get('result', 'ok')}")

    parts.append(f"\nInteractive elements on this page (reference by idx):")
    parts.append(format_elements_for_llm(elements))

    return "\n".join(parts)


async def _execute_step(page, step: PlanStep, elements: List[Dict[str, Any]], cv_path: Optional[str]) -> Tuple[bool, str]:
    """Execute a single plan step using element index. Returns (success, detail)."""
    try:
        # Find the element by index
        if step.idx is None or step.idx >= len(elements):
            return False, f"Element index {step.idx} out of range (have {len(elements)} elements)"

        el = elements[step.idx]
        role = el['role']
        name = el['name']

        # Build locator: try role+name+nth for disambiguation
        locator = None
        nth = el.get('nth', 0)

        if name:
            try:
                locator = page.get_by_role(role, name=name, exact=True)
                if not await locator.count():
                    locator = page.get_by_role(role, name=name)
            except Exception:
                pass

        if locator is None or not await locator.count():
            try:
                locator = page.get_by_role(role)
            except Exception:
                return False, f"Cannot locate: role={role} name={name}"

        if locator is None:
            return False, f"Cannot locate: role={role} name={name}"

        # Disambiguate if multiple matches
        count = await locator.count()
        if count > 1 and nth > 0:
            locator = locator.nth(nth)
        elif count >= 1:
            locator = locator.first

        if not await locator.count():
            return False, f"No element found: role={role} name={name}"

        if step.action == "click":
            await locator.click(timeout=5000)
            await page.wait_for_timeout(1500)
            return True, "clicked"

        elif step.action == "fill":
            await locator.click(timeout=3000)
            # Clear existing value
            await locator.fill("")
            await locator.type(str(step.value or ""), delay=50)
            await page.wait_for_timeout(500)
            return True, f"filled '{step.value}'"

        elif step.action == "select":
            # Try label first, then value, then click+option
            try:
                await locator.select_option(label=str(step.value), timeout=3000)
            except Exception:
                try:
                    await locator.select_option(value=str(step.value), timeout=3000)
                except Exception:
                    # Click the dropdown, then click the option
                    await locator.click(timeout=3000)
                    await page.wait_for_timeout(500)
                    option = page.locator('[role="option"]').filter(has_text=str(step.value)).first
                    await option.click(timeout=3000)
            await page.wait_for_timeout(500)
            return True, f"selected '{step.value}'"

        elif step.action == "upload":
            file_path = step.value or cv_path
            if file_path:
                await locator.set_input_files(file_path)
                await page.wait_for_timeout(500)
                return True, f"uploaded {file_path}"
            return False, "no file path"

        elif step.action == "checkbox":
            should_check = str(step.value).lower() in ("true", "1", "yes", "on")
            is_checked = await locator.is_checked()
            if should_check != is_checked:
                await locator.click(timeout=3000)
            await page.wait_for_timeout(300)
            return True, f"checkbox {'checked' if should_check else 'unchecked'}"

        else:
            return False, f"unknown action: {step.action}"

    except Exception as exc:
        return False, f"{step.action} failed: {exc}"


async def _verify_step(page, step: PlanStep, elements: List[Dict[str, Any]]) -> bool:
    """Verify a fill/select step had the intended effect."""
    try:
        if step.idx is None or step.idx >= len(elements):
            return True

        el = elements[step.idx]
        locator = page.get_by_role(el['role'], name=el['name'] or None)
        if not await locator.count():
            return True  # can't verify

        if step.action == "fill":
            actual = await locator.input_value()
            expected = (step.value or "").lower()
            return expected in actual.lower() or actual.lower() in expected

        elif step.action == "select":
            actual = await locator.input_value()
            return (step.value or "").lower() in actual.lower()

        elif step.action == "checkbox":
            is_checked = await locator.is_checked()
            should_check = str(step.value).lower() in ("true", "1", "yes", "on")
            return is_checked == should_check

        return True
    except Exception:
        return True


async def _is_auth_gate(page) -> bool:
    """Detect if we're on an auth/CAPTCHA page."""
    try:
        text = (await page.locator("body").inner_text(timeout=2000) or "").lower()
        gate_signals = [
            "captcha", "recaptcha", "hcaptcha", "verification code",
            "pin code", "one-time password", "otp", "sign in", "login",
            "verify your identity", "robot",
        ]
        if any(signal in text for signal in gate_signals):
            count = await page.locator("input, select, textarea").count()
            if count <= 3:
                return True
    except Exception:
        pass
    return False


def _is_final_page(elements: List[Dict[str, Any]], *, steps_filled: int = 0) -> bool:
    """Detect if the current page has a final submit button.

    Only returns True if we have already filled at least one field — otherwise
    every "Apply" button on a job listing page would trigger a premature stop.
    """
    if steps_filled == 0:
        return False

    for el in elements:
        if el['role'] == 'button' and el['name']:
            name_lower = el['name'].lower().strip()
            # Only match explicit final-action buttons (not "Apply" which usually
            # means "apply for this job" on a listing page).
            if any(label in name_lower for label in [
                "submit", "send", "finish", "submit application",
                "submit application", "send application",
            ]):
                return True
    return False


async def run_agent(
    job_url: str,
    *,
    headed: bool = True,
    dry_run: bool = False,
    max_steps: int = MAX_STEPS,
) -> JobRecord:
    """Run the agentic loop for a job URL."""
    ensure_dirs()
    record = JobRecord(job_url=job_url)

    try:
        await send_telegram(f"Starting application for:\n{job_url}")
    except Exception:
        pass  # Telegram is best-effort

    p, browser, ctx, page = await setup_browser_page(job_url, headed=headed)

    history: List[Dict[str, Any]] = []
    cv_path = None
    job_description = None

    try:
        job_description = await _scrape_job_description(job_url)

        for page_num in range(MAX_PAGES):
            # Check for auth/CAPTCHA gate
            if await _is_auth_gate(page):
                slug = safe_slug(page.url)
                try:
                    await send_telegram(
                        f"Auth/CAPTCHA required for {slug}.\n"
                        f"Solve it in the browser, then send: ready {slug}"
                    )
                except Exception:
                    pass
                ready = await wait_for_ready_signal(slug, timeout=300)
                if not ready:
                    record.status = "auth_timeout"
                    break
                await page.wait_for_timeout(2000)

            # 1. OBSERVE
            page_state, elements = await capture_page_state(page)

            # Check if this looks like the final page (only after filling fields)
            if _is_final_page(elements, steps_filled=record.filled_count):
                try:
                    await send_telegram(
                        f"Final page detected ({len(history)} steps so far).\n"
                        f"Stopping for your review. Browser is open."
                    )
                except Exception:
                    pass
                record.status = "filled_awaiting_review"
                break

            # 2. THINK
            system_prompt = _build_system_prompt()
            user_prompt = _build_user_prompt(
                page_state, elements, history, job_description, job_url
            )

            plan: Optional[Plan] = None
            for attempt in range(3):
                try:
                    response = call_llm_json(
                        system_prompt,
                        user_prompt,
                        image_path=page_state.screenshot_path if attempt == 0 else None,
                    )
                    plan = Plan.from_dict(response)
                    break
                except Exception as exc:
                    logger.warning("LLM call failed (attempt %d): %s", attempt + 1, exc)
                    if attempt == 2:
                        record.status = "llm_error"
                        record.error_count += 1
                        try:
                            await send_telegram(f"LLM error: {exc}")
                        except Exception:
                            pass
                        break
                    await asyncio.sleep(2)

            if plan is None:
                break

            # Handle CV variant selection
            if plan.cv_variant:
                cv_variants = load_cv_variants()
                for v in cv_variants:
                    if v["name"] == plan.cv_variant:
                        cv_path = v.get("file_path")
                        record.cv_variant = plan.cv_variant
                        break

            # Handle ask_user
            if plan.ask_user:
                try:
                    await send_telegram(f"Question: {plan.ask_user}\n\nReply via Telegram or terminal.")
                except Exception:
                    pass
                await page.wait_for_timeout(5000)

            # Handle stop_for_review
            if plan.stop_for_review:
                try:
                    await send_telegram(
                        f"Form filled ({len(history)} steps completed).\n"
                        f"Browser open for your review. I will NOT submit."
                    )
                except Exception:
                    pass
                record.status = "filled_awaiting_review"
                break

            if dry_run:
                print(f"\n[DRY RUN] Plan: {plan.notes}")
                for step in plan.steps:
                    el = elements[step.idx] if step.idx is not None and step.idx < len(elements) else {}
                    print(f"  {step.action} [{step.idx}] {el.get('role', '?')} \"{el.get('name', '')}\" = {step.value} ({step.reason})")
                break

            # 3. ACT
            for step in plan.steps:
                if len(history) >= max_steps:
                    try:
                        await send_telegram(f"Reached max steps ({max_steps}). Stopping for review.")
                    except Exception:
                        pass
                    record.status = "filled_awaiting_review"
                    break

                success, detail = await _execute_step(page, step, elements, cv_path)
                history.append({
                    "action": step.action,
                    "idx": step.idx,
                    "value": step.value,
                    "result": "ok" if success else f"failed: {detail}",
                })

                if success:
                    record.filled_count += 1
                    verified = await _verify_step(page, step, elements)
                    if not verified and step.action in ("fill", "select"):
                        logger.warning("Verification failed for idx=%s", step.idx)
                else:
                    record.error_count += 1
                    logger.warning("Step failed: %s [%s] - %s", step.action, step.idx, detail)

        else:
            record.status = "max_pages_reached"

    except Exception as exc:
        logger.error("Agent failed: %s", exc, exc_info=True)
        record.status = "error"
        record.error_count += 1
        try:
            await send_telegram(f"Pipeline error: {exc}")
        except Exception:
            pass

    finally:
        try:
            slug = safe_slug(job_url)
            from .config import SCREENSHOT_DIR
            screenshot_path = str(SCREENSHOT_DIR / f"{slug}-final.png")
            await page.screenshot(path=screenshot_path, full_page=True)
        except Exception:
            pass

        if headed:
            print(f"\nBrowser open for review. Press Ctrl+C to close.")
            try:
                await asyncio.Event().wait()
            except KeyboardInterrupt:
                pass

        try:
            await ctx.close()
            await browser.close()
            await p.stop()
        except Exception:
            pass

    from datetime import datetime, timezone
    record.updated_at = datetime.now(timezone.utc).isoformat()
    return record


async def _scrape_job_description(job_url: str) -> Optional[str]:
    """Scrape job description from URL (separate browser instance, headless)."""
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)
            text = await page.evaluate("() => document.body.innerText")
            await browser.close()
            return text[:6000] if text else None
    except Exception:
        return None
