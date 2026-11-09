"""server/tests/test_commentary_ad_segments.py — 解说审核面板「广告口播 / 补充对白」段（2026-11-09）。

用户原话：
    「加个空白可挪动位置或者可自由输入时间的对话框，主要作用是方便后期加入广告对白」

需求拆解（验收口径）：
  1. 审核面板里能**新插一段空白对白**（不必重新生成脚本、不必改 AI 文案）；
  2. 这段的**时间由用户自己填**（原片时间，可随意挪到任意位置）；
  3. 保存后时间不许被旧值盖回去；重新打开面板仍可继续改时间；
  4. 渲染时它和普通解说段一样走 TTS + 字幕，落在用户指定的时刻。

实现要点（本文件守的契约）：
  · 前端把广告段标记成 `ad: true` + `note: "广告"`，时间取卡片输入框；
  · 后端 PUT 对 `ad=true` 的段采用 payload 的 start/end，普通段仍从原脚本回填时间
    —— 这条是「改完时间保存后又被旧时间盖回去」的唯一防线；
  · 保存时 payload 必须与原始段**索引对齐**（删掉的段留空串占位），否则后面的段全体前移、
    时间戳串行。广告段占原位置或追加末尾（末尾 i >= len(existing) ⇒ 采用自带时间）。

运行：
    cd server && VDL_CLOUD_LINK=0 python tests/test_commentary_ad_segments.py
"""
import json
import os
import sys
import tempfile
import pathlib

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402  先完成 app 初始化，避免 routers 循环导入
from routers import commentary as commentary_router  # noqa: E402

_ROOT = os.path.dirname(_SERVER_DIR)
JOB_ID = "offline-ad-seg-test"


def _with_script(segments):
    """在临时目录造一份 script.json 并挂一个 script_ready 的 job，返回 (path, job_id)。"""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="vdl-adseg-"))
    sp = tmp / "script.json"
    sp.write_text(json.dumps({"title": "t", "voice": "", "segments": segments, "options": {}},
                             ensure_ascii=False), encoding="utf-8")
    with server_app._commentary_lock:
        server_app.commentary_jobs[JOB_ID] = {
            "status": "script_ready", "script_path": str(sp), "voice": "",
        }
    return sp, JOB_ID


def _put(job_id, segments):
    payload = server_app.ScriptUpdateRequest(voice="", segments=segments)
    return commentary_router.update_script(job_id, payload)


# ── 1. 后端：广告段自带时间必须生效 ───────────────────────────────────── #
def test_ad_segment_keeps_user_time():
    """★核心：ad=true 的段，start/end 取 payload（用户填的），不许被原脚本时间覆盖。"""
    orig = [{"start": 10.0, "end": 14.0, "narration": "原解说", "note": "情绪"}]
    sp, jid = _with_script(orig)
    _put(jid, [
        {"start": 0, "end": 0, "narration": "改过的解说", "note": "情绪"},
        # 新插入的广告：用户把它放在原片 600s 处
        {"ad": True, "start": 600.0, "end": 603.5, "narration": "这里是广告口播", "note": "广告"},
    ])
    data = json.loads(pathlib.Path(sp).read_text(encoding="utf-8"))
    segs = data["segments"]
    assert len(segs) == 2, f"段数应为 2，实际 {len(segs)}"
    # 普通段：时间仍取原脚本（前端发 0/0 也不会把时间改坏）
    assert (segs[0]["start"], segs[0]["end"]) == (10.0, 14.0), f"普通段时间被改坏：{segs[0]}"
    assert segs[0]["narration"] == "改过的解说"
    # 广告段：时间必须一字不差地落盘
    assert (segs[1]["start"], segs[1]["end"]) == (600.0, 603.5), f"广告段时间被覆盖：{segs[1]}"
    assert segs[1]["ad"] is True, "广告标记必须落盘，否则下次打开面板不能继续改时间"
    assert segs[1]["note"] == "广告"


def test_ad_segment_reposition_after_reopen():
    """广告段二次打开后再改时间（换到别处）仍然生效 —— 用户诉求「可挪动位置」。"""
    orig = [{"start": 10.0, "end": 14.0, "narration": "A", "note": "情绪"},
            {"start": 600.0, "end": 603.0, "narration": "广告词", "note": "广告", "ad": True}]
    sp, jid = _with_script(orig)
    _put(jid, [
        {"start": 0, "end": 0, "narration": "A", "note": "情绪"},
        {"ad": True, "start": 1200.0, "end": 1202.0, "narration": "广告词", "note": "广告"},
    ])
    segs = json.loads(pathlib.Path(sp).read_text(encoding="utf-8"))["segments"]
    assert (segs[1]["start"], segs[1]["end"]) == (1200.0, 1202.0), f"挪位置失败：{segs[1]}"
    assert segs[1]["ad"] is True


def test_ad_time_is_sanitized():
    """负数 / 结束早于开始 ⇒ 归一化成合法区间，绝不写进坏时间把渲染搞崩。"""
    sp, jid = _with_script([])
    _put(jid, [
        {"ad": True, "start": -5, "end": -1, "narration": "x", "note": "广告"},
        {"ad": True, "start": 30, "end": 12, "narration": "y", "note": "广告"},
    ])
    segs = json.loads(pathlib.Path(sp).read_text(encoding="utf-8"))["segments"]
    assert (segs[0]["start"], segs[0]["end"]) == (0.0, 0.0), f"负数未夹零：{segs[0]}"
    assert segs[1]["end"] >= segs[1]["start"], f"结束早于开始：{segs[1]}"


def test_normal_segment_time_untouched_regression():
    """普通段（不带 ad）行为零变化：时间只认原脚本，前端传什么都不影响。"""
    orig = [{"start": 100.0, "end": 110.0, "narration": "旧", "note": ""}]
    sp, jid = _with_script(orig)
    _put(jid, [{"start": 7.0, "end": 9.0, "narration": "新", "note": ""}])
    seg = json.loads(pathlib.Path(sp).read_text(encoding="utf-8"))["segments"][0]
    assert (seg["start"], seg["end"]) == (100.0, 110.0), f"普通段时间被 payload 污染：{seg}"
    assert "ad" not in seg, "普通段不该被写成广告段"


# ── 1b. 自动校准（2026-11-09 用户：「文字多了、匹配的时间少了，能够自动调整」）── #
def test_calibrated_segment_adopts_user_time():
    """★核心：带 user_time 的普通段，start/end 取 payload（校准后的窗口），不许被旧时间盖回去。"""
    orig = [{"start": 778.3, "end": 779.3, "narration": "很长的一段旁白", "note": "高光"}]
    sp, jid = _with_script(orig)
    _put(jid, [{"start": 778.3, "end": 793.9, "narration": "很长的一段旁白",
                "note": "高光", "user_time": True}])
    seg = json.loads(pathlib.Path(sp).read_text(encoding="utf-8"))["segments"][0]
    assert (seg["start"], seg["end"]) == (778.3, 793.9), f"校准后的窗口没落盘：{seg}"
    assert seg["note"] == "高光", "校准不该动情绪标签"
    assert "ad" not in seg, "校准过的普通段不是广告段，不该带 ad 标记"


def test_user_time_needs_valid_span():
    """user_time 但区间不合法（0/0）⇒ 退回原时间，绝不能把段写成 0 时长（渲染会被整段丢掉）。"""
    orig = [{"start": 10.0, "end": 14.0, "narration": "A", "note": ""}]
    sp, jid = _with_script(orig)
    _put(jid, [{"start": 0, "end": 0, "narration": "A", "note": "", "user_time": True}])
    seg = json.loads(pathlib.Path(sp).read_text(encoding="utf-8"))["segments"][0]
    assert (seg["start"], seg["end"]) == (10.0, 14.0), f"坏区间没退回原时间：{seg}"


# ── 2. 前端静态守卫：四环接线缺一不可 ─────────────────────────────────── #
def test_frontend_wiring():
    with open(os.path.join(_ROOT, "web", "index.html"), encoding="utf-8") as f:
        html = f.read()
    assert 'id="comSegAddAd"' in html, "缺少「＋ 插入广告口播」入口"
    assert "com-seg-addbar" in html
    assert 'id="comSegCalib"' in html, "缺少「⚖ 自动校准」入口"

    with open(os.path.join(_ROOT, "web", "app.js"), encoding="utf-8") as f:
        js = f.read()
    for needle, why in [
        ("const AD_NOTE = '广告'", "广告段标识常量"),
        ("const comAddAdSeg", "新插入空白广告卡"),
        ("const comAdCommitTime", "改时间 → 回写草稿/dataset"),
        ('data-role="start"', "起始时间输入框"),
        ("ad: true", "提交时带 ad 标记"),
        ("comAdResort", "按时间重排（改完时间能挪到对应位置）"),
        ("currentScriptAdDrafts", "未保存草稿要能在时间轴上看到"),
        ("const base = orig.map(() => null)", "★保存必须与原始段索引对齐（不许退回过滤式）"),
        ("const comAutoCalibrate", "★自动校准主逻辑"),
        ("comApplyRowSpan", "校准改时间必须走同一个写入口"),
        ("user_time: true", "★校准后的段要带 user_time，后端才采用新窗口"),
        ("dataset.calibrated", "记录「这段时间被校准过」"),
        ("currentScriptDeleted", "普通段删除要记索引（否则时间轴留幽灵段）"),
        ("com-seg-del", "每段都有删除按钮"),
    ]:
        assert needle in js, f"前端缺少{why}：{needle}"
    # 索引对齐的两种落点：广告段占原位置 / 新广告追加末尾
    assert "if (idx >= 0 && idx < base.length) base[idx] = cell;" in js
    assert "else if (narration) ads.push(cell);" in js
    # 时间轴：广告段上轨 + 空占位不上轨
    assert "is-ad" in js and "com-tl-seg" in js

    with open(os.path.join(_ROOT, "web", "styles.css"), encoding="utf-8") as f:
        css = f.read()
    for needle in (".com-seg-row.is-ad", ".com-seg-tin", ".com-seg-addbar", ".com-tl-seg.is-ad",
                   ".com-seg-calib"):
        assert needle in css, f"样式缺失：{needle}"


def test_render_pipeline_accepts_ad_note():
    """渲染管线对 note="广告" 不做特殊处理（只有「开场钩子」特判），广告段按普通旁白出片。

    守的是「note 被用作广告标记」这个约定的安全性：管线若哪天按 note 分支，这里先红。
    """
    pipe = "/Users/suixindelang/WorkBuddy/问问题/commentary-pipeline/scripts/edit_ffmpeg.py"
    if not os.path.exists(pipe):
        return  # 外部管线不在本机：跳过（离线测试环境不依赖它）
    with open(pipe, encoding="utf-8") as f:
        src = f.read()
    assert 'note") == "开场钩子"' in src or 'note"] == "开场钩子"' in src or '开场钩子' in src
    assert '"广告"' not in src, "渲染管线不该按「广告」这个 note 值分支"


_TESTS = [
    test_ad_segment_keeps_user_time,
    test_ad_segment_reposition_after_reopen,
    test_ad_time_is_sanitized,
    test_normal_segment_time_untouched_regression,
    test_calibrated_segment_adopts_user_time,
    test_user_time_needs_valid_span,
    test_frontend_wiring,
    test_render_pipeline_accepts_ad_note,
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
