from __future__ import annotations

from typing import Any, Dict, List, Optional

from .models import FieldEvidence


SYSTEM_PROMPT = """
You are Stage B of a job-application form-filling pipeline — the Field
Interpreter. Your job is to reason about the FORM, not about the
candidate's data.

Given a raw inventory of visible form fields (labels, types, options,
placeholders, nearby text), resolve ambiguity about what each field
actually expects:

1. Normalize the field's semantic meaning (field_context) — what piece
   of information does this field want? Use standard labels like
   full_name, email, phone_number, location, city, nationality,
   date_of_birth, current_salary, expected_salary, notice_period,
   linkedin, cover_letter, cv_upload, terms, gender, etc.
2. Identify the expected value shape:
   - "select_one" — pick exactly one from the options list
   - "free_text" — any text in the expected format
   - "date_us" — MM/DD/YYYY
   - "date_intl" — DD/MM/YYYY
   - "phone" — phone number
   - "email" — email address
   - "numeric" — a number
   - "currency" — a monetary value
   - "file_upload" — file attachment
   - "boolean" — yes/no or agree/disagree
3. For select/option fields, note which option string is most likely the
   correct format (formal country name vs. common name, etc.).
4. Note if the field is sensitive (salary, visa, relocation, notice
   period, work authorization, legal, gender, ethnicity) so Stage C can
   handle it carefully.

Output the same field list annotated. Keep field_idx unchanged — it is
the stable identifier. Return only valid JSON.
""".strip()


def build_interpretation_prompt(
    fields: List[FieldEvidence],
    page_text: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "task": "interpret_form_fields",
        "page_text_sample": (page_text or "")[:4000],
        "form_fields": [f.to_dict() for f in fields],
        "required_output_schema": {
            "fields": [
                {
                    "field_idx": "integer (must match input field_idx)",
                    "field_id": "string (must match input field_id)",
                    "field_context": "string — normalized semantic meaning (e.g. full_name, email, nationality)",
                    "expected_value_shape": "string — one of: select_one, free_text, date_us, date_intl, phone, email, numeric, currency, file_upload, boolean",
                    "sensitive": "boolean — true if this field asks about salary, visa, relocation, legal, demographics",
                    "format_hint": "string — additional guidance for Stage C (e.g. 'expects 3-letter country code', 'accepts MM/DD/YYYY', 'use formal title')",
                    "preferred_option": "string or null — best-matching option if select_one, or null",
                    "confidence": "0.0-1.0 — how sure you are about this interpretation",
                }
            ],
        },
    }


def interpret_fields(
    fields: List[FieldEvidence],
    page_text: Optional[str] = None,
) -> Dict[str, Any]:
    from .llm import _generate, _get_client, load_env_file

    load_env_file()
    prompt = build_interpretation_prompt(fields, page_text)
    client = _get_client()

    if client is None:
        return {
            "mode": "dry_run_no_gemini_api_key",
            "fields": [
                {
                    "field_idx": f.field_idx,
                    "field_id": f.field_id,
                    "field_context": None,
                    "expected_value_shape": "unknown",
                    "sensitive": False,
                    "format_hint": None,
                    "preferred_option": None,
                    "confidence": 0.0,
                }
                for f in fields
            ],
        }

    return _generate(client, prompt, system_prompt=SYSTEM_PROMPT)
