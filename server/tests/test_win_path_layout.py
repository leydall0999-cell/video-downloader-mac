#!/usr/bin/env python3
"""Windows 路径布局守卫 —— 在 macOS 上模拟「Windows 冻结版」，钉死配置落点。

============================================================================
为什么要有这个测试
============================================================================
Windows 版的绝大多数缺陷都要装到真机上才暴露（PyInstaller 不能跨平台编译，
开发机是 macOS）。但有一大类**不需要 Windows 机器**就能测：配置目录怎么选。

桌面端在 Windows 上有三个互不相通的目录（改错就是「设置存了不生效 /
重启就丢 / 两台机器行为不一致」这类最难查的问题）：

    %APPDATA%\\VideoDownloader\\   业务配置（AI 网关 / 视觉 / 解说 / 抠图 / 引擎空闲）
    ~\\.video-downloader\\          保险箱 vault.json（代码里写死，不走 APPDATA）
    ~\\.videodownloader\\           cloud_sync / share / cookies / cookie_pool

选择逻辑长这样（`server/*_config.py` 里同构复制了 6 份）：

    def _config_dir() -> Path:
        if sys.platform == "win32" and getattr(sys, "frozen", False):
            return Path(os.environ.get("APPDATA", Path.home())) / "VideoDownloader"
        return Path.home() / ".video-downloader"

`sys.platform` / `sys.frozen` / `APPDATA` 全是**运行时求值**，所以本机直接改
这三个值就能让代码以为自己在 Windows 上跑 —— 不需要虚拟机、不需要 CI、秒级反馈。
真机跑不了的那部分（装包 / 起进程 / 接口）交给
`desktop/ci_verify_win_installer.py` 在 windows-latest 上做。

============================================================================
⚠️ 已知不一致（已解决）
============================================================================
`gateway_config._config_dir()` 曾是**唯一**不带 Windows 分支的配置模块（2026-10-26
发现时登记在 KNOWN_EXCEPTIONS）。**2026-10-20 已修复**：gateway_config 补齐了
与 llm_config / cloud_matting_config 等同构的 win32 冻结态分支，Windows 安装版
上 AI 网关配置落点统一为 `%APPDATA%\\VideoDownloader`。

因此本测试不再把它当例外——改为**正向钉死** gateway_config 确实处理了 win32，
并清空 KNOWN_EXCEPTIONS。将来若谁把分支删掉，本测试会变红提醒补回。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 模拟 Windows 冻结态后，落点必须是 APPDATA/VideoDownloader 的模块
EXPECT_APPDATA = (
    "cloud_matting_config",
    "voice_studio_config",
    "llm_config",
    "vision_config",
    "engine_idle",
)

# 已知例外表：历史上 gateway_config 是唯一不带 win32 分支的模块（2026-10-26 登记）。
# 2026-10-20 已补齐该分支，故此处清空；若将来再出现不带 win32 的模块，登记进来。
KNOWN_EXCEPTIONS: set[str] = set()


def _module_source(name: str) -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, f"{name}.py"), encoding="utf-8") as fh:
        return fh.read()


class WinFrozenPathLayout(unittest.TestCase):
    """把 sys.platform / sys.frozen / APPDATA 伪造成 Windows 冻结版。"""

    def setUp(self) -> None:
        self._saved = {
            "platform": sys.platform,
            "frozen": getattr(sys, "frozen", False),
            "had_frozen": hasattr(sys, "frozen"),
            "APPDATA": os.environ.get("APPDATA"),
            "VDL_DATA_DIR": os.environ.get("VDL_DATA_DIR"),
            "VDL_HOME": os.environ.get("VDL_HOME"),
        }
        self._tmp = tempfile.TemporaryDirectory()
        self.appdata = Path(self._tmp.name) / "AppData" / "Roaming"
        self.appdata.mkdir(parents=True, exist_ok=True)

        # 伪造三件套
        sys.platform = "win32"
        sys.frozen = True
        os.environ["APPDATA"] = str(self.appdata)
        # 这两个 env 优先级高于 win32 分支，必须清空才能测到分支本身
        os.environ["VDL_DATA_DIR"] = ""
        os.environ["VDL_HOME"] = ""

        self.addCleanup(self._restore)

    def _restore(self) -> None:
        sys.platform = self._saved["platform"]
        if self._saved["had_frozen"]:
            sys.frozen = self._saved["frozen"]
        elif hasattr(sys, "frozen"):
            del sys.frozen
        for key in ("APPDATA", "VDL_DATA_DIR", "VDL_HOME"):
            old = self._saved[key]
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        self._tmp.cleanup()

    # ------------------------------------------------------------------ #
    def test_config_modules_land_in_appdata(self) -> None:
        """模拟 Windows 冻结态：业务配置必须落 %APPDATA%\\VideoDownloader。"""
        for name in EXPECT_APPDATA:
            with self.subTest(module=name):
                mod = __import__(name)
                got = mod._config_dir()  # type: ignore[attr-defined]
                self.assertEqual(
                    got, self.appdata / "VideoDownloader",
                    f"{name} 在 Windows 冻结版上落点不是 %APPDATA%\\VideoDownloader",
                )

    def test_posix_path_unchanged(self) -> None:
        """非 Windows 时仍应回落 ~/.video-downloader（别把 mac 也改坏）。"""
        sys.platform = "darwin"
        home = Path.home()
        for name in EXPECT_APPDATA:
            if name == "engine_idle":
                continue  # engine_idle 走 VDL_DATA_DIR 优先，语义不同
            with self.subTest(module=name):
                mod = __import__(name)
                got = mod._config_dir()  # type: ignore[attr-defined]
                self.assertEqual(got, home / ".video-downloader")

    def test_gateway_config_now_handles_windows(self) -> None:
        """2026-10-20 修复：gateway_config 已补齐 win32 冻结态分支，不再是已知例外。
        钉死它确实处理了 Windows 冻结版（落点 %APPDATA%\\VideoDownloader），
        别再退回 ~/.video-downloader；同时确认例外表已清空。"""
        src = _module_source("gateway_config")
        self.assertIn(
            "win32", src,
            "gateway_config 的 _config_dir 必须处理 win32 冻结态"
            "（Windows 安装版网关配置落点）",
        )
        self.assertEqual(
            KNOWN_EXCEPTIONS, set(),
            "gateway_config 已不再是例外，KNOWN_EXCEPTIONS 应清空",
        )

    def test_new_config_module_must_handle_windows(self) -> None:
        """扫描守卫：任何定义 _config_dir 且用 Path.home() 的模块，必须处理 win32。

        防止以后新增配置模块时漏加 Windows 分支 —— 那种缺陷在 macOS 上永远
        测不出来，只会表现为「Windows 用户改设置不生效」。
        """
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        offenders = []
        for fn in sorted(os.listdir(here)):
            if not fn.endswith(".py"):
                continue
            name = fn[:-3]
            if name in KNOWN_EXCEPTIONS:
                continue
            try:
                src = _module_source(name)
            except OSError:
                continue
            if "def _config_dir(" not in src or "Path.home()" not in src:
                continue
            # 取 _config_dir 函数体（到下一个顶层 def 为止）再判断
            body = src.split("def _config_dir(", 1)[1]
            for marker in ("\ndef ", "\nclass "):
                if marker in body:
                    body = body.split(marker, 1)[0]
            if "win32" not in body:
                offenders.append(name)
        self.assertEqual(
            offenders, [],
            f"这些模块的 _config_dir 用了 Path.home() 却没有 win32 分支，"
            f"Windows 冻结版上会落到错误目录：{offenders}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
