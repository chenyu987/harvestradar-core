"""Score the local prefilter, and compare "all to cloud" with "prefilter, then cloud".

Usage (on the Mac, with Ollama running and the model pulled):
    python -m evals.run_prefilter --data /path/to/dataset --local-model qwen3:4b \\
        --cloud-run runs/20261024-101500_gemini-3.5-flash_v1

The prefilter is judged on two things:
- lost leads: labeled requests on screenshots it dropped. This must stay near 0;
  a dropped real request never reaches the cloud model and is lost for good.
- drop rate: how many screenshots it saves from a cloud call.

--cloud-run (repeatable) points at an earlier run_eval output folder. No new cloud
calls are made: the hybrid numbers reuse that run's per-screenshot results, with
dropped screenshots counted as zero cost and their labeled requests as misses.
"""

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from evals.run_eval import PRICES, fmt, ratio
from harvestradar.prefilter import DEFAULT_LOCAL_MODEL, PREFILTER_VERSION, prefilter


def cloud_totals(rows: list[dict], model: str) -> dict:
    tp = sum(r["score"]["tp"] for r in rows)
    fp = sum(r["score"]["fp"] for r in rows)
    fn = sum(r["score"]["fn"] for r in rows)
    in_tok = sum(r["input_tokens"] for r in rows)
    out_tok = sum(r["output_tokens"] for r in rows)
    pin, pout = PRICES.get(model, (0.0, 0.0))
    p, r = ratio(tp, tp + fp), ratio(tp, tp + fn)
    return {
        "cloud_calls": sum(1 for r in rows if not r.get("dropped")),
        "tp": tp, "fp": fp, "fn": fn,
        "precision": p, "recall": r,
        "f1": 2 * p * r / (p + r) if p and r else None,
        "input_tokens": in_tok, "output_tokens": out_tok,
        "cost_usd": round(in_tok / 1e6 * pin + out_tok / 1e6 * pout, 4),
    }


def compare_with_cloud(run_dir: Path, kept: dict[str, bool]) -> dict:
    model = json.loads((run_dir / "summary.json").read_text())["model"]
    rows = [json.loads(l) for l in (run_dir / "results.jsonl").read_text().splitlines() if l.strip()]
    rows = [r for r in rows if r["file"] in kept]

    hybrid = []
    for r in rows:
        if kept[r["file"]]:
            hybrid.append(r)
        else:
            s = r["score"]
            # Never sent to the cloud: no tokens, no alerts, every labeled request missed.
            hybrid.append({**r, "dropped": True, "input_tokens": 0, "output_tokens": 0,
                           "score": {**s, "tp": 0, "fp": 0, "fn": s["tp"] + s["fn"]}})

    cloud, mixed = cloud_totals(rows, model), cloud_totals(hybrid, model)
    tokens = cloud["input_tokens"] + cloud["output_tokens"]
    return {
        "model": model,
        "run": str(run_dir),
        "all_cloud": cloud,
        "prefilter_then_cloud": mixed,
        "calls_saved": ratio(cloud["cloud_calls"] - mixed["cloud_calls"], cloud["cloud_calls"]),
        "tokens_saved": ratio(tokens - mixed["input_tokens"] - mixed["output_tokens"], tokens),
        "cost_saved": ratio(cloud["cost_usd"] - mixed["cost_usd"], cloud["cost_usd"]),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="dataset folder with labels.jsonl and screenshots/")
    ap.add_argument("--local-model", default=DEFAULT_LOCAL_MODEL, help="Ollama model name")
    ap.add_argument("--no-llm", action="store_true", help="keyword gate only, no local model")
    ap.add_argument("--cloud-run", action="append", default=[], help="run_eval output folder to compare against")
    ap.add_argument("--out", default="runs")
    args = ap.parse_args()

    data = Path(args.data)
    labels = [json.loads(l) for l in (data / "labels.jsonl").read_text().splitlines() if l.strip()]

    # OCR output does not depend on the model or keywords, so cache it across runs.
    ocr_cache = Path(args.out) / "cache" / "ocr"
    ocr_cache.mkdir(parents=True, exist_ok=True)

    rows = []
    for gold in labels:
        cached = ocr_cache / f"{gold['file']}.txt"
        text = cached.read_text() if cached.exists() else None
        res = prefilter(data / "screenshots" / gold["file"], model=args.local_model,
                        use_llm=not args.no_llm, ocr_text=text)
        if text is None and res.stage != "error":
            cached.write_text(res.ocr_text)
        n_leads = len(gold.get("leads", []))
        print(f"  {gold['file']} {'keep' if res.keep else 'drop'} ({res.stage}) leads={n_leads}"
              f"{'  LOST' if n_leads and not res.keep else ''}  {res.reason}", flush=True)
        rows.append({
            "file": gold["file"], "keep": res.keep, "stage": res.stage, "reason": res.reason,
            "gold_leads": n_leads, "ocr_seconds": round(res.ocr_seconds, 2),
            "llm_seconds": round(res.llm_seconds, 2),
            "input_tokens": res.input_tokens, "output_tokens": res.output_tokens,
            "ocr_text": res.ocr_text,
        })

    total_leads = sum(r["gold_leads"] for r in rows)
    lost = [r for r in rows if r["gold_leads"] and not r["keep"]]
    empty = [r for r in rows if not r["gold_leads"]]
    secs = sorted(r["ocr_seconds"] + r["llm_seconds"] for r in rows)
    local_model = "keywords-only" if args.no_llm else args.local_model

    summary = {
        "local_model": local_model,
        "prefilter_version": PREFILTER_VERSION,
        "screenshots": len(rows),
        "kept": sum(r["keep"] for r in rows),
        "drop_rate": ratio(sum(not r["keep"] for r in rows), len(rows)),
        "leads": total_leads,
        "leads_lost": sum(r["gold_leads"] for r in lost),
        "lead_recall": ratio(total_leads - sum(r["gold_leads"] for r in lost), total_leads),
        "empty_screens_dropped": ratio(sum(not r["keep"] for r in empty), len(empty)),
        "errors": sum(r["stage"] == "error" for r in rows),
        "median_seconds": secs[len(secs) // 2] if secs else None,
        "comparisons": [compare_with_cloud(Path(d), {r["file"]: r["keep"] for r in rows}) for d in args.cloud_run],
    }

    run_dir = Path(args.out) / f"{datetime.now():%Y%m%d-%H%M%S}_prefilter_{local_model.replace(':', '-')}_{PREFILTER_VERSION}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    with (run_dir / "results.jsonl").open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"prefilter={local_model} {PREFILTER_VERSION} screenshots={len(rows)}")
    print(f"dropped {fmt(summary['drop_rate'])} of screenshots, {fmt(summary['empty_screens_dropped'])} of empty ones")
    print(f"lead recall {fmt(summary['lead_recall'])} (lost {summary['leads_lost']} of {total_leads})"
          f"  median {summary['median_seconds']}s/shot  errors={summary['errors']}")
    for r in lost:
        print(f"  LOST {r['file']}: {r['gold_leads']} lead(s), {r['stage']}: {r['reason']}")

    if summary["comparisons"]:
        print()
        print("| cloud model | setup | cloud calls | precision | recall | F1 | tokens | $ |")
        print("|---|---|---|---|---|---|---|---|")
        for c in summary["comparisons"]:
            for name, t in (("all to cloud", c["all_cloud"]), (f"{local_model} → cloud", c["prefilter_then_cloud"])):
                print(f"| {c['model']} | {name} | {t['cloud_calls']} | {fmt(t['precision'])} | {fmt(t['recall'])} "
                      f"| {fmt(t['f1'])} | {t['input_tokens'] + t['output_tokens']} | {t['cost_usd']} |")
        print()
        for c in summary["comparisons"]:
            print(f"saved with {c['model']}: calls {fmt(c['calls_saved'])}, tokens {fmt(c['tokens_saved'])},"
                  f" cost {fmt(c['cost_saved'])}")
    print(f"saved to {run_dir}")


if __name__ == "__main__":
    main()
