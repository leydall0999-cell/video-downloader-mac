"""LibreOffice 引擎「不在 Dock 冒图标」守卫（2026-11-19）。

背景：转换虽用 --headless（无窗口），但 macOS 只要 .app 被启动就在 Dock
显示图标，除非 Info.plist 声明 LSUIElement。从 dmg 装的 LibreOffice 没这个键，
导致每次 PDF→PPT 转换都弹一下 Dock。修复 = 装完/探测到引擎时自动补该键。

本守卫钉死四件事，任一被改坏立刻变红：
  ① 补标记函数确实写入 LSUIElement=True，且保留 plist 其他键；
  ② 幂等：已补过/缓存命中时不重复写盘；
  ③ 安装落点 `_install_from_volume` 调用了补标记；
  ④ 存量自愈落点 `installed_app` 对 bundled 引擎调用了补标记。

运行：cd server && python tests/test_lo_engine_no_dock.py
"""
from __future__ import annotations

import os
import plistlib
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_SERVER_DIR = Path(__file__).resolve().parents[1]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

import lo_engine as le  # noqa: E402

_FAILED: list[str] = []


def check(name: str, cond: bool, extra=None) -> None:
    print(("  ✓ " if cond else "  ❌ ") + name + ("" if cond else f"  → {extra!r}"))
    if not cond:
        _FAILED.append(name)


def make_app(tmp: str) -> str:
    app = os.path.join(tmp, "LibreOffice.app")
    os.makedirs(os.path.join(app, "Contents", "MacOS"), exist_ok=True)
    open(os.path.join(app, "Contents", "MacOS", "soffice"), "wb").close()
    with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
        plistlib.dump(
            {"CFBundleIdentifier": "org.libreoffice.test", "CFBundleName": "LibreOffice"}, f
        )
    return app


def test_patch_writes_and_keeps_keys():
    with tempfile.TemporaryDirectory(prefix="vdl_lo_test_") as tmp:
        app = make_app(tmp)
        le._dock_patched.discard(app)
        ok = le._patch_no_dock(app)
        with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
            data = plistlib.load(f)
        check("① 写入 LSUIElement=True", ok and data.get("LSUIElement") is True, data)
        check("① 保留其他键", data.get("CFBundleName") == "LibreOffice", data)


def test_idempotent():
    with tempfile.TemporaryDirectory(prefix="vdl_lo_test_") as tmp:
        app = make_app(tmp)
        le._dock_patched.discard(app)
        le._patch_no_dock(app)
        check("② 缓存命中不重复写", le._patch_no_dock(app) is False)
        le._dock_patched.discard(app)  # 模拟新进程
        check("② 清缓存后识别已补、幂等", le._patch_no_dock(app) is True)


def test_install_calls_patch():
    src = le._install_from_volume.__code__
    names = src.co_names
    check("③ 安装落点调用补标记", "_patch_no_dock" in names, names)


def test_installed_app_calls_patch():
    src = le.installed_app.__code__
    check("④ 存量自愈落点调用补标记", "_patch_no_dock" in src.co_names, src.co_names)


def main() -> int:
    print("== LibreOffice 无 Dock 图标守卫 ==")
    test_patch_writes_and_keeps_keys()
    test_idempotent()
    test_install_calls_patch()
    test_installed_app_calls_patch()
    if _FAILED:
        print(f"\n❌ {len(_FAILED)} 项失败: {_FAILED}")
        return 1
    print("\n✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
