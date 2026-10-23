"""Run the extractor over a labeled screenshot set and score it.

Usage:
    python -m evals.run_eval --data /path/to/dataset --model claude-sonnet-5-5

The dataset folder holds labels.jsonl plus a screenshots/ folder. It is kept
out of git because the screenshots contain real people's names.

Scoring, per screenshot:
- Only kind == "request" counts as a lead. A predicted request is matched to a
  labeled one by sender name.
- precision = matched / predicted requests  (how many alerts are real)
- recall    = matched / labeled requests    (how many real requests we caught)
- For matched leads, each field (item type, location, dates, party) is scored.
- Labeled non-requests ("others") show which distractors got mislabeled.
"""

import argparse
import json
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import anthropic

from harvestradar.extract import DEFAULT_MODEL, extract_leads
from harvestradar.prompt import PROMPT_VERSION

# USD per million tokens (input, output). Approximate: ignores cache discounts.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}
# Gemini models run on the free tier for now, so they cost $0 here.

LOCATION_ALIASES = {"lks": "lakeside", "lakesidenorth": "lakeside", "hillcresteast": "hillcrest"}


def norm_name(s: str) -> str:
    # Emoji in ChatApp names (群友🐚) are easy for a model to drop or misread; ignore them.
    s = unicodedata.normalize("NFKC", s or "")
    return "".join(c for c in s if not c.isspace() and unicodedata.category(c) not in ("So", "Sk", "Cf")).lower()


def norm_location(s: str) -> str:
    s = "".join((s or "").lower().split()).replace("-", "")
    return LOCATION_ALIASES.get(s, s)


def norm_dates(ds: list[str]) -> set[str]:
    out = set()
    for d in ds or []:
        d = d.strip().replace("/", "-")
        parts = d.split("-")[-2:]
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            out.add(f"{int(parts[0]):02d}-{int(parts[1]):02d}")
        else:
            out.add(d)
    return out


def accepts(gold_value, pred_value, norm=lambda x: x) -> bool:
    # A label can list several acceptable answers when the message is ambiguous.
    options = gold_value if isinstance(gold_value, list) else [gold_value]
    return any(norm(o) == norm(pred_value) for o in options)


def score_one(gold: dict, pred_leads: list[dict]) -> dict:
    gold_req = {norm_name(g["sender"]): g for g in gold.get("leads", [])}
    # "ignore": requests too cut off to label either way; neither credit nor penalty.
    ignored = {norm_name(n) for n in gold.get("ignore", [])}
    pred_req = {}
    for p in pred_leads:
        k = norm_name(p.get("sender", ""))
        if p.get("kind") == "request" and k not in ignored:
            pred_req.setdefault(k, p)

    matched = sorted(gold_req.keys() & pred_req.keys())
    fields = {"item_type": [], "location": [], "dates": [], "party": []}
    for k in matched:
        g, p = gold_req[k], pred_req[k]
        fields["item_type"].append(accepts(g.get("item_type", "unknown"), p.get("item_type")))
        fields["location"].append(accepts(g.get("location", ""), p.get("location", ""), norm_location))
        # dates: null in the label means the request gives no exact days; skip scoring it.
        if g.get("dates") is not None:
            fields["dates"].append(norm_dates(g["dates"]) == norm_dates(p.get("dates", [])))
        fields["party"].append((g.get("male", 0), g.get("female", 0)) == (p.get("male"), p.get("female")))

    pred_kinds = {norm_name(p.get("sender", "")): p.get("kind") for p in pred_leads}
    others = [
        {"sender": o["sender"], "gold": o["kind"], "pred": pred_kinds.get(norm_name(o["sender"]), "(none)")}
        for o in gold.get("others", [])
    ]
    return {
        "tp": len(matched),
        "fp": len(pred_req.keys() - gold_req.keys()),
        "fn": len(gold_req.keys() - pred_req.keys()),
        "missed": [gold_req[k]["sender"] for k in gold_req.keys() - pred_req.keys()],
        "false_alarms": [pred_req[k].get("sender") for k in pred_req.keys() - gold_req.keys()],
        "fields": fields,
        "others": others,
    }


def ratio(a: int, b: int) -> float | None:
    return a / b if b else None


def fmt(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="dataset folder with labels.jsonl and screenshots/")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--out", default="runs")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    data = Path(args.data)
    labels = [json.loads(l) for l in (data / "labels.jsonl").read_text().splitlines() if l.strip()]
    if args.model.startswith("gemini"):
        from google import genai
        client = genai.Client()
        args.workers = 1  # free-tier rate limits
    else:
        client = anthropic.Anthropic()

    # Successful extractions are cached per model + prompt version, so a run cut short by
    # rate limits can be resumed later without paying for the same screenshots again.
    cache = Path(args.out) / "cache" / f"{args.model}_{PROMPT_VERSION}"
    cache.mkdir(parents=True, exist_ok=True)
    quota_hit = False

    def run(gold: dict) -> dict:
        nonlocal quota_hit
        cached = cache / f"{gold['file']}.json"
        if cached.exists():
            row = json.loads(cached.read_text())
        elif quota_hit:
            row = {"file": gold["file"], "seconds": 0, "input_tokens": 0, "output_tokens": 0,
                   "refused": False, "error": "daily_quota", "pred": []}
        else:
            t0 = time.monotonic()
            res = extract_leads(data / "screenshots" / gold["file"], model=args.model, client=client)
            print(f"  {gold['file']} done in {time.monotonic() - t0:.0f}s{' ERROR ' + res.error if res.error else ''}", flush=True)
            quota_hit = quota_hit or res.error == "daily_quota"
            row = {
                "file": gold["file"],
                "seconds": round(time.monotonic() - t0, 2),
                "input_tokens": res.input_tokens,
                "output_tokens": res.output_tokens,
                "refused": res.refused,
                "error": res.error,
                "pred": res.leads,
            }
            if not res.error:
                cached.write_text(json.dumps(row, ensure_ascii=False))
        row["score"] = score_one(gold, row["pred"])
        return row

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(run, labels))

    tp = sum(r["score"]["tp"] for r in rows)
    fp = sum(r["score"]["fp"] for r in rows)
    fn = sum(r["score"]["fn"] for r in rows)
    precision, recall = ratio(tp, tp + fp), ratio(tp, tp + fn)
    f1 = 2 * precision * recall / (precision + recall) if precision and recall else None
    field_acc = {}
    for f in ("item_type", "location", "dates", "party"):
        vals = [v for r in rows for v in r["score"]["fields"][f]]
        field_acc[f] = ratio(sum(vals), len(vals))
    in_tok = sum(r["input_tokens"] for r in rows)
    out_tok = sum(r["output_tokens"] for r in rows)
    pin, pout = PRICES.get(args.model, (0.0, 0.0))
    cost = in_tok / 1e6 * pin + out_tok / 1e6 * pout
    secs = sorted(r["seconds"] for r in rows)

    summary = {
        "model": args.model,
        "prompt_version": PROMPT_VERSION,
        "screenshots": len(rows),
        "tp": tp, "fp": fp, "fn": fn,
        "precision": precision, "recall": recall, "f1": f1,
        "field_accuracy": field_acc,
        "input_tokens": in_tok, "output_tokens": out_tok,
        "cost_usd": round(cost, 4),
        "cost_per_screenshot_usd": round(cost / len(rows), 5) if rows else None,
        "median_seconds": secs[len(secs) // 2] if secs else None,
        "refusals": sum(r["refused"] for r in rows),
        "errors": sum(bool(r["error"]) for r in rows),
    }

    run_dir = Path(args.out) / f"{datetime.now():%Y%m%d-%H%M%S}_{args.model}_{PROMPT_VERSION}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    with (run_dir / "results.jsonl").open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"model={args.model} prompt={PROMPT_VERSION} screenshots={len(rows)}")
    print(f"precision={fmt(precision)} recall={fmt(recall)} f1={fmt(f1)}  (tp={tp} fp={fp} fn={fn})")
    print("field accuracy: " + ", ".join(f"{k}={fmt(v)}" for k, v in field_acc.items()))
    print(f"cost=${cost:.4f} (${summary['cost_per_screenshot_usd']}/shot)  median {summary['median_seconds']}s/shot")
    for r in rows:
        s = r["score"]
        if s["missed"] or s["false_alarms"] or r["error"] or r["refused"]:
            print(f"  {r['file']}: missed={s['missed']} false_alarms={s['false_alarms']}"
                  f"{' error=' + r['error'] if r['error'] else ''}{' REFUSED' if r['refused'] else ''}")
    print(f"saved to {run_dir}")


if __name__ == "__main__":
    main()
