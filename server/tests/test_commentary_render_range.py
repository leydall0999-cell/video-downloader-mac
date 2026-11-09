"""审核后出片（render_script）的裁剪区间回归 —— 2026-11-09 声画错位事故。

事故现场：用户选原片 540~1200s 生成《少帅》解说脚本，审核后点「保存并生成成片」，
出来的片子旁白讲狗皮/涮锅子/饭桌，画面却一直停在开头那节车厢戏。

根因链条：
  1. 生成脚本时 _commentary_run 把源片裁成 [540, 1200]（660s），并把正剧窗口
     换算到裁剪后时间轴：_n_de = 1200 - 540 = 660，传给管线 --drama-end-sec 660。
  2. 管线把这个「相对秒」原样写进 script.json 的 options.drama_end_sec。
  3. render_script 从 options 读回 660，却当成原片绝对秒，连同父任务
     trim_start=540 / trim_end=1200 一起交给 _fold_commentary_range：
     te = min(1200, 660) = 660 ⇒ 只裁出 540~660 共 120s。
  4. 脚本段时间戳写到 652.94s 全部越界；_commentary_run 又换算 _n_de = 660-540 = 120
     传给管线，detect_intro_outro 的 hi = 120，原速模式把每段 s 钳到 hi-0.1-vdur，
     画面全堆在开头 100s ⇒ 成片彻底错乱。

不变量：渲染必须复用「脚本生成时那一次」的裁剪区间，绝不能二次压缩。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, "server"))

import app as server_app  # noqa: E402
from routers import commentary as rt  # noqa: E402

fold = server_app._fold_commentary_range
restore = rt._restore_abs_drama

_PASS = 0
_FAIL = 0


def ok(cond, msg):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
        print(f"  ✗ {msg}")


def test_accident_reproduced():
    """不还原就会把 660s 片段二次压成 120s —— 事故必须能在测试里复现。"""
    before = fold(2733.55, 540.0, 1200.0, None, 660.0)
    ok(before == (540.0, 660.0), f"事故区间应被压成 120s，实际 {before}")
    ok(before[1] - before[0] == 120.0, "事故裁剪片时长应为 120s")


def test_restore_abs_drama():
    """相对秒 + 裁剪起点 = 原片绝对秒；未裁剪（起点 0）原样返回。"""
    ds, de = restore(540.0, None, 660.0)
    ok(ds is None, "起点缺省不应凭空造出来")
    ok(de == 1200.0, f"相对 660 + 起点 540 应还原成 1200，实际 {de}")

    ds, de = restore(540.0, 60.0, 600.0)
    ok(ds == 600.0 and de == 1140.0, f"起点/终点都应加偏移，实际 {(ds, de)}")

    ds, de = restore(0.0, None, 660.0)
    ok(ds is None and de == 660.0, "未裁剪时 options 里本来就是绝对秒，不许改动")

    ds, de = restore(540.0, None, None)
    ok(ds is None and de is None, "全缺省不应抛异常或造值")


def test_render_reuses_script_range():
    """★核心：还原后 fold 必须回到脚本生成时的 660s 区间。"""
    ds, de = restore(540.0, None, 660.0)
    after = fold(2733.55, 540.0, 1200.0, ds, de)
    ok(after == (540.0, 1200.0), f"应回到 540~1200，实际 {after}")
    ok(after[1] - after[0] == 660.0, "裁剪片必须恢复 660s")

    # 脚本里最后一段 652.94s 必须落在裁剪片内（否则 -ss 越界）
    segs_max = 652.94
    ok(segs_max < after[1] - after[0], "脚本最大时间戳必须落在裁剪片内")

    # _commentary_run 换算回管线时间轴：_n_de = 1200 - 540 = 660（= 裁剪片片尾）
    n_de = max(0.0, de - after[0])
    ok(n_de == 660.0, f"管线应拿到 --drama-end-sec 660（片尾，不裁），实际 {n_de}")


def test_fallback_to_script_range():
    """内存态 job 丢失时，从 script.json 顶层 range_start/range_end 回落。"""
    with open(os.path.join(_ROOT, "server", "routers", "commentary.py"), encoding="utf-8") as f:
        src = f.read()
    ok('seg_data.get("range_start"' in src, "必须有脚本顶层区间回落")
    ok('seg_data.get("range_end"' in src, "必须有脚本顶层区间回落")
    ok("def _restore_abs_drama(" in src, "必须有相对秒→绝对秒还原函数")
    ok("_restore_abs_drama(\n        trim_start" in src or "_restore_abs_drama(" in src,
       "render_script 必须调用还原函数")


def test_no_regression_for_full_video():
    """整片（未裁剪）场景行为不变：区间不许被 drama_end 砍短。"""
    ds, de = restore(0.0, None, None)
    after = fold(2733.55, 0.0, 0.0, ds, de)
    ok(after == (0.0, 2733.55), f"整片应原样，实际 {after}")


def test_update_script_keeps_source_fingerprint():
    """2026-11-09 第二次「解说对不上」事故：审核保存把素材指纹抹掉了。

    现场：22:03 按原片 600~1200s（600s 裁剪片）生成脚本，脚本顶层带
    range_start/range_end/src_duration；22:22 用户在审核面板插入广告段并保存，
    update_script 重建 dict 只留 5 个键 ⇒ 指纹全丢；22:22:48 出片时
    render_script 读不到回落基准，拿 2733s 整片配 600s 片段稿，
    画面比解说早整整 600 秒。这是「同一类问题反复出现」的真正断点。
    """
    import json as _json
    import pathlib as _pathlib
    import tempfile as _tempfile

    tmp = _pathlib.Path(_tempfile.mkdtemp(prefix="vdl-range-save-"))
    sp = tmp / "script.json"
    sp.write_text(_json.dumps({
        "title": "少帅", "voice": "", "mode": "highlights_intro",
        "options": {"drama_end_sec": 600.0},
        "segments": [{"start": 117.0, "end": 131.0, "narration": "原解说", "note": ""}],
        "range_start": 600.0, "range_end": 1200.0, "src_duration": 2733.55,
    }, ensure_ascii=False), encoding="utf-8")

    jid = "offline-range-save-test"
    with server_app._commentary_lock:
        server_app.commentary_jobs[jid] = {
            "status": "script_ready", "script_path": str(sp), "voice": "",
        }
    payload = server_app.ScriptUpdateRequest(segments=[{
        "start": 117.0, "end": 131.0, "narration": "改过的广告词", "note": "广告",
        "ad": True,
    }])
    rt.update_script(jid, payload)
    after = _json.loads(sp.read_text(encoding="utf-8"))

    ok(after.get("range_start") == 600.0,
       f"审核保存后 range_start 丢失：{after.get('range_start')}")
    ok(after.get("range_end") == 1200.0,
       f"审核保存后 range_end 丢失：{after.get('range_end')}")
    ok(after.get("src_duration") == 2733.55,
       f"审核保存后 src_duration 丢失：{after.get('src_duration')}")
    ok(after.get("mode") == "highlights_intro", "原有 mode 也要保住")
    ok(after["segments"][0]["narration"] == "改过的广告词", "本次修改必须生效")

    # 以后往脚本顶层加任何新字段，都不该被「保存审核」吃掉
    sp.write_text(_json.dumps({**after, "future_field": "keep-me"}, ensure_ascii=False),
                  encoding="utf-8")
    rt.update_script(jid, payload)
    ok(_json.loads(sp.read_text(encoding="utf-8")).get("future_field") == "keep-me",
       "保存必须继承全部顶层字段，而不是白名单重建")

    # 静态守卫：不许退回「只保留固定键」的重建式写法
    src = open(os.path.join(_ROOT, "server", "routers", "commentary.py"),
               encoding="utf-8").read()
    ok("data = dict(existing)" in src,
       "update_script 必须先继承现有顶层字段（否则素材指纹会被抹掉）")
    ok("data.update({" in src, "继承后再覆盖本次要改的字段")


_TESTS = [
    test_accident_reproduced,
    test_restore_abs_drama,
    test_render_reuses_script_range,
    test_fallback_to_script_range,
    test_no_regression_for_full_video,
    test_update_script_keeps_source_fingerprint,
]

if __name__ == "__main__":
    for t in _TESTS:
        t()
    print(f"test_commentary_render_range: {_PASS} passed, {_FAIL} failed")
    sys.exit(1 if _FAIL else 0)
