"""Collector logic tests with a fake ChatApp. Run: python -m unittest discover tests"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

from harvestradar.collector.core import Collector, Config, KillSwitch
from harvestradar.collector.mac import Layout, Line, Window
from harvestradar.collector.safety import danger_hits, title_matches
from harvestradar.collector.timeparse import parse_divider

NOW = datetime(2026, 1, 28, 15, 0)  # a Wednesday
GROUP = "本地活动租物品｜壹"


class TimeParseTest(unittest.TestCase):
    def check(self, text, want):
        self.assertEqual(parse_divider(text, NOW), want, text)

    def test_formats(self):
        self.check("09:47", datetime(2026, 1, 28, 9, 47))
        self.check("Yesterday 10:02", datetime(2026, 1, 27, 10, 2))
        self.check("昨天 10:02", datetime(2026, 1, 27, 10, 2))
        self.check("Monday 08:00", datetime(2026, 1, 26, 8, 0))
        self.check("星期三 08:00", datetime(2026, 1, 21, 8, 0))  # same weekday = a week ago
        self.check("周日 21:30", datetime(2026, 1, 25, 21, 30))
        self.check("1/25 09:47", datetime(2026, 1, 25, 9, 47))
        self.check("12/30 09:47", datetime(2025, 12, 30, 9, 47))  # future date = last year
        self.check("1月25日 09:47", datetime(2026, 1, 25, 9, 47))
        self.check("2025/3/9 21:26", datetime(2025, 3, 9, 21, 26))
        self.check("2025年3月9日 下午 2:05", datetime(2025, 3, 9, 14, 5))
        self.check("2:05 PM", datetime(2026, 1, 28, 14, 5))

    def test_not_dividers(self):
        for t in ["求一男一女 2/21 -2/22 Alpha", "Alex", "13/45 25:99", "1/31-2/1 女生 Oakdale", ""]:
            self.assertIsNone(parse_divider(t, NOW), t)


class SafetyTest(unittest.TestCase):
    def test_danger(self):
        self.assertTrue(danger_hits(["你的账号功能受限，请验证"]))
        self.assertTrue(danger_hits(["Please log in again"]))
        self.assertFalse(danger_hits(["扫码进群", "求租 beta", "登录 beta app 看一下"]))

    def test_title(self):
        self.assertTrue(title_matches(["本地活动租物品｜壹 (497)"], GROUP))
        self.assertTrue(title_matches(["本地活动租物品 | 壹(497)"], GROUP))
        self.assertTrue(title_matches(["本地活动租..."], GROUP))
        self.assertFalse(title_matches(["城西活动群 (300)"], GROUP))


class FakeMac:
    """A ChatApp with one group. `pages[i]` is the chat pane after i scrolls up."""

    def __init__(self, tmp: Path, pages: list[list[str]], title=GROUP + " (497)"):
        self.tmp, self.pages, self.title = tmp, pages, title
        self.pos = 0
        self.results_after = 0  # search results show up on this capture of the search screen
        self.search_looks = 0
        self.win = Window(1, 0, 25, 1440, 780)
        self.extra = 0
        self.actions = []
        self.clicks = []

    def activate(self): pass
    def window(self): return self.win
    def extra_windows(self): return self.extra
    def user_active(self): return False
    def notify(self, *a): self.actions.append(("notify",) + a)

    def capture(self, name):
        img = Image.new("RGB", (2880, 1560), "black")
        page = min(self.pos, len(self.pages) - 1)
        ImageDraw.Draw(img).rectangle([800, 200 + 90 * page, 2700, 300 + 90 * page], fill="white")
        p = self.tmp / f"{name}.png"
        img.save(p)
        return p

    def ocr(self, path, win):
        page = self.pages[min(self.pos, len(self.pages) - 1)]
        lines = [Line(self.title, 360, 20, 300, 20)]
        if path.stem.startswith("search"):
            lines += [Line(GROUP, 120, 30, 200, 20)]
            if self.search_looks >= self.results_after:
                lines += [Line("Group Chats", 120, 90, 100, 16), Line(GROUP, 160, 120, 200, 20)]
            self.search_looks += 1
        return lines + [Line(t, 600, 100 + 40 * i, 200, 20) for i, t in enumerate(page)]

    def search(self, group): self.actions.append(("search", group))
    def nudge_search(self): self.actions.append(("nudge",))
    def click(self, win, line): self.actions.append(("click", line.text))
    def scroll_up(self, win, clicks):
        self.actions.append(("scroll",))
        self.clicks.append(clicks)
        self.pos += 1


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(groups=[GROUP], out_dir=str(self.tmp / "out"), max_screens=5, layout=Layout())

    def collector(self, mac):
        return Collector(self.cfg, mac, now=lambda: NOW, sleep=lambda s: None)

    def test_stops_at_checkpoint_and_saves_state(self):
        pages = [["14:30", "求一张 beta"], ["13:00", "出货"], ["Yesterday 10:02", "hi"], ["1/20 09:00"]]
        mac = FakeMac(self.tmp, pages)
        r = self.collector(mac).scan_group(GROUP)
        # first run looks back 3 days (to 1/25 15:00): page 3 has 1/20, so it stops there
        self.assertEqual((r.stop, len(r.screens)), ("checkpoint", 4))
        self.assertEqual(r.newest, "2026-01-28T14:30:00")
        self.assertIn(("click", GROUP), mac.actions)
        self.assertEqual(sum(a == ("scroll",) for a in mac.actions), 3)
        self.assertTrue(all(Path(s.file).exists() for s in r.screens))

        # next round, nothing new: same first screen, so nothing is saved
        r2 = self.collector(FakeMac(self.tmp, pages)).scan_group(GROUP)
        self.assertEqual((r2.stop, len(r2.screens)), ("no_new", 0))

    def test_waits_for_search_results(self):
        mac = FakeMac(self.tmp, [["14:30"], ["13:00"]])
        mac.results_after = 3  # results only on the 4th look, after a nudge
        self.collector(mac).scan_group(GROUP)
        self.assertIn(("nudge",), mac.actions)
        self.assertIn(("click", GROUP), mac.actions)

        mac = FakeMac(self.tmp, [["14:30"]])
        mac.results_after = 99
        with self.assertRaises(LookupError):
            self.collector(mac).scan_group("本地活动租物品｜壹")

    def test_scroll_grows_when_screens_overlap(self):
        pages = [["14:30", "aaaa", "bbbb"], ["14:00", "aaaa", "bbbb"], ["13:00", "cccc", "aaaa"],
                 ["12:00", "dddd", "eeee"], ["11:00"]]
        mac = FakeMac(self.tmp, pages)
        self.collector(mac).scan_group(GROUP)
        # page 1 shares 2/3 with page 0 -> bigger; page 2 shares 1/3 -> same; page 3 shares none -> smaller
        self.assertEqual(mac.clicks, [30, 46, 46, 27])

    def test_stops_when_screen_stops_changing(self):
        r = self.collector(FakeMac(self.tmp, [["14:30"], ["13:00"]])).scan_group(GROUP)
        self.assertEqual((r.stop, len(r.screens)), ("unchanged", 2))

    def test_max_screens(self):
        r = self.collector(FakeMac(self.tmp, [["14:30"]] + [[f"{12 - i}:00"] for i in range(9)])).scan_group(GROUP)
        self.assertEqual((r.stop, len(r.screens)), ("max_screens", 5))

    def test_kill_on_danger_words(self):
        with self.assertRaises(KillSwitch):
            self.collector(FakeMac(self.tmp, [["14:30"], ["你的账号功能受限"]])).scan_group(GROUP)

    def test_kill_on_wrong_chat(self):
        with self.assertRaises(KillSwitch):
            self.collector(FakeMac(self.tmp, [["14:30"]], title="Rina")).scan_group(GROUP)

    def test_kill_on_dialog_and_stop_file(self):
        mac = FakeMac(self.tmp, [["14:30"]])
        mac.extra = 1
        col = self.collector(mac)
        with self.assertRaises(KillSwitch):
            col.scan_group(GROUP)
        col.kill("test")
        self.assertTrue(col.stop_file.exists())
        with self.assertRaises(KillSwitch):
            self.collector(FakeMac(self.tmp, [["14:30"]])).scan_group(GROUP)


if __name__ == "__main__":
    unittest.main()
