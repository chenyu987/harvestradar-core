"""Command line for the collector. Run on the Mac that has ChatApp open:

    python -m harvestradar.collector doctor                      # no input: permissions, ChatApp window, layout
    python -m harvestradar.collector check                       # no input: screenshot, OCR, draw the measured layout
    python -m harvestradar.collector scan-current --group 群名   # scroll the chat that is already open, no click
    python -m harvestradar.collector run                         # grab every group once, then turn the screenshots into leads
    python -m harvestradar.collector run --days 14               # this time scroll back 14 days, past the bookmarks
    python -m harvestradar.collector once                        # grab only, no processing
    python -m harvestradar.collector stop | resume               # stop a running grab / clear a kill switch

Nothing starts by itself and nothing repeats: each `run` grabs once, when a human asks.
"""

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from harvestradar.collector.core import HAND_STOP, Collector, Config, FailSafe, HumanActive, KillSwitch, Stopped


def cmd_doctor(cfg: Config, mac) -> int:
    """Everything the collector needs, checked without touching mouse or keyboard."""
    exe = os.path.realpath(sys.executable)
    print(f"Python: {exe}")
    ok = True
    screen, ax = mac.has_screen_recording(), mac.has_accessibility()
    print(f"屏幕录制: {'有' if screen else '没有'}")
    print(f"辅助功能: {'有' if ax else '没有'}")
    if not (screen and ax):
        ok = False
        mac.request_permissions()
        print("\n去「系统设置 → 隐私与安全性」，在「屏幕录制」和「辅助功能」里把它打开（找不到就点 + 加上面那个 Python）。"
              "\n在终端里跑的话，要打开的是「终端」。打开后重跑 doctor。")
    win = mac.window()
    if win is None:
        print("聊天软件窗口: 没找到。聊天软件打开并登录了吗？窗口没有最小化吧？")
        return 1
    print(f"聊天软件窗口: {win.w:.0f}x{win.h:.0f} pt，其他聊天软件窗口 {mac.extra_windows()} 个")
    if win.w < 900 or win.h < 600:
        ok = False
        print("  窗口太小，请把聊天软件窗口拉大（最好最大化）。")
    if screen:
        from PIL import Image

        from harvestradar.collector.mac import detect_layout
        with Image.open(mac.capture("doctor")) as img:
            lay = detect_layout(img, win, cfg.layout)
        print("版面:", f"自动识别成功 {asdict(lay)}" if lay else "自动识别失败，会用 collector.toml 里的 [layout]；跑 check 看图")
    stop = Path(cfg.out_dir) / "STOP"
    if stop.exists():
        ok = False
        print(f"STOP 文件在: {stop.read_text().strip()}（看过没问题后运行 resume）")
    print("\n全部正常。" if ok else "\n有问题，按上面说的改。")
    return 0 if ok else 1


def cmd_check(cfg: Config, mac) -> None:
    from datetime import datetime

    from PIL import Image, ImageDraw

    from harvestradar.collector.mac import detect_layout, inside, sidebar_rows
    from harvestradar.collector.safety import danger_hits
    from harvestradar.collector.timeparse import parse_divider

    mac.activate()
    win = mac.window()
    if win is None:
        sys.exit("没找到聊天软件窗口。聊天软件打开并登录了吗？")
    print(f"聊天软件窗口 {win.w:.0f}x{win.h:.0f} pt，位置 ({win.x:.0f}, {win.y:.0f})，其他聊天软件窗口 {mac.extra_windows()} 个")
    full = mac.capture("check")
    with Image.open(full) as img:
        lay = detect_layout(img, win, cfg.layout)
    print("版面:", "自动识别" if lay else "自动识别失败，用 collector.toml 的 [layout]", asdict(lay or cfg.layout))
    lay = lay or cfg.layout
    lines = mac.ocr(full, win)
    if not lines:
        sys.exit("截图里一个字都没认出来：没有「屏幕录制」权限，或者屏幕锁了。先跑 doctor。")
    now = datetime.now()
    print("\n标题栏:", [l.text for l in lines if inside(l, lay.header(win))])
    print("时间分隔线:", [f"{l.text} → {d:%m-%d %H:%M}" for l in lines
                     if inside(l, lay.chat_pane(win)) and (d := parse_divider(l.text, now))])
    print("危险词:", danger_hits([l.text for l in lines], ignore=cfg.groups) or "无")
    rows = sidebar_rows(lines, lay, win)
    print("左栏:", [r.name.text + (" [草稿]" if r.draft else "") for r in rows])
    if drafts := [r.name.text for r in rows if r.draft]:
        print("  这些会话有没发出去的草稿，请手动清掉:", drafts)

    img = Image.open(full).convert("RGB")
    s = img.width / win.w
    draw = ImageDraw.Draw(img)
    for box, color in ((lay.chat_pane(win), "lime"), (lay.header(win), "orange"), (lay.chat_list(win), "cyan")):
        draw.rectangle([v * s for v in box], outline=color, width=6)
    for r in rows:
        draw.rectangle([r.name.x * s, r.name.y * s, (r.name.x + r.name.w) * s, (r.name.y + r.name.h) * s],
                       outline="red" if r.draft else "yellow", width=3)
    out = Path(cfg.out_dir) / "calibrate.png"
    img.save(out)
    print(f"\n已保存 {out}：绿框=聊天区（会被截下来的部分），橙框=标题栏，蓝框=会话列表，黄框=认出来的会话名（红=有草稿）。")


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m harvestradar.collector")
    ap.add_argument("--config", default="collector.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    sub.add_parser("check")
    sc = sub.add_parser("scan-current")
    sc.add_argument("--group", required=True, help="the chat that is open right now (checked against the title bar)")
    on = sub.add_parser("once")
    rn = sub.add_parser("run")
    for p in (on, rn):
        p.add_argument("--days", type=float, help="this time scroll each group back this many days, past where the last grab stopped")
    rn.add_argument("--model", help="cloud extractor model for the screenshots (default: process's default)")
    rn.add_argument("--no-process", action="store_true", help="grab only; run `python -m harvestradar.process` later")
    sub.add_parser("stop")
    sub.add_parser("resume")
    args = ap.parse_args()

    cfg = Config.load(args.config)
    stop_file = Path(cfg.out_dir) / "STOP"
    if args.cmd == "stop":
        stop_file.parent.mkdir(parents=True, exist_ok=True)
        stop_file.write_text(HAND_STOP + "\n")
        return print("已停止：正在抓的会在几秒内停下。下次 run 会自动清掉这个停止标记。")
    if args.cmd == "resume":
        if stop_file.exists():
            print("删除 STOP，原因是：", stop_file.read_text().strip())
            stop_file.unlink()
        return print("可以重新运行了。")

    from harvestradar.collector.mac import MacBackend
    mac = MacBackend(cfg.layout, Path(cfg.out_dir) / "tmp")
    if args.cmd == "doctor":
        sys.exit(cmd_doctor(cfg, mac))
    if args.cmd == "check":
        return cmd_check(cfg, mac)

    col = Collector(cfg, mac)
    if args.cmd in ("run", "once"):
        # A stop by hand only ends that grab; a kill switch needs `resume` after a human looked.
        if stop_file.exists() and stop_file.read_text().startswith(HAND_STOP):
            stop_file.unlink()
        if stop_file.exists():
            sys.exit(f"急停标记还在：{stop_file.read_text().strip()}\n看过聊天软件没问题后运行 resume 再 run。")
        if not (mac.has_screen_recording() and mac.has_accessibility()):
            mac.request_permissions()
            sys.exit("没有「屏幕录制」或「辅助功能」权限，先跑 doctor 看怎么开。")
        import subprocess
        # Keep the display awake while we grab (screenshots of a dark screen have
        # no text); -w ends it when this process ends.
        subprocess.Popen(["caffeinate", "-dims", "-w", str(os.getpid())])
        back = f"每个群往回翻 {args.days:g} 天（最多 {cfg.lookback_max_screens} 屏）" if args.days else "只看上次之后的新消息"
        print(f"开始抓取 {len(cfg.groups)} 个群，只抓这一遍，{back}。\n请把手从鼠标键盘上拿开，"
              f"{cfg.hands_off_seconds:g} 秒没人碰就开始（最多等 {cfg.start_wait_minutes:g} 分钟）。"
              "\n停止：另开终端运行 stop，或在这里按 Ctrl+C，或把鼠标甩到屏幕角上。", flush=True)
    try:
        if args.cmd == "scan-current":
            mac.activate()
            results = [col.scan_group(args.group)]
        else:
            results = col.grab(days=args.days)
    except (KillSwitch, FailSafe) as e:
        col.kill(str(e))
        sys.exit(f"急停：{e}\n看过没问题后运行 resume 再继续。")
    except HumanActive as e:
        sys.exit(f"有人在用这台 Mac（{e}），这次没抓。没人用的时候再 run 一次。")
    except Stopped:
        sys.exit("已按 stop 停下。")
    except KeyboardInterrupt:
        col.log("stopped", reason="Ctrl+C")
        sys.exit("\n已停止。")
    finally:
        mac.show_terminal()  # done with ChatApp either way: show where things stand
    for r in results:
        print(json.dumps({"group": r.group, "screens": len(r.screens), "stop": r.stop, "newest": r.newest}, ensure_ascii=False))
    shots = sum(len(r.screens) for r in results)
    col.log("finished", groups=len(results), screens=shots)
    mac.notify("HarvestRadar 抓完了", f"{len(results)} 个群，{shots} 屏截图")
    print(f"\n==== 抓完了：{len(results)} 个群，{shots} 屏截图。聊天软件可以用了。 ====", flush=True)

    if args.cmd == "run" and not args.no_process:
        print("\n把截图变成线索：", flush=True)
        from harvestradar import process
        sys.argv = ["process", "--out-dir", cfg.out_dir] + (["--model", args.model] if args.model else [])
        process.main()


if __name__ == "__main__":
    main()
