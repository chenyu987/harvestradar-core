"""inbox -> prefilter -> cloud -> leads, with a fake cloud model. Run: python -m unittest discover tests"""

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from harvestradar.extract import ExtractResult
from harvestradar.process import process, read_jsonl

GROUP = "本地活动租物品｜壹"
REQUEST = {"sender": "Alex", "kind": "request", "item_type": "beta", "location": "riverton",
           "dates": ["03-14"], "male": 1, "female": 0, "quote": "求一张男 beta 3/14 riverton",
           "sent_at": "3/9 21:26", "notes": ""}
OFFER = {**REQUEST, "sender": "物主", "kind": "offer", "quote": "出女 beta"}


class FakeCloud:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, path, model):
        self.calls.append(path.name)
        answer = self.answers[path.name]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, str):
            return ExtractResult([], model, "v1", 0, 0, error=answer)
        return ExtractResult(answer, model, "v1", 100, 10)


class ProcessTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(tmp.name)  # the manifest stores paths relative to where the collector ran
        self.out = Path("data/collector")
        self.day = self.out / "inbox" / "20260128"
        self.day.mkdir(parents=True)

    def shot(self, name, text, group=GROUP):
        path = self.day / name
        path.write_bytes(b"png")
        with (self.day / "manifest.jsonl").open("a") as f:
            f.write(json.dumps({"file": str(path), "group": group, "index": 0,
                                "captured_at": "2026-01-28T15:00:00", "dividers": [],
                                "ocr_text": text}, ensure_ascii=False) + "\n")

    def run_once(self, cloud, model="gemini-test"):
        return process(self.out, model, None, extract=cloud, now=lambda: datetime(2026, 1, 28, 15, 5),
                       log=lambda *_: None)

    def test_pipeline(self):
        self.shot("a_00.png", "Alex\n求一张男 beta 3/14 riverton")
        self.shot("a_01.png", "Alex\n求一张男 beta 3/14 riverton\n物主\n出女 beta")  # overlaps a_00
        self.shot("b_00.png", "周末去哪玩\n下雨了")  # no keyword: never reaches the cloud
        cloud = FakeCloud({"a_00.png": [REQUEST], "a_01.png": [REQUEST, OFFER]})
        t = self.run_once(cloud)

        self.assertEqual(cloud.calls, ["a_00.png", "a_01.png"])
        self.assertEqual((t.screenshots, t.dropped, t.extracted, t.new_leads, t.duplicates), (3, 1, 2, 1, 1))
        leads = read_jsonl(self.out / "leads.jsonl")
        self.assertEqual(len(leads), 1)
        self.assertEqual((leads[0]["sender"], leads[0]["group"]), ("Alex", GROUP))
        self.assertTrue(leads[0]["file"].endswith("a_00.png"))

        # Second run: everything is done, nothing is sent again.
        cloud2 = FakeCloud({})
        t2 = self.run_once(cloud2)
        self.assertEqual((t2.screenshots, cloud2.calls), (0, []))

    def test_same_quote_other_group_is_a_new_lead(self):
        self.shot("a_00.png", "求 beta")
        self.shot("c_00.png", "求 beta", group="城西活动群")
        t = self.run_once(FakeCloud({"a_00.png": [REQUEST], "c_00.png": [REQUEST]}))
        self.assertEqual((t.new_leads, t.duplicates), (2, 0))

    def test_no_cloud_waits_then_sends(self):
        self.shot("a_00.png", "求 beta")
        t = process(self.out, None, None, log=lambda *_: None)
        self.assertEqual((t.pending, t.extracted), (1, 0))
        self.assertFalse((self.out / "leads.jsonl").exists())

        cloud = FakeCloud({"a_00.png": [REQUEST]})
        t2 = self.run_once(cloud)
        self.assertEqual((cloud.calls, t2.new_leads), (["a_00.png"], 1))

    def test_errors_are_retried_and_stop_the_run(self):
        for n in range(5):
            self.shot(f"a_{n:02d}.png", "求 beta")
        cloud = FakeCloud({f"a_{n:02d}.png": RuntimeError("down") for n in range(5)})
        t = self.run_once(cloud)
        self.assertEqual((len(cloud.calls), t.errors), (3, 3))  # gives up after 3 in a row

        cloud2 = FakeCloud({f"a_{n:02d}.png": [] for n in range(5)})
        t2 = self.run_once(cloud2)
        self.assertEqual((len(cloud2.calls), t2.extracted), (5, 5))

    def test_daily_quota_stops_at_once(self):
        self.shot("a_00.png", "求 beta")
        self.shot("a_01.png", "求 beta")
        cloud = FakeCloud({"a_00.png": "daily_quota", "a_01.png": []})
        t = self.run_once(cloud)
        self.assertEqual((cloud.calls, t.errors), (["a_00.png"], 1))

    def test_missing_screenshot_and_partial_manifest_line(self):
        self.shot("a_00.png", "求 beta")
        (self.day / "a_00.png").unlink()
        with (self.day / "manifest.jsonl").open("a") as f:
            f.write('{"file": "half-writ')  # the collector is mid-write
        t = self.run_once(FakeCloud({}))
        self.assertEqual((t.screenshots, t.errors), (1, 1))


if __name__ == "__main__":
    unittest.main()
