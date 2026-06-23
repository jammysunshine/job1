# Job Application Fill Agent (V2 Spec)

## 1. System Overview
A human-in-the-loop job application FILL system. It does the repetitive
work of navigating an ATS and entering your information; you do the
final review and click submit yourself, every time.

Input: Job posting URL (e.g. shared from LinkedIn or pasted directly)
Output: A fully filled-out application, left open for your review +
your own manual submission, plus an audit log entry.

Flow:
1. User sends job URL (Telegram, MVP)
2. System fetches the page, detects the ATS vendor
3. Decision engine maps your profile data to the form's fields
4. System asks you (via Telegram) for anything it can't infer —
   salary expectations, relocation, notice period, anything ambiguous
5. Playwright opens the form and fills it, uploads your CV
6. If a CAPTCHA appears: system pauses, notifies you, waits for you to
   solve it yourself, then resumes filling once you confirm
7. Once fully filled: system STOPS. Leaves the browser open (or sends
   you a screenshot) and waits for YOUR manual review + YOUR manual
   submit click.
8. Logs the outcome (filled / pending-review / submitted-by-user) once
   you confirm what happened

---

## 2. Core Architecture

### 2.1 Mobile Trigger Layer
- Telegram bot (MVP)
- Payload: `{ job_url, user_note, timestamp }`

### 2.2 Job Intake Service
- Gather ATS signals from URL/domain patterns, page title, visible text,
  scripts, form structure, and browser-observed behavior. URL/domain
  patterns are only cheap hints, not the whole detection strategy.
- Fetch job page HTML when available; if the page is JS-rendered,
  login-gated, or blocks plain HTTP fetches, fall back to Playwright
  inspection in a headed browser
- Use the LLM to classify/confirm the ATS and application flow from the
  collected evidence, especially when the URL pattern is ambiguous or
  the page is a custom-branded wrapper over a known ATS
- Detect ATS: Oracle Taleo / Oracle Recruiting Cloud / Workday /
  Greenhouse / Lever / Ashby / Custom
- Extract: job title, company, location, application steps, visible
  required fields
- Output: `{ ats_type, detection_confidence, intake_mode, steps,
  fields_required, difficulty_score }`

### 2.2a CV Variant Catalog
Since one CV doesn't fit every role, this is a small registry mapping
named variants to files + tags, so the Decision Engine has something
concrete to match against:

```yaml
cv_variants:
  - name: "CIO_Leadership"
    file_path: /path/to/Mohit Mendiratta 022.pdf
    tags: [leadership, P&L, digital strategy, CIO, VP, transformation]
  - name: "Hands_on_AI_Technical"
    file_path: /path/to/Mohit_AI_Technical.pdf
    tags: [agentic AI, GenAI, hands-on, automation, Playwright, builder]
  - name: "Program manager"
    file_path: /path/to/Mohit_PM.pdf
    tags: []   # fallback if nothing else matches and you don't want
               # to be asked every single time — your call whether to
               # keep this or always ask when ambiguous
```
You maintain this file directly — add a new variant any time you create
a new CV version. No code change needed to add variants.

### 2.2b Learned Answers Store (separate from static profile)
A second store, `learned_answers.json` (or a DB table), that captures
corrections and new answers over time — this is what makes the system
get smarter rather than repeating stale defaults:

```json
{
  "answers": [
    {
      "question_normalized": "are you willing to relocate",
      "answer": "Yes, within UAE and India only",
      "question_type": "relocation",
      "scope": {
        "country": "UAE",
        "role_family": "leadership",
        "company": null,
        "global_ok": false
      },
      "source": "telegram_reply",
      "job_url": "...",
      "ats_type": "greenhouse",
      "timestamp": "...",
      "confirmed_by_user": true
    }
  ]
}
```

**Rules:**
- `learned_answers` ALWAYS takes precedence over `profile.yaml` static
  defaults when both could answer the same question, but only inside a
  compatible scope
- Most recent entry for a semantically-matched question wins over older
  entries within the same scope (not appended/averaged — replaced)
- Scope matters. Salary, visa, sponsorship, relocation, notice period,
  and work-location answers can vary by country, role type, company, or
  currency. If a learned answer is not marked `global_ok: true`, the
  Decision Engine should reuse it only when the new job context is close
  enough; otherwise it asks you again.
- Two ways an answer gets learned:
  1. **Telegram reply** — when the agent asks you something it can't
     infer, your answer is saved automatically
  2. **Review-diff confirmation** — after the agent fills the form, it
     snapshots the values it filled. During final review, you can edit
     fields directly in the browser as usual. Before closing the job,
     the agent rescans the visible form fields, compares "agent-filled"
     vs. "user-reviewed" values, and sends you a compact Telegram
     summary:

     `I noticed 3 changes: relocation: No -> Yes, salary: blank ->
     AED X, notice period: 60 -> 30 days. Learn these for future UAE
     roles? [Learn all] [Choose] [Do not learn]`

     This removes the need for you to manually type "I changed X".
     The agent proposes learned-answer updates automatically, but it
     does not commit review-diff changes until you confirm the batch.
- Review-diff learning rules:
  - Only compare fields that the vendor handler can read reliably and
    tie back to a stable field label/question
  - Ignore hidden fields, tracking fields, generated IDs, disabled
    controls, and fields whose value cannot be read back confidently
  - If a field changed because of an ATS default, autofill, browser
    password manager, or unclear widget behavior, show it as
    `review_needed` rather than learning it automatically
  - Let you choose per change: learn globally, learn only for this
    country/role/company scope, ignore once, or never learn this
    question type
  - Store the source as `review_diff_confirmed`, with the original
    agent-filled value, final reviewed value, scope, job URL, ATS type,
    and timestamp
- **Matching is semantic, not exact-string**: the Decision Engine
  checks whether a new form's question means the same thing as a
  stored question (e.g. "legally authorized to work here?" vs. "do you
  need visa sponsorship?" are related but need separate thought —
  don't blindly conflate them; when unsure whether two questions are
  asking the same thing, treat them as different and ask)


### 2.3 Decision Engine (LLM)
- Inputs: job data, structured profile (CV memory), CV variant catalog,
  learned answers, preferences
- Outputs: `{ field_mapping, missing_fields_questions[], cv_variant,
  cv_variant_confidence, cover_letter_needed }`
- **Central design choice: the LLM is the primary reasoning engine.**
  It is used from day 1, including the first MVP vendor. It decides what
  the application page is asking, which profile/learned-answer/CV data
  best answers each question, which questions need clarification, and
  which CV variant fits the role.
- Field matching must be semantic and LLM-driven, not a hard-coded
  lookup table of labels like `first_name -> firstName`. Lookup tables
  may exist only for low-level normalization and safety checks, not as
  the main intelligence of the system.
- The LLM does NOT directly operate the browser. Vendor handlers remain
  deterministic guardrails: they discover fields, expose rich field
  metadata/evidence to the LLM, validate the returned mapping, and
  perform the actual Playwright actions.
- Vendor handlers should avoid encoding personal-answer logic. Their
  job is to understand the mechanics of a platform (selectors, widgets,
  multi-step navigation, file upload, validation errors), not to decide
  what an answer means or which answer should be chosen.
- Output must be strict structured JSON, not free-form prose. Each
  mapped field should include:
  - `field_id`
  - `field_label`
  - `field_evidence` (nearby text, placeholder, options, aria label,
    section heading, required/optional state)
  - `answer`
  - `answer_source` (`profile`, `learned_answers`, `user_reply`,
    `generated`, or `unknown`)
  - `confidence`
  - `requires_user_confirmation`
  - `reason`
- Decides which CV/profile fields go where on the detected form, what
  it still needs to ask you, AND which CV variant fits this job best
- Sensitive or context-dependent fields (salary, visa sponsorship,
  relocation, notice period, work authorization, disability/diversity
  questions, legal declarations) default to
  `requires_user_confirmation: true` unless the answer is explicitly
  known for this exact context.
- If the LLM returns an answer that does not match an allowed option,
  targets a missing/hidden field, has low confidence, or conflicts with
  vendor-handler validation, the system asks you rather than trying to
  repair the mapping silently.
- **CV variant selection is a recommendation, not a silent choice**:
  - If confidence is high (job clearly matches one variant's tag — e.g.
    job title/description strongly skews "technical/hands-on" and you
    have a variant tagged that way) → agent picks it, but always states
    which variant it chose and why in the Telegram message, so you can
    override before the fill starts
  - If confidence is low/ambiguous (job could plausibly fit 2+ variants,
    or none fit well) → agent asks you via Telegram BEFORE filling,
    rather than guessing: "This role looks like it could fit your
    [CIO/Leadership] or [Hands-on AI/Technical] CV — which one?"
  - This mirrors the same principle as unknown ATS vendors: ambiguity
    gets surfaced to you, never silently resolved

### 2.4 Human-in-the-loop Layer (Telegram)
Used for:
- Salary expectations, relocation, notice period, anything genuinely
  ambiguous in your profile vs. the question asked
- **CAPTCHA checkpoint**: bot pings you, you solve it in the open
  browser window yourself (a few seconds), you reply "done"/tap a
  button, agent resumes
- **Final review checkpoint**: bot tells you the form is fully filled
  and ready; sends a screenshot or confirms the browser tab is open;
  waits for you to review and submit it yourself, then confirm back
  ("submitted" / "skipping this one")
- **Learning checkpoint**: if your final reviewed answers differ from
  what the agent filled, bot summarizes the changes and lets you learn
  all, choose specific changes, or ignore them

### 2.5 Execution Engine (Playwright)
- Opens job portal, maintains session per ATS (cookie persistence)
- Fills forms per the Decision Engine's field mapping
- Uploads CV (correct variant per job)
- Navigates multi-step application flows
- Verifies each step after filling:
  - reads back filled values where possible
  - detects required-field validation errors after "next" clicks
  - captures a screenshot per step for debugging/audit
  - reports any mismatch back through Telegram instead of forcing the
    flow forward
- Supports review-diff learning at the final review checkpoint:
  - records a structured snapshot of every visible field it filled
  - waits while you review and edit the form manually
  - rescans readable visible fields before the job is closed
  - proposes changed answers back to you as a batch for learning
  - writes to `learned_answers` only after your Telegram confirmation
- Detects blockers:
  - **CAPTCHA** → pause, notify user via Telegram, wait for explicit
    "resume" signal from user before continuing
  - **Login/MFA required** → pause, notify user, wait for user to log
    in manually if needed, then resume
- **Hard stop before the final submit button on every application.**
  The submit selector is located only to confirm "this is the end of
  the form" — never clicked by the agent, ever, no exceptions, no
  config flag to change this.
- One job per browser session (per the v1 anti-blocking principle)

### 2.6 Audit & Memory Layer
Database stores: job_url, company, ATS type, CV version used, CV file
hash/path, answers filled, screenshots/checkpoint artifacts, blocker
events, timestamp, and status (`filled_awaiting_review` /
`user_submitted` / `user_skipped`)

### 2.7 Deployment Model — Option B (start here)
**Playwright cannot run on your Android phone** — it needs a real
desktop browser engine. So the split is:

- **Where it runs**: your Mac. The FastAPI backend + Playwright both
  run locally on your machine, which needs to be on and reachable when
  you trigger a job.
- **How mobile triggers it**: Telegram bot backend also runs locally
  (or is reachable from Telegram's servers via a tunnel like ngrok or
  Tailscale) — you message the bot from your phone, your Mac picks up
  the job and starts filling.
- **How you "step into" the session**: since the browser is running
  physically on your Mac, the CAPTCHA-pause and final-review checkpoints
  happen there — you walk over to your machine and look/solve/submit.
  Telegram messages from the agent are notifications ("ready for you")
  and a quick screenshot preview, not a remote-control interface.
- **What this sacrifices, deliberately, for now**: true "trigger and
  review from anywhere, Mac asleep in another room" operation. That's
  fine for the learning-project stage — you're not running a 24/7
  service yet.

**Future upgrade path (Option A, not now)**: move the backend +
Playwright to an always-on cloud VM running a virtual display (Xvfb),
and expose the live browser session through noVNC so you can view and
click into it from your phone's browser, not just see a static
screenshot. Same fill logic, different hosting — revisit this once
Option B is proven and you find yourself wanting to act from places
where you're not near your laptop.


---

## 3. Tech Stack
- Backend: Python (FastAPI) — matches your existing stack from Robin
- Automation: Playwright, headed mode
- Database: PostgreSQL (or SQLite for MVP — simpler, no separate
  service to run)
- Queue: Celery, or skip entirely for MVP since volume is low
  (10ish applications, not thousands) — a simple sequential loop may
  be enough until you have a reason for a queue
- Messaging: Telegram Bot API
- AI: structured outputs for field mapping / question generation
- Design posture: LLM-centric reasoning with deterministic browser
  execution. The LLM interprets form meaning; code enforces safety and
  performs actions.
- Hosting for MVP: local Mac only. Cloud Run is a future option for API
  pieces that do not need a local headed browser, but the Playwright
  browser session stays local until the noVNC/cloud-VM upgrade path is
  deliberately built.

---

## 4. Supported ATS (priority order, confirm against your real 10 links)
Because your target market is UAE / Middle East, enterprise ATS usage
may skew toward Oracle Taleo, Oracle Recruiting Cloud, Workday,
SuccessFactors, and other large-company platforms rather than
startup-heavy tools like Greenhouse or Lever. Do not rely on a generic
global ranking here — validate this against your first 10-20 real
target application links.

Recommended MVP order:
- First candidate: Oracle Taleo / Oracle Recruiting Cloud, if your UAE
  target links confirm it is common enough to justify the extra
  complexity
- Simpler fallback candidate: Greenhouse or Lever, if you want a faster
  first end-to-end proof before tackling Oracle

Tier 1: Oracle Taleo, Oracle Recruiting Cloud, Workday, Greenhouse,
Lever, Ashby
Tier 2 (later): SmartRecruiters, BambooHR, SuccessFactors
Tier 3 (later, lower confidence, flagged as such): custom/government
portals

---

## 5. Anti-Blocking Principles (unchanged from v1 — these are good)
- Real browser sessions, cookie persistence
- No rapid automation bursts; randomized delays (300ms–2s)
- One job per session
- Session isolation per ATS type
- Do not re-login repeatedly

---

## 6. Build Order
- [ ] Profile store (`profile.yaml`) — your real data, source of truth
- [ ] CV variant catalog (`cv_variants.yaml`) — start with 2 variants
      (e.g. CIO/Leadership and Hands-on/AI-Technical), expand as needed
- [ ] Learned answers store — empty at first, gets populated as you use
      the system; confirm the precedence rule (learned > static) works
      with a couple of manual test entries before relying on it
- [ ] Telegram bot: receive job URL, store it, basic acknowledgment
- [ ] Job intake v0: collect page evidence (URL/domain, visible text,
      form structure, scripts, page title) and use the LLM to classify
      or confirm the ATS/application flow. If confidence is low, log
      `unknown` and ask rather than guessing.
- [ ] Collect/classify your first 10-20 UAE target links, then choose
      the first vendor handler. If Oracle Taleo or Oracle Recruiting
      Cloud is common in that set, do Oracle first; otherwise pick the
      most common vendor or Greenhouse/Lever for a faster proof.
- [ ] Decision Engine v0 from day 1: given normalized fields from the
      first vendor handler plus rich field evidence, return strict JSON
      mappings with confidence, answer source, and confirmation flags.
      Do not build hard-coded personal-answer mappings as the primary
      logic.
- [ ] First vendor handler, fill-only, tested end to end against 2-3
      real postings. The handler owns browser mechanics; the LLM owns
      field interpretation and answer selection.
- [ ] Step-level verification: read back values, catch validation
      errors, and save screenshots
- [ ] CAPTCHA detection → Telegram pause/resume loop
- [ ] Final-review checkpoint: confirm the agent stops cleanly and
      waits, every single time, with no path to auto-submit
- [ ] CV variant selection logic — test with a job that should be
      unambiguous, then one that should trigger the "which CV?" ask
- [ ] Second + third vendor handlers
- [ ] Audit log + simple history view
