#!/usr/bin/env python3
"""「插件发过来是一串数字，要原名」服务端回归测试（2026-11-13，纯离线）。

链路：扩展页面侧采集原名 → ext-push / send 带 title → cdp_sniffer 落库 →
桌面端 items 列表与 /api/download 用原名 → downloader.run_download 保留原名
并写回 info["title"]（yt-dlp outtmpl 的 %(title)s 才不会落成 basename）。

根因钉子（本测试特意保留）：
  downloader.probe() 对直链短路，返回 {"title": URL basename}，实测
  2044590383.mp3 / M5000039SjyS2Oznrv.mp3；响应头 Content-Disposition 也是哈希名。
  所以原名**只能**由页面侧采集，且 run_download 绝不能让 info["title"] 覆盖已有原名。

配套扩展侧用例：extension/tests/test_media_name.js
运行：
    cd server && python tests/test_sniffer_media_name.py
"""
import os
import sys
import tempfile
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(HERE))

# 测试隔离：绝不写用户家目录
os.environ["VDL_DATA_DIR"] = tempfile.mkdtemp(prefix="vdl_test_medianame_")

import cdp_sniffer  # noqa: E402

NAME = "友谊之光 - Maria Cordero"
URL = "https://cdn.example.com/2044590383.mp3"


def _row(url=URL, **kw):
    d = {"url": url, "mime": "audio/mpeg", "page_url": "https://flac.music.hi.cn/",
         "page_title": "无损音乐下载", "kind_hint": "media", "title": NAME}
    d.update(kw)
    return d


def test_ext_push_media_name():
    """自动推送：原名入库、后到覆盖、空 title 补推不抹掉、page_title 不受影响。"""
    s = cdp_sniffer.CDPSniffer()      # 独立实例，不碰全局 SNIFFER
    assert s.add_ext_items([_row()]) == 1
    it = next(r for r in s.items() if r["url"] == URL)
    assert it["title"] == NAME, f"原名未入库: {it}"
    assert it["page_title"] == "无损音乐下载", "page_title 与 title 是两个独立字段"

    # 扩展常在嗅探之后才拿到名字 → 补推只改名，不新增行
    s.add_ext_items([_row(title="友谊之光 (Live)")])
    rows = [r for r in s.items() if r["url"] == URL]
    assert len(rows) == 1 and rows[0]["title"] == "友谊之光 (Live)"

    # 不带 title 的重复推送（心跳/重推）不许把原名抹成空
    s.add_ext_items([_row(title="")])
    assert next(r for r in s.items() if r["url"] == URL)["title"] == "友谊之光 (Live)"

    # 缺省值不回归
    s.add_ext_items([_row(url="https://cdn.example.com/plain.mp3", title="")])
    assert next(r for r in s.items() if r["url"].endswith("plain.mp3"))["title"] == ""
    print("✅ 自动推送：原名入库 / 后到覆盖 / 空值不抹 / 缺省不回归")


def test_manual_media_name():
    """popup 点「下载」→ /api/sniffer/send → add_manual → take_picked 带原名。"""
    s = cdp_sniffer.CDPSniffer()
    got = s.add_manual(_row())
    assert got["title"] == NAME, f"手推条目丢原名: {got}"
    picked = s.take_picked()
    assert len(picked) == 1 and picked[0]["title"] == NAME, "出队后原名丢失"
    # 超长输入要截断，避免把任意字符串塞进文件名
    long = s.add_manual(_row(title="X" * 500))
    assert len(long["title"]) == 200
    print("✅ 手推入口：出队保留原名 / 超长截断")


def test_downloader_keeps_media_name():
    """执行 downloader.py 落盘的真实代码块：有原名时 basename 不许覆盖，且写回 info。"""
    src = open(os.path.join(ROOT, "server", "downloader.py"), encoding="utf-8").read()
    anchor = '            if info.get("webpage_url"):'
    tail = 'task.add_step("解析视频信息", "done", "未获取到标题")'
    try:
        start = src.index(anchor)
        end = src.index(tail, start) + len(tail)
    except ValueError as exc:
        raise AssertionError("downloader.run_download 的标题处理块已被改动，请同步本守卫") from exc
    block = textwrap.dedent(src[start:end])

    class Task:
        def __init__(self, title):
            self.title, self.source_url, self.steps, self.id = title, "", [], "t1"

        def add_step(self, *a):
            self.steps.append(a)

    class Store:
        def __init__(self):
            self.calls = []

        def update(self, *a, **kw):
            self.calls.append(kw)

    def run(task_title, info):
        t, st = Task(task_title), Store()
        exec(block, {"task": t, "info": info, "store": st})  # noqa: S102 - 被测代码来自本仓
        return t, st, info

    basename = "2044590383"        # probe() 对直链的短路结果
    t, st, info = run(NAME, {"title": basename, "url": URL, "ext": "mp3"})
    assert info["title"] == NAME, f"原名被 basename 覆盖（文件名会变数字）: {info}"
    assert t.title == NAME and not st.calls, "已有原名时不得回写 store"

    # 回归：没有原名时保持原行为（用解析出的标题回填）
    t2, st2, info2 = run("", {"title": basename, "url": URL, "ext": "mp3"})
    assert info2["title"] == basename and st2.calls[0]["title"] == basename

    # 回归：两者都没有也不崩
    t3, _, _ = run("", {"url": URL, "ext": "mp3"})
    assert any("未获取到标题" in str(s) for s in t3.steps)
    print("✅ 下载器：保留原名并写回 info['title'] / 无原名时原行为不变")


def test_contract_wiring():
    """整条链路的接线守卫：少任何一环，原名都会在某个边界丢掉。"""
    d = open(os.path.join(ROOT, "server", "downloader.py"), encoding="utf-8").read()
    c = open(os.path.join(ROOT, "server", "cdp_sniffer.py"), encoding="utf-8").read()
    js = open(os.path.join(ROOT, "web", "js", "desktop-app.js"), encoding="utf-8").read()
    bg = open(os.path.join(ROOT, "extension", "background.js"), encoding="utf-8").read()

    # 根因仍在：probe() 直链短路返回 basename。守卫的意义是提醒「必须靠页面采集」
    assert '"title": filename,' in d, "probe() 直链行为变了，请重新评估原名来源"
    assert 'title=str(row.get("title") or "").strip()[:200],' in c
    assert '"title": (payload.get("title") or "").strip()[:200],' in c
    assert "title: nameForUrl(url, tabId)," in bg
    assert "title: item.title || ''," in bg
    assert "String(it.title || it.page_title || '').trim()" in js
    assert "title: itemTitle," in js
    assert "title: it.page_title || ''," not in js, "桌面端还在用页面标题覆盖原名"
    print("✅ 接线守卫：扩展→服务端→桌面端→下载器 全链路无断点")


if __name__ == "__main__":
    test_ext_push_media_name()
    test_manual_media_name()
    test_downloader_keeps_media_name()
    test_contract_wiring()
    print("🎉 媒体原名回归测试全部通过")
