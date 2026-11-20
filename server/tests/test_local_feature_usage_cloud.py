"""server/tests/test_local_feature_usage_cloud.py — 纯本地功能用量上云守卫（2026-11-20）

需求来源：用户反馈后台「使用统计」里纯本地功能（订阅追更/种子下载/文档转换等）
完全看不到。根因是这些功能既不走配额 use_daily、也没有 report_feature_usage_cloud
上报，本机 stats.json 的数据服务器读不到。

本文件钉死：
  1) 原本完全没有上云通道的 3 个功能（docconvert/subscribe/torrent）都在成功点
     调了 `_cloud_report` + `record_event`；
  2) 已有上云通道的功能（compress/sr/convert 走 app_compute_count、matting/dewatermark
     走 report_feature_usage_cloud、idphoto 走 use_daily）**不得**再加 _cloud_report，
     防止双计；
  3) `report_feature_usage_cloud` 走的是 report_usage_async（daily 计数），不经
     use_daily（配额闸门）——保证「只统计、不拦用户」。
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✓ " if cond else "  ❌ ") + name + ("" if cond else "  " + detail[:300]))
    if not cond:
        FAILED.append(name)


def _read(rel: str) -> str:
    return (SERVER / rel).read_text(encoding="utf-8")


def test_new_cloud_reports() -> None:
    """3 个原本没上云的功能必须有 _cloud_report + record_event。"""
    cases = {
        "routers/docconvert.py": ("_cloud_report(\"docconvert\")", "record_event(\"docconvert\")"),
        "routers/subscriptions.py": ("_cloud_report(\"subscribe\")", "record_event(\"subscribe\")"),
        "routers/torrents.py": ("_cloud_report(\"torrent\")", "record_event(\"torrent\")"),
    }
    for rel, (cloud, local) in cases.items():
        src = _read(rel)
        check(f"{rel}: 有云上报 {cloud}", cloud in src)
        check(f"{rel}: 有本机埋点 {local}", local in src)
        check(f"{rel}: 定义了 _cloud_report", "def _cloud_report(" in src)


def test_no_double_count() -> None:
    """已有上云通道的功能不得再加 _cloud_report（防双计）。"""
    # compress/sr/convert 走 app_compute_count -> use_daily 上云
    for rel in ("routers/compress.py", "routers/sr.py", "routers/convert.py"):
        src = _read(rel)
        check(f"{rel}: 未加 _cloud_report（防与配额双计）", "_cloud_report(" not in src)
    # matting/dewatermark 自己已有 _cloud_report；idphoto/image 走 use_daily
    for rel in ("routers/idphoto.py", "routers/image.py"):
        src = _read(rel)
        check(f"{rel}: 未加重复 _cloud_report", "_cloud_report(" not in src)


def test_reporter_uses_daily_not_quota() -> None:
    """report_feature_usage_cloud 必须走 report_usage_async，不经 use_daily。"""
    src = _read("routers/cloud_account.py")
    m = re.search(r"def report_feature_usage_cloud\([^)]*\)[^:]*:.*?(?=\ndef |\Z)", src, re.S)
    check("report_feature_usage_cloud 存在", bool(m))
    if m:
        body = m.group(0)
        check("走 report_usage_async（只计次数）", "report_usage_async(" in body)
        check("不碰 use_daily（不拦配额）", "use_daily(" not in body)


def test_web_admin_mapping() -> None:
    """后台 FULL_FEATURES 的 keys 必须能匹配新上报的 resource。"""
    admin = (ROOT.parent / "video-downloader" / "web" / "admin" / "index.html")
    if not admin.exists():
        # 本测试默认单独跑（未纳入 run_offline_tests.sh）；换机/打包环境没有
        # web 仓时跳过这一项即可，不应让 App 端守卫因此变红。
        print("  ⚠️ web 后台文件不存在，跳过映射检查：" + str(admin))
        return
    src = admin.read_text(encoding="utf-8")
    for key in ("docconvert", "subscribe", "torrent"):
        check(f"后台映射含 {key}", f"'{key}'" in src or f'"{key}"' in src)


def main() -> None:
    test_new_cloud_reports()
    test_no_double_count()
    test_reporter_uses_daily_not_quota()
    test_web_admin_mapping()
    if FAILED:
        print(f"\n❌ {len(FAILED)} 项失败：" + "；".join(FAILED))
        sys.exit(1)
    print("\n✅ 纯本地功能用量上云守卫全部通过")


if __name__ == "__main__":
    main()
