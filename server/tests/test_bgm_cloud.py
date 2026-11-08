# -*- coding: utf-8 -*-
"""在线配乐曲库离线守卫（脚本式，run_one 直接当脚本跑）。

不联网：_fetch_url 全部打桩；落盘目录重定向到临时目录，绝不碰用户真实数据。
运行：cd server && ../.build_venv/bin/python tests/test_bgm_cloud.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SERVER = REPO / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from fastapi import HTTPException  # noqa: E402
from routers import bgm_cloud as m  # noqa: E402

PASS = 0
FAIL = 0


def ok(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def expect_http(label, code, fn, *a, **kw):
    try:
        fn(*a, **kw)
        ok(label + " 应抛 HTTPException", False)
    except HTTPException as e:
        ok(label + " 状态码", e.status_code == code, f"got={e.status_code} want={code}")
    except Exception as e:  # noqa: BLE001
        ok(label + " 不应抛其它异常", False, repr(e))


class Sandbox:
    """把曲库目录与网络层整体换掉，进出都还原。"""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="vdl_bgm_cloud_")
        self.dir = Path(self.tmp.name) / "bgm"
        self.dir.mkdir(parents=True)
        self.orig_dir, self.orig_fetch = m._bgm_dir, m._fetch_url
        m._bgm_dir = lambda: self.dir
        self.fetches = []
        self.responses = {}
        m._fetch_url = self.fake_fetch
        return self

    def fake_fetch(self, url, timeout=m.BGM_TIMEOUT):
        self.fetches.append(url)
        if url not in self.responses:
            raise OSError("stub network down")
        v = self.responses[url]
        if isinstance(v, Exception):
            raise v
        return v

    def __exit__(self, *a):
        m._bgm_dir, m._fetch_url = self.orig_dir, self.orig_fetch
        self.tmp.cleanup()


def track(fname="a" * 16 + ".mp3", tid="a" * 16, sha="", name="测试曲"):
    return {"id": tid, "name": name, "category": "轻快", "file": fname,
            "size": 4, "duration": 8.0, "sha256": sha, "license": ""}


def main():
    # ---- A. 常量与正则：URL 前缀硬编码，文件名白名单 -------------------------
    ok("清单 URL 固定", m.BGM_LIBRARY_URL == "https://hanyuxz.top/bgm/library.json")
    ok("音频前缀固定", m.BGM_BASE_URL == "https://hanyuxz.top/bgm/")
    ok("上限 30MB", m.BGM_MAX_BYTES == 30 * 1024 * 1024)
    ok("合法文件名", bool(m.BGM_FILE_RE.match("a" * 16 + ".mp3")))
    ok("合法 flac", bool(m.BGM_FILE_RE.match("0" * 16 + ".flac")))
    for bad in ("../x.mp3", "/etc/passwd", "a" * 15 + ".mp3", "a" * 17 + ".mp3",
                "a" * 16 + ".exe", "a" * 16 + ".mp3/../../x", "zzzzzzzzzzzzzzzz.mp3"):
        ok("拒绝非法文件名 " + bad, not bool(m.BGM_FILE_RE.match(bad)))
    ok("合法 id", bool(m.BGM_ID_RE.match("a" * 16)))
    ok("拒绝非法 id", not bool(m.BGM_ID_RE.match("../")))
    ok("合法 sha", bool(m.BGM_SHA_RE.match("a" * 64)))
    ok("拒绝短 sha", not bool(m.BGM_SHA_RE.match("abc")))

    # ---- B. 清单归一化：脏数据不得进入落盘路径 -------------------------------
    good = track()
    bad_size = track(fname="b" * 16 + ".wav")
    bad_size["size"] = "oops"
    norm = m._normalize_tracks({"tracks": [
        good,
        track(fname="../evil.mp3"),
        track(fname="a" * 16 + ".exe"),
        track(fname="a" * 16 + ".mp3", name="曲" * 100),
        {"id": "x"}, None, "not a dict",
        bad_size,
    ]})
    ok("只留下合规条目", len(norm) == 3, f"got={len(norm)}")
    ok("曲名截断到 60", all(len(t["name"]) <= 60 for t in norm))
    ok("非法 size 归零", norm[-1]["size"] == 0)
    ok("无 file 的条目被丢", all(t["file"] for t in norm))
    ok("空清单安全", m._normalize_tracks({"tracks": []}) == [])
    ok("缺 tracks 安全", m._normalize_tracks({}) == [])

    # ---- C. 本地状态：sha256 必须逐字节吻合 ----------------------------------
    blob = b"ID3" + b"\x00" * 61
    sha = hashlib.sha256(blob).hexdigest()
    with Sandbox() as s:
        t = track(sha=sha)
        ok("文件不存在 → 未下载", m._local_state(t) == (False, ""))
        (s.dir / t["file"]).write_bytes(blob)
        local, path = m._local_state(t)
        ok("sha 吻合 → 已下载", local is True)
        ok("返回绝对路径", path == str(s.dir / t["file"]) and os.path.isabs(path))
        (s.dir / t["file"]).write_bytes(blob + b"x")
        ok("sha 不符 → 未下载", m._local_state(t) == (False, ""))
        t2 = track(sha="")
        ok("无 sha → 未下载", m._local_state(t2) == (False, ""))
        t3 = track(fname="b" * 16 + ".mp3", sha=sha)
        (s.dir / t3["file"]).write_bytes(b"x" * (m.BGM_MAX_BYTES + 1))
        ok("超上限 → 未下载", m._local_state(t3) == (False, ""))

    # ---- D. 清单端点：联网刷新 / 断网回退 / 不被云端改 base_url ---------------
    with Sandbox() as s:
        ok("无缓存清单为空", m._read_cached_library()["tracks"] == [])
        cloud = {"version": 1, "base_url": "https://evil.example/",
                 "updated_at": "2026-11-08T00:00:00Z", "tracks": [track(sha=sha)]}
        s.responses[m.BGM_LIBRARY_URL] = json.dumps(cloud).encode()
        r = m.bgm_library()
        ok("联网时 offline=False", r["offline"] is False)
        ok("清单条目数", len(r["tracks"]) == 1)
        ok("base_url 不被云端覆盖", r["base_url"] == m.BGM_BASE_URL)
        ok("直链由固定前缀拼出", r["tracks"][0]["url"] == m.BGM_BASE_URL + good["file"])
        ok("条目带 local 标记", r["tracks"][0]["local"] is False)
        ok("清单已缓存", len(m._read_cached_library()["tracks"]) == 1)
        ok("只请求固定清单 URL", s.fetches == [m.BGM_LIBRARY_URL])

        s.responses.clear()
        r2 = m.bgm_library()
        ok("断网 offline=True", r2["offline"] is True)
        ok("断网仍列出缓存曲目", len(r2["tracks"]) == 1)
        ok("缓存 base_url 仍是固定前缀", r2["base_url"] == m.BGM_BASE_URL)

    # ---- E. 下载端点：防 SSRF / 校验 / 原子落盘 / 幂等 -------------------------
    with Sandbox() as s:
        t = track(sha=sha)
        m._save_cached_library({"version": 1, "base_url": m.BGM_BASE_URL,
                                "updated_at": "", "tracks": [t]})
        expect_http("非法 id", 400, m.bgm_fetch, {"id": "../../etc"})
        expect_http("未知 id", 404, m.bgm_fetch, {"id": "b" * 16})
        ok("非法 id 不出网", s.fetches == [])

        bad = track(fname="c" * 16 + ".mp3", tid="c" * 16, sha="")
        m._save_cached_library({"version": 1, "base_url": m.BGM_BASE_URL,
                                "updated_at": "", "tracks": [t, bad]})
        expect_http("缺 sha", 409, m.bgm_fetch, {"id": bad["id"]})

        url = m.BGM_BASE_URL + t["file"]
        s.responses[url] = b"NOT_AN_MP3"
        expect_http("sha 不符", 409, m.bgm_fetch, {"id": t["id"]})

        bad_blob = b"XXXX" + b"\x00" * 60
        s.responses[url] = bad_blob
        fake_sha = hashlib.sha256(bad_blob).hexdigest()
        t_bad_magic = dict(t, sha256=fake_sha)
        m._save_cached_library({"version": 1, "base_url": m.BGM_BASE_URL,
                                "updated_at": "", "tracks": [t_bad_magic]})
        expect_http("MP3 魔数不符", 409, m.bgm_fetch, {"id": t["id"]})

        m._save_cached_library({"version": 1, "base_url": m.BGM_BASE_URL,
                                "updated_at": "", "tracks": [t]})
        s.responses[url] = blob
        r = m.bgm_fetch({"id": t["id"], "url": "http://169.254.169.254/latest/meta-data"})
        ok("下载成功", r["ok"] is True and r["cached"] is False)
        ok("返回绝对路径", os.path.isabs(r["path"]) and Path(r["path"]).is_file())
        ok("落盘字节一致", Path(r["path"]).read_bytes() == blob)
        ok("忽略客户端传来的 url", s.fetches[-1] == url, f"got={s.fetches[-1]}")
        ok("无半截 .tmp 残留", not list(s.dir.glob(".*.tmp")))

        before = len(s.fetches)
        r2 = m.bgm_fetch({"id": t["id"]})
        ok("二次命中 cached", r2["cached"] is True and r2["path"] == r["path"])
        ok("命中缓存不再出网", len(s.fetches) == before)

        s.responses[url] = OSError("offline")
        (s.dir / t["file"]).unlink()
        expect_http("断网下载", 502, m.bgm_fetch, {"id": t["id"]})

        too_big = b"ID3" + b"\x00" * m.BGM_MAX_BYTES
        t_big = dict(t, sha256=hashlib.sha256(too_big).hexdigest())
        m._save_cached_library({"version": 1, "base_url": m.BGM_BASE_URL,
                                "updated_at": "", "tracks": [t_big]})
        s.responses[url] = too_big
        expect_http("超 30MB", 413, m.bgm_fetch, {"id": t["id"]})

    # ---- F. 接线：路由注册 / 前端 DOM / 样式 / 版本 ---------------------------
    app_py = (SERVER / "app.py").read_text(encoding="utf-8")
    ok("app.py 注册新路由", "from routers import bgm_cloud as _bgm_cloud_rtr" in app_py
       and "app.include_router(_bgm_cloud_rtr.router)" in app_py)
    ok("路由清单含两个端点", any("/api/commentary/bgm/library" in str(r.path) for r in m.router.routes)
       and any("/api/commentary/bgm/fetch" in str(r.path) for r in m.router.routes))
    ok("路由清单含专用试听端点",
       any("/api/commentary/bgm/preview" in str(r.path) for r in m.router.routes))

    with Sandbox() as s:
        t = track(sha=sha)
        m._save_cached_library({"version": 1, "base_url": m.BGM_BASE_URL,
                                "updated_at": "", "tracks": [t]})
        expect_http("试听非法 id", 400, m.bgm_preview, "../../etc")
        expect_http("试听未知 id", 404, m.bgm_preview, "b" * 16)
        expect_http("未下载不能本地试听", 404, m.bgm_preview, t["id"])
        (s.dir / t["file"]).write_bytes(blob)
        resp = m.bgm_preview(t["id"])
        ok("已下载返回本地文件", Path(resp.path) == s.dir / t["file"])
        ok("试听 media_type 正确", resp.media_type == "audio/mpeg")
        (s.dir / t["file"]).write_bytes(blob + b"x")
        expect_http("本地文件被改动则拒绝试听", 404, m.bgm_preview, t["id"])

    html = (REPO / "web/index.html").read_text(encoding="utf-8")
    for eid in ("comTlMusicCloudToggle", "comTlMusicCloud", "comTlMusicCloudRefresh",
                "comTlMusicCloudList", "comTlMusicCloudInfo"):
        ok("index.html 含 " + eid, f'id="{eid}"' in html)
    ok("曲库区块默认隐藏", 'id="comTlMusicCloud" hidden' in html)

    js = (REPO / "web/app.js").read_text(encoding="utf-8")
    for sym in ("comMusicCloudLoad", "comMusicCloudRender", "comMusicCloudAudition",
                "comMusicCloudUse", "/api/commentary/bgm/library",
                "/api/commentary/bgm/fetch", "comMusic.cloudName = t.name"):
        ok("app.js 含 " + sym, sym in js)
    ok("下载后复用 bgm=user", "comMusic.kind = 'user';" in js and "comMusic.file = p;" in js)
    ok("试听本地曲走专用端点", "/api/commentary/bgm/preview?id=" in js)
    cloud_aud = js[js.index("const comMusicCloudAudition"):js.index("const comMusicCloudUse")]
    ok("曲库试听不依赖通用 audio-preview", "/api/commentary/audio-preview" not in cloud_aud)
    ok("曲库试听按 id 取本地流", "/api/commentary/bgm/preview?id=" in cloud_aud)
    css = (REPO / "web/styles.css").read_text(encoding="utf-8")
    changelog = (SERVER / "changelog.py").read_text(encoding="utf-8")
    for sym in ("comMusicFold", "comMusicSortBtn", "comMusicList", "comMusicRefresh"):
        ok("index.html 含左栏音乐卡 " + sym, f'id="{sym}"' in html)
    for sym in ("comMusicCardRender", "comMusicCardPick", "comMusicSyncCurrentOutput",
               "comMusicSortDesc", "comMusicCloudUse(t, null)",
               "musicFold.addEventListener('toggle'"):
        ok("app.js 含左栏卡接线 " + sym, sym in js)
    ok("排序按上传时间且默认最新在前",
       "comMusicSortDesc = true" in js and "uploaded_at" in js
       and "bv.localeCompare(av)" in js)
    ok("选中后自动同步到成片", "comMusicSyncCurrentOutput()" in js
       and "/api/commentary/remux-bgm/" in js)
    ok("后端清单透传 uploaded_at", "uploaded_at" in (SERVER / "routers/bgm_cloud.py").read_text(encoding="utf-8"))
    h_left = html.index('<div class="com-left"'); h_left_end = html.index("</div><!-- /com-left -->")
    h_right = html.index('<div class="com-right"')
    h_music = html.index('id="comMusicFold"')
    ok("背景音乐卡确实落在左栏、不在右栏", h_left < h_music < h_left_end < h_right,
       f"left={h_left} music={h_music} leftEnd={h_left_end} right={h_right}")
    ok("左栏窄栏样式已接", ".com-left .com-music-fold" in css
       and ".com-left .com-music-row" in css)
    for cls in (".com-music-row", ".com-music-row.is-on", ".com-music-sortrow"):
        ok("styles.css 含 " + cls, cls in css)
    # 排序按钮必须与「成片」历史列表同款（用户 2026-11-09「排序方式跟成片一样」）：
    # 同 SVG 图标（不是文字 ⇅）、同紧凑内距、同 is-asc 品牌色高亮。
    music_html = html[h_music:html.index("</details>", h_music)]
    ok("音乐卡排序按钮用成片同款 SVG 图标", 'class="com-sort-ico"' in music_html)
    ok("音乐卡排序按钮不再用文字箭头", ">⇅<" not in music_html)
    ok("音乐卡排序按钮尺寸与成片一致",
       ".com-music-sortrow .com-sort-btn { flex: none; min-width: 0; padding: .3rem .34rem;" in css)
    ok("音乐卡排序按钮有 is-asc 高亮",
       ".com-music-sortrow .com-sort-btn.is-asc {" in css)

    for cls in (".com-tl-music-cloudbar", ".com-tl-music-cloudrow", ".com-tl-music-cloud[hidden]"):
        ok("styles.css 含 " + cls, cls in css)

    runner = (SERVER / "tests/run_offline_tests.sh").read_text(encoding="utf-8")
    ok("守卫已登记 runner", "run_one test_bgm_cloud.py" in runner)

    ver = (REPO / "VERSION").read_text(encoding="utf-8").strip()
    ok("VERSION 已升到 1.1.15", ver == "1.1.15", f"got={ver}")
    ok("changelog 含本版条目且说明留白",
       '"version": "1.1.15",\n        "date": "2026-11-09",\n        "items": [],' in changelog)

    print(f"\n=== test_bgm_cloud.py: {PASS} 通过 / {FAIL} 失败 ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
