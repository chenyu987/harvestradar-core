"""Collector logic tests with a fake ChatApp. Run: python -m unittest discover tests"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

from harvestradar.collector.core import Collector, Config, HumanActive, KillSwitch, Stopped
from harvestradar.collector.mac import Layout, Line, Window, detect_layout, find_sidebar_rows, sidebar_rows
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


class SidebarTest(unittest.TestCase):
    """Chat list as OCR saw it on the real Mac (2026-10-24), in window points."""
    WIN = Window(1, 0, 30, 1440, 780)
    LINES = [Line("Search", 100, 20, 60, 16), Line("68", 92, 70, 14, 12),
             Line("工作群", 109, 80, 50, 16), Line("Remy：好的", 109, 98, 80, 16), Line("00:17", 300, 80, 30, 14),
             Line("alpha item东区拼单", 109, 140, 120, 16), Line("［Draft］ ［Photo］", 109, 158, 120, 16),
             Line("城东Beta Alpha互助群", 111, 200, 150, 16), Line("Ann：城东Beta Alpha互助群 欢迎", 111, 218, 200, 16),
             Line("本地活动租物品｜壹", 109, 260, 120, 16), Line("Mia：求2女1男beta", 109, 278, 150, 16),
             Line("城东Beta Alpha互助群", 600, 20, 150, 16)]  # chat title: not in the list

    def test_rows_and_drafts(self):
        rows = sidebar_rows(self.LINES, Layout(), self.WIN)
        self.assertEqual([r.name.text for r in rows], ["工作群", "alpha item东区拼单", "城东Beta Alpha互助群", "本地活动租物品｜壹"])
        self.assertEqual([r.draft for r in rows], [False, True, False, False])

    def test_only_names_match(self):
        hits = find_sidebar_rows(self.LINES, "城东Beta Alpha互助群", Layout(), self.WIN)
        self.assertEqual([(h.x, h.y) for h in hits], [(111, 200)])  # not the preview line, not the title
        self.assertEqual(len(find_sidebar_rows(self.LINES, GROUP, Layout(), self.WIN)), 1)
        self.assertEqual(find_sidebar_rows(self.LINES, "城西活动群", Layout(), self.WIN), [])


class LayoutTest(unittest.TestCase):
    def test_detects_dividers(self):
        # dark ChatApp at 2x: sidebar grey, 1 pt lines at x=300, y=60 and y=780-150
        win = Window(1, 0, 0, 1200, 780)
        img = Image.new("L", (2400, 1560), 25)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, 599, 1559], fill=40)
        d.rectangle([600, 0, 601, 1559], fill=48)
        d.rectangle([602, 120, 2399, 121], fill=48)
        d.rectangle([602, 1260, 2399, 1261], fill=48)
        d.rectangle([800, 300, 1400, 340], fill=200)  # a message bubble
        lay = detect_layout(img, win, Layout())
        self.assertEqual((lay.sidebar_right, lay.header_bottom, lay.input_top_from_bottom), (300, 61, 150))
        self.assertIsNone(detect_layout(Image.new("L", (2400, 1560), 25), win, Layout()))


class NoKeyboardTest(unittest.TestCase):
    def test_mac_backend_cannot_type(self):
        """The collector must never send a key (see mac.py). Fail on anything that could."""
        src = (Path(__file__).parent.parent / "harvestradar/collector/mac.py").read_text()
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        code = code.split('"""', 2)[2]  # skip the module docstring, which talks about keys
        for banned in ("Keyboard", "pyautogui", "hotkey", "pbcopy", "keystroke", "key code", "typewrite",
                       "press(", "keyDown", "keyUp", "CGEventKeyboard"):
            self.assertNotIn(banned, code, banned)


class FakeMac:
    """A ChatApp whose chat list is `sidebar` (one list of names per list screen)
    and whose open chat shows `pages[i]` after i scrolls up."""

    def __init__(self, tmp: Path, pages: list[list[str]], title=GROUP + " (497)", sidebar=None):
        self.tmp, self.pages, self.title = tmp, pages, title
        self.sidebar = sidebar or [[GROUP]]
        self.list_pos = 0
        self.pos = 0
        self.win = Window(1, 0, 25, 1440, 780)
        self.layout = Layout()
        self.extra = 0
        self.idle = 3600.0
        self.draft_after_click = None  # a chat that shows "[Draft]" once something was clicked
        self.actions = []
        self.clicks = []

    def activate(self): pass
    def window(self): return self.win
    def extra_windows(self): return self.extra
    def user_active(self): return False
    def idle_seconds(self): return self.idle
    def key_idle_seconds(self): return self.idle
    def screen_locked(self): return False
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
        names = self.ocr_sidebar() if self.sidebar is None else self.sidebar[min(self.list_pos, len(self.sidebar) - 1)]
        for i, name in enumerate(names):
            clicked = any(a[0] == "click" for a in self.actions)
            preview = "[Draft] hi" if clicked and name == self.draft_after_click else "someone: hello"
            lines += [Line(name, 110, 90 + 60 * i, 150, 16), Line(preview, 110, 108 + 60 * i, 150, 16)]
        return lines + [Line(t, 600, 100 + 40 * i, 200, 20) for i, t in enumerate(page)]

    def click(self, win, line):
        self.actions.append(("click", line.text))
        self.pos = 0

    def scroll_sidebar(self, win, clicks):
        self.actions.append(("list", clicks))
        self.list_pos = 0 if clicks > 0 else self.list_pos + 1

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
        [r] = self.collector(mac).round()
        # first run looks back 3 days (to 1/25 15:00): page 3 has 1/20, so it stops there
        self.assertEqual((r.stop, len(r.screens)), ("checkpoint", 4))
        self.assertEqual(r.newest, "2026-01-28T14:30:00")
        self.assertIn(("click", GROUP), mac.actions)
        self.assertEqual(sum(a == ("scroll",) for a in mac.actions), 3)
        self.assertTrue(all(Path(s.file).exists() for s in r.screens))

        # next round, nothing new: same first screen, so nothing is saved
        [r2] = self.collector(FakeMac(self.tmp, pages)).round()
        self.assertEqual((r2.stop, len(r2.screens)), ("no_new", 0))

    def test_days_goes_back_past_the_bookmark(self):
        # A quiet group: the last message is from 1/20, so the bookmark is there already.
        pages = [["1/20 09:00", "hi"], ["1/15 10:00", "求 beta"], ["1/10 08:00", "出货"], ["1/2 08:00"]]
        [r] = self.collector(FakeMac(self.tmp, pages)).round()
        self.assertEqual(len(r.screens), 1)  # older than 3 days on the first screen: stop

        col = self.collector(FakeMac(self.tmp, pages))
        col.lookback_days = 14  # back to 1/14
        [r2] = col.round()
        self.assertEqual((r2.stop, len(r2.screens)), ("checkpoint", 3))  # 1/10 is past 1/14
        # the bookmark stays at the newest message, not 14 days ago
        self.assertEqual(col.state[GROUP]["checkpoint"], "2026-01-20T09:00:00")

    def test_finds_groups_down_the_chat_list(self):
        self.cfg.groups = [GROUP, "城西活动群", "没有新消息的群"]
        sidebar = [["工作群", "Remy"], ["城西活动群", "Lucas"], [GROUP, "闲聊群"], [GROUP, "闲聊群"]]
        mac = FakeMac(self.tmp, [["14:30"], ["13:00"]], sidebar=sidebar)
        mac.title = "城西活动群 (80)"
        col = self.collector(mac)
        # the title must follow the click
        orig = mac.click
        def click(win, line):
            orig(win, line)
            mac.title = line.text + " (80)"
        mac.click = click
        results = col.round()
        self.assertEqual(sorted(r.group for r in results), sorted([GROUP, "城西活动群"]))
        self.assertEqual([a[1] for a in mac.actions if a[0] == "click"], ["城西活动群", GROUP])
        log = (self.tmp / "out" / "log.jsonl").read_text()
        self.assertIn('"not_in_recent", "group": "没有新消息的群"', log)

    def test_sidebar_scroll_learns_rows_per_click(self):
        # A long chat list where 10 wheel clicks move one row, as on the M1 Air (2026-10-24).
        chats = [f"聊天{i:02d}" for i in range(60)]
        chats[40] = GROUP
        mac = FakeMac(self.tmp, [["14:30"], ["13:00"]])
        mac.sidebar = None
        top = [0]
        mac.ocr_sidebar = lambda: chats[top[0]:top[0] + 12]
        def scroll_sidebar(win, clicks):
            mac.actions.append(("list", clicks))
            top[0] = 0 if clicks > 0 else min(len(chats) - 12, top[0] + (-clicks) // 10)
        mac.scroll_sidebar = scroll_sidebar
        col = self.collector(mac)
        results = col.round()
        self.assertEqual([r.group for r in results], [GROUP])  # row 41 found within 5 screens
        steps = [-a[1] for a in mac.actions if a[0] == "list" and a[1] < 0]
        self.assertEqual(steps[0], 30)
        self.assertEqual(steps[1], 100)  # moved 3 rows for 30 clicks; wants 10 rows per screen

    def test_grab_waits_for_hands_off_then_runs_once(self):
        mac = FakeMac(self.tmp, [["14:30"], ["13:00"], ["Yesterday 10:02"]])
        idle = iter([0, 4, 12])  # just pressed Enter, still near the Mac, then stepped away
        mac.idle_seconds = lambda: next(idle, 3600)
        col = self.collector(mac)
        waits = []
        col.sleep = waits.append
        results = col.grab()
        self.assertEqual([r.group for r in results], [GROUP])
        self.assertEqual(waits[:2], [2, 2])  # waited twice before starting
        self.assertEqual(sum(a[0] == "click" for a in mac.actions), 1)  # one item, no repeat

    def test_grab_gives_up_if_nobody_steps_away(self):
        mac = FakeMac(self.tmp, [["14:30"]])
        mac.idle = 1
        self.cfg.start_wait_minutes = 1
        col = self.collector(mac)
        with self.assertRaises(HumanActive):
            col.grab()
        self.assertEqual(mac.actions, [])

    def test_grab_stops_on_stop_file_while_waiting(self):
        mac = FakeMac(self.tmp, [["14:30"]])
        mac.idle = 1
        col = self.collector(mac)
        col.sleep = lambda s: col.stop_file.write_text("stopped by hand\n")  # someone runs `stop`
        with self.assertRaises(Stopped):
            col.grab()
        self.assertEqual(mac.actions, [])
        self.assertNotIn("KILL", (self.tmp / "out" / "log.jsonl").read_text())  # a stop by hand is not a kill

    def test_old_timer_settings_are_ignored(self):
        cfg_path = self.tmp / "old.toml"
        cfg_path.write_text('[collector]\ngroups = ["a"]\ninterval_minutes = [30, 60]\nrun_hours = 3\n'
                            'active_hours = [9, 23]\nidle_minutes = 5\n')
        self.assertEqual(Config.load(cfg_path).groups, ["a"])

    def test_waits_for_nobody_at_the_mac(self):
        mac = FakeMac(self.tmp, [["14:30"]])
        mac.idle = 3
        with self.assertRaises(HumanActive):
            self.collector(mac).round()
        self.assertEqual(mac.actions, [])  # ChatApp was not even brought to the front

    def test_ambiguous_name_is_not_clicked(self):
        mac = FakeMac(self.tmp, [["14:30"]], sidebar=[[GROUP, GROUP + "二群"]])
        self.assertEqual(self.collector(mac).round(), [])
        self.assertFalse([a for a in mac.actions if a[0] == "click"])

    def test_kill_on_new_draft(self):
        mac = FakeMac(self.tmp, [["14:30"], ["13:00"]], sidebar=[[GROUP, "工作群"]])
        mac.draft_after_click = "工作群"
        with self.assertRaises(KillSwitch):
            self.collector(mac).round()

    def test_old_draft_is_only_logged(self):
        mac = FakeMac(self.tmp, [["14:30"], ["13:00"]], sidebar=[[GROUP, "工作群"]])
        mac.draft_after_click = "工作群"
        mac.actions.append(("click", "earlier"))  # draft already there when the run starts
        self.assertEqual(len(self.collector(mac).round()), 1)
        self.assertIn("draft_present", (self.tmp / "out" / "log.jsonl").read_text())

    def test_scroll_grows_when_screens_overlap(self):
        pages = [["14:30", "aaaa", "bbbb"], ["14:00", "aaaa", "bbbb"], ["13:00", "cccc", "aaaa"],
                 ["12:00", "dddd", "eeee"], ["11:00"]]
        mac = FakeMac(self.tmp, pages)
        self.collector(mac).round()
        # page 1 shares 2/3 with page 0 -> bigger; page 2 shares 1/3 -> same; page 3 shares none -> smaller
        self.assertEqual(mac.clicks, [30, 46, 46, 27])

    def test_stops_when_screen_stops_changing(self):
        [r] = self.collector(FakeMac(self.tmp, [["14:30"], ["13:00"]])).round()
        self.assertEqual((r.stop, len(r.screens)), ("unchanged", 2))

    def test_max_screens(self):
        [r] = self.collector(FakeMac(self.tmp, [["14:30"]] + [[f"{12 - i}:00"] for i in range(9)])).round()
        self.assertEqual((r.stop, len(r.screens)), ("max_screens", 5))

    def test_kill_on_danger_words(self):
        with self.assertRaises(KillSwitch):
            self.collector(FakeMac(self.tmp, [["14:30"], ["你的账号功能受限"]])).round()

    def test_kill_on_wrong_chat(self):
        with self.assertRaises(KillSwitch):
            self.collector(FakeMac(self.tmp, [["14:30"]], title="Rina")).round()

    def test_kill_on_dialog_and_stop_file(self):
        mac = FakeMac(self.tmp, [["14:30"]])
        mac.extra = 1
        col = self.collector(mac)
        with self.assertRaises(KillSwitch):
            col.round()
        col.kill("test")
        self.assertTrue(col.stop_file.exists())
        with self.assertRaises(KillSwitch):
            self.collector(FakeMac(self.tmp, [["14:30"]])).round()


if __name__ == "__main__":
    unittest.main()
