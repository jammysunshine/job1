from __future__ import annotations

import json
import os
from typing import Any, Dict

from .models import PageEvidence


SYSTEM_PROMPT = """
You are the central reasoning engine for a human-in-the-loop job
application fill assistant. Interpret application pages semantically.
Do not rely on hard-coded field-name lookup behavior. Return strict JSON.
If confidence is low, ask for human confirmation rather than guessing.
The assistant never clicks submit and never bypasses CAPTCHA.
""".strip()


def build_intake_prompt(evidence: PageEvidence) -> Dict[str, Any]:
    fields = [field.to_dict() for field in evidence.fields if field.visible]
    return {
        "task": "classify_ats_and_understand_form",
        "job_url": evidence.job_url,
        "final_url": evidence.final_url,
        "title": evidence.title,
        "visible_text_sample": evidence.visible_text_sample,
        "ats_signals": evidence.ats_signals,
        "fields": fields,
        "buttons": evidence.buttons,
        "required_output_schema": {
            "ats_type": "oracle_taleo|oracle_recruiting_cloud|workday|greenhouse|lever|ashby|custom|unknown",
            "detection_confidence": "0.0-1.0",
            "application_flow_summary": "string",
            "visible_required_fields": ["string"],
            "missing_context_questions": ["string"],
            "reasoning_summary": "short string",
        },
    }


def classify_intake(evidence: PageEvidence) -> Dict[str, Any]:
    prompt = build_intake_prompt(evidence)
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return {
            "mode": "dry_run_no_openai_api_key",
            "system_prompt": SYSTEM_PROMPT,
            "prompt": prompt,
        }

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("OpenAI package is not installed. Run: pip install -r requirements.txt") from exc

    client = OpenAI(api_key=api_key)
    response = client.responses.create(
        model=os.environ.get("JOB_AGENT_MODEL", "gpt-4.1-mini"),
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(prompt)},
        ],
        text={"format": {"type": "json_object"}},
    )
    return json.loads(response.output_text)

