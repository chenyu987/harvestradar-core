"""Turn what the collector saved into leads: inbox -> prefilter -> cloud extraction -> leads.jsonl.

Usage (on the Mac, after the collector has run):
    python -m harvestradar.process                       # prefilter + gemini-3.5-flash
    python -m harvestradar.process --model claude-haiku-5-5
    python -m harvestradar.process --no-cloud            # prefilter only; send later

Reads every <out_dir>/inbox/<date>/manifest.jsonl the collector wrote and handles
each screenshot once:
1. Prefilter on the OCR text the collector already saved (no second OCR). The
   local model is used when Ollama answers; otherwise keywords only. Fails open.
2. Screenshots that item go to the cloud extractor (same prompt and schema as the evals).
   A screenshot whose text matches one already sent (same group, same OCR text,
   e.g. a quiet group grabbed again) is not sent twice.
3. Requests (kind == "request") are appended to <out_dir>/leads.jsonl, deduplicated
   by group + sender + quote. Neighboring screens overlap, so the same message is
   usually seen twice; only the first sighting is kept.

<out_dir>/processed.jsonl records what happened to each screenshot. A screenshot
counts as done once it was dropped or extracted; one that was kept but not sent
(no key, --no-cloud) or hit an error is tried again next time.

Nothing here touches ChatApp. Like the rest of data/, both files hold real names
and stay out of git.
"""

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from harvestradar.collector.safety import normalize
from harvestradar.prefilter import DEFAULT_LOCAL_MODEL, OLLAMA_URL, PREFILTER_VERSION, prefilter
from harvestradar.prompt import PROMPT_VERSION

DEFAULT_CLOUD_MODEL = "gemini-3.5-flash"
DONE = {"dropped", "extracted", "same_as_sent"}
MAX_ERRORS_IN_A_ROW = 3  # the API is down or the key is bad; stop instead of burning through the inbox


def read_jsonl(path: Path) -> list[dict]:
    """Lines of a JSONL file. A half-written last line (the collector may be writing) is skipped."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def append_jsonl(path: Path, row: dict) -> None:
    with path.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def lead_key(group: str, lead: dict) -> str:
    return "|".join([normalize(group), normalize(lead.get("sender", "")), normalize(lead.get("quote", ""))])


def screen_key(shot: dict) -> str:
    """Same group + same text on screen = same screenshot, as far as the extractor cares."""
    text = normalize(shot.get("ocr_text") or "")
    if not text:
        return ""  # no OCR text to compare; always send
    return hashlib.sha1(f"{normalize(shot.get('group', ''))}|{text}".encode()).hexdigest()


def ollama_up(client: httpx.Client | None = None) -> bool:
    try:
        return (client or httpx.Client(timeout=3)).get(f"{OLLAMA_URL}/api/tags").status_code == 200
    except httpx.HTTPError:
        return False


def cloud_key_missing(model: str) -> str:
    var = "GEMINI_API_KEY" if model.startswith("gemini") else "ANTHROPIC_API_KEY"
    return "" if os.environ.get(var) else var


@dataclass
class Totals:
    screenshots: int = 0
    dropped: int = 0
    extracted: int = 0
    pending: int = 0
    same_as_sent: int = 0
    errors: int = 0
    new_leads: int = 0
    duplicates: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def process(out_dir: Path, cloud_model: str | None, local_model: str | None,
            extract=None, limit: int = 0, now=datetime.now, log=print) -> Totals:
    """Handle every screenshot in the inbox that is not done yet.

    cloud_model None: prefilter only, kept screenshots wait for a later run.
    local_model None: keyword gate only.
    extract: the cloud call, extract_leads(path, model) -> ExtractResult; swapped out in tests.
    """
    if extract is None and cloud_model:
        from harvestradar.extract import extract_leads as extract
    processed_path, leads_path = out_dir / "processed.jsonl", out_dir / "leads.jsonl"
    processed = read_jsonl(processed_path)
    done = {r["file"] for r in processed if r.get("status") in DONE}
    seen = {lead_key(r["group"], r) for r in read_jsonl(leads_path)}

    shots = []
    for manifest in sorted((out_dir / "inbox").glob("*/manifest.jsonl")):
        shots += [s for s in read_jsonl(manifest) if s.get("file")]
    extracted = {r["file"] for r in processed if r.get("status") == "extracted"}
    sent = {k: s["file"] for s in shots if s["file"] in extracted and (k := screen_key(s))}
    todo = [s for s in shots if s["file"] not in done]
    if limit:
        todo = todo[:limit]

    t = Totals()
    errors_in_a_row = 0
    for shot in todo:
        t.screenshots += 1
        path = Path(shot["file"])
        record = {"file": shot["file"], "group": shot.get("group", ""), "at": now().isoformat(timespec="seconds"),
                  "prefilter_version": PREFILTER_VERSION}
        if not path.exists():
            t.errors += 1
            append_jsonl(processed_path, {**record, "status": "error", "error": "screenshot missing"})
            log(f"  {path.name}  缺图，跳过")
            continue

        key = screen_key(shot)
        if key in sent:
            t.same_as_sent += 1
            append_jsonl(processed_path, {**record, "status": "same_as_sent", "same_as": sent[key]})
            log(f"  {path.name}  和已送过的 {Path(sent[key]).name} 一样，不再送")
            continue

        pf = prefilter(path, model=local_model or DEFAULT_LOCAL_MODEL, use_llm=bool(local_model),
                       ocr_text=shot.get("ocr_text"))
        record.update(keep=pf.keep, stage=pf.stage, reason=pf.reason)
        if not pf.keep:
            t.dropped += 1
            append_jsonl(processed_path, {**record, "status": "dropped"})
            log(f"  {path.name}  丢掉（{pf.stage}：{pf.reason}）")
            continue
        if not cloud_model:
            t.pending += 1
            append_jsonl(processed_path, {**record, "status": "pending_cloud"})
            log(f"  {path.name}  放行（{pf.stage}：{pf.reason}），等送云端")
            continue

        try:
            res = extract(path, cloud_model)
            error = "refused" if res.refused else res.error
        except Exception as e:  # keep going; this screenshot is retried next run
            res, error = None, f"{type(e).__name__}: {e}"
        if error:
            t.errors += 1
            errors_in_a_row += 1
            append_jsonl(processed_path, {**record, "status": "error", "model": cloud_model, "error": error})
            log(f"  {path.name}  云端出错：{error}")
            if error == "daily_quota" or errors_in_a_row >= MAX_ERRORS_IN_A_ROW:
                log("云端连续出错，先停下，剩下的下次再处理。")
                break
            continue
        errors_in_a_row = 0

        t.extracted += 1
        if key:
            sent[key] = shot["file"]
        t.input_tokens += res.input_tokens
        t.output_tokens += res.output_tokens
        requests = [l for l in res.leads if l.get("kind") == "request"]
        new = 0
        for lead in requests:
            key = lead_key(shot.get("group", ""), lead)
            if key in seen:
                t.duplicates += 1
                continue
            seen.add(key)
            new += 1
            append_jsonl(leads_path, {**lead, "group": shot.get("group", ""), "file": shot["file"],
                                      "captured_at": shot.get("captured_at"), "found_at": record["at"],
                                      "model": res.model, "prompt_version": res.prompt_version})
            log(f"  ★ {shot.get('group', '')} · {lead.get('sender', '')}：{lead.get('item_type', '')} "
                f"{lead.get('location', '')} {','.join(lead.get('dates') or [])} "
                f"男{lead.get('male', 0)}女{lead.get('female', 0)}  「{lead.get('quote', '')}」")
        t.new_leads += new
        append_jsonl(processed_path, {**record, "status": "extracted", "model": res.model,
                                      "prompt_version": res.prompt_version, "requests": len(requests),
                                      "new_leads": new, "input_tokens": res.input_tokens,
                                      "output_tokens": res.output_tokens})
        log(f"  {path.name}  抽取：{len(requests)} 条求单，新的 {new} 条")
    return t


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m harvestradar.process")
    ap.add_argument("--out-dir", default="data/collector", help="the collector's out_dir")
    ap.add_argument("--model", default=DEFAULT_CLOUD_MODEL, help="cloud extractor model")
    ap.add_argument("--no-cloud", action="store_true", help="prefilter only; kept screenshots wait for a later run")
    ap.add_argument("--local-model", default=DEFAULT_LOCAL_MODEL, help="Ollama model for the prefilter")
    ap.add_argument("--no-llm", action="store_true", help="keyword gate only, no local model")
    ap.add_argument("--limit", type=int, default=0, help="handle at most this many screenshots")
    args = ap.parse_args()

    cloud = None if args.no_cloud else args.model
    if cloud and (var := cloud_key_missing(cloud)):
        print(f"没有设 {var}，这次只做初筛，放行的截图等下次送云端。")
        cloud = None
    local = None if args.no_llm else args.local_model
    if local and not ollama_up():
        print(f"连不上 Ollama（{OLLAMA_URL}），这次只用关键词初筛。")
        local = None
    print(f"初筛：{local or '只用关键词'} {PREFILTER_VERSION}；云端：{cloud or '不送'}"
          + (f" {PROMPT_VERSION}" if cloud else ""))

    out = Path(args.out_dir)
    t = process(out, cloud, local, limit=args.limit)
    print(f"\n处理了 {t.screenshots} 张：丢掉 {t.dropped}，和送过的一样 {t.same_as_sent}，抽取 {t.extracted}，"
          f"等送云端 {t.pending}，出错 {t.errors}")
    if cloud:
        print(f"新求单 {t.new_leads} 条（重复的 {t.duplicates} 条没记），token {t.input_tokens}+{t.output_tokens}")
    print(f"线索在 {out / 'leads.jsonl'}，处理记录在 {out / 'processed.jsonl'}")

    from harvestradar import leads_sync
    url, token = leads_sync.settings()
    if url and token:
        sent, added = leads_sync.push(out, url, token)
        if sent:
            print(f"送到后台「求单」页：{sent} 条，新增 {added} 条")


if __name__ == "__main__":
    main()
