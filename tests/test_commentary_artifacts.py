"""bundled 解说管线中间产物（input/work）自动清理的单元测试。

覆盖：
  * _commentary_stage_dirs 的安全边界（只认 commentary 工作根下的 input/work，
    绝不返回 output；环境变量缺失/命名不符/指向 HOME 一律 fail-safe 返回空）
  * _purge_commentary_artifacts 的删旧留新、目录活跃保护、符号链接不跟随目标、
    运行中任务引用跳过（含目录祖先关系）、output 成片永不触碰。

只调底层清理函数，用 os.utime 造旧 mtime，不触发解说 worker、不联网。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import pytest


@pytest.fixture(scope="module")
def vdl_app(tmp_path_factory):
    # 与 test_commentary_stash 同款最小 import：stub 掉解说 worker，落点重定向到临时目录
    import types
    stub = types.ModuleType("commentary_worker_stub")
    sys.modules.setdefault("commentary_worker", stub)
    out = tmp_path_factory.mktemp("artifacts-out")
    os.environ.setdefault("VDL_COMMENTARY_LOCAL_OUTPUT", str(out))
    os.environ.setdefault("VDL_COMMENTARY_ENABLED", "1")
    sys.path.insert(0, str(HERE.parent / "server"))
    import app as _app
    return _app


@pytest.fixture
def stage_root(tmp_path, monkeypatch, vdl_app):
    """名字为 commentary 的工作根，含 input/work/output；注入 COMMENTARY_WORK_ROOT。"""
    root = tmp_path / "commentary"
    for sub in ("input", "work", "output"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("COMMENTARY_WORK_ROOT", str(root))
    return root


def _age(p: Path, days: int = 30, *, follow_symlinks: bool = True) -> None:
    """把 mtime 调到 days 天前（默认保留期 7 天，30 天前稳稳过期）。"""
    old = time.time() - days * 86400
    os.utime(str(p), (old, old), follow_symlinks=follow_symlinks)


# ---- _commentary_stage_dirs 安全边界 ----

def test_stage_dirs_only_input_work(vdl_app, stage_root):
    dirs = vdl_app._commentary_stage_dirs()
    assert len(dirs) == 2
    assert [d.name for d in dirs] == ["input", "work"]
    # resolve 后比较，规避 macOS /var -> /private/var 符号链接差异
    assert all(d.parent.resolve() == stage_root.resolve() for d in dirs)
    assert all(d.name != "output" for d in dirs)


def test_stage_dirs_missing_env_is_failsafe(vdl_app, monkeypatch):
    monkeypatch.delenv("COMMENTARY_WORK_ROOT", raising=False)
    assert vdl_app._commentary_stage_dirs() == []


def test_stage_dirs_rejects_non_commentary_name(vdl_app, tmp_path, monkeypatch):
    monkeypatch.setenv("COMMENTARY_WORK_ROOT", str(tmp_path))
    assert vdl_app._commentary_stage_dirs() == []


def test_stage_dirs_rejects_home(vdl_app, monkeypatch):
    monkeypatch.setenv("COMMENTARY_WORK_ROOT", str(Path.home()))
    assert vdl_app._commentary_stage_dirs() == []


# ---- _purge_commentary_artifacts 行为 ----

def test_purge_removes_old_keeps_recent(vdl_app, stage_root):
    old_in = stage_root / "input" / "old.mp4"
    fresh_in = stage_root / "input" / "fresh.mp4"
    old_w = stage_root / "work" / "old.wav"
    fresh_w = stage_root / "work" / "fresh.wav"
    for p in (old_in, fresh_in, old_w, fresh_w):
        p.write_bytes(b"x" * 32)
    for p in (old_in, old_w):
        _age(p)

    n = vdl_app._purge_commentary_artifacts()
    assert n == 2
    assert not old_in.exists() and not old_w.exists()
    assert fresh_in.exists() and fresh_w.exists()


def test_retention_boundary_is_seven_days(vdl_app, stage_root):
    """8 天前该删、6 天前该留：确认默认保留期确为 7 天（而非沿用旧值 14 天）。"""
    older = stage_root / "input" / "eight_days.mp4"
    newer = stage_root / "input" / "six_days.mp4"
    older.write_bytes(b"x" * 32)
    newer.write_bytes(b"x" * 32)
    _age(older, days=8)
    _age(newer, days=6)
    assert vdl_app._purge_commentary_artifacts() == 1
    assert not older.exists()
    assert newer.exists()


def test_purge_never_touches_output(vdl_app, stage_root):
    final = stage_root / "output" / "final_old.mp4"
    final.write_bytes(b"final")
    _age(final)
    assert vdl_app._purge_commentary_artifacts() == 0
    assert final.exists()


def test_symlink_removes_link_only_not_target(vdl_app, stage_root, tmp_path):
    target = tmp_path / "outside_target.mp4"   # 故意放在 input 之外
    target.write_bytes(b"target")
    _age(target)
    link = stage_root / "input" / "alias.mp4"
    link.symlink_to(target)
    try:
        _age(link, follow_symlinks=False)
    except (NotImplementedError, OSError):
        pytest.skip("平台不支持修改符号链接自身 mtime")

    n = vdl_app._purge_commentary_artifacts()
    assert n == 1
    assert not link.exists()        # 链接本身被删
    assert target.exists()          # 目标绝对不能被动到


def test_running_job_file_is_skipped(vdl_app, stage_root):
    busy = stage_root / "work" / "busy.wav"
    busy.write_bytes(b"busy")
    _age(busy)
    with vdl_app._commentary_lock:
        vdl_app.commentary_jobs["_test_busy"] = {
            "status": "running", "src_path": str(busy),
        }
    try:
        assert vdl_app._purge_commentary_artifacts() == 0
        assert busy.exists()
    finally:
        with vdl_app._commentary_lock:
            vdl_app.commentary_jobs.pop("_test_busy", None)


def test_dir_with_recent_file_is_protected(vdl_app, stage_root):
    d = stage_root / "work" / "active_job"
    d.mkdir()
    seg = d / "seg_recent.wav"
    seg.write_bytes(b"new")
    _age(d)                 # 目录壳子很老，但里面有刚写入的活跃文件
    assert vdl_app._purge_commentary_artifacts() == 0
    assert d.exists() and seg.exists()


def test_running_job_inside_dir_protects_dir(vdl_app, stage_root):
    d = stage_root / "work" / "active_job2"
    d.mkdir()
    seg = d / "seg_old.wav"
    seg.write_bytes(b"old")
    _age(d)
    _age(seg)              # 连文件也设旧，排除「近期文件保护」的干扰
    with vdl_app._commentary_lock:
        vdl_app.commentary_jobs["_test_dir"] = {
            "status": "running", "src_path": str(seg),
        }
    try:
        # 仅靠 running 目录祖先前缀匹配保住目录
        assert vdl_app._purge_commentary_artifacts() == 0
        assert d.exists() and seg.exists()
    finally:
        with vdl_app._commentary_lock:
            vdl_app.commentary_jobs.pop("_test_dir", None)


def test_old_dir_all_old_files_removed(vdl_app, stage_root):
    d = stage_root / "work" / "done_job"
    d.mkdir()
    f = d / "seg.wav"
    f.write_bytes(b"old")
    _age(d)
    _age(f)
    assert vdl_app._purge_commentary_artifacts() == 1
    assert not d.exists()


# ---- 回归守卫：本次根因是「_purge_commentary_stash 有定义却从未被调用」----
# 三处清理必须同时接进周期循环与启动期清理，缺一即视为回归。

def test_all_purges_wired_into_cleanup_loop(vdl_app):
    import inspect
    src = inspect.getsource(vdl_app._cleanup_loop)
    for name in ("_purge_commentary_work", "_purge_commentary_stash",
                 "_purge_commentary_artifacts"):
        assert name in src, f"{name} 未接入 _cleanup_loop（写了却没接线）"


def test_all_purges_wired_into_startup_cleanup(vdl_app):
    import inspect
    src = inspect.getsource(vdl_app.lifespan)
    for name in ("_purge_commentary_work", "_purge_commentary_stash",
                 "_purge_commentary_artifacts"):
        assert name in src, f"{name} 未接入启动期清理"
