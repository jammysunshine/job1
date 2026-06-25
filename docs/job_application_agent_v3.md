# Job Application Fill Agent (V3 Spec)

## What changed from v2
v2 assumed most ATS traffic would cluster on a few major platforms
(Workday/Greenhouse/Lever/Ashby) with stable, hardcodable form
structures, so it specified per-vendor Python handlers. In practice,
most real-world postings turn out to be **custom company career pages**
with no stable structure to hardcode against — writing new Python per
company doesn't scale and isn't really "agentic" so much as it's a
script library that grows forever.

v3 replaces vendor-specific handlers with a **generic four-stage
pipeline** that uses an LLM only where genuine judgment is required
(interpreting what a field means, deciding what value belongs in it)
and plain deterministic code everywhere else (finding fields, typing
into them, detecting page changes). The result: a new company's custom
form requires **zero new code** — it flows through the same pipeline
every time.

---

## 1. System Overview
A human-in-the-loop job application FILL system. It does the repetitive
work of navigating any ATS or custom career page and entering your
information; you do the final review and click submit yourself, every
time.

Input: Job posting URL (e.g. shared from LinkedIn or pasted directly)
Output: A fully filled-out application, left open for your review +
your own manual submission, plus an audit log entry.

High-level flow:
1. User sends job URL (Telegram, MVP)
2. Orchestrator loads the page and runs the four-stage pipeline
   (Extract → Interpret → Map → Fill) on whatever form it finds
3. Orchestrator checks if filling triggered a new step/page; if so,
   repeats the pipeline on the new state
4. Anything genuinely ambiguous gets asked to the user via Telegram,
   at any stage
5. If a CAPTCHA appears: pause, notify user, wait for the user to
   solve it themselves, resume on confirmation
6. Once the final step's form is fully filled: STOP. Leave the browser
   open for review. Wait for the user's manual review + manual submit.
7. Log the outcome once the user confirms what happened

---

## 2. Core Architecture

### 2.1 Mobile Trigger Layer
- Telegram bot (MVP)
- Payload: `{ job_url, user_note, timestamp }`

### 2.2 The Four-Stage Form Pipeline (the core of the system)

This pipeline runs once per page/step encountered, in a loop, until no
more "next/continue" action is found. It replaces all vendor-specific
handler code from v2.

**Stage A — Extractor** (`extractor.py`, deterministic, NO LLM call)
- Walks the current page's DOM via Playwright
- For every visible `<input>`, `<select>`, `<textarea>`, and common
  custom widgets (date pickers, searchable dropdowns, file uploads):
  captures its label (or `aria-label`/placeholder if unlabeled), its
  type, its selector, and — critically for `<select>` / custom
  dropdowns — its **full list of available options**, verbatim
- Output: a plain structured list, e.g.
  ```json
  [
    { "label": "Nationality", "type": "select", "selector": "#nat",
      "options": ["United Kingdom of Great Britain and Northern Ireland",
                  "India", "United Arab Emirates", "..."] },
    { "label": "Date of Birth", "type": "text", "selector": "input[name=dob]",
      "placeholder": "MM/DD/YYYY" },
    { "label": "Current Location", "type": "text", "selector": "#loc" }
  ]
  ```
- This is plain code. No reasoning happens here — it's a faithful
  inventory of what's on the page.

**Stage B — Field Interpreter** (`field_interpreter.py`, ONE LLM call)
- Input: the raw field list from Stage A
- Job: resolve ambiguity about what each field actually expects —
  e.g. "this nationality select wants the formal country name, not
  the common one," "this DOB field's placeholder implies US date
  format," "this is a free-text city field, no structured format
  enforced"
- Output: the same field list, annotated with a normalized "expected
  value shape" per field
- This is reasoning about the FORM, not about your data yet

**Stage C — Mapper** (`mapper.py`, ONE LLM call)
- Input: Stage B's annotated field list + `profile.yaml` +
  `cv_variants.yaml` + `learned_answers.json`
- Job: decide what value goes in each field, in the exact shape Stage
  B identified (e.g. picks the exact matching option string for a
  select, formats the DOB correctly for that field's expected format)
- Also decides CV variant for this job (see 2.2a) if not yet decided
  for this application
- Output: `{ selector: value }` pairs for everything it could
  confidently resolve, PLUS a list of fields it could NOT confidently
  resolve — these become Telegram questions to the user before filling
  continues
- This is reasoning about YOUR DATA matched against the form's needs

**Stage D — Filler** (`filler.py`, deterministic, NO LLM call)
- Input: the final `{ selector: value }` map (Stage C's output, plus
  any user-supplied answers from Telegram folded in)
- Mechanically executes: `.select_option()` for dropdowns, `.fill()`
  for text fields, `.set_input_files()` for CV upload, date-picker
  interaction if a calendar widget is detected
- No judgment exercised here — just execute exactly what was decided

**The loop**: after Stage D, deterministic code checks whether a
Next/Continue action exists and whether clicking it changes the
page/DOM root (multi-step application). If yes, loop back to Stage A
on the new state. A lightweight LLM check can disambiguate "is this a
new step or a validation error" only if the deterministic heuristic
is itself ambiguous — this should be the exception, not the default
path.

### 2.2a CV Variant Catalog
```yaml
cv_variants:
  - name: "CIO_Leadership"
    file_path: /path/to/Mohit Mendiratta 022.pdf
    tags: [leadership, P&L, digital strategy, CIO, VP, transformation]
  - name: "Hands_on_AI_Technical"
    file_path: /path/to/Mohit Mendiratta 022.pdf
    tags: [agentic AI, GenAI, hands-on, automation, Playwright, builder]
```
Decided by no-Default-fallback: when confidence is anything less than
high, the Mapper (Stage C) asks via Telegram rather than guessing.
You maintain this file directly — add a new variant any time you
create a new CV version. No code change needed.

### 2.2b Learned Answers Store
```json
{
  "answers": [
    { "question_normalized": "are you willing to relocate",
      "answer": "Yes",
      "source": "telegram_reply", "job_url": "...",
      "timestamp": "..." }
  ]
}
```
**Rules (unchanged from v2):**
- Always takes precedence over `profile.yaml` static defaults
- Most recent entry wins over older entries for a matched question
- Learned via Telegram reply (automatic) or explicit user confirmation
  of a manual correction (never via silent DOM-diffing — too fragile,
  over-generalizes one-off exceptions)
- Matching is semantic (via the LLM in Stage C), not exact-string —
  when genuinely unsure if two questions mean the same thing, treat
  them as different and ask

### 2.3 Human-in-the-loop Layer (Telegram)
Used for:
- Any field Stage C couldn't confidently resolve
- CV variant choice when ambiguous
- **CAPTCHA checkpoint**: pause, notify, wait for user to solve it
  themselves, resume on confirmation
- **Final review checkpoint**: notify that the (last step of the)
  form is fully filled and ready; user reviews and submits manually,
  then confirms back ("submitted" / "skipping this one")

### 2.4 Browser Session Management (Playwright)
- Maintains session per site (cookie persistence)
- Detects blockers: CAPTCHA, login/MFA required → pause, notify, wait
  for user, resume on signal
- **Hard stop before the final submit button, every time. Not
  configurable. No exceptions.** The submit selector is only ever
  located to confirm "this is the end of the form."
- One job per browser session

### 2.5 Audit & Memory Layer
Database/log stores: job_url, company, ATS type (if identifiable),
CV version used, field mapping decisions made, answers submitted,
timestamp, status (`filled_awaiting_review` / `user_submitted` /
`user_skipped`)

### 2.6 Deployment Model — Option B (start here)
**Playwright cannot run on your Android phone** — it needs a real
desktop browser engine.
- Backend (FastAPI) + Playwright run on your Mac, which needs to be on
  and reachable when you trigger a job
- Telegram bot triggers it from your phone; your Mac picks up the job
- CAPTCHA-pause and final-review checkpoints happen physically at your
  Mac — Telegram messages are notifications + screenshot previews, not
  a remote-control interface
- Deliberately sacrifices "trigger from anywhere, Mac asleep" for
  simplicity at this stage

**Future upgrade path (Option A, not now)**: always-on cloud VM with a
virtual display (Xvfb) + noVNC so you can view/click into the live
session from your phone's browser. Same pipeline, different hosting.

---

## 3. Tech Stack
- Backend: Python (FastAPI)
- Automation: Playwright, headed mode
- Database: SQLite for MVP (simple, no separate service)
- Messaging: Telegram Bot API
- AI: structured outputs for Stage B (interpret) and Stage C (map)
- Hosting: local (your Mac) for now

---

## 4. ATS Awareness (informational only, not a routing mechanism)
The pipeline does NOT branch its logic based on detected ATS vendor.
Vendor detection (if implemented at all) is purely for the audit log
and your own visibility ("this one was a Greenhouse posting") — it
never changes which code path runs. Every form, known platform or
fully custom, goes through the same Stages A–D.

---

## 5. Anti-Blocking Principles (unchanged from v1/v2)
- Real browser sessions, cookie persistence
- No rapid automation bursts; randomized delays (300ms–2s)
- One job per session
- Do not re-login repeatedly

---

## 6. Build Order
- [ ] Telegram bot: receive job URL, store it, basic acknowledgment
- [ ] Stage A (Extractor) — test against 3-4 real, different job pages;
      confirm it correctly inventories fields, types, and (critically)
      full option lists for dropdowns, with zero hardcoded assumptions
      about any specific site
- [ ] Profile store (`profile.yaml`) — your real data
- [ ] CV variant catalog (`cv_variants.yaml`) — 2 variants to start
- [ ] Learned answers store — empty at first
- [ ] Stage B (Field Interpreter) — single generic LLM call, tested
      against the same 3-4 pages from Stage A
- [ ] Stage C (Mapper) — single generic LLM call, tested end to end;
      confirm it correctly asks via Telegram when genuinely unsure
      rather than guessing
- [ ] Stage D (Filler) — mechanical execution, tested end to end
- [ ] CAPTCHA detection → Telegram pause/resume loop
- [ ] Final-review checkpoint — confirm hard stop before submit, always
- [ ] Multi-step loop (re-run A–D after Next/Continue) — test against
      at least one genuinely multi-page application
- [ ] Audit log

Note: there is no "first vendor handler, second vendor handler" step
anymore — the pipeline above IS the handler, generically, for every
form from day one.

---
