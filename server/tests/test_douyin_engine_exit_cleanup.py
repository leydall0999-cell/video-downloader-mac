"""内嵌 Chromium 的「退出必带走」守卫（2026-11-03，踩过两次）。

背景：1.1.08 把 chrome-headless-shell 打进 App 后，退出 App 仍留 6 个进程驻留
（约 200MB）。第一版修复把清理挂到 pywebview 的 `window.events.closing`，
**实测完全没生效**——macOS 的 Cmd+Q / Dock Quit / AppleScript quit 走
`AppDelegate.applicationShouldTerminate_`，该补丁直接 return YES ⇒ AppKit 立刻
终止进程，closing 事件根本不触发。

这类缺陷有两个特点，所以必须用源码守卫而不是「看代码觉得对」：
  ① 挂错事件时**一点报错都没有**（函数写了、被 import 了，就是不执行）；
  ② 验收容易假通过（只看 App 主进程归零，残留的是浏览器子进程）。

本测试只做静态断言（毫秒级、零依赖），覆盖：
  ① applicationShouldTerminate_ 放行前必须调 _kill_douyin_engine；
  ② 前端「退出」按钮路径（quit_app → os._exit）之前必须调；
  ③ server 侧 atexit 兜底仍在；
  ④ 杀进程不能退化成裸 pkill 单点（要有绝对路径 + ps 兜底），否则 PATH 极简时静默失败。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LAUNCHER = REPO / "desktop" / "desktop_launcher.py"
DOUYIN = REPO / "server" / "douyin_cdp.py"

PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}" + (f" —— {detail}" if detail else ""))


def _block(src: str, start_marker: str) -> str:
    """取出从 start_marker 起、到下一个同级 def 之前的源码块（够断言调用顺序）。"""
    i = src.find(start_marker)
    if i < 0:
        return ""
    rest = src[i:]
    m = re.search(r"\n(?=    def |\ndef |\n        # ──)", rest[1:])
    return rest[: m.start() + 1] if m else rest


def main() -> int:
    print("=== test_douyin_engine_exit_cleanup.py ===")

    launcher = LAUNCHER.read_text(encoding="utf-8")
    douyin = DOUYIN.read_text(encoding="utf-8")

    # ① 唯一的真收口点：applicationShouldTerminate_ 里、return YES 之前
    term = _block(launcher, "def _patch_app_should_terminate")
    check("① 存在 applicationShouldTerminate_ 补丁", bool(term))
    check(
        "① 放行前调用了 _kill_douyin_engine()",
        "_kill_douyin_engine()" in term,
        "缺少调用 ⇒ Cmd+Q / Dock Quit 退出后 Chromium 一定残留",
    )
    if "_kill_douyin_engine()" in term and "_Foundation.YES" in term:
        check(
            "① 调用顺序在 return YES 之前（否则 AppKit 已终止进程）",
            term.index("_kill_douyin_engine()") < term.index("return _Foundation.YES"),
            "顺序反了等于没写",
        )

    # ② 前端「退出」按钮：quit_app 内 os._exit 之前
    quit_block = _block(launcher, "def quit_app")
    check("② quit_app 存在", bool(quit_block))
    if "_kill_douyin_engine()" in quit_block and "os._exit" in quit_block:
        check(
            "② quit_app 在 os._exit 之前清理",
            quit_block.index("_kill_douyin_engine()") < quit_block.index("os._exit"),
        )
    else:
        check("② quit_app 在 os._exit 之前清理", False, "缺少调用或 os._exit")

    # ③ server 侧 atexit 兜底（开发态 / CLI / uvicorn 停机）
    check(
        "③ douyin_cdp 注册 atexit 兜底",
        bool(re.search(r"atexit\.register\(\s*_kill_chrome\s*\)", douyin)),
        "开发态跑完会留下 headless 进程",
    )

    # ④ 杀进程不能是裸 pkill 单点（冻结包 PATH 极简 ⇒ FileNotFoundError ⇒ 静默失败）
    kill_fn = _block(launcher, "def _kill_douyin_engine")
    check("④ 用绝对路径 /usr/bin/pkill", "/usr/bin/pkill" in kill_fn)
    check(
        "④ 有 ps + os.kill 兜底",
        "/bin/ps" in kill_fn and "os.kill" in kill_fn,
        "pkill 不可用时会静默失败",
    )
    check(
        "④ 清理结果写日志（可事后核对）",
        "_launch_log(" in kill_fn,
        "静默 pass 会让下次再犯时无从定位",
    )
    # ⑤ 匹配必须收窄到 `--user-data-dir=`：裸 `pkill -f chrome-headless-shell` 会误杀
    #    **任何**命令行里带这串字的进程（实测把验证命令自己的 shell 杀掉；用户侧
    #    终端 grep / 编辑器开着源码同理）。浏览器进程恒带该参数，用它匹配才唯一。
    check(
        "⑤ 按 --user-data-dir= 精确匹配",
        "--user-data-dir=" in kill_fn,
        "必须用 profile 参数定位，否则会误杀无关进程",
    )
    check(
        "⑤ 禁止裸 pkill -f chrome-headless-shell（会误杀）",
        '"-f", "chrome-headless-shell"' not in kill_fn
        and "'-f', 'chrome-headless-shell'" not in kill_fn,
        "宽匹配会杀掉验证命令/编辑器等无关进程",
    )

    print()
    print(f"通过: {PASS}  失败: {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
