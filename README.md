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

Optional for real LLM calls:

```bash
export OPENAI_API_KEY=...
```

## First Slice

```bash
python -m job_agent.cli intake "https://example.com/job"
```

Artifacts are written under `data/`.

