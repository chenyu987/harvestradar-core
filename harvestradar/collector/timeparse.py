"""Parse the time dividers ChatApp draws between messages ("1/25 09:47", "昨天 10:02").

ChatApp desktop only shows a divider when messages are more than a few minutes
apart, and its format depends on how old the message is and the UI language:

    09:47                today
    Yesterday 09:47      昨天 09:47
    Wednesday 09:47      星期三 09:47 / 周三 09:47     (within the last week)
    1/25 09:47           1月25日 09:47                 (this year)
    2025/1/25 09:47      2025年1月25日 09:47           (older)

Times can also be 12-hour ("下午 2:30", "2:30 PM"). Everything here is pure
string handling so it can be tested without a Mac.
"""

import re
from datetime import date, datetime, timedelta

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
}

_TIME = r"(?P<ampm1>上午|下午|中午|晚上|凌晨|早上)?\s*(?P<h>\d{1,2})[:：](?P<m>\d{2})\s*(?P<ampm2>AM|PM|am|pm)?"
_PATTERNS = [
    re.compile(rf"^(?P<y>\d{{4}})[/\-.年](?P<mo>\d{{1,2}})[/\-.月](?P<d>\d{{1,2}})日?\s*{_TIME}$"),
    re.compile(rf"^(?P<mo>\d{{1,2}})[/月](?P<d>\d{{1,2}})日?\s*{_TIME}$"),
    re.compile(rf"^(?P<rel>yesterday|昨天)\s*{_TIME}$", re.I),
    re.compile(rf"^(?:(?P<wd_en>monday|tuesday|wednesday|thursday|friday|saturday|sunday)|(?:星期|周|礼拜)(?P<wd_zh>[一二三四五六日天]))\s*{_TIME}$", re.I),
    re.compile(rf"^{_TIME}$"),
]


def _hour(m: re.Match) -> int:
    h = int(m["h"])
    ampm = (m["ampm1"] or m["ampm2"] or "").lower()
    if ampm in ("下午", "晚上", "pm") and h < 12:
        h += 12
    elif ampm in ("凌晨", "上午", "早上", "am") and h == 12:
        h = 0
    elif ampm == "中午" and h < 11:
        h += 12
    return h


def parse_divider(text: str, now: datetime) -> datetime | None:
    """Turn one divider line into a datetime, or None if it is not a divider."""
    t = re.sub(r"\s+", " ", text.strip())
    for pat in _PATTERNS:
        m = pat.match(t)
        if not m:
            continue
        g = m.groupdict()
        try:
            hh, mm = _hour(m), int(g["m"])
            if g.get("y"):
                day = date(int(g["y"]), int(g["mo"]), int(g["d"]))
            elif g.get("mo"):
                day = date(now.year, int(g["mo"]), int(g["d"]))
                if day > now.date():  # "12/30" seen in January is last year
                    day = date(now.year - 1, day.month, day.day)
            elif g.get("rel"):
                day = now.date() - timedelta(days=1)
            elif g.get("wd_en") or g.get("wd_zh"):
                wd = WEEKDAYS[(g.get("wd_en") or g["wd_zh"]).lower()]
                back = (now.weekday() - wd) % 7 or 7  # ChatApp uses weekday names for 2-6 days ago
                day = now.date() - timedelta(days=back)
            else:
                day = now.date()
            return datetime(day.year, day.month, day.day, hh, mm)
        except ValueError:  # OCR garbage like 13/45 25:99
            return None
    return None
