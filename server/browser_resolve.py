"""通用浏览器兜底解析（2026-11-13）。

背景：需求榜里 lespark.us 这类站是「腾讯云 VOD + SPA」——页面里只有一个
`<video src="blob:...">`，直链和 m3u8 都靠播放器 JS 在播放时向 CDN 请求，
所以 yt-dlp 的 generic 提取器永远看不到（报 unsupported），静态抓 HTML 也
抓不到。唯一可靠办法是开一个真实浏览器把页面跑起来，监听它发出的媒体请求。

抖音 CDP（`douyin_cdp`）已经证明这套机制在本机可行；这里把它抽成**通用版**：
  - 独立 profile / 独立端口，与抖音实例完全隔离（不抢抖音标签页、不被抖音的
    活跃标记和退出清理牵连）；
  - 优先用随包 chromium（`chrome-headless-shell`，零安装），没有再退本机
    Chrome / Edge / Brave；
  - 只做「抓流」这一件事：抓到 m3u8 / mp4 就返回给上层交给 yt-dlp 下载，
    不在本模块里下载，也不改任何用户数据。

安全约束：
  - 用完即关（`finally` 里 terminate），不常驻、不抢占用户屏幕（headless）；
  - 保留测试域名（`.invalid`/`.example`/`.test`/`.localhost`）直接拒绝，避免
    健康巡检的假样本也去开浏览器；
  - `VDL_GENERIC_BROWSER=0` 可整体关闭。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.parse
import urllib.request

# 用中文环境的正常 Chrome UA：部分站（lespark）会按 UA 返回不同语言标题，
# 中文 UA 拿到的标题才是用户看到的那个。
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# RFC 2606 保留域名：这些永远不会是真实视频站，开浏览器纯属浪费 10 秒。
_TEST_SUFFIXES = (
    ".invalid", ".example", ".example.com", ".example.net", ".example.org",
    ".test", ".localhost",
)

_MAX_WAIT_TITLE = 3.0


def _log(msg: str) -> None:
    """统一走 logging；Windows GUI 下 print 中文会因 cp1252 管道炸掉（见 douyin_cdp）。"""
    try:
        import logging
        logging.getLogger("vdl.browser_resolve").info(msg)
    except Exception:
        pass


def _enabled() -> bool:
    return str(os.environ.get("VDL_GENERIC_BROWSER", "1")).strip().lower() not in (
        "0", "false", "no", "off")


def _port() -> int:
    try:
        return int(os.environ.get("VDL_GENERIC_BROWSER_PORT", "9350"))
    except (TypeError, ValueError):
        return 9350


def _profile_dir() -> str:
    p = os.environ.get("VDL_GENERIC_BROWSER_PROFILE") or ""
    if not p:
        base = os.environ.get("VDL_DATA_DIR") or os.path.join(
            os.path.expanduser("~"), ".video-downloader")
        p = os.path.join(base, "generic-browser-profile")
    p = os.path.expanduser(p)
    os.makedirs(p, exist_ok=True)
    return p


def _is_test_host(host: str) -> bool:
    h = (host or "").lower().strip(".")
    if not h:
        return True
    if h == "localhost":
        return True
    return any(h.endswith(s) for s in _TEST_SUFFIXES)


def _find_browser() -> str:
    """随包 chromium 优先（用户零安装），其次本机 Chromium 内核浏览器。"""
    try:
        import douyin_cdp
        bundled = douyin_cdp._bundled_chromium()
        if bundled and os.path.exists(bundled):
            return bundled
    except Exception:
        pass
    if os.name == "nt":
        cands = [
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    else:
        cands = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return ""


def _http_json(url: str, method: str = "GET", timeout: float = 5.0):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def _wait_cdp(port: int, timeout: float = 25.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            _http_json(f"http://127.0.0.1:{port}/json/version", timeout=2)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def _launch(browser: str, port: int, profile: str) -> subprocess.Popen:
    args = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--mute-audio",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--autoplay-policy=no-user-gesture-required",
        # 强制直连：兜底浏览器需要自己去抓播放器发出的媒体流，若继承系统/launchd 的
        # HTTP 代理（如 Clash 7892），TLS 会被代理掐断（SSL EOF），导致抓流失败。
        # 直连能稳定拿到流（lespark.us 等站本机直连可达，无需代理）。
        "--no-proxy-server",
        f"--user-agent={UA}",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "about:blank",
    ]
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    # 清掉代理相关环境变量，双保险（避免某些 Chrome 从环境读取代理）
    _env = dict(os.environ)
    for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
              "https_proxy", "all_proxy", "NO_PROXY", "no_proxy"):
        _env.pop(_k, None)
    kwargs["env"] = _env
    return subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, **kwargs)


def _shutdown(proc: subprocess.Popen | None) -> None:
    if not proc:
        return
    try:
        proc.terminate()
        proc.wait(timeout=6)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _pick_stream(hls: list[str], mp4: list[str]) -> str:
    """优先 master playlist（含 adp./index.m3u8 这类总清单），其次任意 m3u8，再 mp4。

    分片 URL（`.ts`）永远不作为结果 —— 它们只是 HLS 的一片。
    """
    for u in hls:
        low = u.lower()
        if "adp." in low or "/index.m3u8" in low or "master" in low:
            return u
    if hls:
        return hls[0]
    return mp4[0] if mp4 else ""


def resolve(url: str, timeout: int = 55, cookies: str = "") -> dict:
    """打开页面抓媒体流。成功返回 info dict，失败抛 RuntimeError。

    返回字段与 downloader 的 `_browser_fallback_info` 对齐：
      {ok, title, video_url, ext, protocol, http_headers, webpage_url}

    cookies: 可选，登录态 Cookie 头（形如 ``name1=v1; name2=v2``）。注入后页面
      发出的请求与 ``document.cookie`` 都带登录态——这是咪咕这类「必须登录才出流」
      的 SPA 站能解析的前提（首次注入在 ``Page.navigate`` 之前完成）。
    """
    if not _enabled():
        raise RuntimeError("通用浏览器兜底已被 VDL_GENERIC_BROWSER=0 关闭")
    host = urllib.parse.urlparse(url).hostname or ""
    if _is_test_host(host):
        raise RuntimeError(f"测试域名不启用浏览器兜底：{host}")
    if not url.lower().startswith(("http://", "https://")):
        raise RuntimeError("只支持 http(s) 链接")

    browser = _find_browser()
    if not browser:
        raise RuntimeError("未找到可用的 Chromium 内核浏览器")

    port = _port()
    profile = _profile_dir()
    _log(f"启动兜底浏览器 {os.path.basename(browser)} port={port} host={host}")
    t0 = time.time()
    proc = _launch(browser, port, profile)
    try:
        if not _wait_cdp(port, timeout=25):
            raise RuntimeError("兜底浏览器启动超时")
        try:
            tab = _http_json(
                f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url, safe='')}",
                method="PUT")
        except Exception:
            # 老版本 CDP 的 /json/new 只认 GET
            tab = _http_json(
                f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url, safe='')}")
        ws_url = tab.get("webSocketDebuggerUrl") or ""
        if not ws_url:
            raise RuntimeError("无法挂接兜底浏览器标签页")
        got = _capture(ws_url, timeout=max(12, int(timeout)), page_url=url, cookies=cookies)
        if not got.get("video_url"):
            raise RuntimeError("页面已打开，但没有捕获到音视频流（可能需要登录或该内容已下架）")
        got["elapsed"] = round(time.time() - t0, 1)
        _log(f"抓流成功 {got.get('ext')} {got['video_url'][:110]}")
        return got
    finally:
        _shutdown(proc)


def _cookie_domain(page_url: str) -> str:
    """取根域（带前导点）作为注入 Cookie 的 domain，使其对全子域生效。"""
    host = urllib.parse.urlparse(page_url).hostname or ""
    if not host:
        return ""
    parts = host.split(".")
    if len(parts) >= 3:
        return "." + ".".join(parts[-2:])
    return "." + host


def _inject_cookies(send, cookies_header: str, page_url: str) -> None:
    """把 ``name=v; name2=v2`` 形式的 Cookie 头逐条经 CDP 写入目标上下文。

    - ``httpOnly=False``：部分站（咪咕）的取流签名 JS 会读 ``document.cookie``，
      若设成 HttpOnly 则 JS 读不到、签名失败；兜底场景是一次性抓流，无持久化风险。
    - ``secure`` 随 URL 协议：https 页必须 secure 才落得下。
    """
    try:
        domain = _cookie_domain(page_url)
        if not domain:
            return
        secure = page_url.lower().startswith("https")
        for part in cookies_header.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            name, _, value = part.partition("=")
            name, value = name.strip(), value.strip()
            if not name:
                continue
            try:
                send("Network.setCookie", {
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": "/",
                    "httpOnly": False,
                    "secure": secure,
                })
            except Exception:
                pass
    except Exception:
        pass


def _capture(ws_url: str, timeout: int, page_url: str, cookies: str = "") -> dict:
    from douyin_cdp import _WSClient

    ws = _WSClient(ws_url)
    mid = [10]

    def send(method: str, params: dict | None = None) -> None:
        mid[0] += 1
        payload = {"id": mid[0], "method": method}
        if params:
            payload["params"] = params
        try:
            ws.send_text(json.dumps(payload))
        except Exception:
            pass

    hls: list[str] = []
    mp4: list[str] = []
    stale = 0.0
    try:
        send("Network.enable")
        if cookies:
            _inject_cookies(send, cookies, page_url)
        send("Page.enable")
        send("Runtime.enable")
        send("Page.navigate", {"url": page_url})
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                _op, raw = ws.recv_msg()
            except Exception:
                break
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if m.get("method") != "Network.responseReceived":
                continue
            resp = m.get("params", {}).get("response", {})
            u = resp.get("url", "")
            ct = (resp.get("mimeType") or "").lower()
            if not u:
                continue
            if ("mpegurl" in ct or ".m3u8" in u.lower()) and u not in hls:
                hls.append(u)
                stale = time.time()
            elif (ct.startswith("video/") or ".mp4" in u.lower()) and ".ts" not in u.lower():
                if u not in mp4:
                    mp4.append(u)
                    stale = time.time()
            # 抓到主清单后再等 2.5s 收尾（确保 master 与子清单都进来）
            if stale and (time.time() - stale) > 2.5 and (hls or mp4):
                break

        title, href = _fetch_page_meta(ws, send, mid)
        final_url = href or page_url
        stream = _pick_stream(hls, mp4)
        if not stream:
            return {"ok": False, "title": title, "video_url": ""}
        is_hls = ".m3u8" in stream.lower()
        return {
            "ok": True,
            "title": title or "视频",
            "video_url": stream,
            "ext": "mp4",
            "protocol": "m3u8_native" if is_hls else "https",
            "webpage_url": final_url,
            "http_headers": {"User-Agent": UA, "Referer": final_url},
        }
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _fetch_page_meta(ws, send, mid) -> tuple[str, str]:
    """取标题 + 浏览器最终落地页 URL。

    落地页 URL 必须用作 Referer：lespark 的腾讯云 VOD 只认**带 www 的完整页面地址**
    （实测 `https://lespark.us/` 或根路径一律返回 "the source host is forbidden"，
    只有 `https://www.lespark.us/videos/<id>` 才放行）。用户粘贴的链接经常没有 www，
    所以这里必须以浏览器真实 location.href 为准。
    """
    try:
        send("Runtime.evaluate", {
            "expression": "JSON.stringify({t:(document.title||'').trim(),h:location.href})",
            "returnByValue": True})
        want = mid[0]
        end = time.time() + _MAX_WAIT_TITLE
        while time.time() < end:
            try:
                _op, raw = ws.recv_msg()
            except Exception:
                break
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if m.get("id") != want:
                continue
            val = (m.get("result", {}).get("result") or {}).get("value")
            try:
                d = json.loads(val or "{}")
            except Exception:
                return str(val or "").strip(), ""
            return str(d.get("t") or "").strip(), str(d.get("h") or "").strip()
    except Exception:
        pass
    return "", ""


if __name__ == "__main__":  # 手工排查用：python3 browser_resolve.py <url>
    import sys
    try:
        print(json.dumps(resolve(sys.argv[1]), ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001
        print("FAIL:", exc)
        sys.exit(2)
