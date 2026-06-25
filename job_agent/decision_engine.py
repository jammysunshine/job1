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
- Radio / boolean fields come in pairs: one input for each option (e.g. idx=9 is the "Yes" radio, idx=10 is the "No" radio for the same question, both with the same field_context). For each pair, set the VALUE of the chosen radio's field entry to its label text (e.g. "Yes" or "No") and set the unchosen radio's value to null. Do NOT set both to the same value.
- For legal/conflict-of-interest / ethics / compliance yes/no questions (e.g. financial interest, officer/director role, esports ownership, family relationship, vendor connection, gifts, language fluency), default to answering "No" (set the "No" radio's value to "No", leave the "Yes" radio's value as null). Only answer "Yes" if the profile or learned_answers clearly indicate otherwise. Set requires_user_confirmation=true and add the question to missing_fields_questions so the user can override.
- Demographic/D&I fields: the profile may have these under personal_details. Search the entire profile tree, not just top-level keys. For common D&I fields apply these inference rules:
  * "gender_identity" / "Gender Identity": if not in profile explicitly, infer from profile.personal_details.gender (e.g. "Male" → "Male"). Set confidence to 0.8 and requires_user_confirmation=true so user can override if different.
  * "preferred_pronouns" / "Preferred Pronouns": if profile.personal_details.preferred_pronouns exists, use it. Otherwise infer from gender (gender="Male" → "He/Him", gender="Female" → "She/Her", otherwise → "They/Them"). Set confidence 0.8 and requires_user_confirmation=true.
  * "marital_status" / "Marital Status": use profile.personal_details.marital_status if available. Set requires_user_confirmation=false since this is stable personal data.
  * "disability" / "Disability": default to "No" (same as legal default-to-No rule). Set requires_user_confirmation=true because it's sensitive. Only answer "Yes" if profile explicitly indicates a disability.
  * "ethnicity" / "Ethnicity": use profile.personal_details.ethnicity if available. If not, try to infer from profile.other_residency, profile.nationality, or profile.personal.location patterns — but set confidence low (<0.5) and requires_user_confirmation=true.
  * "religion" / "Religion": use profile.personal_details.religion if available. Do NOT infer from name or background — this is sensitive. Leave null if not in profile, with requires_user_confirmation=true.
  * "sexual_orientation" / "Sexual Orientation": use profile.personal_details.sexual_orientation if available. Do NOT infer. Leave null if not in profile, with requires_user_confirmation=true.
  * "personal_summary" / "Personal Summary" / "Professional Profile": use profile.professional_summary if available. Set requires_user_confirmation=true since these are often role-specific.
- If you cannot confidently map a field, set value to null and explain why in reason.
- For honeypot / anti-spam fields (field_context="honeypot" or Stage B format_hint says "leave empty"), set value to the JSON literal null — do NOT set it to the string "None" or "null". The Stage B annotation already explained why this field should be empty; your job is to return null.
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
