"""抖音「详情接口快路」守卫（2026-11-03）。

背景：抖音解析原来只有一条路——开浏览器 → 打开视频页 → 等它自己拉流 → 从 CDP
网络事件里抄 douyinvod 地址。慢（6~10s），而且只能拿到播放器当前选的那一档
（常见 540/720），时长恒为 None。

改用「页面内 fetch 详情接口」后实测 2.1s，一次给出 11~20 档码率、真实时长、封面、
无水印源。但这条路上有三个**静默失败**的坑，所以必须用测试钉住：

  ① 裸 HTTP 打 /aweme/v1/web/aweme/detail/ 恒 403「Uifid Not Found」（缺 a_bogus
     签名）⇒ 必须在**页面上下文**里发，不能自己发；失败必须回退抓流而不是报错。
  ② 无头浏览器 UA 带 `HeadlessChrome` ⇒ 抖音判机器人，接口恒 S403（实测有头 1.2s
     成功、无头 4.7s 全败、无头+真 UA 1.4s 成功）⇒ 无头必须伪装 UA。
  ③ 竖屏 1080P 的真实像素是 1080x1440，若拿长边去判「≤1080P」上限，竖屏视频会
     被整片误判超限降档 ⇒ 档位判定用标签短边，显示用真实像素。

前两条只能静态守卫（要真浏览器才能验），第三条是纯函数，直接跑数据。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SERVER = REPO / "server"
DOUYIN = SERVER / "douyin_cdp.py"

sys.path.insert(0, str(SERVER))

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


def _src() -> str:
    return DOUYIN.read_text(encoding="utf-8")


def _fn_block(src: str, marker: str) -> str:
    """取从 marker 起、到下一个顶层 def/class 之前的源码块。"""
    i = src.find(marker)
    if i < 0:
        return ""
    rest = src[i + len(marker):]
    m = re.search(r"\n(?=(?:def |class |@))", rest)
    return marker + (rest[: m.start() + 1] if m else rest)


def main() -> int:
    src = _src()
    import douyin_cdp as d

    print("① 作品 ID 解析（各种链接形态）")
    cases = {
        "https://www.douyin.com/video/7683799657923301297": "7683799657923301297",
        "https://www.douyin.com/video/7683799657923301297?previous_page=app": "7683799657923301297",
        "https://www.douyin.com/note/7500000000000000001": "7500000000000000001",
        "https://www.douyin.com/user/MS4wLjAB?modal_id=7300000000000000002": "7300000000000000002",
        "https://example.com/video/123": "",
        "": "",
    }
    for url, want in cases.items():
        got = d._aweme_id(url)
        check(f"{url[:46] or '(空)'} → {want or '空'}", got == want, f"得到 {got!r}")

    print()
    print("② 档位挑选（封顶 1080P，竖屏不能被误杀）")

    def gear(name, br, w, h):
        return {
            "gear_name": name,
            "bit_rate": br,
            "play_addr": {"url_list": [f"https://cdn/{name}.mp4"],
                          "width": w, "height": h},
        }

    # 竖屏 1080P：档位标签 1080，真实像素 1080x1440 —— 必须命中，且宽高取真实值
    aw = {"video": {"bit_rate": [
        gear("normal_1080_0", 3529864, 1080, 1440),
        gear("normal_540_0", 2337323, 576, 768),
        gear("low_540_0", 2167953, 576, 768),
    ]}}
    tr = d._pick_track(aw)
    check("竖屏 1080P 命中（不被长边 1440 误判超限）",
          tr and tr["gear"] == "normal_1080_0", str(tr))
    check("宽高取真实像素 1080x1440",
          tr and (tr["w"], tr["h"]) == (1080, 1440), str(tr and (tr["w"], tr["h"])))

    # 有 4K 也有 1080：必须封顶在 1080，不能因 4K 码率高就选它
    aw = {"video": {"bit_rate": [
        gear("adapt_2160_0", 9000000, 2160, 2880),
        gear("normal_1080_0", 3529864, 1080, 1440),
    ]}}
    tr = d._pick_track(aw)
    check(">1080P 档位被封顶拦下（免费档上限）",
          tr and tr["gear"] == "normal_1080_0", str(tr))

    # 只有 540：如实给 540
    aw = {"video": {"bit_rate": [gear("low_540_0", 2167953, 576, 768)]}}
    tr = d._pick_track(aw)
    check("只有 540 档时如实返回", tr and tr["gear"] == "low_540_0", str(tr))

    # 同档位多条：取码率最高的
    aw = {"video": {"bit_rate": [
        gear("normal_720_0", 1500000, 720, 960),
        gear("normal_720_1", 2000000, 720, 960),
    ]}}
    tr = d._pick_track(aw)
    check("同分辨率取最高码率",
          tr and tr["br"] == 2000000 and tr["gear"] == "normal_720_1", str(tr))

    # 没有 bit_rate（老视频）：退到 play_addr 单条
    aw = {"video": {"play_addr": {"url_list": ["https://cdn/only.mp4"],
                                  "width": 720, "height": 1280}}}
    tr = d._pick_track(aw)
    check("无 bit_rate 时回退 play_addr",
          tr and tr["urls"] == ["https://cdn/only.mp4"] and tr["h"] == 1280, str(tr))

    # 图文/私密：连地址都没有 → None（让调用方回退抓流，而不是崩）
    check("完全没有地址时返回 None", d._pick_track({"video": {}}) is None)
    check("video 字段缺失时返回 None", d._pick_track({}) is None)

    print()
    print("③ 无头必须伪装 UA（HeadlessChrome 会被抖音判死）")
    ensure = _fn_block(src, "def _ensure_mode")
    check("无头 flags 里带 --user-agent", "--user-agent=" in ensure,
          "删掉这行会让快路恒 403（实测 4.7s 全败）")
    check("UA 由 _real_ua() 现算（版本与引擎自洽）",
          "_real_ua()" in ensure and "def _real_ua" in src,
          "硬编码版本会和实际引擎矛盾，是更明显的指纹")
    check("UA 里不含 HeadlessChrome",
          "HeadlessChrome" not in _fn_block(src, "def _real_ua"))

    print()
    print("④ 快路优先、失败静默回退（不能把 403 抛给用户）")
    unlock = _fn_block(src, "def _resolve_unlocked")
    i_detail = unlock.find("_detail(")
    i_capture = unlock.find("_capture(")
    check("_detail 在 _capture 之前被调用",
          i_detail > 0 and i_capture > 0 and i_detail < i_capture,
          f"detail@{i_detail} capture@{i_capture}")
    check("快路失败时回退 _capture",
          "if data:" in unlock and "return _capture(" in unlock)
    detail = _fn_block(src, "def _detail")
    check("_detail 内部吞异常并返回 None", "except Exception" in detail and "return None" in detail)
    check("_detail 走 _DETAIL_JS（页面上下文里发，不自己发裸请求）",
          "_DETAIL_JS % uid" in detail
          and "fetch('/aweme/v1/web/aweme/detail/" in src,
          "裸 HTTP 会 403 Uifid Not Found")
    check("_detail 用 awaitPromise 拿异步结果", "awaitPromise" in src)

    print()
    print("⑤ 抓流老路仍在（图文/私密/风控 的最后兜底）")
    cap = _fn_block(src, "def _capture")
    check("_capture 仍解析 douyinvod 流", "_classify_douyinvod" in cap)

    print()
    print(f"通过: {PASS}  失败: {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
