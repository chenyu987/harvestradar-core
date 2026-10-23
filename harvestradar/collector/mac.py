"""Everything that touches macOS: find the ChatApp window, screenshot it, OCR it,
and the three input actions the collector is allowed to make.

Allowed input, and nothing else:
- ⌘F, ⌘A, ⌘V into ChatApp's search box (paste a group name)
- one click on a search result
- scroll wheel over the chat pane

Never Enter, never typing, never a click inside the chat pane. After a group is
open the keyboard focus sits in the message box, so any key press from there on
could become a message; the collector only scrolls and screenshots.

Needs, in System Settings → Privacy & Security, for the Terminal app you run it
from: Screen Recording (or screenshots come back without ChatApp in them) and
Accessibility (or clicks and scrolls are silently ignored).
"""

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from harvestradar.collector.safety import normalize

BUNDLE_ID = "com.example.ChatApp"
OWNER_NAMES = ("ChatApp", "聊天软件")


@dataclass
class Window:
    id: int
    x: float  # points, top-left origin, like the screen
    y: float
    w: float
    h: float


@dataclass
class Line:
    text: str
    x: float  # points relative to the window's top-left corner
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


@dataclass
class Layout:
    """Where the parts of the ChatApp window are, in points from the window edges.

    Defaults match ChatApp 4.x on a 1440x780 window with the default sidebar
    width. Run `python -m harvestradar.collector check` and look at
    calibrate.png if the boxes are off.
    """
    sidebar_right: float = 340   # chat pane starts here (x)
    header_bottom: float = 65    # chat title bar ends here (y)
    input_top_from_bottom: float = 160  # message box height, measured from the bottom
    search_box_bottom: float = 70  # search box sits above this line in the sidebar

    def chat_pane(self, win: Window) -> tuple[float, float, float, float]:
        return (self.sidebar_right, self.header_bottom, win.w - 14, win.h - self.input_top_from_bottom)

    def header(self, win: Window) -> tuple[float, float, float, float]:
        return (self.sidebar_right, 0, win.w * 0.75, self.header_bottom)

    def sidebar(self, win: Window) -> tuple[float, float, float, float]:
        return (60, 0, self.sidebar_right, win.h)


def inside(line: Line, box: tuple[float, float, float, float]) -> bool:
    x0, y0, x1, y1 = box
    return x0 <= line.cx <= x1 and y0 <= line.cy <= y1


class MacBackend:
    def __init__(self, layout: Layout, work_dir: Path):
        import pyautogui  # macOS only; imported here so the rest is testable anywhere

        pyautogui.FAILSAFE = True  # slam the mouse into a screen corner to abort
        pyautogui.PAUSE = 0.2
        self.gui = pyautogui
        self.layout = layout
        self.work_dir = work_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._last_mouse: tuple[int, int] | None = None

    # ---------- windows ----------

    def _chat_windows(self, any_layer: bool = False) -> list[dict]:
        from Quartz import CGWindowListCopyWindowInfo, kCGNullWindowID, kCGWindowListOptionOnScreenOnly

        wins = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID) or []
        return [w for w in wins if w.get("kCGWindowOwnerName") in OWNER_NAMES
                and (any_layer or w.get("kCGWindowLayer", 1) == 0)]

    def describe_windows(self) -> list[dict]:
        """Every on-screen ChatApp window, any layer. For diag-search."""
        return [{"id": int(w["kCGWindowNumber"]), "layer": int(w.get("kCGWindowLayer", 0)),
                 "name": str(w.get("kCGWindowName", "")), **{k.lower(): float(v) for k, v in w["kCGWindowBounds"].items()}}
                for w in self._chat_windows(any_layer=True)]

    @staticmethod
    def _to_window(w: dict) -> Window:
        b = w["kCGWindowBounds"]
        return Window(int(w["kCGWindowNumber"]), b["X"], b["Y"], b["Width"], b["Height"])

    def window(self) -> Window | None:
        """The main chat window: the biggest ChatApp window on screen."""
        wins = [self._to_window(w) for w in self._chat_windows()]
        return max(wins, key=lambda w: w.w * w.h) if wins else None

    def extra_windows(self) -> int:
        """Other ChatApp windows bigger than a tooltip: dialogs, login prompts, chat pop-outs."""
        main = self.window()
        return sum(1 for w in map(self._to_window, self._chat_windows())
                   if main and w.id != main.id and w.w > 150 and w.h > 100)

    def activate(self) -> None:
        subprocess.run(["open", "-b", BUNDLE_ID], check=True)
        time.sleep(1.0)

    # ---------- screen ----------

    def capture(self, name: str) -> Path:
        win = self.window()
        if win is None:
            raise RuntimeError("ChatApp window not found")
        path = self.work_dir / f"{name}.png"
        # -R: the window's rectangle on screen, so pop-ups drawn over it (like the search
        # results list) are in the picture too; -l <window id> would leave them out.
        # -x: no shutter sound.
        rect = f"{win.x:.0f},{win.y:.0f},{win.w:.0f},{win.h:.0f}"
        subprocess.run(["screencapture", "-x", "-R", rect, str(path)], check=True)
        return path

    def ocr(self, path: Path, win: Window) -> list[Line]:
        from ocrmac import ocrmac

        out = []
        for text, _conf, (bx, by, bw, bh) in ocrmac.OCR(
                str(path), language_preference=["zh-Hans", "en-US"]).recognize():
            # Vision boxes are normalized with the origin at bottom-left.
            out.append(Line(text, bx * win.w, (1 - by - bh) * win.h, bw * win.w, bh * win.h))
        out.sort(key=lambda l: (round(l.y), l.x))
        return out

    # ---------- input ----------

    def _set_clipboard(self, text: str) -> None:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)

    def _remember_mouse(self) -> None:
        p = self.gui.position()
        self._last_mouse = (p.x, p.y)

    def user_active(self) -> bool:
        """Did someone move the mouse since our last action? Then a human is using the Mac."""
        if self._last_mouse is None:
            return False
        p = self.gui.position()
        return abs(p.x - self._last_mouse[0]) > 3 or abs(p.y - self._last_mouse[1]) > 3

    def search(self, group: str) -> None:
        """Put the group name in ChatApp's search box. Does not press Enter."""
        self._set_clipboard(group)
        self.gui.hotkey("command", "f")
        time.sleep(0.6)
        self.gui.hotkey("command", "a")
        self.gui.hotkey("command", "v")
        time.sleep(1.5)
        self._remember_mouse()

    def nudge_search(self) -> None:
        """Space then backspace, in case ChatApp did not start searching on a paste.
        Leaves the text as it was, wherever the focus is."""
        self.gui.press("space")
        self.gui.press("backspace")
        time.sleep(1.0)
        self._remember_mouse()

    def escape(self) -> None:
        """Only used by diag-search, to clear the search box afterwards."""
        self.gui.press("escape")

    def click(self, win: Window, line: Line) -> None:
        self.gui.click(win.x + line.cx, win.y + line.cy)
        time.sleep(0.3)
        self._remember_mouse()

    def scroll_up(self, win: Window, clicks: int) -> None:
        x0, y0, x1, y1 = self.layout.chat_pane(win)
        self.gui.moveTo(win.x + (x0 + x1) / 2, win.y + (y0 + y1) / 2, duration=0.2)
        self.gui.scroll(clicks)  # positive = up, toward older messages
        self._remember_mouse()

    def notify(self, title: str, message: str) -> None:
        script = f'display notification {applescript_str(message)} with title {applescript_str(title)} sound name "Basso"'
        subprocess.run(["osascript", "-e", script], check=False)


def applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def find_search_result(lines: list[Line], group: str, layout: Layout, win: Window) -> Line | None:
    """The search result row for this group: below the search box, starting in the sidebar
    column (a results pop-up may be wider than the sidebar), name matches."""
    want = normalize(group)
    for line in lines:
        if not (60 <= line.x < layout.sidebar_right) or line.cy <= layout.search_box_bottom:
            continue
        got = normalize(line.text).rstrip("…")
        if len(got) >= 4 and (want.startswith(got) or got.startswith(want)):
            return line
    return None


def search_box_has(lines: list[Line], group: str, layout: Layout, win: Window) -> bool:
    """Did our paste land in the search box (and not, say, in a chat's message box)?"""
    want = normalize(group)
    for line in lines:
        if inside(line, layout.sidebar(win)) and line.cy <= layout.search_box_bottom:
            got = normalize(line.text)
            if got and (want.startswith(got[:8]) or got.startswith(want[:8])):
                return True
    return False
