from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .models import PageEvidence
from .storage import ROOT


SYSTEM_PROMPT = """
You are the central reasoning engine for a human-in-the-loop job
application fill assistant. Interpret application pages semantically.
Do not rely on hard-coded field-name lookup behavior. Return strict JSON.
If confidence is low, ask for human confirmation rather than guessing.
The assistant never clicks submit and never bypasses CAPTCHA.
""".strip()


def _get_client():
    from google import genai

    if os.environ.get("VERTEX_AI") == "true":
        project = os.environ.get("GCP_PROJECT_ID")
        if not project:
            raise RuntimeError("GCP_PROJECT_ID required when VERTEX_AI=true")
        location = os.environ.get("GCP_LOCATION", "us-central1")
        return genai.Client(vertexai=True, project=project, location=location)

    api_key = os.environ.get("GEMINI_LLM_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    return genai.Client(api_key=api_key)


def _generate(client, prompt: Dict[str, Any], system_prompt: Optional[str] = None) -> Dict[str, Any]:
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    sp = system_prompt or SYSTEM_PROMPT
    response = client.models.generate_content(
        model=model,
        contents=f"{sp}\n\nInput JSON:\n{json.dumps(prompt)}",
        config={"response_mime_type": "application/json"},
    )
    return json.loads(response.text)


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
    load_env_file()
    prompt = build_intake_prompt(evidence)
    client = _get_client()
    if client is None:
        return {
            "mode": "dry_run_no_gemini_api_key",
            "system_prompt": SYSTEM_PROMPT,
            "prompt": prompt,
        }
    return _generate(client, prompt)


def load_env_file(path: Optional[Path] = None) -> None:
    env_path = path or ROOT / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
