"""The collector loop: open each whitelisted group, scroll up a screen at a time,
save a screenshot of the chat pane per screen, and stop at the right moment.

Stop scrolling a group when any of these happens:
- checkpoint:  a time divider on screen is at or before where last round stopped
- unchanged:   the screen looks the same as the one before (top of the chat)
- max_screens: hit the per-group cap (15 by default)

Stop everything (kill switch) when any of these happens:
- ChatApp shows a danger phrase (功能受限, 重新登录, ...), see safety.py
- the main window disappears, shrinks to login size, or an extra dialog window opens
- the open chat is not the group we clicked
- a chat in the list gains a "[Draft]" it did not have before (something got typed)
- a STOP file exists in the output folder (touch it to stop from another terminal)
- the mouse is pushed into a screen corner (FailSafe)

Groups are found by name in the sidebar chat list, from the top down. A chat
with new messages moves to the top of that list, so a whitelisted group that is
not in the first few screens of it has had no news; it is skipped this round.

A kill writes the STOP file, so nothing runs again until a human deletes it.
Someone using the Mac is not a kill: a round only starts after `idle_minutes`
with no mouse or keyboard input and the screen unlocked, and it is dropped (and
retried next time) as soon as the mouse moves or any key is pressed. That also
keeps a human's typing from ever landing in ChatApp when it comes to the front.

Output, per round, under <out_dir>/inbox/<date>/:
    <time>_<group>_<nn>.png     chat pane only, same crop as the eval set
    manifest.jsonl              one line per screenshot: group, dividers seen, OCR text
"""

import hashlib
import json
import random
import re
import time
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image

from harvestradar.collector.mac import FailSafe, Layout, Line, Window, detect_layout, find_sidebar_rows, inside, sidebar_rows
from harvestradar.collector.safety import danger_hits, normalize, title_matches
from harvestradar.collector.timeparse import parse_divider

COLLECTOR_VERSION = "v1"


class KillSwitch(Exception):
    """Something looks wrong with ChatApp or the account. Stop and wait for a human."""


class HumanActive(Exception):
    """Someone is using the Mac. Drop this round and try again later."""


@dataclass
class Config:
    groups: list[str] = field(default_factory=list)
    max_screens: int = 15
    first_run_days: int = 3
    scroll_clicks: int = 30  # starting value; adjusted per group from how much screens overlap
    interval_minutes: tuple[float, float] = (30, 60)
    active_hours: tuple[int, int] = (9, 23)  # run only from 09:00 to 22:59
    pause_seconds: tuple[float, float] = (1.5, 4.0)  # between scrolls
    group_pause_seconds: tuple[float, float] = (20, 90)  # between groups
    idle_minutes: float = 5  # start a round only after this long with nobody at the Mac
    sidebar_pages: int = 5  # screens of the chat list to look through; quieter groups wait for next round
    sidebar_scroll_clicks: int = 10  # wheel clicks per chat-list screen; a little overlap is fine
    auto_layout: bool = True  # measure the window layout from each screenshot; [layout] is the fallback
    out_dir: str = "data/collector"
    layout: Layout = field(default_factory=Layout)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        raw = tomllib.loads(Path(path).read_text())
        c = raw.get("collector", {})
        cfg = cls(layout=Layout(**raw.get("layout", {})))
        for k, v in c.items():
            if not hasattr(cfg, k):
                raise ValueError(f"unknown setting in {path}: {k}")
            setattr(cfg, k, tuple(v) if isinstance(getattr(cfg, k), tuple) else v)
        if not cfg.groups:
            raise ValueError(f"{path}: list at least one group under [collector] groups")
        return cfg


@dataclass
class ScreenInfo:
    file: str
    group: str
    index: int
    captured_at: str
    dividers: list[str]
    ocr_text: str


@dataclass
class GroupResult:
    group: str
    screens: list[ScreenInfo]
    stop: str  # checkpoint | unchanged | max_screens | no_new
    newest: str | None


def slug(name: str) -> str:
    return re.sub(r"[^\w]+", "-", name).strip("-")[:30] or "group"


def text_key(lines: list[Line]) -> str:
    """Fingerprint of a screen by its OCR text. Text, not pixels: one new short
    message barely changes a pixel hash but always changes the text."""
    return hashlib.sha1("\n".join(l.text for l in lines).encode()).hexdigest()


def crop_chat(full: Path, win: Window, layout: Layout, dest: Path) -> None:
    img = Image.open(full)
    scale = img.width / win.w  # 2 on a Retina screen
    x0, y0, x1, y1 = layout.chat_pane(win)
    img.crop((int(x0 * scale), int(y0 * scale), int(x1 * scale), int(y1 * scale))).save(dest)


class Collector:
    def __init__(self, cfg: Config, backend, now=datetime.now, sleep=time.sleep, rand=random.Random()):
        self.cfg = cfg
        self.mac = backend
        self.now = now
        self.sleep = sleep
        self.rand = rand
        self.out = Path(cfg.out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.state_path = self.out / "state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.layout = cfg.layout
        self.drafts: dict[str, bool] = {}  # chat name -> showed "[Draft]" when last seen
        self.round_started = time.monotonic()

    # ---------- files ----------

    @property
    def stop_file(self) -> Path:
        return self.out / "STOP"

    def log(self, event: str, **kw) -> None:
        line = {"at": self.now().isoformat(timespec="seconds"), "event": event, **kw}
        with open(self.out / "log.jsonl", "a") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        print(f"[{line['at']}] {event} {json.dumps(kw, ensure_ascii=False)}", flush=True)

    def save_state(self) -> None:
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=1))

    def kill(self, reason: str) -> None:
        self.stop_file.write_text(f"{self.now().isoformat()} {reason}\n")
        self.log("KILL", reason=reason)
        self.mac.notify("HarvestRadar 已急停", reason)

    # ---------- checks ----------

    def look(self, name: str, allow_extra: bool = False) -> tuple[Window, Path, list[Line]]:
        """Screenshot + OCR the ChatApp window, and run every safety check on it."""
        if self.stop_file.exists():
            raise KillSwitch("STOP file present")
        if self.mac.user_active():
            raise HumanActive("mouse moved")
        if self.mac.key_idle_seconds() < time.monotonic() - self.round_started:
            raise HumanActive("a key was pressed")
        if self.mac.screen_locked():
            raise HumanActive("screen locked")
        win = self.mac.window()
        if win is None:
            raise KillSwitch("ChatApp window not found (logged out or quit?)")
        if win.w < 700 or win.h < 450:
            raise KillSwitch(f"ChatApp window is {win.w:.0f}x{win.h:.0f}, looks like a login window")
        if not allow_extra and (n := self.mac.extra_windows()):
            raise KillSwitch(f"{n} extra ChatApp window(s) open (dialog?)")
        full = self.mac.capture(name)
        if self.cfg.auto_layout:
            with Image.open(full) as img:
                found = detect_layout(img, win, self.cfg.layout)
            if found and found != self.layout:
                self.log("layout", **asdict(found))
            self.layout = found or self.cfg.layout
            self.mac.layout = self.layout
        lines = self.mac.ocr(full, win)
        if not lines:
            raise KillSwitch("OCR found no text: Screen Recording permission missing, or the screen is locked")
        if hits := danger_hits([l.text for l in lines], ignore=self.cfg.groups):
            raise KillSwitch(f"danger words on screen: {', '.join(sorted(set(hits)))}")
        self.check_drafts(win, lines)
        return win, full, lines

    def check_drafts(self, win: Window, lines: list[Line]) -> None:
        """The collector never types, so a chat that gains a "[Draft]" while it runs
        means a key went somewhere. Drafts already there before are left alone."""
        for row in sidebar_rows(lines, self.layout, win):
            name = normalize(row.name.text)
            if len(name) < 2:
                continue
            if row.draft and self.drafts.get(name) is False:
                raise KillSwitch(f"new draft in chat list: {row.name.text}")
            if row.draft and name not in self.drafts:
                self.log("draft_present", chat=row.name.text, note="was there before this run; please clear it by hand")
            self.drafts[name] = row.draft

    def check_title(self, win: Window, lines: list[Line], group: str) -> None:
        header = [l.text for l in lines if inside(l, self.layout.header(win))]
        if not title_matches(header, group):
            raise KillSwitch(f"open chat is {header[:2]}, expected {group}")

    # ---------- one group ----------

    def scan_group(self, group: str) -> GroupResult:
        """Scan the chat that is open now. It must be `group` (checked on every screen)."""
        now = self.now()
        st = self.state.get(group, {})
        checkpoint = datetime.fromisoformat(st["checkpoint"]) if "checkpoint" in st else now - timedelta(days=self.cfg.first_run_days)

        day_dir = self.out / "inbox" / now.strftime("%Y%m%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        stamp = now.strftime("%H%M%S")
        screens: list[ScreenInfo] = []
        newest: datetime | None = None
        prev_key = None
        prev_lines: set[str] = set()
        clicks = self.cfg.scroll_clicks
        stop = "max_screens"

        for i in range(self.cfg.max_screens):
            win, full, lines = self.look(f"screen{i:02d}")
            self.check_title(win, lines, group)  # every screen, in case something switched chats
            pane = [l for l in lines if inside(l, self.layout.chat_pane(win))]
            key = text_key(pane)
            if i == 0 and key == st.get("first_key"):
                stop = "no_new"
                break
            if key == prev_key:
                stop = "unchanged"
                break
            if i == 0:
                first_key = key
            prev_key = key

            # Two screens in a row should share a few lines. None shared means the
            # scroll may have jumped past messages; mostly shared means it barely moved.
            cur_lines = {t for l in pane if len(t := normalize(l.text)) >= 4}
            if i > 0 and cur_lines:
                shared = len(cur_lines & prev_lines) / len(cur_lines)
                if shared == 0:
                    self.log("no_overlap", group=group, screen=i, clicks=clicks,
                             note="may have skipped messages; scrolling less from here")
                    clicks = max(3, int(clicks * 0.6))
                elif shared > 0.6:
                    clicks = min(150, int(clicks * 1.5) + 1)
            prev_lines = cur_lines

            dest = day_dir / f"{stamp}_{slug(group)}_{i:02d}.png"
            crop_chat(full, win, self.layout, dest)
            dividers = [d for l in pane if (d := parse_divider(l.text, now)) and d <= now + timedelta(minutes=5)]
            if dividers and newest is None:
                newest = max(dividers)  # screens go newest to oldest, so the first divider seen wins
            screens.append(ScreenInfo(str(dest), group, i, now.isoformat(timespec="seconds"),
                                      [d.isoformat() for d in dividers], "\n".join(l.text for l in pane)))
            if dividers and min(dividers) <= checkpoint:
                stop = "checkpoint"
                break
            if i + 1 < self.cfg.max_screens:
                self.mac.scroll_up(win, clicks)
                self.sleep(self.rand.uniform(*self.cfg.pause_seconds))

        if stop == "max_screens":
            self.log("gap", group=group, checkpoint=checkpoint.isoformat(),
                     note="hit max_screens before reaching the checkpoint; older messages skipped")
        if screens:
            with open(day_dir / "manifest.jsonl", "a") as f:
                for s in screens:
                    f.write(json.dumps({**asdict(s), "collector_version": COLLECTOR_VERSION}, ensure_ascii=False) + "\n")
            self.state[group] = {"checkpoint": (newest or checkpoint).isoformat(), "first_key": first_key}
            self.save_state()
        self.log("group_done", group=group, screens=len(screens), stop=stop, last_clicks=clicks,
                 newest=newest.isoformat() if newest else None)
        return GroupResult(group, screens, stop, newest.isoformat() if newest else None)

    # ---------- rounds ----------

    def round(self) -> list[GroupResult]:
        """Walk the chat list from the top. Whenever a whitelisted group is on
        screen, click it and scan it, then look at the list again (a new message
        anywhere reorders it). Groups not seen in `sidebar_pages` screens had no
        news and wait for the next round."""
        todo = set(self.cfg.groups)
        results: list[GroupResult] = []
        if self.mac.screen_locked():
            raise HumanActive("screen locked")
        if (idle := self.mac.idle_seconds()) < self.cfg.idle_minutes * 60:
            raise HumanActive(f"someone used the Mac {idle:.0f} s ago")
        self.round_started = time.monotonic()
        self.mac.activate()
        win = self.mac.window()
        if win is None:
            raise KillSwitch("ChatApp window not found (logged out or quit?)")
        self.look("sidebar_top")  # measures the layout before the first scroll
        self.mac.scroll_sidebar(win, self.cfg.sidebar_pages * self.cfg.sidebar_scroll_clicks * 2)  # to the top
        self.sleep(1.0)
        page, prev_key = 0, None
        while todo and page < self.cfg.sidebar_pages:
            win, _, lines = self.look(f"sidebar{page:02d}")
            picked = None
            for g in sorted(todo, key=lambda _: self.rand.random()):
                hits = find_sidebar_rows(lines, g, self.layout, win)
                if len(hits) > 1:
                    self.log("group_ambiguous", group=g, rows=[h.text for h in hits])
                    todo.discard(g)
                elif hits:
                    picked = (g, hits[0])
                    break
            if picked:
                g, row = picked
                todo.discard(g)
                if results:
                    self.sleep(self.rand.uniform(*self.cfg.group_pause_seconds))
                self.mac.click(win, row)
                self.sleep(self.rand.uniform(*self.cfg.pause_seconds))
                results.append(self.scan_group(g))
                continue  # same list screen again: it may have changed
            key = text_key([l for l in lines if inside(l, self.layout.chat_list(win))])
            if key == prev_key:
                break  # bottom of the chat list
            prev_key = key
            page += 1
            if page < self.cfg.sidebar_pages:
                self.mac.scroll_sidebar(win, -self.cfg.sidebar_scroll_clicks)
                self.sleep(self.rand.uniform(*self.cfg.pause_seconds))
        for g in sorted(todo):
            self.log("not_in_recent", group=g, pages=page,
                     note="not near the top of the chat list, so no new messages; next round")
        return results

    def in_active_hours(self) -> bool:
        start, end = self.cfg.active_hours
        return start <= self.now().hour < end

    def run_forever(self) -> None:
        self.log("start", groups=self.cfg.groups, version=COLLECTOR_VERSION)
        while True:
            if self.stop_file.exists():
                self.log("stopped", reason=self.stop_file.read_text().strip())
                return
            if self.in_active_hours():
                try:
                    self.round()
                except HumanActive as e:
                    self.log("paused", reason=str(e))
                except FailSafe as e:
                    self.kill(str(e))
                    return
                except KillSwitch as e:
                    self.kill(str(e))
                    return
            wait = self.rand.uniform(*self.cfg.interval_minutes) * 60
            self.log("sleep", minutes=round(wait / 60, 1))
            self.sleep(wait)
