"""回归（2026-11-16）：自动清理「跑过但没删东西」也必须刷新 last_run。

旧实现把 last_run 写在 `if result["removed"] or result["failed"]` 里，
保留期内没到期项时时间戳不动 ⇒ 界面一直显示上一次清理日期，用户以为
自动清理没生效。本测试用假的清理结果直接打 `_run_retention_once()`，
不扫真实磁盘、不删任何文件。

运行：cd server && python tests/test_retention_last_run.py
"""
import os
import sys
from unittest.mock import patch

_SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER not in sys.path:
    sys.path.insert(0, _SERVER)

import app as server_app  # noqa: E402


def test_last_run_updated_when_nothing_removed():
    result = {
        "ran_at": 1799999999.0,
        "removed": 0,
        "failed": 0,
        "freed": 0,
    }
    calls = []
    with patch.object(server_app.retention_mod, "run", return_value=result) as run_mock, \
            patch.object(server_app.retention_store, "update",
                         side_effect=lambda **kw: calls.append(kw)):
        got = server_app._run_retention_once()

    assert run_mock.called, "没有实际调用清理扫描"
    assert got is result, "应原样返回清理结果"
    assert calls, "没有到期项时也必须写回配置（否则界面时间不刷新）"
    assert calls[0].get("last_run") == result["ran_at"], \
        f"last_run 应刷新为本轮时间，实际 {calls[0]}"
    assert calls[0].get("last_removed") == 0, "清理条数应如实写 0"
    print("OK  零删除时 last_run 仍然刷新")


def test_failed_scan_does_not_write_last_run():
    calls = []
    with patch.object(server_app.retention_mod, "run", side_effect=RuntimeError("boom")), \
            patch.object(server_app.retention_store, "update",
                         side_effect=lambda **kw: calls.append(kw)):
        got = server_app._run_retention_once()
    assert got is None, "扫描失败应返回 None"
    assert not calls, "扫描失败时不应记录 last_run（避免误报清理成功）"
    print("OK  扫描失败不写 last_run")


if __name__ == "__main__":
    test_last_run_updated_when_nothing_removed()
    test_failed_scan_does_not_write_last_run()
    print("\n🎉 时效清理 last_run 回归测试全部通过")
