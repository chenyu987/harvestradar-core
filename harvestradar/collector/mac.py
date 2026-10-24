"""Everything that touches macOS: find the ChatApp window, screenshot it, OCR it,
and the mouse actions the collector is allowed to make.

Allowed input, and nothing else:
- scroll wheel over the sidebar chat list (to find a group)
- one click on a group's name in the sidebar chat list
- scroll wheel over the chat pane

No keyboard at all: no ⌘F, no paste, no Enter, no key of any kind. Keyboard focus
in ChatApp is often in a chat's message box, and ChatApp does not tell other apps
where the focus is, so any key press could land in a chat. On 2026-10-24 a ⌘F
that did not move the focus pasted a group name into a group's message box.
Mouse events are posted with Quartz directly; pyautogui (which can type) is not
used, and a test checks that this file never creates a keyboard event.

Needs, in System Settings → Privacy & Security, for the program that runs it
(Terminal, or the Python binary when it runs as a background service): Screen
Recording (or screenshots come back without ChatApp in them) and Accessibility
(or clicks and scrolls are silently ignored). `python -m harvestradar.collector
doctor` checks both.
"""

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from harvestradar.collector.safety import normalize

BUNDLE_ID = "com.example.ChatApp"
OWNER_NAMES = ("ChatApp", "聊天软件", "ChatAppCN")
DRAFT_MARKS = ("[draft]", "［draft］", "[草稿]", "［草稿］")


class FailSafe(Exception):
    """The mouse is in a screen corner: a human wants everything to stop now."""


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

    Measured from each screenshot by `detect_layout` (the thin divider lines
    ChatApp draws between sidebar, title bar, messages and message box), so a
    different Mac, screen or sidebar width needs no tuning. These defaults are
    only used when detection fails; they match ChatApp 4.x on a 1440x780 window.
    """
    sidebar_right: float = 340   # chat pane starts here (x)
    header_bottom: float = 65    # chat title bar ends here (y)
    input_top_from_bottom: float = 160  # message box height, measured from the bottom
    search_box_bottom: float = 70  # search box sits above this line in the sidebar
    sidebar_left: float = 60     # the icon bar ends here (x)

    def chat_pane(self, win: Window) -> tuple[float, float, float, float]:
        return (self.sidebar_right, self.header_bottom, win.w - 14, win.h - self.input_top_from_bottom)

    def header(self, win: Window) -> tuple[float, float, float, float]:
        return (self.sidebar_right, 0, win.w * 0.75, self.header_bottom)

    def sidebar(self, win: Window) -> tuple[float, float, float, float]:
        return (self.sidebar_left, 0, self.sidebar_right, win.h)

    def chat_list(self, win: Window) -> tuple[float, float, float, float]:
        """The scrollable chat list: the sidebar below the search box."""
        return (self.sidebar_left, self.search_box_bottom, self.sidebar_right, win.h)


def inside(line: Line, box: tuple[float, float, float, float]) -> bool:
    x0, y0, x1, y1 = box
    return x0 <= line.cx <= x1 and y0 <= line.cy <= y1


def detect_layout(img, win: Window, fallback: Layout) -> Layout | None:
    """Find the sidebar edge, the title bar and the message box from the 1-pixel
    divider lines ChatApp draws (same in light and dark mode). None if not found.

    `img` is a screenshot of exactly the window (PIL image, any scale).
    """
    gray = img.convert("L")
    s = gray.width / win.w
    px = gray.load()
    W, H = gray.size

    def median(vals: list[int]) -> float:
        return sorted(vals)[len(vals) // 2]

    def col(x: int) -> list[int]:
        return [px[x, y] for y in range(int(H * 0.1), int(H * 0.9), 4)]

    # Sidebar edge: a full-height flat column that stands out from both sides.
    right = None
    for x in range(int(200 * s), int(W * 0.6)):
        vals = col(x)
        if max(vals) - min(vals) > 2:
            continue
        m = median(vals)
        if abs(median(col(x - 2)) - m) >= 6 and abs(median(col(x + 2)) - m) >= 6:
            right = x / s
            break
    if right is None:
        return None

    x0, x1 = int((right + 10) * s), W - int(30 * s)

    def row(y: int) -> list[int]:
        return [px[x, y] for x in range(x0, x1, 3)]

    # Title bar and message box: full-width flat rows that stand out from both sides.
    lines = []
    for y in range(2, H - 2):
        vals = row(y)
        if max(vals) - min(vals) > 2:
            continue
        m = median(vals)
        if abs(median(row(y - 2)) - m) >= 6 and abs(median(row(y + 2)) - m) >= 6:
            lines.append(y / s)
    header = [y for y in lines if 30 <= y <= 150]
    message_box = [y for y in lines if win.h * 0.5 <= y <= win.h - 60]
    if not header or not message_box:
        return None
    header_bottom = round(min(header)) + 1
    return Layout(
        sidebar_right=round(right),
        header_bottom=header_bottom,
        input_top_from_bottom=round(win.h - max(message_box)),
        search_box_bottom=header_bottom + 5,
        sidebar_left=fallback.sidebar_left,
    )


@dataclass
class SidebarRow:
    name: Line
    preview: Line | None

    @property
    def draft(self) -> bool:
        return self.preview is not None and is_draft(self.preview.text)


def is_draft(text: str) -> bool:
    low = text.replace(" ", "").lower()
    return any(m in low for m in DRAFT_MARKS)


def sidebar_rows(lines: list[Line], layout: Layout, win: Window) -> list[SidebarRow]:
    """The chat list, row by row: name on the first line, last message (or
    "[Draft] ...") on the second. Both start at the same x, right of the avatar;
    times and badges sit further right and are left out.
    """
    box = layout.chat_list(win)
    cand = [l for l in lines if inside(l, box) and l.cy < win.h - 8]
    if not cand:
        return []
    # Names and previews share one left edge, so it is the most common one.
    # Unread badges (left of it) and times (right of it) have their own.
    left = max(cand, key=lambda l: sum(abs(o.x - l.x) <= 4 for o in cand)).x
    col = sorted((l for l in cand if abs(l.x - left) <= 8), key=lambda l: l.cy)
    rows, i = [], 0
    while i < len(col):
        name = col[i]
        nxt = col[i + 1] if i + 1 < len(col) else None
        if nxt and 8 <= nxt.cy - name.cy <= 32:
            rows.append(SidebarRow(name, nxt))
            i += 2
        else:
            rows.append(SidebarRow(name, None))
            i += 1
    return rows



def find_sidebar_rows(lines: list[Line], group: str, layout: Layout, win: Window) -> list[Line]:
    """Names in the chat list that match this group.

    ChatApp cuts long names with "...", so either name may be a prefix of the
    other, with at least 4 characters in common. Only row names count, never the
    last-message line under them. More than one match means the name is
    ambiguous: the caller must not click.
    """
    want = normalize(group)
    hits = []
    for row in sidebar_rows(lines, layout, win):
        got = normalize(row.name.text).rstrip("…")
        if len(got) >= 4 and (want.startswith(got) or got.startswith(want)):
            hits.append(row.name)
    return hits


class MacBackend:
    CORNER = 5  # points; the mouse this close to a screen corner stops everything

    def __init__(self, layout: Layout, work_dir: Path):
        import Quartz  # macOS only; imported here so the rest is testable anywhere

        self.q = Quartz
        self.layout = layout
        self.work_dir = work_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._last_mouse: tuple[float, float] | None = None

    # ---------- permissions ----------

    def has_screen_recording(self) -> bool:
        return bool(self.q.CGPreflightScreenCaptureAccess())

    def has_accessibility(self, prompt: bool = False) -> bool:
        from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt

        return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: prompt}))

    def request_permissions(self) -> None:
        """Make macOS list this program in both settings panes, so the human only flips switches."""
        self.q.CGRequestScreenCaptureAccess()
        self.has_accessibility(prompt=True)

    # ---------- windows ----------

    def _chat_windows(self) -> list[dict]:
        q = self.q
        wins = q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionOnScreenOnly, q.kCGNullWindowID) or []
        return [w for w in wins
                if w.get("kCGWindowOwnerName") in OWNER_NAMES and w.get("kCGWindowLayer", 1) == 0]

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
        # -R: the window's rectangle on screen, so anything drawn over it is in the
        # picture too (a dialog, a pop-up). -x: no shutter sound.
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

    # ---------- mouse (the only input there is) ----------

    def _mouse(self) -> tuple[float, float]:
        p = self.q.CGEventGetLocation(self.q.CGEventCreate(None))
        return p.x, p.y

    def _check_corner(self) -> None:
        x, y = self._mouse()
        b = self.q.CGDisplayBounds(self.q.CGMainDisplayID())
        w, h = b.size.width, b.size.height
        near = lambda v, edge: abs(v - edge) <= self.CORNER
        if (near(x, 0) or near(x, w - 1)) and (near(y, 0) or near(y, h - 1)):
            raise FailSafe("mouse in a screen corner")

    def _post_mouse(self, kind, x: float, y: float) -> None:
        q = self.q
        q.CGEventPost(q.kCGHIDEventTap, q.CGEventCreateMouseEvent(None, kind, (x, y), q.kCGMouseButtonLeft))

    def _move(self, x: float, y: float) -> None:
        self._check_corner()
        self._post_mouse(self.q.kCGEventMouseMoved, x, y)
        time.sleep(0.15)

    def _scroll(self, lines: int) -> None:
        """Positive = content moves down (older messages come into view)."""
        q = self.q
        lines = int(lines)
        step = 10 if lines > 0 else -10
        for _ in range(abs(lines) // 10):
            q.CGEventPost(q.kCGHIDEventTap, q.CGEventCreateScrollWheelEvent(None, q.kCGScrollEventUnitLine, 1, step))
            time.sleep(0.02)
        if rest := (abs(lines) % 10) * (1 if lines > 0 else -1):
            q.CGEventPost(q.kCGHIDEventTap, q.CGEventCreateScrollWheelEvent(None, q.kCGScrollEventUnitLine, 1, rest))

    def _remember_mouse(self) -> None:
        self._last_mouse = self._mouse()

    def user_active(self) -> bool:
        """Did someone move the mouse since our last action? Then a human is using the Mac."""
        self._check_corner()
        if self._last_mouse is None:
            return False
        x, y = self._mouse()
        return abs(x - self._last_mouse[0]) > 3 or abs(y - self._last_mouse[1]) > 3

    def idle_seconds(self) -> float:
        """Seconds since anyone touched mouse, trackpad or keyboard."""
        q = self.q
        return q.CGEventSourceSecondsSinceLastEventType(q.kCGEventSourceStateHIDSystemState, q.kCGAnyInputEventType)

    def key_idle_seconds(self) -> float:
        """Seconds since a key was pressed. The collector never presses one, so it was a human."""
        q = self.q
        return q.CGEventSourceSecondsSinceLastEventType(q.kCGEventSourceStateHIDSystemState, q.kCGEventKeyDown)

    def screen_locked(self) -> bool:
        info = self.q.CGSessionCopyCurrentDictionary() or {}
        return bool(info.get("CGSSessionScreenIsLocked")) or not info.get("kCGSessionOnConsoleKey", True)

    def click(self, win: Window, line: Line) -> None:
        x, y = win.x + line.cx, win.y + line.cy
        self._move(x, y)
        self._post_mouse(self.q.kCGEventLeftMouseDown, x, y)
        time.sleep(0.05)
        self._post_mouse(self.q.kCGEventLeftMouseUp, x, y)
        time.sleep(0.3)
        self._remember_mouse()

    def scroll_up(self, win: Window, clicks: int) -> None:
        x0, y0, x1, y1 = self.layout.chat_pane(win)
        self._move(win.x + (x0 + x1) / 2, win.y + (y0 + y1) / 2)
        self._scroll(clicks)  # positive = up, toward older messages
        self._remember_mouse()

    def scroll_sidebar(self, win: Window, clicks: int) -> None:
        """Scroll the chat list. Negative = down, toward older chats."""
        x0, y0, x1, y1 = self.layout.chat_list(win)
        self._move(win.x + (x0 + x1) / 2, win.y + (y0 + y1) / 2)
        self._scroll(clicks)
        self._remember_mouse()

    def show_terminal(self) -> None:
        """Bring the terminal this was started from to the front, so whoever comes
        back to the Mac sees the grab is over (and ChatApp is no longer driven)."""
        app = {"iTerm.app": "iTerm"}.get(os.environ.get("TERM_PROGRAM", ""), "Terminal")
        subprocess.run(["osascript", "-e", f'tell application {applescript_str(app)} to activate'], check=False)

    def notify(self, title: str, message: str) -> None:
        script = f'display notification {applescript_str(message)} with title {applescript_str(title)} sound name "Basso"'
        subprocess.run(["osascript", "-e", script], check=False)


def applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
