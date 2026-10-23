"""Safety checks the collector runs on every screen. Pure logic, no Mac APIs.

The kill switch is deliberately trigger-happy: any sign that ChatApp is unhappy
stops everything until a human looks. A missed round costs nothing; a banned
account ends the project.
"""

import re
import unicodedata

# Phrases that mean the account or session is in trouble. Checked against OCR of
# the whole ChatApp window, including any dialog on top of it. Kept to full
# phrases: single words like 登录 or 二维码 show up in normal group chat
# ("扫码进群") and would stop every round. Window checks in mac.py catch the
# rest (a login window, an extra dialog window).
DANGER_WORDS = [
    "功能受限", "功能被限制", "账号异常", "帐号异常", "账号存在异常", "帐号存在异常", "行为异常",
    "操作频繁", "操作过于频繁", "已被限制", "被封", "封禁", "解封", "安全验证", "身份验证",
    "重新登录", "登录已失效", "已退出登录", "已在其他设备登录", "扫码登录", "请使用手机扫码",
    "restricted", "suspended", "unusual activity", "frequent", "verify your", "log in again",
    "logged out", "log in on", "scan the qr", "scan qr",
]

STOP_REASONS = ("checkpoint", "unchanged", "max_screens")


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    return re.sub(r"[\s\W_]+", "", s)


def danger_hits(lines: list[str], ignore: list[str] = ()) -> list[str]:
    """Danger words found in OCR lines. `ignore` lists lines (e.g. group names) that may contain them."""
    ignored = {normalize(x) for x in ignore if x}
    hits = []
    for line in lines:
        if normalize(line) in ignored:
            continue
        low = unicodedata.normalize("NFKC", line).lower()
        hits += [w for w in DANGER_WORDS if w in low]
    return hits


def title_matches(header_lines: list[str], group: str) -> bool:
    """Is the open chat the group we meant to open?

    The header shows the name plus a member count, e.g. "本地活动租物品｜壹 (497)",
    and long names get cut with "...". So: strip the count, then require one to be
    a prefix of the other, with at least 4 characters in common.
    """
    want = normalize(group)
    for line in header_lines:
        got = normalize(re.sub(r"[(（]\d+[)）]\s*$", "", line.strip()).rstrip(".…"))
        if len(got) >= 4 and (want.startswith(got) or got.startswith(want)):
            return True
    return False
