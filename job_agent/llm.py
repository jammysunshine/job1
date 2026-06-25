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


def _get_client() -> Optional[Dict[str, Any]]:
    """Returns a context dict for REST API calls (Vertex via ADC or Gemini API key)."""
    load_env_file()

    if os.environ.get("VERTEX_AI") == "true":
        project = os.environ.get("GCP_PROJECT_ID")
        if not project:
            raise RuntimeError("GCP_PROJECT_ID required when VERTEX_AI=true")
        location = os.environ.get("GCP_LOCATION", "us-central1")
        import subprocess

        token = subprocess.check_output(
            ["gcloud", "auth", "print-access-token", f"--project={project}"]
        ).decode().strip()
        return {"vertex": True, "project": project, "location": location, "token": token}

    api_key = os.environ.get("GEMINI_LLM_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    return {"vertex": False, "api_key": api_key}


def _generate(client: Dict[str, Any], prompt: Dict[str, Any], system_prompt: Optional[str] = None) -> Dict[str, Any]:
    import requests

    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    sp = system_prompt or SYSTEM_PROMPT
    body = {
        "contents": [{"role": "user", "parts": [{"text": f"{sp}\n\nInput JSON:\n{json.dumps(prompt)}"}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }

    if client.get("vertex"):
        url = (
            f"https://{client['location']}-aiplatform.googleapis.com/v1beta1/"
            f"projects/{client['project']}/locations/{client['location']}/"
            f"publishers/google/models/{model}:generateContent"
        )
        headers = {"Authorization": f"Bearer {client['token']}", "Content-Type": "application/json"}
    else:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={client['api_key']}"
        headers = {"Content-Type": "application/json"}

    resp = requests.post(url, headers=headers, json=body, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return json.loads(data["candidates"][0]["content"]["parts"][0]["text"])


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
            "ats_type": "oracle_taleo|oracle_recruiting_cloud|workday|greenhouse|lever|ashby|people_ksa|careers_page|custom|unknown",
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
