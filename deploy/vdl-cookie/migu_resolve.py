#!/usr/bin/env python3
"""咪咕视频（miguvideo.com）解析 —— ECS Playwright worker。

为什么必须真实浏览器 + 登录态：
  播放页是 React SPA，取流接口
  ``webapi.miguvideo.com/gateway/playurl/secret/v2/play/playurl`` 的请求/响应由
  前端 ``gateway-crypto.wasm`` 签名加密；静态抓 HTML 只得到加密令牌
  （``window.__PLAYING_INFO__[*].cdnData``），yt-dlp 无 migu 提取器。游客态不出流，
  必须注入已登录账号 Cookie 让 WASM 在真实浏览器里跑起来。

实测结论（2026-11-14，见当日日志）：
  - 🔴 **CDN 不绑 IP**：从与浏览器完全不同的 IP 拉 m3u8/ts 均 200（client_ip= 只是
    内嵌字段，CDN 不校验）→ 服务端抓流 + 任意端下载成立，不必留在本机。
  - 🔴 ECS 机房 IP **不被风控**：headless_shell 实测跑通 WASM 签名、playurl 200、
    m3u8/ts 全 200。headed/xvfb 反而 about:blank，不要用。
  - 🔴 竖屏页有**间歇性 about:blank 竞态**：SPA 偶发不渲染（title 空、DOM 39 字节）。
    用「全新 context（非持久 profile）+ 主域预热 + 检测空白自动重载 + 边等边抓」
    收敛；不要用固定 persistent profile（累积脏状态放大竞态）。
  - 竖屏页预加载**多个推荐视频**：第一个 playurl 对应 ``/media/<contentId>_`` 才是
    目标，按 contentId 精确定位，避免下错片。
  - 索引 m3u8（gslbmgspvod）body 常只是一行裸二级 URL；二级清单（hlszy/hlszte）
    才是 ``#EXTM3U`` + ts 分片。逐个拉挑真清单。

Cookie 优先级：显式传入文本 > ``/opt/vdl-worker/cookies/migu.txt``。
支持 Netscape jar 或 ``"k1=v1; k2=v2; ..."``；无 Cookie 诚实报错。

对外 ``resolve(url, timeout=75, cookie="")`` 供 vdl_cookie_daemon.py 复用，
返回 dict 或抛 RuntimeError，契约与 finder/lespark 一致。
"""
import os
import re
import sys
import time
import urllib.parse
import urllib.request

COOKIE_FILE = "/opt/vdl-worker/cookies/migu.txt"
HOME = "https://www.miguvideo.com/"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REFERER = "https://www.miguvideo.com/"
_CDN_HOSTS = ("miguvideo.com", "cmvideo.cn")
_CID_RE = re.compile(r"/media/(\d{8,14})_")


def _log(msg: str) -> None:
    print("[migu] " + msg, file=sys.stderr, flush=True)


def _load_cookies(cookie_text: str = "") -> list:
    raw = (cookie_text or "").strip()
    if not raw and os.path.exists(COOKIE_FILE):
        try:
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                raw = f.read().strip()
        except Exception:
            raw = ""
    if not raw:
        return []

    out = []
    if raw.startswith("# Netscape") or "\t" in (raw.splitlines()[0] if raw else ""):
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            p = line.split("\t")
            if len(p) < 7:
                continue
            domain, _flag, path, secure, exp, name, value = p[:7]
            dom = (domain or "").lstrip(".").lower()
            if not any(dom == h or dom.endswith("." + h) for h in ("miguvideo.com", "cmvideo.cn")):
                continue
            item = {"name": name, "value": value, "domain": "." + dom,
                    "path": path or "/", "secure": str(secure).upper() == "TRUE"}
            try:
                e = int(exp)
                if 0 < e < 4_000_000_000:
                    item["expires"] = float(e)
            except (TypeError, ValueError):
                pass
            out.append(item)
        return out

    for seg in raw.split(";"):
        seg = seg.strip()
        if "=" not in seg:
            continue
        k, _, v = seg.partition("=")
        k, v = k.strip(), v.strip()
        if k:
            out.append({"name": k, "value": v, "domain": ".miguvideo.com",
                        "path": "/", "secure": True})
    return out


def _is_stream(u: str) -> bool:
    low = u.lower()
    if not any(h in low for h in _CDN_HOSTS):
        return False
    if "playurl" in low or "/gateway/" in low or "display/v3" in low:
        return False
    if any(x in low for x in ("cover", "poster", "avatar", "thumb", ".jpg", ".png", ".webp")):
        return False
    return ".m3u8" in low


def _cid(u: str) -> str:
    m = _CID_RE.search(u)
    return m.group(1) if m else ""


def _fetch(u: str) -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(u, headers={"User-Agent": UA, "Referer": REFERER})
    with opener.open(req, timeout=8) as r:
        return r.read(65536).decode("utf-8", "ignore")


def _pick_playlist(candidates: list) -> str:
    bodies = {}
    for u in candidates[:16]:
        try:
            bodies[u] = _fetch(u)
        except Exception:
            continue
    for u, body in bodies.items():
        if "#EXTM3U" in body and ".ts" in body:
            return u
    for u, body in bodies.items():
        m = re.search(r"https?://[^\s\"']+\.m3u8\??[^\s\"']*", body)
        if m:
            return m.group(0)
        m2 = re.search(r"[^\s\"']+\.m3u8\??[^\s\"']*", body)
        if m2:
            return urllib.parse.urljoin(u, m2.group(0))
    return next(iter(bodies), "")


def _page_alive(page) -> bool:
    """判断 SPA 是否真的渲染（about:blank 竞态检测）。"""
    try:
        u = page.url or ""
        if "about:blank" in u:
            return False
        n = page.evaluate("() => document.documentElement.outerHTML.length")
        return int(n or 0) > 2000
    except Exception:
        return False


def resolve(url: str, timeout: int = 75, cookie: str = "") -> dict:
    from playwright.sync_api import sync_playwright

    cookies = _load_cookies(cookie)
    if not cookies:
        raise RuntimeError(
            "咪咕视频需要登录态才能解析，当前没有可用的咪咕 Cookie。\n"
            "请在已登录咪咕的浏览器复制 www.miguvideo.com 的 Cookie 粘贴，"
            "或由运维把账号 Cookie 落到 /opt/vdl-worker/cookies/migu.txt。\n"
            "游客态咪咕只返回加密令牌，不出可播放的视频流。")

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled",
                  "--autoplay-policy=no-user-gesture-required", "--mute-audio",
                  "--no-proxy-server"],
        )
        try:
            # 全新 context（非持久 profile）：与本机成功探针同款，避开脏状态竞态
            context = browser.new_context(
                user_agent=UA, viewport={"width": 1280, "height": 800}, locale="zh-CN")
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            context.add_cookies(cookies)

            # 主域预热：让 WASM / 会话就绪（直接进竖屏页偶发 about:blank）
            try:
                warm = context.new_page()
                warm.goto(HOME, wait_until="domcontentloaded", timeout=25000)
                warm.wait_for_timeout(1200)
                warm.close()
            except Exception as e:  # noqa: BLE001
                _log("主域预热失败(继续): %s" % str(e)[:120])

            page = context.new_page()
            hits: list = []

            def _on_response(resp):
                try:
                    u = resp.url or ""
                    if _is_stream(u) and u not in hits:
                        hits.append(u)
                except Exception:
                    pass

            page.on("response", _on_response)

            # 导航 + 空白重载重试（咪咕 SPA 偶发 about:blank）
            for attempt in range(3):
                try:
                    page.goto(url, wait_until="load", timeout=30000)
                except Exception as e:  # noqa: BLE001
                    _log("goto 尝试%d: %s" % (attempt + 1, str(e)[:100]))
                page.wait_for_timeout(2500)
                if _page_alive(page) or hits:
                    break
                _log("第%d次 SPA 未渲染，重载" % (attempt + 1))
                try:
                    page.reload(wait_until="load", timeout=30000)
                except Exception:
                    pass

            # 边等边抓：抓到目标 cid 的候选即可停，减少无效等待与限流窗口
            wait_s = min(max(timeout - 14, 14), 24)
            deadline = time.time() + wait_s
            while time.time() < deadline:
                page.wait_for_timeout(1000)
                if len(hits) >= 3 and (time.time() - (deadline - wait_s)) > 9:
                    break
            _log("等待完成 alive=%s hits=%d" % (_page_alive(page), len(hits)))

            try:
                title = (page.title() or "").strip()
            except Exception:
                title = ""
            if not title:
                title = "咪咕视频"

            if not hits:
                raise RuntimeError(
                    "页面已打开但未捕获到咪咕播放流（登录态可能已失效 / 视频需会员 / 被风控）。"
                    "请更新咪咕 Cookie 或换一条公开视频链接重试。")

            target_cid = ""
            for u in hits:
                c = _cid(u)
                if c:
                    target_cid = c
                    break
            scoped = [u for u in hits if (not target_cid or _cid(u) == target_cid)]
            _log("target_cid=%s scoped=%d/%d" % (target_cid, len(scoped), len(hits)))

            playlist = _pick_playlist(scoped or hits) or _pick_playlist(hits)
            if not playlist:
                raise RuntimeError("捕获到 m3u8 但均无法访问（签名已过期或 CDN 拒绝）。")

            vid = ""
            mv = re.search(r"/(?:p/vertical|m/play|p/play)/([0-9a-zA-Z_]+)", url or "")
            if mv:
                vid = mv.group(1)

            return {"ok": True, "title": title, "duration": None,
                    "video_id": vid, "video_url": playlist,
                    "webpage_url": page.url or url, "thumbnail": "", "ext": "mp4"}
        finally:
            browser.close()
    finally:
        pw.stop()


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else ""
    ck = sys.argv[2] if len(sys.argv) > 2 else ""
    if not u:
        print("用法: python migu_resolve.py <咪咕链接> [cookie文本]", file=sys.stderr)
        sys.exit(1)
    import json
    try:
        print(json.dumps(resolve(u, cookie=ck), ensure_ascii=False, indent=2))
    except Exception as e:  # noqa: BLE001
        print("FAIL:", str(e), file=sys.stderr)
        sys.exit(2)
