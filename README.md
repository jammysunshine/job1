# Job Application Fill Agent

Local-first, human-in-the-loop job application fill assistant.

Current build target: a thin vertical slice that accepts a job URL,
captures page evidence with Playwright, and prepares an LLM-centric
classification/mapping request. The agent never submits applications.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

Add your Gemini key to `.env` for real LLM calls:

```bash
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.5-flash
```

`.env` is ignored by git. Keep `.env.example` as the shareable template.

## First Slice

```bash
python -m job_agent.cli intake "https://example.com/job"
```

Artifacts are written under `data/`.
