"""Send new leads from data/collector/leads.jsonl to the admin app's Request (求单) page.

Usage (process does this by itself at the end when the two settings are there):
    python -m harvestradar.leads_sync

Settings, from the environment or a .env file in the repo root (not in git):
    LEADS_API_URL    https://<project>.supabase.co/functions/v1/ingest-chat-requests
    LEADS_API_TOKEN  the token set on that function; it can only add leads

leads.jsonl stays the record on this Mac. Keys already sent are listed in
<out_dir>/leads_synced.txt; a failed send is simply tried again next time.
The server skips rows it already has (same dedup_key), so sending twice is harmless.
"""

import argparse
import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from harvestradar.collector.timeparse import parse_divider
from harvestradar.groups import ids_by_name
from harvestradar.process import lead_key, read_jsonl

BATCH = 100
ITEM_TYPES = {"alpha", "beta", "unknown"}


def load_dotenv(path: Path = Path(".env")) -> None:
    """KEY=value lines; values already in the environment win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def dedup_key(lead: dict) -> str:
    return hashlib.sha1(lead_key(lead.get("group", ""), lead).encode()).hexdigest()


def local_iso(naive: str | None) -> str | None:
    """The collector writes local time without a zone; the database wants one."""
    if not naive:
        return None
    try:
        return datetime.fromisoformat(naive).astimezone().isoformat()
    except ValueError:
        return None


def sent_time(sent_at: str | None, captured_at: str | None) -> str | None:
    """ChatApp's divider text ("Thursday 02:07", "Yesterday 10:02", "4/18 16:15") as an absolute
    time, counted back from when the screenshot was taken. ChatApp shows this Mac's local time,
    and only this Mac knows that zone, so it is worked out here and not in the admin app.
    Unreadable text, or a time after the screenshot (a bare "12:53" on a screen grabbed at 01:24
    can't be today), gives None and the admin page shows the text as it was."""
    if not sent_at or not captured_at:
        return None
    try:
        taken = datetime.fromisoformat(captured_at)
    except ValueError:
        return None
    when = parse_divider(sent_at.strip(), taken)
    if when is None or when > taken + timedelta(minutes=5):
        return None
    return when.astimezone().isoformat()


def to_row(lead: dict, group_ids: dict[str, str] | None = None) -> dict:
    count = lambda v: v if isinstance(v, int) and v >= 0 else 0
    return {
        "group_id": (group_ids or {}).get(lead.get("group", "")),
        "dedup_key": dedup_key(lead),
        "group_name": lead.get("group", ""),
        "sender": lead.get("sender", ""),
        "item_type": lead.get("item_type") if lead.get("item_type") in ITEM_TYPES else "unknown",
        "location": lead.get("location") or None,
        "dates": [d for d in lead.get("dates") or [] if isinstance(d, str)],
        "male": count(lead.get("male")),
        "female": count(lead.get("female")),
        "quote": lead.get("quote", ""),
        "sent_at": lead.get("sent_at") or None,
        "sent_time": sent_time(lead.get("sent_at"), lead.get("captured_at")),
        "extractor_note": lead.get("notes") or None,
        "captured_at": local_iso(lead.get("captured_at")),
        "screenshot": Path(lead["file"]).name if lead.get("file") else None,
        "model": lead.get("model"),
        "prompt_version": lead.get("prompt_version"),
    }


def push(out_dir: Path, url: str, token: str, client: httpx.Client | None = None, log=print) -> tuple[int, int]:
    """Send leads not sent yet. Returns (sent, newly added on the server)."""
    synced_path = out_dir / "leads_synced.txt"
    synced = set(synced_path.read_text().split()) if synced_path.exists() else set()
    ids = ids_by_name(out_dir)
    rows = [r for r in (to_row(l, ids) for l in read_jsonl(out_dir / "leads.jsonl"))
            if r["dedup_key"] not in synced and r["sender"] and r["quote"]]
    if not rows:
        return 0, 0
    client = client or httpx.Client(timeout=30)
    sent = added = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        try:
            resp = client.post(url, json={"requests": batch}, headers={"x-ingest-token": token})
            resp.raise_for_status()
            added += resp.json().get("inserted", 0)
        except (httpx.HTTPError, ValueError) as e:
            log(f"求单没送上后台（下次再送）：{e}")
            break
        with synced_path.open("a") as f:
            f.writelines(r["dedup_key"] + "\n" for r in batch)
        sent += len(batch)
    return sent, added


def settings() -> tuple[str, str]:
    load_dotenv()
    return os.environ.get("LEADS_API_URL", ""), os.environ.get("LEADS_API_TOKEN", "")


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m harvestradar.leads_sync")
    ap.add_argument("--out-dir", default="data/collector")
    args = ap.parse_args()
    url, token = settings()
    if not (url and token):
        raise SystemExit("没设 LEADS_API_URL / LEADS_API_TOKEN（环境变量或仓库根目录的 .env）")
    sent, added = push(Path(args.out_dir), url, token)
    print(f"送了 {sent} 条，后台新增 {added} 条")


if __name__ == "__main__":
    main()
