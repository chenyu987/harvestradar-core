"""Print a comparison table of eval runs.

Usage:
    python -m evals.compare runs/*/summary.json
"""

import json
import sys
from pathlib import Path


def pct(x):
    return "n/a" if x is None else f"{x:.0%}"


def main() -> None:
    rows = [json.loads(Path(p).read_text()) for p in sys.argv[1:]]
    header = "| model | prompt | n | precision | recall | F1 | dates | party | $/shot | s/shot |"
    print(header)
    print("|" + "---|" * (header.count("|") - 1))
    for s in rows:
        fa = s["field_accuracy"]
        print(
            f"| {s['model']} | {s['prompt_version']} | {s['screenshots']} | {pct(s['precision'])} "
            f"| {pct(s['recall'])} | {pct(s['f1'])} | {pct(fa['dates'])} | {pct(fa['party'])} "
            f"| {s['cost_per_screenshot_usd']} | {s['median_seconds']} |"
        )


if __name__ == "__main__":
    main()
