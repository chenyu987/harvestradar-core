"""Which ChatApp groups to grab: the list kept in the admin app.

The 求单 (Request) page in the admin app (chenyu987/admin-app, migration 0030) holds the
group list; whoever runs the business adds, renames and pauses groups there. The collector
asks for the active ones at the start of each grab, through the same ingest function and
token it sends requests with (GET returns only id and name of active groups).

If the admin app can't be reached, the copy saved last time (<out_dir>/groups.json) is
used, and if there has never been one, the groups in collector.toml. A network hiccup
should not stop a grab someone asked for.
"""

import json
from pathlib import Path

import httpx

CACHE = "groups.json"


def fetch(url: str, token: str, client: httpx.Client | None = None) -> list[dict]:
    """Active groups as [{id, name}]. Raises on any failure."""
    resp = (client or httpx.Client(timeout=15)).get(url, headers={"x-ingest-token": token})
    resp.raise_for_status()
    groups = resp.json()["groups"]
    return [{"id": str(g["id"]), "name": str(g["name"])} for g in groups if g.get("id") and g.get("name")]


def resolve(out_dir: Path, toml_groups: list[str], url: str, token: str,
            client: httpx.Client | None = None) -> tuple[list[dict], str]:
    """(groups, where they came from). Groups from collector.toml have no id."""
    cache = out_dir / CACHE
    if url and token:
        try:
            groups = fetch(url, token, client)
            out_dir.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(groups, ensure_ascii=False, indent=1))
            return groups, "后台群名单"
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            pass
        if cache.exists():
            return json.loads(cache.read_text()), "上次存下的群名单（这次连不上后台）"
    return [{"id": None, "name": g} for g in toml_groups], "collector.toml"


def ids_by_name(out_dir: Path) -> dict[str, str]:
    """Group name -> id, from the last list fetched. Requests are tagged with the group they came from."""
    cache = out_dir / CACHE
    if not cache.exists():
        return {}
    return {g["name"]: g["id"] for g in json.loads(cache.read_text()) if g.get("id")}
