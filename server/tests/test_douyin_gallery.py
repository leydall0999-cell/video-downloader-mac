"""抖音图文（多图作品）守卫（2026-11-03）。

背景：图文作品 `aweme_type=68` 的详情里**同时带 `video.play_addr`** —— 那是抖音
把几张图串成幻灯片配 BGM 合成的 720×720 视频。若按视频处理，用户粘图文链接会
下到一个幻灯片视频而不是图片（改之前实测就是这个行为，且不报错，属于"静默下错"）。

本测试钉住这条链路最容易断裂的 9 处：

  ① 图文必须先于 `_pick_track` 判定 —— 否则会先命中那个合成视频；
  ② gallery info **绝不能带 `url`/`direct`** —— 带了前端会走"直接保存"，
     用户点一下就存下一个视频文件；
  ③ 图片直链**不需要 Referer**（实测裸下 200），但换成 `~tplv` 原图变体恒 403，
     所以只能给 url_list 里现成的地址，不许自己拼；
  ④ 成品是**目录**：`_locate_output` 能返回目录，但 `_ensure_audio_track`（ffprobe）
     与 `_write_sidecar` 对目录必炸，必须跳过；
  ⑤ 完成态体积要用真实图片总和 —— 目录的 `stat().st_size` 只是个 96B 元数据；
  ⑥ `/api/tasks/{id}/file` 对目录必须就地打包 zip，否则 FileResponse 抛
     IsADirectoryError → 500，用户点「保存」必然失败；
  ⑦ 前端建议保存名必须带 `.zip`，否则存出来没有扩展名、双击打不开；
  ⑧ 清晰度面板要换成单档（key=best），否则前端渲染不出按钮或沿用上一条视频的档位；
  ⑨ 每一张图要能用 3 条备用地址换源重试（链接有时效）。

全部为静态/纯函数守卫，不需要联网、不需要浏览器。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SERVER = REPO / "server"
DOUYIN = SERVER / "douyin_cdp.py"
DL = SERVER / "downloader.py"
CORE = SERVER / "routers" / "core.py"
TASKS = SERVER / "tasks.py"
APPJS = REPO / "web" / "app.js"

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


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _fn_block(src: str, marker: str) -> str:
    """取从 marker 起、到下一个顶层 def/class 之前的源码块。"""
    i = src.find(marker)
    if i < 0:
        return ""
    rest = src[i + len(marker):]
    m = re.search(r"\n(?=(?:def |class |@))", rest)
    return marker + (rest[: m.start() + 1] if m else rest)


def main() -> int:
    src_d = _read(DOUYIN)
    src_dl = _read(DL)
    src_core = _read(CORE)
    src_tasks = _read(TASKS)
    src_js = _read(APPJS)

    import douyin_cdp as d
    import downloader as dl

    print("① 图文识别：先于 _pick_track，且非图文不误伤")
    aw_gallery = {
        "aweme_id": "7450744229229235491",
        "desc": "四张图的作品",
        "aweme_type": 68,
        "duration": 0,
        "images": [
            {"url_list": ["https://p3.douyinpic.com/a~tplv-dy-aweme-images:q75.webp?x=1",
                          "https://p9.douyinpic.com/a~tplv-dy-aweme-images:q75.webp?x=1"],
             "width": 2560, "height": 1600},
            {"url_list": ["https://p3.douyinpic.com/b~tplv-dy-aweme-images:q75.webp?x=1"],
             "width": 540, "height": 542},
        ],
        # 🔴 图文也有 play_addr（抖音合成的幻灯片视频）—— 这是本测试的核心陷阱
        "video": {"play_addr": {"url_list": ["https://v11.douyinvod.com/slide.mp4"],
                                "width": 720, "height": 720}},
    }
    gal = d._gallery_images(aw_gallery)
    check("图文作品能取到 2 张图", len(gal) == 2, f"得到 {len(gal)}")
    check("每张保留全部备用地址", gal[0]["urls"] and len(gal[0]["urls"]) == 2)
    check("保留真实像素（2560x1600）", gal[0]["w"] == 2560 and gal[0]["h"] == 1600)
    check("普通视频不误判成图文",
          d._gallery_images({"aweme_type": 0, "images": None,
                             "video": {"play_addr": {"url_list": ["x"]}}}) == [])
    check("images 为空的图文不误判", d._gallery_images({"images": []}) == [])

    blk_detail = _fn_block(src_d, "def _detail(")
    i_gal = blk_detail.find("_gallery_images(aw)")
    i_pick = blk_detail.find("_pick_track(aw)")
    check("_detail 里图文判定在 _pick_track 之前",
          i_gal > 0 and i_pick > 0 and i_gal < i_pick,
          "顺序反了会先命中抖音合成的幻灯片视频")

    print()
    print("② gallery info 形态：绝不能带 url/direct")
    data = {
        "is_gallery": True,
        "title": "四张图的作品",
        "video_id": "7450744229229235491",
        "thumbnail": "https://p3.douyinpic.com/a.webp",
        "webpage_url": "https://www.douyin.com/note/7450744229229235491",
        "images": ["https://p3.douyinpic.com/a.webp", "https://p3.douyinpic.com/b.webp"],
        "gallery": gal,
    }
    info = dl._shape_douyin_info(data, data["webpage_url"])
    check("_vdl_gallery 带 2 条", len(info.get("_vdl_gallery") or []) == 2)
    check("不带视频 url（否则前端走「直接保存」存下幻灯片视频）",
          not info.get("url"), f"得到 {info.get('url')!r}")
    check("不带 direct 标记", not info.get("direct"))
    check("ext 标成 webp", info.get("ext") == "webp")
    check("标题/网页地址透传", info.get("title") == "四张图的作品"
          and "douyin.com/note" in (info.get("webpage_url") or ""))

    # 上游只给 images、没给 gallery（旧通道）时也要能用
    info2 = dl._shape_douyin_info(
        {"is_gallery": True, "images": ["https://a/1.webp", "https://a/2.webp"]}, "https://www.douyin.com/note/1")
    check("上游缺 gallery 时用 images 兜底", len(info2.get("_vdl_gallery") or []) == 2)

    print()
    print("③ summarize / qualities：前端拿到的字段")
    s = dl.summarize(info)
    check("is_gallery=True", s.get("is_gallery") is True)
    check("images 下发给前端（2 条）", len(s.get("images") or []) == 2)
    check("direct_url 必须为空（否则按钮变成「直接保存到本机」）",
          s.get("direct_url") is None, f"得到 {s.get('direct_url')!r}")
    q = dl.build_quality_options(info)
    check("清晰度只有 1 档", len(q) == 1, f"得到 {len(q)} 档")
    check("该档 key=best（is_valid_quality 必须放行）",
          q and q[0].get("key") == "best" and dl.is_valid_quality("best"))
    check("档位标签说清张数", "2" in str(q[0].get("label") if q else ""))
    # 视频仍走原逻辑
    vq = dl.build_quality_options({"formats": [{"url": "u", "height": 1080, "vcodec": "avc1"}]})
    check("视频不受影响（仍按真实档位平铺）", len(vq) >= 1 and vq[0].get("key") != "best"
          or any(x.get("key") == "1080" for x in vq))

    print()
    print("④ 下载分支：目录形态的上下游适配")
    blk_gd = _fn_block(src_dl, "def _gallery_download(")
    check("_gallery_download 存在", bool(blk_gd))
    check("逐条换源重试（不只用第一条）", "for u in it[\"urls\"]" in blk_gd or "for u in it['urls']" in blk_gd)
    check("按文件头判真实扩展名（CDN content-type 不可全信）",
          "_image_ext(" in blk_gd)
    check("回报 requested_downloads（_locate_output 才拿得到目录）",
          "requested_downloads" in blk_gd)
    check("记录真实总字节（目录 stat 只有 96B）",
          "_vdl_gallery_bytes" in blk_gd)

    # run_download 有 700+ 行，_fn_block 会在函数内部的边界处截断 ⇒ 改用行号比较。
    _lines = src_dl.split("\n")

    def _ln(needle: str) -> int:
        return next((i for i, l in enumerate(_lines) if needle in l), -1)

    i_call = _ln("_gallery_download(task, info, task.workdir")
    i_proc = _ln("ydl.process_info(info)")
    check("run_download 图文分支在 process_info 之前",
          0 < i_call < i_proc,
          "顺序反了会进 yt-dlp 报 No video formats found")
    i_audio = _ln("output = _ensure_audio_track(output, info, task)")
    check("图文跳过 _ensure_audio_track（ffprobe 对目录会误报无音轨）",
          i_audio > 0 and any('info.get("_vdl_gallery")' in l for l in _lines[max(0, i_audio - 6):i_audio]),
          "必须在调用前判 gallery 并跳过")
    check("完成态体积按真实字节换算（目录 stat 只有 96B）",
          _ln('_format_bytes(_out_bytes)') > 0 and _ln("_vdl_gallery_bytes") > 0)

    print()
    print("⑤ 保存链路：目录必须打包 zip，否则点保存必 500")
    blk_file = _fn_block(src_core, "def download_file(")
    check("/file 有目录分支", "task.filepath.is_dir()" in blk_file)
    check("目录就地打包 zip", "zipfile.ZipFile" in blk_file)
    check("不压缩存储（图片已是压缩格式）", "ZIP_STORED" in blk_file)
    check("给干净的 ASCII 兜底名（纯中文标题 encode 后只剩 `_# ` 残渣）",
          "re.search(r'[A-Za-z0-9]'" in blk_file and "gallery.zip" in blk_file)
    # 🔴 2026-11-03 实测踩到：判据只判「剩没剩字母数字」时，标题 `小米塔可爱捏_#米塔
    # #steam游戏` encode 后剩 `_# #steam.zip` —— 有 `steam` 这几个字母，于是残渣被
    # 当成合格名放行。正解是只要原标题含非 ASCII 就一律走通用名。
    check("标题含非 ASCII 时不再放过 `_# #steam.zip` 这类残渣",
          "_zip_name.isascii()" in blk_file)
    check("任务对外字段带 is_directory", '"is_directory"' in src_tasks)

    print()
    print("⑥ 前端：图文要说人话、保存名要带 .zip")
    blk_rv = _fn_block(src_js, "const renderVideo = (data) => {")
    check("renderVideo 有 is_gallery 分支", "video.is_gallery" in blk_rv)
    check("按钮文案改成张数", "保存" in blk_rv and "张图片" in blk_rv)
    check("图文时收敛到 best 档", "selectedQuality = 'best'" in blk_rv)
    check("保存建议名补 .zip", ".zip" in src_js and "task.is_directory" in src_js)
    check("任务卡清晰度位置显示「图文作品」", "图文作品（多图）" in src_js)
    check("完成提示指向文件夹", "图片已保存到这个文件夹" in src_js)

    print()
    print("⑦ 图片地址不许自己拼（原图变体恒 403）")
    check("不出现去掉 tplv 转码段的拼法",
          not re.search(r"replace\([^)]*tplv", blk_gd) and "tplv" not in blk_gd,
          "去掉 ~tplv-... 或换 .jpeg 一律 403（签名绑定了转码参数）")
    check("只取 url_list 里现成地址",
          "url_list" in src_d or "urls" in blk_gd)

    print()
    print(f"结果：{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
