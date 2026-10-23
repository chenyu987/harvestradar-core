"""Screenshot -> structured leads, via Claude vision with a JSON-schema output."""

import base64
import json
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

from .prompt import LEAD_SCHEMA, PROMPT_VERSION, SYSTEM_PROMPT

DEFAULT_MODEL = "claude-opus-5-5"

# Haiku has no server-side refusal fallback; the others get "default" routing.
_NO_FALLBACK_MODELS = {"claude-haiku-5-5"}


@dataclass
class ExtractResult:
    leads: list[dict]
    model: str
    prompt_version: str
    input_tokens: int
    output_tokens: int
    refused: bool = False
    error: str = ""
    raw: dict = field(default_factory=dict)


def _image_block(path: Path) -> dict:
    media_type = mimetypes.guess_type(path.name)[0] or "image/png"
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def extract_leads(image_path: str | Path, model: str = DEFAULT_MODEL, client=None) -> ExtractResult:
    """Route by model name: "gemini-*" goes to Google, everything else to Claude."""
    if model.startswith("gemini"):
        from .gemini import extract_leads_gemini
        return extract_leads_gemini(Path(image_path), model, client)
    return _extract_claude(Path(image_path), model, client)


def _extract_claude(path: Path, model: str, client: anthropic.Anthropic | None) -> ExtractResult:
    client = client or anthropic.Anthropic()

    kwargs: dict = {
        "model": model,
        "max_tokens": 16000,
        "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "messages": [{
            "role": "user",
            "content": [_image_block(path), {"type": "text", "text": "找出这张群聊截图里的相关消息。"}],
        }],
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": LEAD_SCHEMA}},
    }
    if model not in _NO_FALLBACK_MODELS:
        kwargs["betas"] = ["server-side-fallback-2026-07-01"]
        kwargs["fallbacks"] = "default"
        response = client.beta.messages.create(**kwargs)
    else:
        response = client.messages.create(**kwargs)

    usage = response.usage
    result = ExtractResult(
        leads=[],
        model=response.model,
        prompt_version=PROMPT_VERSION,
        input_tokens=(usage.input_tokens or 0)
        + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0),
        output_tokens=usage.output_tokens or 0,
    )

    # Check stop_reason before reading content: a refusal has no usable output.
    if response.stop_reason == "refusal":
        result.refused = True
        return result
    if response.stop_reason == "max_tokens":
        result.error = "max_tokens"
        return result

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        result.raw = json.loads(text)
        result.leads = result.raw.get("leads", [])
    except json.JSONDecodeError as e:
        result.error = f"bad json: {e}"
    return result
