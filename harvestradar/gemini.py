"""Screenshot -> structured leads, via the Gemini API (same prompt and schema as Claude)."""

import json
import mimetypes
import time
from pathlib import Path

import httpx
from google import genai
from google.genai import errors, types

from .extract import ExtractResult
from .prompt import LEAD_SCHEMA, PROMPT_VERSION, SYSTEM_PROMPT

# The free tier allows only a few requests per minute, so back off on 429.
# 500/503 ("high demand") are transient too, so retry them the same way.
_RETRY_WAITS = [15, 30, 60, 60]
_RETRY_CODES = {429, 500, 503}


def extract_leads_gemini(path: Path, model: str, client: genai.Client | None = None) -> ExtractResult:
    client = client or genai.Client()  # reads GEMINI_API_KEY
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    contents = [types.Part.from_bytes(data=path.read_bytes(), mime_type=mime), "找出这张群聊截图里的相关消息。"]
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_json_schema=LEAD_SCHEMA,
        max_output_tokens=16000,
    )

    for wait in [*_RETRY_WAITS, None]:
        try:
            response = client.models.generate_content(model=model, contents=contents, config=config)
            break
        except errors.APIError as e:
            # The free tier also has a per-day cap; waiting minutes won't help, so stop now.
            if e.code == 429 and "PerDay" in str(e.details):
                return ExtractResult([], model, PROMPT_VERSION, 0, 0, error="daily_quota")
            if e.code not in _RETRY_CODES or wait is None:
                return ExtractResult([], model, PROMPT_VERSION, 0, 0, error=f"{e.code}: {e.message}")
            time.sleep(wait)
        except httpx.TransportError as e:
            # Dropped connections ("Server disconnected") happen under load.
            if wait is None:
                return ExtractResult([], model, PROMPT_VERSION, 0, 0, error=f"transport: {e}")
            time.sleep(wait)

    usage = response.usage_metadata
    result = ExtractResult(
        leads=[],
        model=model,
        prompt_version=PROMPT_VERSION,
        input_tokens=(usage.prompt_token_count or 0) if usage else 0,
        # Thinking tokens are billed as output.
        output_tokens=((usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)) if usage else 0,
    )

    # Check why it stopped before reading the text: a block has no usable output.
    if response.prompt_feedback and response.prompt_feedback.block_reason:
        result.refused = True
        return result
    finish = response.candidates[0].finish_reason if response.candidates else None
    if finish == types.FinishReason.MAX_TOKENS:
        result.error = "max_tokens"
        return result
    if finish not in (None, types.FinishReason.STOP):
        result.refused = True
        result.error = str(finish)
        return result

    try:
        result.raw = json.loads(response.text or "")
        result.leads = result.raw.get("leads", [])
    except json.JSONDecodeError as e:
        result.error = f"bad json: {e}"
    return result
