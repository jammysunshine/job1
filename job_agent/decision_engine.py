from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .models import FieldEvidence
from .storage import ROOT


SYSTEM_PROMPT = """
You are Stage C — the Mapper — of the four-stage job application
pipeline. Stage B (Field Interpreter) has already annotated each form
field with its semantic meaning and expected value shape. Your job is
to decide what actual value belongs in each field, using the candidate's
profile, learned answers, and the field annotations provided.

Inputs available:
1. form_fields — each field has a field_context and expected_value_shape
   provided by Stage B. Trust those annotations.
2. profile — the candidate's personal/professional data
3. learned_answers — previously user-verified answers (take precedence
   over profile for matching field_context)
4. cv_variants — available CV files with tag metadata
5. cover_letter_template — optional style reference
6. job_context — job description, URL, page context

Rules:
- For fields where the answer is clearly in the profile, set requires_user_confirmation=false.
- For sensitive fields (salary, visa, relocation, notice, work authorization, legal), set requires_user_confirmation=true unless the answer exactly matches this context.
- If you cannot confidently map a field, set value to null and explain why in reason.
- Select the best CV variant for this job. If confidence is low (<0.7), flag it.
- answer_source must be one of: profile, learned_answers, generated, unknown.
- Return the field_idx unchanged for every field — it is the stable identifier used to locate the field during filling. The field_id may change between page loads for some fields.
- Cover letter: if a job_description is provided in job_context and you find a field that is clearly a cover letter textarea, write a cover letter (200-350 words) tailored to the specific job. Follow the style and tone of the cover_letter_template if provided, but customize the content to match the job description. Use concrete examples from the candidate's work experience that are relevant to the JD. Keep it concise — no fluff. Set answer_source to "generated". If no job_description is available, set cover letter to null.
- Learned answers with field_context like "location", "nationality", "date_of_birth" should be preferred over profile values when there's a direct match — they represent user-verified corrections.
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


def load_cover_letter_template() -> Optional[str]:
    path = ROOT / "config" / "cover_letter_template.txt"
    if path.exists():
        return path.read_text().strip()
    return None


def build_field_mapping_prompt(
    fields: List[FieldEvidence],
    job_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    profile = load_profile()
    cv_variants = load_cv_variants()
    learned_answers = load_learned_answers()

    cl_template = load_cover_letter_template()

    return {
        "task": "map_form_fields_to_profile",
        "job_context": job_context or {},
        "profile": profile,
        "cv_variants": cv_variants,
        "learned_answers": learned_answers,
        "cover_letter_template": cl_template,
        "form_fields": [f.to_dict() for f in fields],
        "required_output_schema": {
            "fields": [
                {
                    "field_idx": "integer (must match the form_field field_idx)",
                    "field_id": "string (must match one of the form_fields field_id values)",
                    "field_context": "string — the semantic meaning of this field (e.g. 'full_name', 'email', 'phone', 'location', 'current_salary', 'expected_salary', 'current_salary_currency', 'expected_salary_currency', 'current_salary_frequency', 'expected_salary_frequency', 'notice_period', 'linkedin', 'cover_letter', 'cv_upload', 'terms', 'nationality', 'date_of_birth'). Use null if unsure.",
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
