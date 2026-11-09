"""server/tests/test_commentary_range_trim.py — 解说「正剧范围 → 实际输入」裁剪判据回归（2026-09-20）。

用户实测（原话）：
    「渲染只选了 15 分钟，解说也是 15 分钟没问题，但是成片全集 46 分钟，
      关键是我只截取 15 分钟，后面 31 分钟没有解说为什么也在成片里面，没有剪掉呢？」

根因（实证）：`_commentary_run` 的裁剪判据在 2026-09-19 引入 `_fold_commentary_range`
时被写成 `0.0 < ts < te` —— 只要「正剧开始」留空（ds=0 ⇒ ts=0），就走 else 分支拿**整片源**；
而 drama 窗口只作用于旁白 ⇒ 出现「解说 15 分钟 + 画面 46 分钟」的错位成片。
证据链：`input/<job>.mp4` 直接软链整片（无 `trim_*`）、whisper 的 wav 2733.5s、
transcript 覆盖 0~2653s，而 script.json options 是 `drama_start_sec=null, drama_end_sec=900`。

覆盖：
  fold_head_to_middle_keeps_start_zero   fold(整片, ds 空, de=900) → (0, 900)，不是整片
  needs_trim_head_to_middle              ★核心回归：ts=0 也必须裁
  needs_trim_full_span_skipped           覆盖整片 ⇒ 不裁（不许白重编码一遍）
  needs_trim_empty_or_unknown_skipped    空区间 / 源时长未知 ⇒ 不裁（fail-open，绝不因参数出废片）
  needs_trim_middle_subrange             中间子区间照旧裁
  needs_trim_swapped_range               「片尾开始」<「正剧开始」→ 交换归一化后仍要裁
  needs_trim_tolerance_matches_fold      容差口径与 fold 的「覆盖整片」判据一致
  fold_and_needs_trim_always_agree       ★不变量：fold 返回真子区间 ⇔ 必须裁（两处判据不许再漂移）

运行：
    cd server && python tests/test_commentary_range_trim.py
    cd server && python -m pytest tests/test_commentary_range_trim.py -v
"""
import os
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402  先完成 app 初始化，避免 routers 循环导入

fold = server_app._fold_commentary_range
needs = server_app._commentary_needs_trim

# 用户那支片子：45 分 33 秒
DUR = 2733.545941


# ── 1. 折叠区间本身 ─────────────────────────────────────────────────── #
def test_fold_head_to_middle_keeps_start_zero():
    """「正剧开始」留空 + 「片尾开始」= 15 分钟 → (0, 900)，且**不是**整片。"""
    ts, te = fold(DUR, 0.0, 0.0, None, 900.0)
    assert (ts, te) == (0.0, 900.0), f"应折成 (0, 900)，实际 {(ts, te)}"


def test_fold_unknown_duration_reports_whole_span():
    """源时长探测失败（0）时保持 fail-open：返回 (0, 0)。"""
    assert fold(0.0, 0.0, 0.0, None, 900.0) == (0.0, 0.0)


# ── 2. 裁剪判据（本次病灶）───────────────────────────────────────────── #
def test_needs_trim_head_to_middle():
    """★核心回归：从片头裁到中间（ts=0）必须裁——否则 worker 拿到整片。"""
    assert needs(0.0, 900.0, DUR) is True, "ts=0 + te<整片 必须触发预裁"


def test_needs_trim_full_span_skipped():
    """覆盖整片 ⇒ 不裁（否则整片会被白重编码一遍，正是 08-26 版的毛病）。"""
    assert needs(0.0, DUR, DUR) is False
    assert needs(0.0, 0.0, DUR) is False


def test_needs_trim_empty_or_unknown_skipped():
    """空区间 / 源时长未知 ⇒ 不裁（fail-open，绝不因参数问题出废片）。"""
    assert needs(300.0, 300.0, DUR) is False      # te == ts
    assert needs(900.0, 300.0, DUR) is False      # 反向（fold 之前不该走到这里）
    assert needs(0.0, 900.0, 0.0) is False        # 源时长未知
    assert needs(0.0, 0.0, 0.0) is False


def test_needs_trim_middle_subrange():
    """中间子区间（原 09-19 前唯一能裁的场景）行为不许变。"""
    assert needs(600.0, 1500.0, DUR) is True


def test_needs_trim_swapped_range():
    """「片尾开始」填得比「正剧开始」早 → 交换归一化后仍要裁（不许退回整片）。"""
    ts, te = fold(DUR, 0.0, 0.0, 900.0, 300.0)
    assert (ts, te) == (300.0, 900.0), f"交换归一化失败：{(ts, te)}"
    assert needs(ts, te, DUR) is True


def test_needs_trim_tolerance_matches_fold():
    """容差口径必须与 fold 的「覆盖整片 ⇒ 不裁」一致（0.5s 内视为整片）。"""
    assert needs(0.0, DUR - 0.4, DUR) is False, "0.5s 容差内的「接近整片」不该裁"
    assert needs(0.0, DUR - 2.0, DUR) is True, "超出容差的真子区间必须裁"


# ── 3. 不变量：两处判据永远不许再漂移 ────────────────────────────────── #
def test_fold_and_needs_trim_always_agree():
    """★不变量：fold 返回「整片」⇔ 不裁；返回真子区间 ⇔ 必须裁。

    这条正是本次事故的抽象：fold 的「覆盖整片」规则与调用方的裁剪条件各写了一份，
    09-19 改了一处忘了另一处 ⇒ ts=0 的区间被静默丢掉。此后任何一侧改动，
    只要口径不一致就会被这条用例抓住。
    """
    cases = [
        (0.0, 0.0, None, None),      # 全空 → 整片
        (0.0, 0.0, None, 900.0),     # 只有片尾开始（用户本次的配置）
        (0.0, 0.0, 900.0, None),     # 只有正剧开始
        (0.0, 0.0, 300.0, 900.0),    # 中段
        (0.0, DUR, None, None),      # 显式整片
        (0.0, 0.0, 0.0, DUR),        # 显式整片（绝对秒）
        (100.0, 0.0, None, None),    # 外层起点
        (0.0, 1000.0, 200.0, 800.0), # 交集
    ]
    for ts0, te0, ds, de in cases:
        ts, te = fold(DUR, ts0, te0, ds, de)
        whole = (ts <= 0.0 and te >= DUR) or te <= ts
        assert needs(ts, te, DUR) is (not whole), (
            f"fold({ts0},{te0},{ds},{de}) → ({ts},{te}) 与裁剪判据不一致"
            f"（expected needs_trim={not whole}）"
        )


# ── 4. 静态守卫：判据只许有一份 ──────────────────────────────────────── #
def test_run_uses_shared_trim_decision():
    """静态守卫：`_commentary_run` 必须调用共享判据，不许再自己写一遍。

    两次翻车都是「同一规则在两处各写一份，改一处忘另一处」：
    08-26 写了 `0 <= ts < te`，09-19 收窄成 `0.0 < ts < te` —— 静默丢掉 ts=0。
    上面的用例都在测共享判据本身，只有这条能拦住「调用方又抄一份」。
    """
    with open(os.path.join(_SERVER_DIR, "app.py"), encoding="utf-8") as f:
        src = f.read()
    assert "if _commentary_needs_trim(ts, te, src_dur):" in src, \
        "裁剪分支必须调用 _commentary_needs_trim"
    for bad in ("if 0.0 < ts < te:", "if 0 <= ts < te", "if te > ts > 0"):
        assert bad not in src, f"发现散布在别处的裁剪判据（会把 ts=0 漏掉）：{bad}"


def test_range_offset_surfaces_in_script_api():
    """★2026-11-09 用户：「选了 10:00–15:00，解说文案还是从头开始说」。

    实证：裁剪本身是对的（步骤写「只处理 600~900s 这段，共 300s」、wav 恰好 300s、
    转写内容就是那段），但管线拿到的片段**时间轴从 0 起**，审核面板原样显示成 2:11 / 2:22
    —— 用户对不上自己选的 10 分钟，误判成「没按范围生成」。
    修法：job 记下绝对区间，`/api/commentary/script` 回传，前端把时间显示换算成原片位置。
    本用例守四环接线，任一环掉了就退回「显示 2:11」的旧观感。
    """
    root = os.path.dirname(_SERVER_DIR)
    with open(os.path.join(_SERVER_DIR, "app.py"), encoding="utf-8") as f:
        app_src = f.read()
    assert '_job["range_start"] = round(ts, 3)' in app_src, \
        "裁剪后必须把绝对起点记进 job（否则前端无从换算）"
    with open(os.path.join(_SERVER_DIR, "routers", "commentary.py"), encoding="utf-8") as f:
        rt_src = f.read()
    assert '"range_start": float(job.get("range_start") or 0.0)' in rt_src, \
        "/api/commentary/script 必须回传 range_start"
    with open(os.path.join(root, "web", "app.js"), encoding="utf-8") as f:
        js = f.read()
    assert "seg.start + _rangeStart" in js, "审核面板必须把片段内时间换算成原片位置"
    assert "com-script-range" in js, "缺少「本片只处理原片 X–Y」说明条"
    with open(os.path.join(root, "web", "styles.css"), encoding="utf-8") as f:
        css = f.read()
    assert ".com-script-range" in css, "说明条样式缺失"


_TESTS = [
    test_range_offset_surfaces_in_script_api,
    test_fold_head_to_middle_keeps_start_zero,    test_fold_unknown_duration_reports_whole_span,
    test_needs_trim_head_to_middle,
    test_needs_trim_full_span_skipped,
    test_needs_trim_empty_or_unknown_skipped,
    test_needs_trim_middle_subrange,
    test_needs_trim_swapped_range,
    test_needs_trim_tolerance_matches_fold,
    test_fold_and_needs_trim_always_agree,
    test_run_uses_shared_trim_decision,
]

if __name__ == "__main__":
    failed = 0
    for t in _TESTS:
        try:
            t()
            print(f"  ✓ {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ✗ {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(_TESTS) - failed}/{len(_TESTS)} 通过")
    raise SystemExit(1 if failed else 0)
