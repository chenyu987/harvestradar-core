"""Screenshot -> leads with a local model only: macOS Vision OCR + Qwen3 via Ollama.

Same output as the cloud extractors (ExtractResult with the LEAD_SCHEMA leads), so
evals/run_eval.py can score it on the same set: `--model qwen3:4b`.

The model never sees the image, only OCR text. What the image shows and the text
loses is who said what: names and messages come out as lines in the same flat list.
One clue survives: in ChatApp the sender name and the time dividers are small grey
text, the message bubble is normal size. Lines noticeably shorter than the typical
line are marked (小字) so the model can tell a name from a message.
"""

import json
import re
from pathlib import Path

import httpx

from harvestradar.extract import ExtractResult
from harvestradar.prefilter import OLLAMA_URL
from harvestradar.prompt import LEAD_SCHEMA, PROMPT_VERSION, SYSTEM_PROMPT

TEXT_NOTE = """\
注意：你拿到的不是图片，是这张群聊截图的 OCR 文字，从上到下一行一行排好。
标了（小字）的行通常是发言人名字或者时间分隔线；它下面正常字号的行，一般就是这个人发的消息。
下面说的「截图」都指这些文字。
"""


def ocr_lines(path: Path) -> list[tuple[str, float]]:
    """(text, height) per line, top to bottom. Height is a fraction of the image height."""
    from ocrmac import ocrmac  # macOS only

    found = ocrmac.OCR(str(path), language_preference=["zh-Hans", "en-US"]).recognize()
    # Vision boxes are (x, y, w, h) normalized with the origin at bottom-left
    found.sort(key=lambda a: (-round(a[2][1] + a[2][3], 2), a[2][0]))
    return [(a[0], a[2][3]) for a in found]


def layout_text(lines: list[tuple[str, float]]) -> str:
    if not lines:
        return ""
    heights = sorted(h for _, h in lines)
    typical = heights[len(heights) // 2]
    return "\n".join(f"（小字）{t}" if h < typical * 0.85 else t for t, h in lines)


def tidy_dates(dates) -> list[str]:
    """Small models often write 1/26 instead of the asked-for 01-26. Fix the format, nothing else."""
    out = []
    for d in dates if isinstance(dates, list) else []:
        m = re.fullmatch(r"\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?\s*", str(d))
        out.append(f"{int(m[1]):02d}-{int(m[2]):02d}" if m else str(d))
    return out


def extract_leads_local(path: Path, model: str, client: httpx.Client | None = None,
                        text: str | None = None) -> ExtractResult:
    """Pass text to skip OCR (e.g. cached)."""
    try:
        text = text if text is not None else layout_text(ocr_lines(path))
    except Exception as e:
        return ExtractResult([], model, PROMPT_VERSION, 0, 0, error=f"ocr: {e}")
    client = client or httpx.Client(timeout=300)
    try:
        resp = client.post(f"{OLLAMA_URL}/api/chat", json={
            "model": model,
            "messages": [
                {"role": "system", "content": TEXT_NOTE + "\n" + SYSTEM_PROMPT},
                {"role": "user", "content": "找出这张群聊截图里的相关消息。\n\n" + text},
            ],
            "format": LEAD_SCHEMA,
            "think": False,
            "stream": False,
            "options": {"temperature": 0, "num_ctx": 8192},
        })
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        return ExtractResult([], model, PROMPT_VERSION, 0, 0, error=f"ollama: {e}")
    result = ExtractResult([], model, PROMPT_VERSION, body.get("prompt_eval_count", 0), body.get("eval_count", 0))
    try:
        result.raw = json.loads(body["message"]["content"])
        # The （小字） marker is ours, not ChatApp's: models sometimes copy it into the name
        result.leads = [{**l, "dates": tidy_dates(l.get("dates")),
                         "sender": str(l.get("sender", "")).replace("（小字）", "").strip()}
                        for l in result.raw.get("leads", [])]
    except (json.JSONDecodeError, KeyError) as e:
        result.error = f"bad json: {e}"
    return result
