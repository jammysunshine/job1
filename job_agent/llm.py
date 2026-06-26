from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import ROOT

_ENV_LOADED = False


def _load_env() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    env_path = ROOT / ".env"
    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    _ENV_LOADED = True


def _get_model() -> str:
    _load_env()
    return os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


# ---------------------------------------------------------------------------
# Provider helpers
# ---------------------------------------------------------------------------

def _is_vertex() -> bool:
    _load_env()
    return os.environ.get("VERTEX_AI", "").lower() == "true" and bool(os.environ.get("GCP_PROJECT_ID"))


def _vertex_url(model: str) -> str:
    project = os.environ.get("GCP_PROJECT_ID", "")
    location = os.environ.get("GCP_LOCATION", "us-central1")
    return (
        f"https://{location}-aiplatform.googleapis.com/v1beta1/"
        f"projects/{project}/locations/{location}/"
        f"publishers/google/models/{model}:generateContent"
    )


def _vertex_token() -> str:
    return subprocess.check_output(
        ["gcloud", "auth", "print-access-token"],
        stderr=subprocess.DEVNULL,
    ).decode().strip()


def _gemini_key() -> Optional[str]:
    _load_env()
    return (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("GEMINI_LLM_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
    )


def _openrouter_key() -> Optional[str]:
    _load_env()
    return os.environ.get("OPENROUTER_API_KEY")


def _openrouter_url() -> str:
    return os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")


# Free OpenRouter models good at structured JSON output
_FREE_MODELS: List[str] = [
    "openrouter/owl-alpha",
    "google/gemini-2.0-flash-001",
    "google/gemini-2.1-flash-preview",
    "meta-llama/llama-3.3-70b-instruct:free",
    "openai/gpt-oss-120b",
    "qwen/qwen3-235b-a22b-instruct:free",
]


# ---------------------------------------------------------------------------
# Request builders per provider
# ---------------------------------------------------------------------------

def _build_vertex_body(system_prompt: str, parts: list) -> Dict[str, Any]:
    return {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
        "system_instruction": {"parts": [{"text": system_prompt}]},
    }


def _build_gemini_body(system_prompt: str, parts: list) -> Dict[str, Any]:
    return {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseMimeType": "application/json"},
    }


def _build_openrouter_body(system_prompt: str, parts: list, model: str) -> Dict[str, Any]:
    # OpenRouter uses OpenAI-compatible format
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": []},
    ]
    # Flatten parts into user content
    for part in parts:
        if "text" in part:
            messages[1]["content"].append({"type": "text", "text": part["text"]})
        elif "inline_data" in part:
            b64 = part["inline_data"]["data"]
            mime = part["inline_data"]["mime_type"]
            messages[1]["content"].append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })
    return {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def call_llm(
    system_prompt: str,
    user_content: str,
    *,
    image_path: Optional[str] = None,
    max_retries: int = 3,
    timeout: int = 120,
) -> str:
    """Call LLM with text (+ optional image). Returns raw text response.

    Provider priority:
    1. Vertex AI (if VERTEX_AI=true and GCP_PROJECT_ID set)
    2. Gemini Developer API (if GEMINI_LLM_API_KEY or GEMINI_API_KEY set)
    3. OpenRouter free models (if OPENROUTER_API_KEY set, or as fallback on 429)
    """
    import requests

    model = _get_model()
    parts = _build_parts(user_content, image_path)

    # Build ordered list of (name, url, headers, body) attempts
    attempts = _build_attempts(system_prompt, parts, model)

    last_exc: Optional[Exception] = None
    for attempt_name, url, headers, body in attempts:
        for retry in range(max_retries):
            try:
                resp = requests.post(url, headers=headers, json=body, timeout=timeout)
                if resp.status_code == 429:
                    # Rate limited — try next provider immediately
                    raise RuntimeError(f"429 rate limited on {attempt_name}")
                resp.raise_for_status()
                data = resp.json()
                return _extract_text(data, attempt_name)
            except RuntimeError:
                raise  # re-raise our own 429 marker
            except Exception as exc:
                last_exc = exc
                if "429" in str(exc):
                    break  # rate limited, try next provider
                if retry < max_retries - 1:
                    time.sleep(2 ** retry)
        # If we got here with 429, try next provider
        if "429" in str(last_exc):
            continue

    raise RuntimeError(f"All providers failed. Last error: {last_exc}")


def call_llm_json(
    system_prompt: str,
    user_content: str,
    *,
    image_path: Optional[str] = None,
    max_retries: int = 3,
) -> Dict[str, Any]:
    """Call LLM and parse JSON response."""
    raw = call_llm(
        system_prompt,
        user_content,
        image_path=image_path,
        max_retries=max_retries,
    )
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1]) if len(lines) > 2 else raw[3:-3]
        raw = raw.strip()
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_parts(user_content: str, image_path: Optional[str]) -> list:
    parts: list = []
    if image_path and Path(image_path).exists():
        import base64
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        mime = "image/png" if image_path.endswith(".png") else "image/jpeg"
        parts.append({"inline_data": {"mime_type": mime, "data": b64}})
    parts.append({"text": user_content})
    return parts


def _build_attempts(
    system_prompt: str, parts: list, model: str
) -> List[tuple[str, str, dict, dict]]:
    """Return ordered list of (name, url, headers, body) to try."""
    attempts: List[tuple[str, str, dict, dict]] = []

    # 1. Vertex AI
    if _is_vertex():
        attempts.append((
            "vertex",
            _vertex_url(model),
            {
                "Authorization": f"Bearer {_vertex_token()}",
                "Content-Type": "application/json",
            },
            _build_vertex_body(system_prompt, parts),
        ))

    # 2. Gemini Developer API
    gkey = _gemini_key()
    if gkey:
        attempts.append((
            "gemini",
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gkey}",
            {"Content-Type": "application/json"},
            _build_gemini_body(system_prompt, parts),
        ))

    # 3. OpenRouter free models
    or_key = _openrouter_key()
    if or_key:
        for free_model in _FREE_MODELS:
            attempts.append((
                f"openrouter/{free_model}",
                _openrouter_url(),
                {
                    "Authorization": f"Bearer {or_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://your-app.com",
                    "X-Title": "Job Application Agent",
                },
                _build_openrouter_body(system_prompt, parts, free_model),
            ))

    if not attempts:
        raise RuntimeError(
            "No LLM provider configured. Set one of:\n"
            "  - VERTEX_AI=true + GCP_PROJECT_ID (Vertex AI)\n"
            "  - GEMINI_LLM_API_KEY or GEMINI_API_KEY (Gemini API)\n"
            "  - OPENROUTER_API_KEY (OpenRouter, free tier)"
        )

    return attempts


def _extract_text(data: Dict[str, Any], provider: str) -> str:
    """Extract text content from provider response."""
    if "choices" in data:
        # OpenAI / OpenRouter format
        return data["choices"][0]["message"]["content"]
    elif "candidates" in data:
        # Gemini / Vertex format
        candidate = data["candidates"][0]
        parts = candidate.get("content", {}).get("parts", [{}])
        return parts[0].get("text", "") if parts else ""
    raise RuntimeError(f"Unexpected response from {provider}: {json.dumps(data)[:500]}")
