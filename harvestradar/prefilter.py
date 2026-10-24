"""Local prefilter: drop screenshots that clearly hold no item request before paying for a cloud call.

Runs on the Mac next to ChatApp, free and offline:
1. OCR the screenshot with macOS Vision (via the `ocrmac` package).
2. Keyword gate: no item / rent words at all -> drop without asking a model.
3. A small local model (Qwen3 via Ollama) reads the OCR text and answers
   "is anyone asking for an item here?".

The filter fails open: any OCR or model error keeps the screenshot, because
a dropped real request is lost money while an extra cloud call costs cents.

Bump PREFILTER_VERSION whenever the keywords or the prompt change.
"""

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

PREFILTER_VERSION = "v2"
DEFAULT_LOCAL_MODEL = "qwen3:4b"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

# Lenient on purpose: this only throws away screens with no hint of an item at all.
# OCR often splits or misreads words, so keep the list short and broad.
KEYWORDS = [
    "beta", "alpha", "item", "物品", "货",
    "求", "租", "借", "rent", "borrow", "need",
]

# A line that both asks and names an item is kept without asking the model. The model
# misreads busy screens (offers, carpools and one English request mixed together),
# and a lost request costs more than a cloud call.
ASKING = re.compile(r"求|想租|想借|有没有|有沒有|looking for|need|anyone (have|has|got)|lf\b", re.I)
ITEM_WORDS = re.compile(r"beta|alpha|item|物品", re.I)

CLASSIFY_PROMPT = """\
下面是一张群聊截图的 OCR 文字，顺序和排版可能有点乱。
判断里面有没有人「想租 / 借 / 求」Alpha 或 Beta 物品。

算：「求一张女beta」「有人出alpha吗」「想借一下货」「need a beta item」。
不算：出货、出租物品（「出女beta」「有货可租」）；拼车、找车（「人找车」「车找人」）；换汇；闲聊；广告。
名字里带「出货」但消息本身是在求货的，算。拿不准就算有。

只输出 JSON：{"has_request": true 或 false, "reason": "一句话说明"}
"""

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {"has_request": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["has_request", "reason"],
}


@dataclass
class PrefilterResult:
    keep: bool
    stage: str  # "keywords" | "rule" | "llm" | "error": which step made the call
    reason: str
    ocr_text: str
    ocr_seconds: float
    llm_seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


def ocr(path: str | Path) -> str:
    """macOS Vision OCR, lines in reading order (top to bottom, left to right)."""
    from ocrmac import ocrmac  # macOS only

    annotations = ocrmac.OCR(str(path), language_preference=["zh-Hans", "en-US"]).recognize()
    # Vision boxes are (x, y, w, h) normalized with the origin at bottom-left.
    annotations.sort(key=lambda a: (-round(a[2][1] + a[2][3], 2), a[2][0]))
    return "\n".join(a[0] for a in annotations)


def keyword_hit(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in KEYWORDS)


def sure_request(text: str) -> str:
    """The first OCR line that asks for an item, or ""."""
    return next((l for l in text.splitlines() if ASKING.search(l) and ITEM_WORDS.search(l)), "")


def classify(text: str, model: str = DEFAULT_LOCAL_MODEL, client: httpx.Client | None = None) -> tuple[bool, str, int, int]:
    """Ask a local Ollama model whether the OCR text holds an item request."""
    client = client or httpx.Client(timeout=120)
    resp = client.post(f"{OLLAMA_URL}/api/chat", json={
        "model": model,
        "messages": [
            {"role": "system", "content": CLASSIFY_PROMPT},
            {"role": "user", "content": text},
        ],
        "format": CLASSIFY_SCHEMA,
        "think": False,  # Qwen3 thinking mode is slow and not needed for a yes/no
        "stream": False,
        "options": {"temperature": 0},
    })
    resp.raise_for_status()
    body = resp.json()
    answer = json.loads(body["message"]["content"])
    return (bool(answer["has_request"]), answer.get("reason", ""),
            body.get("prompt_eval_count", 0), body.get("eval_count", 0))


def prefilter(path: str | Path, model: str = DEFAULT_LOCAL_MODEL, use_llm: bool = True,
              ocr_text: str | None = None, client: httpx.Client | None = None) -> PrefilterResult:
    """Decide whether a screenshot should go on to cloud extraction.

    Pass ocr_text to skip OCR (e.g. text cached from an earlier run).
    """
    t0 = time.monotonic()
    try:
        text = ocr_text if ocr_text is not None else ocr(path)
    except Exception as e:  # fail open
        return PrefilterResult(True, "error", f"ocr: {e}", "", time.monotonic() - t0)
    ocr_seconds = time.monotonic() - t0

    if not keyword_hit(text):
        return PrefilterResult(False, "keywords", "no item keywords", text, ocr_seconds)
    if line := sure_request(text):
        return PrefilterResult(True, "rule", f"asks for an item: {line[:60]}", text, ocr_seconds)
    if not use_llm:
        return PrefilterResult(True, "keywords", "keyword hit", text, ocr_seconds)

    t1 = time.monotonic()
    try:
        keep, reason, in_tok, out_tok = classify(text, model, client)
    except Exception as e:  # fail open
        return PrefilterResult(True, "error", f"llm: {e}", text, ocr_seconds, time.monotonic() - t1)
    return PrefilterResult(keep, "llm", reason, text, ocr_seconds, time.monotonic() - t1, in_tok, out_tok)
