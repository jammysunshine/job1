from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .models import FieldEvidence
from .storage import ROOT


SYSTEM_PROMPT = """
You are the decision engine for a human-in-the-loop job application filler.
Map each form field to the best available answer from the user's profile,
learned answers, or mark it as needing clarification.

Rules:
- Semantic matching: interpret what the field is asking, don't just match labels.
- For fields where the answer is clearly in the profile, set requires_user_confirmation=false.
- For sensitive fields (salary, visa, relocation, notice, work authorization, legal), set requires_user_confirmation=true unless the answer exactly matches this context.
- If you cannot confidently map a field, set answer to null and explain why in reason.
- Select the best CV variant for this job. If confidence is low (<0.7), flag it.
- answer_source must be one of: profile, learned_answers, generated, unknown.
- Return the field_idx unchanged for every field — it is the stable identifier used to locate the field during filling. The field_id may change between page loads for some fields.
- Return only valid JSON matching the output schema below.
""".strip()


def load_profile() -> Dict[str, Any]:
    path = ROOT / "config" / "profile.yaml"
    if path.exists():
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}


def load_cv_variants() -> List[Dict[str, Any]]:
    path = ROOT / "config" / "cv_variants.yaml"
    if path.exists():
        with open(path) as f:
            data = yaml.safe_load(f) or {}
            return data.get("cv_variants", [])
    return []


def load_learned_answers() -> List[Dict[str, Any]]:
    path = ROOT / "data" / "learned_answers.json"
    if path.exists():
        with open(path) as f:
            data = json.load(f) or {}
            return data.get("answers", [])
    return []


def build_field_mapping_prompt(
    fields: List[FieldEvidence],
    job_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    profile = load_profile()
    cv_variants = load_cv_variants()
    learned_answers = load_learned_answers()

    return {
        "task": "map_form_fields_to_profile",
        "job_context": job_context or {},
        "profile": profile,
        "cv_variants": cv_variants,
        "learned_answers": learned_answers,
        "form_fields": [f.to_dict() for f in fields],
        "required_output_schema": {
            "fields": [
                {
                    "field_idx": "integer (must match the form_field field_idx)",
                    "field_id": "string (must match one of the form_fields field_id values)",
                    "value": "string or null (null if not mappable)",
                    "answer_source": "profile|learned_answers|generated|unknown",
                    "confidence": "0.0-1.0",
                    "requires_user_confirmation": "boolean",
                    "reason": "short explanation of how this value was chosen",
                }
            ],
            "cv_variant": "string (name of best-matching CV variant, or null)",
            "cv_variant_confidence": "0.0-1.0",
            "cover_letter_needed": "boolean",
            "missing_fields_questions": ["strings describing info still needed"],
            "requires_overall_confirmation": "boolean (true if any field needs confirmation)",
        },
    }


def map_fields(
    fields: List[FieldEvidence],
    job_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    from .llm import _generate, _get_client, load_env_file

    load_env_file()
    prompt = build_field_mapping_prompt(fields, job_context)
    client = _get_client()

    if client is None:
        return {
            "mode": "dry_run_no_gemini_api_key",
            "fields": [],
            "cv_variant": None,
            "cv_variant_confidence": 0.0,
            "cover_letter_needed": False,
            "missing_fields_questions": [],
            "requires_overall_confirmation": True,
        }

    return _generate(client, prompt, system_prompt=SYSTEM_PROMPT)
