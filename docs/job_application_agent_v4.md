# Job Application Fill Agent (V4 Spec)

> **Status**: Active development on `v4-rebuild` branch.
> See `docs/job_application_agent_v3.md` for the previous architecture (now superseded).

## The Job

A human-in-the-loop system that takes a job posting URL and mechanically
applies on your behalf. You review before anything is submitted. You
always click submit yourself.

Input: a job posting URL.
Output: a fully-filled application left open in the browser for your
review, plus an audit log entry.

---

## Architecture Overview

The system is a **single agentic loop**. There are no vendor-specific
handlers, no hardcoded button regexes, no per-field-type switch
statements, and no hardcoded label-matching rules anywhere in the code.

The loop:

```
1. Observe:  capture the current page state (screenshot + a11y tree)
2. Think:    LLM sees the page + user profile + history → returns a plan
3. Act:      execute the plan's steps deterministically (click, type, upload)
4. Verify:   re-observe the page, confirm each step succeeded
5. Repeat:   until the form is complete or the LLM is unsure → ask user
```

The LLM is the brain. Code is the hands. Code never decides *what* to
do; it only decides *how* to do what the LLM told it to.

---

## Core Principles

1. **LLM plans, code executes.** Every decision about what to click,
   what to type, which option to pick comes from the LLM. Code only
   handles the mechanical "how" (which Playwright API call to make).

2. **No hardcoded form knowledge.** Zero regex patterns for button
   labels. Zero hardcoded field names. Zero vendor detection for routing.
   The system works on forms it has never seen before.

3. **Observe after every action.** After each click or fill, the system
   re-captures the page state and confirms the action had the intended
   effect. If it didn't, the LLM re-plans.

4. **Graceful degradation.** If the LLM is unsure at any point, it
   asks the user via Telegram. The system never guesses when
   confidence is low.

5. **Single responsibility per component.** Each file does exactly one
   thing. No file is a grab-bag of unrelated utilities.

---

## The Agentic Loop (Detailed)

### Step 1 — Observe

Capture the current page state as an **accessibility tree** (not raw
HTML). Playwright's `aria_snapshot()` gives a structured, semantic
representation of every interactive element with its role, name, ref,
and state.

The snapshot looks like:

```
- navigation "Careers" [ref=e1]
  - link "Apply Now" [ref=e42]
- form "Application"
  - textbox "First Name" [ref=e10] [required]
  - textbox "Last Name" [ref=e11] [required]
  - combobox "Country" [ref=e20]
    - option "United Arab Emirates" [ref=e21]
    - option "India" [ref=e22]
  - button "Submit Application" [ref=e50]
```

This is far more reliable than CSS selectors or DOM traversal because:
- `ref` is stable within a page session
- Roles and names are semantic (a button named "Submit" is a button
  named "Submit" regardless of its HTML tag)
- The tree structure encodes parent-child relationships

**Also capture**: a full-page screenshot (for the LLM's visual
understanding and for the user's review later).

### Step 2 — Think

Send the following to the LLM:

- The accessibility tree (current page state)
- The screenshot (as multimodal input, if the model supports it)
- The user's profile (`profile.yaml`)
- The CV variant catalog (`cv_variants.yaml`)
- Previously learned answers (`learned_answers.json`)
- The job description (scraped from the posting URL)
- The action history (what's been done so far on this application)
- Any pending questions from the user

The LLM returns a **plan** — an ordered list of steps:

```json
{
  "steps": [
    {
      "action": "click",
      "ref": "e42",
      "reason": "This is the Apply Now link that starts the application form"
    },
    {
      "action": "fill",
      "ref": "e10",
      "value": "Mohit",
      "reason": "First Name field, value from profile.personal.first_name"
    },
    {
      "action": "select",
      "ref": "e20",
      "value": "United Arab Emirates",
      "reason": "Country dropdown, matching profile.personal.location"
    },
    {
      "action": "click",
      "ref": "e50",
      "reason": "Submit the completed application"
    }
  ],
  "cv_variant": "CIO_Leadership",
  "notes": "Single-step form, all fields visible on one page"
}
```

**The LLM decides everything:**
- Which button is the "Apply" button (no regex needed)
- Which field is the name field, email field, location field (no DOM
  heuristics needed)
- What value goes in each field (no hardcoded mapping rules)
- Which CV variant to use (no tag-matching code)
- Whether to click submit or stop for review (no hardcoded submit
  detection)

**The system prompt tells the LLM:**
- What the user's data is (from profile.yaml)
- That it must never click final submit without explicit user
  confirmation
- That it must stop before submit and wait for the user to review
- That CAPTCHA/auth gates require pausing and notifying the user
- That it should ask when unsure rather than guess

### Step 3 — Act

Execute each step deterministically using Playwright:

| action | Playwright call |
|--------|----------------|
| click  | `page.locator(ref=step.ref).click()` |
| fill   | `page.locator(ref=step.ref).fill(step.value)` |
| select | `page.locator(ref=step.ref).select_option(label=step.value)` |
| upload | `page.locator(ref=step.ref).set_input_files(path)` |
| checkbox | `page.locator(ref=step.ref).click()` (toggle) |

If a step fails (element not found, wrong ref, timeout), capture the
error and go back to Step 1 (re-observe) so the LLM can re-plan.

### Step 4 — Verify

After each action:
1. Re-capture the accessibility tree
2. Check if the expected change occurred:
   - For `fill`: does the textbox now contain the expected value?
   - For `click`: did the page navigate or did a new element appear?
   - For `select`: is the expected option now selected?
   - For `upload`: does the file input now have a file?
3. If verification fails, include the failure in the next Think cycle
   and let the LLM re-plan

### Step 5 — Repeat

Continue the loop until:
- The LLM's plan includes a "stop for review" signal → pause, notify
  user, wait for manual review + manual submit
- The LLM is unsure about any step → ask user via Telegram
- A CAPTCHA or auth gate appears → pause, notify, wait for user
  to solve it themselves, resume on signal

---

## Conditional Fields

Many forms show fields conditionally (e.g., selecting "Email my
application" reveals an email field). The agentic loop handles this
naturally:

1. LLM sees radio button "Email my application" → plans to click it
2. Code clicks it
3. Re-observe: the email textbox now appears in the tree
4. LLM sees the new field → plans to fill it
5. Code fills it

No special-case code needed. The observe-think-act cycle handles
dynamic pages by design.

---

## Multi-Step Forms

Many ATS systems split the application across 2-5 pages. The agentic
loop handles this:

1. LLM fills page 1, plans to click "Next"
2. Code clicks Next, waits for page load
3. Re-observe: new page with new fields
4. LLM sees new fields, plans next actions
5. Repeat until final step, then STOP (never click submit)

The LLM determines "is this the last page?" by looking at the page
content — if it sees a submit/finalize button, it knows to stop before
clicking it.

---

## CV Variant Selection

The LLM selects the CV variant as part of its plan (Step 2). It sees
the job description and the available CV variants (with their tags) and
picks the best match. If confidence is low, it asks the user via
Telegram before proceeding.

No separate "Stage A" for CV selection. It's part of the LLM's
reasoning about the whole application.

---

## Learned Answers

When the user corrects a field or confirms a value after review, the
system records the correction in `learned_answers.json`. On subsequent
applications, learned answers are included in the LLM's context and
take precedence over profile defaults.

The LLM decides when to use a learned answer (semantic matching in its
reasoning), not code-level string comparison.

---

## Human-in-the-Loop

The user is involved at these points:

1. **Before filling begins**: optional — the LLM can show the user its
   plan ("I'm about to fill these 15 fields. Go?")
2. **When the LLM is unsure**: any step with confidence below threshold
   triggers a Telegram question
3. **CAPTCHA / auth gate**: pause, notify, wait for user to solve
   themselves in the browser, resume on Telegram signal
4. **Before submit**: ALWAYS stop. Show screenshot. Wait for user to
   review in browser. User submits manually. User confirms back
   ("submitted" / "skipping this one")

---

## Error Recovery

The system is designed to recover from failures without crashing:

| failure | recovery |
|---------|----------|
| Element not found | Re-observe page, let LLM re-plan with updated tree |
| LLM returns malformed plan | Retry with error context, ask user if persistent |
| Page navigation fails | Retry, if persistent → ask user |
| LLM rate-limited / unavailable | Retry with backoff, ask user if persistent |
| Unexpected page state | Re-observe, LLM sees new state, re-plans |
| Form validation error after fill | Re-observe (error message visible), LLM adjusts |

---

## Notification Protocol

Telegram messages are sent at key moments:

1. "Starting application for: {url}"
2. "Step {n}: {description}" (optional, for verbose mode)
3. "Question: {the thing I need to know}" (when LLM is unsure)
4. "CAPTCHA/Auth required — please solve in browser, then send 'ready'"
5. "Form filled ({n} fields). Browser open for your review. I will NOT submit."
6. "Submitted / Skipped" (after user confirms)

---

## Tech Stack

- Backend: Python (FastAPI) — API endpoint for job submission
- Automation: Playwright (headed mode, persistent context per site)
- AI: LLM with multimodal capability (sees screenshots + text)
- Database: SQLite (audit log, learned answers, job history)
- Messaging: Telegram Bot API
- Hosting: local Mac (Option B — same machine runs browser + backend)

---

## File Structure

```
job_agent/
  cli.py                  # Entry point: intake, fill, bot commands
  agent.py                # The agentic loop (observe → think → act → verify)
  llm.py                  # LLM client (with retry, fallback, multimodal)
  models.py               # Data classes (JobRecord, FieldEvidence, etc.)
  storage.py              # SQLite + file paths
  intake.py               # Page evidence capture (screenshot + a11y tree)
  telegram_bot.py         # Telegram bot handlers
  handlers/
    _shared.py            # Telegram helpers, chat ID management
    popups.py             # Cookie banner / popup dismissal
  config/
    profile.yaml          # User's data
    cv_variants.yaml      # Available CV files + tags
  data/
    learned_answers.json  # User-verified corrections
    screenshots/          # Page screenshots per job
```

---

## What This Architecture Eliminates

| eliminated | why |
|------------|-----|
| Per-vendor handlers | LLM reasons about any form generically |
| Hardcoded button regex | LLM identifies buttons by their a11y role + name |
| Hardcoded field label heuristics | LLM understands field semantics from context |
| Hardcoded "select_option by label then value then index" chains | LLM tells us which option to pick; code tries the obvious path |
| Separate "Stage A B C D" pipeline | Single loop with LLM reasoning at each step |
| Hardcoded field_type → fill logic dispatch | Code has one action handler per action type (click/fill/select/upload) |
| "Is this a submit button?" detection | LLM knows from context and page content |
| "Is this the last page?" detection | LLM decides based on page content |
| Hardcoded D&I / legal / demographic rules | LLM applies common-sense reasoning to each specific form |

---

## Build Order

- [ ] `agent.py` — the core agentic loop (observe → think → act → verify → repeat)
- [ ] `llm.py` — multimodal LLM client with retry + structured output parsing
- [ ] `intake.py` — capture page state as a11y tree + screenshot
- [ ] `handlers/popups.py` — dismiss cookie banners (keep, it's mechanical not decision-making)
- [ ] `config/profile.yaml` — user's data (already exists)
- [ ] `config/cv_variants.yaml` — CV catalog (already exists)
- [ ] `data/learned_answers.json` — starts empty (already exists)
- [ ] `agent.py` — CAPTCHA/auth gate detection → pause/resume
- [ ] `agent.py` — final review checkpoint (hard stop before submit)
- [ ] `telegram_bot.py` — notifications + user interaction
- [ ] `cli.py` — wire it all together
- [ ] `storage.py` — audit log
- [ ] Test against 3+ real, different job pages (Greenhouse, custom career page, multi-step Oracle)

---

## Key Differences From Prior Versions

1. **No pipeline stages.** The "Extract → Interpret → Map → Fill"
   sequence is replaced by a single agentic loop where the LLM does
   all reasoning at each step.

2. **No hardcoded form knowledge anywhere.** Zero regex for button
   labels. Zero hardcoded field names. Zero vendor-specific code.

3. **Accessibility tree as the primary observation mechanism.**
   More reliable than DOM traversal, more semantic than raw HTML.

4. **LLM sees the whole page, not a pre-processed field list.**
   The LLM looks at the same page the user sees and decides what to
   do, rather than working from a simplified/extracted representation.

5. **Conditional fields are handled by design, not by special code.**
   The observe-act cycle naturally handles fields that appear mid-interaction.

6. **Verification is built-in, not an afterthought.** Every action is
   confirmed before the next one begins.
