"""Command line for the collector. Run on the spare Mac:

    python -m harvestradar.collector check                       # no input at all: screenshot, OCR, draw the layout boxes
    python -m harvestradar.collector scan-current --group 群名   # scroll the chat that is already open, no search, no click
    python -m harvestradar.collector diag-search --group 群名    # ⌘F + paste, screenshots over 5 s, list windows, Esc. No Enter, no click
    python -m harvestradar.collector once                        # one round over every group in collector.toml
    python -m harvestradar.collector run                         # rounds every 30-60 min, daytime only, until STOP
    python -m harvestradar.collector stop | resume               # create / delete the STOP file
"""

import argparse
import json
import sys
from pathlib import Path

from harvestradar.collector.core import Collector, Config, HumanActive, KillSwitch


def cmd_check(cfg: Config, mac) -> None:
    from PIL import Image, ImageDraw

    from harvestradar.collector.mac import inside
    from harvestradar.collector.safety import danger_hits
    from harvestradar.collector.timeparse import parse_divider
    from datetime import datetime

    mac.activate()
    win = mac.window()
    if win is None:
        sys.exit("没找到聊天软件窗口。聊天软件打开并登录了吗？")
    print(f"聊天软件窗口 {win.w:.0f}x{win.h:.0f} pt，位置 ({win.x:.0f}, {win.y:.0f})，其他聊天软件窗口 {mac.extra_windows()} 个")
    full = mac.capture("check")
    lines = mac.ocr(full, win)
    if not lines:
        sys.exit("截图里一个字都没认出来：给「终端」开「屏幕录制」权限，然后重启终端。")
    lay = cfg.layout
    now = datetime.now()
    print("\n标题栏:", [l.text for l in lines if inside(l, lay.header(win))])
    print("时间分隔线:", [f"{l.text} → {d:%m-%d %H:%M}" for l in lines
                     if inside(l, lay.chat_pane(win)) and (d := parse_divider(l.text, now))])
    print("危险词:", danger_hits([l.text for l in lines], ignore=cfg.groups) or "无")

    img = Image.open(full).convert("RGB")
    s = img.width / win.w
    draw = ImageDraw.Draw(img)
    for box, color in ((lay.chat_pane(win), "lime"), (lay.header(win), "orange"), (lay.sidebar(win), "cyan")):
        draw.rectangle([v * s for v in box], outline=color, width=6)
    draw.line([(60 * s, lay.search_box_bottom * s), (lay.sidebar_right * s, lay.search_box_bottom * s)], fill="red", width=4)
    out = Path(cfg.out_dir) / "calibrate.png"
    img.save(out)
    print(f"\n已保存 {out}：绿框=聊天区（会被截下来的部分），橙框=标题栏，蓝框=左边栏，红线=搜索框下沿。"
          "\n框不对就改 collector.toml 的 [layout]。")


def cmd_diag_search(cfg: Config, mac, group: str) -> None:
    """Find out how ChatApp shows search results: how fast, and in which window."""
    import subprocess
    import time

    out = Path(cfg.out_dir) / "diag"
    out.mkdir(parents=True, exist_ok=True)
    mac.activate()
    print("搜索前的聊天软件窗口:", json.dumps(mac.describe_windows(), ensure_ascii=False))
    mac.search(group)  # ends 1.5 s after the paste
    t0 = time.monotonic()
    for label in ("1.5s", "3s", "5s"):
        while time.monotonic() - t0 < {"1.5s": 0, "3s": 1.5, "5s": 3.5}[label]:
            time.sleep(0.1)
        # Whole screen, so a results pop-up anywhere is in the picture.
        subprocess.run(["screencapture", "-x", str(out / f"search_{label}.png")], check=True)
        print(f"{label} 的聊天软件窗口:", json.dumps(mac.describe_windows(), ensure_ascii=False))
    mac.escape()
    print(f"\n截图在 {out}/search_1.5s.png、search_3s.png、search_5s.png，连同上面的窗口列表发给我。已按 Esc 清掉搜索。")


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m harvestradar.collector")
    ap.add_argument("--config", default="collector.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sc = sub.add_parser("scan-current")
    sc.add_argument("--group", required=True, help="the chat that is open right now (checked against the title bar)")
    ds = sub.add_parser("diag-search")
    ds.add_argument("--group", required=True)
    sub.add_parser("once")
    sub.add_parser("run")
    sub.add_parser("stop")
    sub.add_parser("resume")
    args = ap.parse_args()

    cfg = Config.load(args.config)
    stop_file = Path(cfg.out_dir) / "STOP"
    if args.cmd == "stop":
        stop_file.parent.mkdir(parents=True, exist_ok=True)
        stop_file.write_text("stopped by hand\n")
        return print("已创建 STOP，正在跑的会在下一屏停下。")
    if args.cmd == "resume":
        if stop_file.exists():
            print("删除 STOP，原因是：", stop_file.read_text().strip())
            stop_file.unlink()
        return print("可以重新运行了。")

    from harvestradar.collector.mac import MacBackend
    mac = MacBackend(cfg.layout, Path(cfg.out_dir) / "tmp")
    if args.cmd == "check":
        return cmd_check(cfg, mac)
    if args.cmd == "diag-search":
        return cmd_diag_search(cfg, mac, args.group)

    col = Collector(cfg, mac)
    if args.cmd == "run":
        return col.run_forever()
    try:
        if args.cmd == "scan-current":
            mac.activate()
            results = [col.scan_group(args.group, open_it=False)]
        else:
            results = col.round()
    except KillSwitch as e:
        col.kill(str(e))
        sys.exit(f"急停：{e}\n看过没问题后运行 resume 再继续。")
    except HumanActive as e:
        sys.exit(f"有人在用这台 Mac（{e}），这一轮放弃。")
    for r in results:
        print(json.dumps({"group": r.group, "screens": len(r.screens), "stop": r.stop, "newest": r.newest}, ensure_ascii=False))


if __name__ == "__main__":
    main()
