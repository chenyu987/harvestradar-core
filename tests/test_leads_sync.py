"""Sending leads to the admin app, against a fake server. Run: python -m unittest discover tests"""

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from harvestradar.leads_sync import dedup_key, push, to_row

LEAD = {"sender": "Alex", "kind": "request", "item_type": "beta", "location": "riverton", "dates": ["03-14"],
        "male": 1, "female": 0, "quote": "求一张男 beta 3/14", "sent_at": "3/9 21:26", "notes": "",
        "group": "本地活动租物品｜壹", "file": "data/collector/inbox/20260128/150000_x_00.png",
        "captured_at": "2026-01-28T15:00:00", "model": "gemini-test", "prompt_version": "v1"}


class FakeServer:
    def __init__(self, fail=False):
        self.fail, self.calls, self.rows = fail, [], {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.fail:
            return httpx.Response(500, json={"error": "down"})
        if request.headers.get("x-ingest-token") != "tok":
            return httpx.Response(401, json={"error": "bad token"})
        new = [r for r in json.loads(request.content)["leads"] if r["dedup_key"] not in self.rows]
        self.rows.update((r["dedup_key"], r) for r in new)
        return httpx.Response(200, json={"received": 1, "inserted": len(new)})


class LeadsSyncTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)

    def write(self, *leads):
        with (self.out / "leads.jsonl").open("a") as f:
            for l in leads:
                f.write(json.dumps(l, ensure_ascii=False) + "\n")

    def push(self, server, token="tok"):
        return push(self.out, "https://x/functions/v1/ingest-leads", token,
                    client=httpx.Client(transport=httpx.MockTransport(server)), log=lambda *_: None)

    def test_sends_once(self):
        self.write(LEAD, {**LEAD, "sender": "Sam", "quote": "求女 alpha"})
        server = FakeServer()
        self.assertEqual(self.push(server), (2, 2))
        self.assertEqual(self.push(server), (0, 0))  # already sent: no request at all
        self.assertEqual(len(server.calls), 1)

        self.write({**LEAD, "sender": "Kim", "quote": "求 beta"})
        self.assertEqual(self.push(server), (1, 1))

    def test_failure_is_retried_next_time(self):
        self.write(LEAD)
        self.assertEqual(self.push(FakeServer(fail=True)), (0, 0))
        self.assertEqual(self.push(FakeServer()), (1, 1))

    def test_wrong_token_sends_nothing(self):
        self.write(LEAD)
        self.assertEqual(self.push(FakeServer(), token="nope"), (0, 0))
        self.assertFalse((self.out / "leads_synced.txt").exists())

    def test_row_shape(self):
        row = to_row({**LEAD, "item_type": "season", "male": -1})
        self.assertEqual(row["dedup_key"], dedup_key(LEAD))
        self.assertEqual((row["group_name"], row["screenshot"]), ("本地活动租物品｜壹", "150000_x_00.png"))
        self.assertEqual((row["item_type"], row["male"]), ("unknown", 0))  # the table's check would reject these
        self.assertTrue(row["captured_at"].startswith("2026-01-28T15:00:00"))
        self.assertRegex(row["captured_at"], r"[+-]\d\d:\d\d$")  # local zone added

    def test_same_message_same_key_regardless_of_spacing(self):
        self.assertEqual(dedup_key(LEAD), dedup_key({**LEAD, "quote": "求一张男beta 3/14 "}))
        self.assertNotEqual(dedup_key(LEAD), dedup_key({**LEAD, "group": "城西活动群"}))


if __name__ == "__main__":
    unittest.main()
