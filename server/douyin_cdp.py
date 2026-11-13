#!/usr/bin/env python3
"""抖音本地 CDP 解析（桌面端破 Argus 第二层 UIFID 风控）。

原理（2026-11-02 实弹验证通过）：
  抖音 PC 网页对匿名解析启用 Argus 风控（Layer1 登录态缺失 + Layer2 UIFID 设备指纹）。
  服务器侧（VPS Playwright / HTTP a_bogus）拿不到与登录态绑定的设备指纹，永远卡在
  Layer2「Uifid Not Found」。破局唯一活路 = 桌面端**系统真实 Chrome**（指纹完全真实）
  + 用户真实登录态 + CDP 监听播放器发出的 douyinvod 真实流地址。

本模块：
  - 启动/复用系统真实 Chrome（独立 profile `~/.video-downloader/douyin-cdp-profile` +
    独立调试端口，避免与 cdp_sniffer 的 cdp-profile 抢锁/抢端口）。
  - 解析抖音时：在 Chrome 里打开视频页 → CDP 监听 Network.responseReceived →
    捕获 douyinvod 的视频/音轨直链 → 构造成 downloader 兼容的 info dict。
  - 提供「扫码登录」入口：打开抖音登录页（展示二维码），用户在手机抖音扫码；
    登录态写进独立 profile，跨解析会话与 App 重启持久化。
  - 纯标准库 WebSocket 客户端（无 websockets 第三方依赖，适配 onefile 打包）。

⚠️ 仅桌面端（mac/win）可用；web 版（VDL_INSTANCE=cloud）无系统 Chrome，走降级引导。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse

_LOG = logging.getLogger("vdl.douyin_cdp")


def _log(msg: str) -> None:
    """打日志用这个，**别用 print**。

    🔴 2026-11-03 真机 CI 实测（Windows 安装包跑 /api/resolve）：
        500 UnicodeEncodeError: 'charmap' codec can't encode characters ...
    根因是本模块原来用 `print("捕获视频流[1]")` —— Windows 上 sys.stdout 未必是
    None，而可能是 **cp1252 编码的管道**（父进程用 PIPE 起进程、或输出重定向到
    文件），中文一写就抛，整个解析 500。logging 的 handler 会自己吞掉编码异常，
    且 App 内 uvicorn 已配好 handler ⇒ 统一走 logging，并再包一层 try/except 兜底。
    """
    try:
        _LOG.info(msg)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 常量 / 路径
# ---------------------------------------------------------------------------
def _bundled_chromium() -> str:
    """App 内嵌的 Chromium（chrome-headless-shell），找不到返回 ""。

    🔴 2026-11-03 定案：抖音解析必须有**真实 Chromium 内核**（Argus 风控要求设备
    指纹与浏览器同源），而 App 外壳是 WKWebView/mac 或 WebView2/win，都拿不到
    CDP 网络层。原来靠用户自装 Chrome/Edge ⇒ 用户否决「装浏览器才能用抖音」。
    改为随包分发 headless-shell（压缩 99MB / 解压 204MB）：零安装、零窗口、
    零外部进程。构建侧见 desktop/fetch_chromium.sh 与 build_mac.sh。

    路径推导（PyInstaller onedir + macOS .app）：
        <App>.app/Contents/MacOS/<可执行>  ← sys.executable
        <App>.app/Contents/Resources/chromium/chrome-headless-shell
    开发态（非 frozen）返回 ""，照旧走系统浏览器。
    """
    if not getattr(sys, "frozen", False):
        return ""
    try:
        exe = os.path.realpath(sys.executable)
        contents = os.path.dirname(os.path.dirname(exe))   # .../Contents
        for rel in (
            os.path.join("Resources", "chromium"),
            os.path.join("Frameworks", "chromium"),
        ):
            base = os.path.join(contents, rel)
            for name in ("chrome-headless-shell", "chrome-headless-shell.exe"):
                p = os.path.join(base, name)
                if os.path.exists(p):
                    return p
    except Exception:
        pass
    return ""


def _browser_candidates() -> list[str]:
    """按优先级列出可用的 Chromium 内核浏览器（都能跑 `--remote-debugging-port`
    和 `--headless=new`，因此都能当抖音解析引擎）。

    🔴 2026-11-03 用户质问「没装 Chrome 就用不了，那和不能用有什么区别」——这条
    确实是真缺口。两端的解法不同：
      - **内嵌优先**：App 自带 chrome-headless-shell（见 `_bundled_chromium`），
        打头的就是它 ⇒ 装了本 App 的用户 100% 能用，不看系统里有什么。
      - Windows 另有 **自带 Edge** 兜底（Chromium 内核），覆盖率接近 100%。
      - macOS 自带 Safari（WKWebView）**不支持 CDP**，系统候选只认 Chromium 内核的：
        Chrome / Edge / Brave / Chromium / Arc / Opera / Vivaldi / Yandex / QQ浏览器 等
        —— 仅在内嵌缺失（旧包/开发态）或需要「扫码登录可见窗口」时才需要。
    """
    bundled = _bundled_chromium()
    if sys.platform == "darwin":
        return ([bundled] if bundled else []) + [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Arc.app/Contents/MacOS/Arc",
            "/Applications/Opera.app/Contents/MacOS/Opera",
            "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",
            "/Applications/Yandex.app/Contents/MacOS/Yandex",
            "/Applications/QQBrowser.app/Contents/MacOS/QQBrowser",
            "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
            os.path.expanduser(
                "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    if sys.platform == "win32":
        pf = os.path.expandvars(r"%PROGRAMFILES%")
        pf86 = os.path.expandvars(r"%PROGRAMFILES(X86)%")
        la = os.path.expandvars(r"%LOCALAPPDATA%")
        return ([bundled] if bundled else []) + [
            os.path.join(la, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
            # Edge 是 Windows 预装，放在 Chrome 之后作兜底 ⇒ 覆盖率接近 100%
            os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(la, r"BraveSoftware\Brave-Browser\Application\brave.exe"),
            os.path.join(la, r"Programs\Opera\launcher.exe"),
            os.path.join(la, r"Programs\Opera\opera.exe"),
            os.path.join(pf, r"Opera\launcher.exe"),
            os.path.join(la, r"Vivaldi\Application\vivaldi.exe"),
            os.path.join(la, r"Yandex\YandexBrowser\Application\browser.exe"),
            os.path.join(la, r"Chromium\Application\chrome.exe"),
            # 国内主流 Chromium 内核浏览器（排在 Chrome/Edge 之后作兜底）
            os.path.join(pf86, r"Tencent\QQBrowser\QQBrowser.exe"),
            os.path.join(pf, r"Tencent\QQBrowser\QQBrowser.exe"),
            os.path.join(pf86, r"360\360se6\Application\360se.exe"),
            os.path.join(la, r"360Chrome\Chrome\Application\360chrome.exe"),
            os.path.join(pf86, r"Sogou\SogouExplorer\SogouExplorer.exe"),
        ]
    return [
        "/usr/bin/google-chrome",
        "/usr/bin/microsoft-edge",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]


def _default_chrome() -> str:
    cands = _browser_candidates()
    for p in cands:
        if p and os.path.exists(p):
            return p
    return cands[0]   # 都没有：返回首选路径，交给调用方报「未找到浏览器」


def _system_browser() -> str:
    """返回本机已安装的**系统** Chromium 浏览器（不含内嵌 headless-shell）。

    登录扫码需要可见窗口，而内嵌 headless-shell 是无头二进制、永远没有窗口，
    所以登录路径必须挑一个系统浏览器。没有任何系统浏览器时返回 ""（调用方据此
    给友好提示，而不是崩一个不认识的 500）。
    """
    bundled = _bundled_chromium()
    for p in _browser_candidates():
        if p and p != bundled and os.path.exists(p):
            return p
    return ""


PORT = int(os.environ.get("VDL_DOUYIN_CDP_PORT", "9341"))
PROFILE = os.path.expanduser(
    os.environ.get("VDL_DOUYIN_CDP_PROFILE", "~/.video-downloader/douyin-cdp-profile"))
PROFILE = os.path.expanduser(PROFILE)
CHROME = os.environ.get("VDL_CHROME_BIN") or _default_chrome()
DOUYIN_HOME = "https://www.douyin.com/"

# 登录态判定用的 Cookie 名（命中任一即视为已登录）
# 🔴 不要把 odin_tt 放进来（2026-11-03 实测误判）：odin_tt / ttwid / UIFID / passport_csrf_token
# 都是**设备级** Cookie，未登录也存在 —— 放进判定会出现「显示已登录，一解析就要登录」的假阳性。
# 只有下面这些是扫码登录成功后才下发的会话 Cookie。
_LOGIN_COOKIES = ("sid_tt", "sessionid", "sessionid_ss", "sid_guard", "uid_tt")

_LOCK = threading.RLock()   # 可重入：resolve() 内部会再调 is_logged_in()
_PROC = {"proc": None}
# 附加启动参数（诊断/实验用，例如 ["--headless=new"]；正常运行为空）
_EXTRA_FLAGS: list[str] = []
# 实际拉起的浏览器路径，由 _ensure_mode 按模式动态设定：
#   · headless 解析 + 内嵌 chrome-headless-shell 可用 → 用内嵌（零安装、不碰用户 Chrome.app）
#   · headless 解析 + 无内嵌 → 用系统 Chrome 带 --headless=new
#   · 有头登录（扫码）→ 必须用系统 Chrome（headless-shell 无窗口，扫码页看不见）
_ENGINE_BIN = CHROME  # 默认与 CHROME 一致，避免未初始化时装 None

# 空闲回收：抖音解析用的无头实例，在「没有进行中任务」且「空闲超过此秒数」后自动关闭，
# 避免常驻的后台 Chrome 进程让 macOS 误以为 Chrome 已在运行、从而绑架用户点图标开 Chrome。
_IDLE_TIMEOUT = float(os.environ.get("VDL_DOUYIN_IDLE_TIMEOUT", "60"))
_LAST_ACTIVE = [time.time()]
_ACTIVE_TASKS = [0]
_WDOG = {"thread": None}


# ---------------------------------------------------------------------------
# 纯标准库 WebSocket 客户端（仅处理 CDP 文本帧 + ping/pong 回复）
# ---------------------------------------------------------------------------
class _WSClient:
    def __init__(self, ws_url: str):
        assert ws_url.startswith("ws://"), ws_url
        rest = ws_url[5:]
        host, _, path = rest.partition("/")
        self.host = host
        self.port = PORT
        if ":" in host:
            self.host, p = host.split(":")
            self.port = int(p)
        self.path = "/" + path
        self.sock = socket.create_connection((self.host, self.port), timeout=15)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
               % (self.path, self.host, self.port, key))
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("handshake: connection closed")
            buf += chunk
        if b"101" not in buf.split(b"\r\n\r\n", 1)[0]:
            raise RuntimeError("handshake failed: %r" % buf[:160])

    def _recvn(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            c = self.sock.recv(n - len(data))
            if not c:
                raise RuntimeError("WS connection closed")
            data += c
        return data

    def send_text(self, data: str) -> None:
        payload = data.encode("utf-8")
        mask = os.urandom(4)
        h = bytes([0x81])
        ln = len(payload)
        if ln < 126:
            h += bytes([0x80 | ln])
        elif ln < 65536:
            h += bytes([0x80 | 126]) + struct.pack(">H", ln)
        else:
            h += bytes([0x80 | 127]) + struct.pack(">Q", ln)
        self.sock.sendall(h + mask + bytes(payload[i] ^ mask[i % 4] for i in range(ln)))

    def _send_pong(self, payload: bytes) -> None:
        mask = os.urandom(4)
        h = bytes([0x8A])
        ln = len(payload)
        if ln < 126:
            h += bytes([0x80 | ln])
        else:
            h += bytes([0x80 | 126]) + struct.pack(">H", ln)
        self.sock.sendall(h + mask + bytes(payload[i] ^ mask[i % 4] for i in range(ln)))

    def recv_frame(self):
        b = self._recvn(2)
        opcode = b[0] & 0x0F
        ln = b[1] & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._recvn(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._recvn(8))[0]
        payload = self._recvn(ln)
        return opcode, payload

    def recv_msg(self):
        """读取一条非控制消息；遇 ping 立即回 pong（关键：否则 Chrome reset）。"""
        while True:
            op, pl = self.recv_frame()
            if op == 0x8:
                raise RuntimeError("WS closed by peer")
            if op == 0x9:  # ping
                self._send_pong(pl)
                continue
            if op == 0xA:  # pong
                continue
            return op, pl.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# HTTP 辅助
# ---------------------------------------------------------------------------
# 🔴 本地 CDP 端点必须绕开系统/环境代理：App 若在带 HTTP_PROXY 的环境里被拉起
# （终端、启动器、CI 都可能），urllib 默认会把 http://127.0.0.1:9341 也发给代理，
# 代理再回 502/超时 —— 表现为「Chrome 起不来 / 建 tab 失败 / 登录态读不出来」，
# 而实际 Chrome 好好的。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_json(url: str, timeout: int = 10):
    with _LOCAL_OPENER.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _chrome_running() -> bool:
    try:
        _http_json("http://127.0.0.1:%d/json/version" % PORT, timeout=2)
        return True
    except Exception:
        return False


def _page_targets() -> list[dict]:
    """当前 Chrome 里的页面标签（type == 'page'）。空 = 无窗口僵尸。"""
    try:
        return [t for t in _http_json("http://127.0.0.1:%d/json/list" % PORT, timeout=3)
                if t.get("type") == "page"]
    except Exception:
        return []


def _kill_by_profile() -> None:
    """按命令行里的 `--user-data-dir=<本 profile>` 精确杀掉本模块起的浏览器。

    🔴 2026-11-03 跨端补齐：原来只有 `pkill -f`，Windows 上没有 pkill ⇒
    subprocess 抛 FileNotFoundError 被吞掉 ⇒ 杀不掉 ⇒ 僵尸浏览器越堆越多，
    最后表现为「解析突然全部失败、端口 9341 被占」。改成平台分派：
      · POSIX：pkill -f --user-data-dir=<PROFILE>（原逻辑不变）
      · Windows：wmic 取命令行（Win11 24H2 已移除 wmic ⇒ 退 PowerShell 兜底），
        再 `taskkill /F /PID`。**只杀命令行匹配本 profile 的进程**，
        绝不 `taskkill /IM chrome.exe`（会把用户自己开着的浏览器一起关掉）。
    """
    if sys.platform == "win32":
        base = os.path.basename(PROFILE.rstrip("\\/")) or "douyin-cdp-profile"
        no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        pids: list[str] = []
        try:
            out = subprocess.run(
                ["wmic", "process", "where",
                 "name='chrome.exe' or name='msedge.exe' or name='brave.exe'",
                 "get", "processid,commandline", "/format:csv"],
                capture_output=True, text=True, timeout=10, creationflags=no_win)
            for line in (out.stdout or "").splitlines():
                if base.lower() in line.lower():
                    tail = line.rsplit(",", 1)[-1].strip()
                    if tail.isdigit():
                        pids.append(tail)
        except Exception:
            pass
        if not pids:
            ps = (
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.CommandLine -and "
                "($_.CommandLine -like '*__BASE__*') } | "
                "ForEach-Object { $_.ProcessId }".replace("__BASE__", base))
            try:
                out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                     capture_output=True, text=True, timeout=20,
                                     creationflags=no_win)
                pids = [x.strip() for x in (out.stdout or "").splitlines()
                        if x.strip().isdigit()]
            except Exception:
                pass
        for pid in pids:
            try:
                subprocess.run(["taskkill", "/F", "/PID", pid],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=10, creationflags=no_win)
            except Exception:
                pass
        return
    try:
        subprocess.run(["pkill", "-f", "--user-data-dir=%s" % PROFILE],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _kill_chrome() -> None:
    """关掉本 profile 的 Chrome（先走 CDP，再兜底按命令行杀）。

    macOS 上 Chrome 关掉所有窗口后进程会继续驻留（Dock 里那个点），
    CDP target 变成 0 个，既建不了 tab 也不会自动退出。这种"僵尸"必须先清掉。
    """
    try:
        ws = _WSClient(_browser_ws())
        ws.send_text(json.dumps({"id": 1, "method": "Browser.close"}))
        try:
            ws.sock.close()
        except Exception:
            pass
    except Exception:
        pass
    for _ in range(12):
        if not _chrome_running():
            break
        time.sleep(0.5)
    if _chrome_running():
        _kill_by_profile()
        for _ in range(10):
            if not _chrome_running():
                break
            time.sleep(0.5)
    _PROC["proc"] = None


# 窗口位置：解析时把窗口挪到屏幕外（用户无感），扫码登录时挪回屏幕内。
# 🔴 2026-11-03 用户实测报「点解析为什么会打开浏览器」——CDP 解析必须真的跑一个 Chrome，
# 但没必要让用户看见它。窗口位置控制在「新建 Chrome 时」决定，已运行的不动
# （否则登录页轮询 login-status 会把二维码窗口一起挪走）。
_HIDDEN_POS = "-3000,60"   # 越界值交给 macOS 钳制（实际会停在露 40px 的位置）
_VISIBLE_POS = "120,80"
_WIN_W, _WIN_H = 1100, 820


def launch_chrome(open_url: str | None = None, visible: bool = False) -> subprocess.Popen:
    """启动或复用系统 Chrome（独立 profile + 独立端口）。返回进程对象。

    visible=False（默认）= 后台静默：窗口放到屏幕外，只做解析，不打扰用户。
    visible=True = 扫码登录：窗口放回屏幕内。
    仅在需要**新建** Chrome 时生效；已运行实例不改动其窗口（见 _set_window_visible）。
    """
    if _chrome_running():
        if _page_targets():
            return _PROC["proc"]
        # 有端口没窗口 ⇒ 僵尸，关掉重开（否则建 tab 拿不到 webSocket 地址）
        _kill_chrome()
    os.makedirs(PROFILE, exist_ok=True)
    args = [
        _ENGINE_BIN, "--remote-debugging-port=%d" % PORT,
        "--user-data-dir=%s" % PROFILE, "--no-first-run",
        "--no-default-browser-check", "--no-sandbox",
        "--disable-dev-shm-usage", "--disable-gpu",
        "--window-size=%d,%d" % (_WIN_W, _WIN_H),
        # 静默模式先贴屏幕左沿起窗（macOS 会钳制坐标，实际露约 40px 窄边）
        "--window-position=%s" % (_VISIBLE_POS if visible else _HIDDEN_POS),
    ] + list(_EXTRA_FLAGS)
    if open_url:
        args.append(open_url)
    else:
        # 必须带一个初始页面：否则 Chrome 可能走"恢复上次会话"，上次窗口已关时
        # 就起来一个没有窗口的进程（CDP target=0），建 tab 直接失败。
        args.append("about:blank")
    proc = subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    for _ in range(50):
        try:
            _http_json("http://127.0.0.1:%d/json/version" % PORT, timeout=2)
            _PROC["proc"] = proc
            # 会话级登录 Cookie 已随上次退出被清掉 → 启动后先灌回快照，
            # 否则用户每次重启 App 都得重新扫码。
            try:
                _restore_cookie_snapshot()
            except Exception:
                pass
            if not visible and "--headless=new" not in _EXTRA_FLAGS and \
                    "chrome-headless-shell" not in os.path.basename(_ENGINE_BIN):
                # 有头 + 静默：起来就在屏幕左沿待着，别在用户眼前弹窗
                # （headless-shell 无窗口，跳过窗口定位，否则 CDP setWindowBounds 报错）
                try:
                    _set_window_visible(False)
                except Exception:
                    pass
            return proc
        except Exception:
            if proc.poll() is not None:
                raise RuntimeError("Chrome 启动失败，退出码 %s" % proc.poll())
            time.sleep(0.5)
    raise RuntimeError("Chrome 未在预期时间内就绪")


def _browser_ws() -> str:
    return _http_json("http://127.0.0.1:%d/json/version" % PORT, timeout=3)["webSocketDebuggerUrl"]


def _cdp_browser_call(method: str, params: dict | None = None,
                      timeout: float = 5.0) -> dict:
    """Browser 域单次调用（连接浏览器级 WS，收一条同名 id 的响应就返回）。"""
    ws = None
    try:
        ws = _WSClient(_browser_ws())
        ws.send_text(json.dumps({"id": 1, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                _, raw = ws.recv_msg()
            except RuntimeError:
                break
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if m.get("id") == 1:
                if m.get("error"):
                    return {}
                return m.get("result") or {}
    except Exception:
        return {}
    finally:
        try:
            if ws is not None:
                ws.sock.close()
        except Exception:
            pass
    return {}


def _set_window_visible(visible: bool) -> bool:
    """把 Chrome 主窗口挪到屏幕外沿（解析静默）/ 移回屏幕内（扫码可见）。

    🔴 2026-11-03 实测三条硬约束，别随手改：
      1. 不能"最小化"：窗口 minimized ⇒ 页面 visibilityState=hidden ⇒ 抖音播放器
         不初始化，整个解析 56s 抓不到任何 douyinvod 流（有端口、无流）。
      2. 不能 left=-3200 一放了之：macOS 钳制坐标，窗口至少留 ~40px 在屏内
         （1100 宽时 left 最低 -1060）。
      3. 窗口尺寸必须保持正常（1100x800）：抖音按视口大小选码率，窗口缩到很小
         只能拿到低清晰度流。
    所以静默 = 保持 normal 状态 + 贴屏幕最左沿（只露一条 40px 边，不抢焦点）。
    """
    targets = _page_targets()
    if not targets:
        return False
    tid = targets[0].get("id") or ""
    if not tid:
        return False
    res = _cdp_browser_call("Browser.getWindowForTarget", {"targetId": tid})
    wid = res.get("windowId")
    if not wid:
        return False
    bounds = res.get("bounds") or {}
    state = (bounds.get("windowState") or "normal").lower()
    left = bounds.get("left") or 0
    try:
        if visible:
            if not (state == "normal" and left > -1000):
                if state != "normal":
                    _cdp_browser_call("Browser.setWindowBounds",
                                      {"windowId": wid, "bounds": {"windowState": "normal"}})
                _cdp_browser_call("Browser.setWindowBounds", {
                    "windowId": wid,
                    "bounds": {"left": 120, "top": 80, "width": _WIN_W, "height": _WIN_H}})
            if sys.platform == "darwin":
                # 必须显式 activate：Chrome 进程在后台时窗口不会获得焦点，
                # 用户会以为"点了登录没反应"（二维码藏在别的应用后面）
                subprocess.run(["osascript", "-e",
                                'tell application "Google Chrome" to activate'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        # ---- 静默：normal + 贴最左沿（坐标越界由系统钳制，别指望真出屏）----
        if state != "normal":
            _cdp_browser_call("Browser.setWindowBounds",
                              {"windowId": wid, "bounds": {"windowState": "normal"}})
        if state == "normal" and left < -900 and (bounds.get("width") or 0) >= _WIN_W - 8:
            return True
        _cdp_browser_call("Browser.setWindowBounds", {
            "windowId": wid,
            "bounds": {"left": -3000, "top": 60, "width": _WIN_W, "height": _WIN_H}})
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Cookie 快照（🔴 2026-11-03 实测坑）
# 抖音的登录 Cookie（sid_tt / sessionid_ss / sid_guard / odin_tt）是**会话级**的：
# Chrome 正常退出时会把它们从 Cookies 库里清掉（除非开"继续上次会话"），结果就是
# 用户扫一次码，App 重启后又变未登录，得反复扫码。profile 里只剩 UIFID/ttwid 这类
# 设备 Cookie（所以"看起来有抖音 cookie"其实没登录）。
# 解法：登录态一出现就 CDP 导出快照落盘，下次启动灌回浏览器再导航。
# ---------------------------------------------------------------------------
_COOKIE_SNAPSHOT = os.path.join(PROFILE, "douyin_cookies.json")
_LOGIN_COOKIE_SET = set(_LOGIN_COOKIES)
_SNAP_SIG = [None]


def _douyin_cookies(cookies) -> list[dict]:
    """从 CDP cookie 列表里挑出抖音域，转成 Storage.setCookies 可回灌的形状。"""
    out: list[dict] = []
    for c in cookies or []:
        dom = (c.get("domain") or "").lower()
        if "douyin.com" not in dom:
            continue
        item = {
            "name": c.get("name") or "",
            "value": c.get("value") or "",
            "domain": c.get("domain") or ".douyin.com",
            "path": c.get("path") or "/",
            "secure": bool(c.get("secure")),
            "httpOnly": bool(c.get("httpOnly")),
            "sameSite": c.get("sameSite") or "Lax",
        }
        exp = c.get("expires")
        if isinstance(exp, (int, float)) and exp > 0:
            item["expires"] = exp
        out.append(item)
    return out


def _browser_cookies() -> list[dict]:
    """浏览器级取全部 Cookie（含刚登录写入的会话 Cookie）。失败返回空表。"""
    ws = None
    try:
        ws = _WSClient(_browser_ws())
        ws.send_text(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                _, raw = ws.recv_msg()
            except RuntimeError:
                break
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if m.get("id") == 1:
                return (m.get("result") or {}).get("cookies") or []
    except Exception:
        return []
    finally:
        try:
            if ws is not None:
                ws.sock.close()
        except Exception:
            pass
    return []


def _save_cookie_snapshot(cookies=None) -> int:
    """把当前抖音 Cookie 落盘（0600）。不含登录 Cookie 就不写，避免存一堆无用的设备 Cookie。"""
    ck = _douyin_cookies(cookies if cookies is not None else _browser_cookies())
    if not ck or not any(c["name"] in _LOGIN_COOKIE_SET for c in ck):
        return 0
    sig = (len(ck), sum(len(c["value"]) for c in ck))
    if _SNAP_SIG[0] == sig and os.path.exists(_COOKIE_SNAPSHOT):
        return len(ck)
    tmp = _COOKIE_SNAPSHOT + ".tmp.%d" % os.getpid()
    try:
        os.makedirs(PROFILE, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"saved_at": time.time(), "cookies": ck}, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, _COOKIE_SNAPSHOT)
        _SNAP_SIG[0] = sig
        return len(ck)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return 0


def _restore_cookie_snapshot() -> int:
    """把上次落盘的 Cookie 灌回浏览器（每个 Chrome 生命周期灌一次）。"""
    if not os.path.exists(_COOKIE_SNAPSHOT):
        return 0
    try:
        with open(_COOKIE_SNAPSHOT, encoding="utf-8") as f:
            ck = (json.load(f) or {}).get("cookies") or []
    except Exception:
        return 0
    if not ck:
        return 0
    # 老/坏快照（只有设备 Cookie、没有真正登录 Cookie）不要回灌，否则会一直假阳性已登录
    if not any(c.get("name") in _LOGIN_COOKIE_SET for c in ck):
        return 0
    ws = None
    try:
        ws = _WSClient(_browser_ws())
        ws.send_text(json.dumps({"id": 1, "method": "Storage.setCookies",
                                 "params": {"cookies": ck}}))
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                _, raw = ws.recv_msg()
            except RuntimeError:
                break
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if m.get("id") == 1:
                if m.get("error"):
                    return 0
                _SNAP_SIG[0] = (len(ck), sum(len(c.get("value") or "") for c in ck))
                return len(ck)
    except Exception:
        return 0
    finally:
        try:
            if ws is not None:
                ws.sock.close()
        except Exception:
            pass
    return 0


# ── UA 伪装（2026-11-03）─────────────────────────────────────────────────────
# 🔴 无头浏览器默认 UA 带 `HeadlessChrome/`，抖音据此直接判机器人：详情接口恒回
# `S403`（在页面内 fetch 也一样，签名/指纹都正常）。实测三种形态跑同一段代码：
#   有头            → 1.2s 拿到 62KB JSON
#   headless=new    → 4.7s 死循环 403
#   headless=new+真UA → 1.4s 拿到 62KB JSON
# 所以无头必须把 UA 换成**同版本**真 Chrome 的 UA。版本号从浏览器自身 --version
# 取，避免「引擎 156 却自称 120」这种更刺眼的自相矛盾（结果进程内缓存，只跑一次）。
_UA_CACHE: dict[str, str] = {}


def _real_ua() -> str:
    key = sys.platform
    if key in _UA_CACHE:
        return _UA_CACHE[key]
    ver = "156.0.8078.80"
    try:
        out = subprocess.run([CHROME, "--version"], capture_output=True,
                             text=True, timeout=5).stdout
        m = re.search(r"\d+\.\d+\.\d+\.\d+", out or "")
        if m:
            ver = m.group(0)
    except Exception:
        pass
    plat = ("(Windows NT 10.0; Win64; x64)" if sys.platform.startswith("win")
            else "(Macintosh; Intel Mac OS X 10_15_7)")
    ua = ("Mozilla/5.0 %s AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/%s Safari/537.36" % (plat, ver))
    _UA_CACHE[key] = ua
    return ua


def _headless_enabled() -> bool:
    """解析默认走无头 Chrome（零窗口，用户完全无感）。

    🔴 2026-11-03 实测 `--headless=new` + 同一 profile/cookie 快照可以正常抓到
    douyinvod 流（9.9s），所以没必要让用户看窗口。万一某台机器上被抖音拦，
    设 VDL_DOUYIN_HEADLESS=0 可退回「有头但贴屏幕左沿」，或由 _ensure_mode 自动回退。
    """
    return os.environ.get("VDL_DOUYIN_HEADLESS", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _instance_is_headless() -> bool | None:
    """当前 9341 端口上的实例是否无头。无实例返回 None。"""
    try:
        v = _http_json("http://127.0.0.1:%d/json/version" % PORT, timeout=2)
    except Exception:
        return None
    ua = v.get("User-Agent") or ""
    # 页面内 navigator.userAgent 在 headless=new 里与有头一致，
    # 但浏览器进程的 UA（/json/version）仍带 HeadlessChrome —— 这是唯一可靠信号
    return "HeadlessChrome" in ua


def _ensure_mode(headless: bool) -> None:
    """确保 Chrome 实例形态正确：解析=headless（无窗口）/ 登录=有头（看得见二维码）。

    形态不符时先落 Cookie 快照再重启（抖音登录 Cookie 是会话级的，关了就没）。

    🔴 2026-11-13 引擎与 headless 参数适配：
      · 内嵌 chrome-headless-shell 本身就是无头二进制，**不能**再接 `--headless=new`
        （会导致启动失败、进而 fallback 到系统 Chrome 常驻 → 绑架用户 Chrome 图标）。
        故 headless + shell 可用时，只传 UA 伪装，不传 --headless=new。
      · 有头登录必须用系统真实 Chrome（headless-shell 无窗口，二维码看不见）。
    """
    global _ENGINE_BIN
    shell = _bundled_chromium()
    if headless and shell:
        # 内嵌 headless-shell：无头、零安装、不碰用户 Chrome.app
        _ENGINE_BIN = shell
        want = ["--user-agent=" + _real_ua()]
    elif headless:
        # 无内嵌，退回系统 Chrome + 新无头模式
        _ENGINE_BIN = CHROME
        want = ["--headless=new", "--user-agent=" + _real_ua()]
    else:
        # 有头扫码：必须用系统 Chromium 浏览器（headless-shell 无窗口，二维码看不见）。
        # 🔴 不能直接用 CHROME —— 候选列表内嵌排首位，打包版 CHROME 会解析成
        # headless-shell，导致登录弹不出可见窗口。这里显式挑一个系统浏览器。
        sys_bin = _system_browser()
        _ENGINE_BIN = sys_bin or CHROME
        want = []
    cur = _instance_is_headless()
    if cur is not None and cur == headless and _page_targets():
        _EXTRA_FLAGS[:] = want
        return
    if cur is not None:
        try:
            _save_cookie_snapshot()
        except Exception:
            pass
        _kill_chrome()
    _EXTRA_FLAGS[:] = want
    launch_chrome(visible=not headless)


def _mark_active() -> None:
    """记录一次活跃，并懒启动空闲回收看门狗。"""
    _LAST_ACTIVE[0] = time.time()
    _start_watchdog()


def _start_watchdog() -> None:
    """后台线程：抖音解析用的无头实例空闲超过 _IDLE_TIMEOUT 秒后自动关闭。

    🔴 2026-11-13 根治：解析 / 探测完成后不再常驻后台 Chrome（否则 macOS 单实例
    激活会让用户点图标只激活这个无窗口实例 → 「Chrome 打不开」）。连续解析不关，
    闲置达到阈值即回收。有头登录实例（visible）不回收，由用户显式关闭 / App 退出清理。
    """
    if _WDOG["thread"] is not None and _WDOG["thread"].is_alive():
        return

    def _loop():
        while True:
            time.sleep(10)
            try:
                # 仅在没有进行中任务、且当前实例确为无头（解析态）时回收
                if _ACTIVE_TASKS[0] == 0 and _chrome_running():
                    if _instance_is_headless() and (time.time() - _LAST_ACTIVE[0] > _IDLE_TIMEOUT):
                        _log("抖音引擎空闲 %.0fs，自动回收无头实例" % (time.time() - _LAST_ACTIVE[0]))
                        _kill_chrome()
            except Exception:
                pass

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    _WDOG["thread"] = t


def _create_target(url: str, background: bool = False) -> str:
    """在 Chrome 里新开一个 tab（target），返回 targetId。

    当前没有任何页面时用新窗口建（否则建出来的是"无窗口后台 tab"，
    拿不到可挂接的 webSocket 地址）。

    background=True：后台建 tab 不激活窗口 —— 🔴 实测 Chrome 建前台 tab 会
    自动把最小化的窗口恢复出来，导致"解析时浏览器弹出来"（用户 2026-11-03 反馈）。
    """
    no_page = not _page_targets()
    bws = _WSClient(_browser_ws())
    params = {"url": url, "newWindow": no_page}
    if background and not no_page:
        params["background"] = True
    bws.send_text(json.dumps({"id": 1, "method": "Target.createTarget",
                              "params": params}))
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            _, raw = bws.recv_msg()
        except RuntimeError:
            break
        try:
            m = json.loads(raw)
        except ValueError:
            continue
        if m.get("id") == 1 and m.get("result", {}).get("targetId"):
            return m["result"]["targetId"]
    raise RuntimeError("创建抖音 tab 失败")


def _target_ws(target_id: str) -> str | None:
    try:
        targets = _http_json("http://127.0.0.1:%d/json/list" % PORT, timeout=3)
    except Exception:
        return None
    for t in targets:
        if t.get("id") == target_id:
            return t.get("webSocketDebuggerUrl")
    return None


def _close_target(target_id: str) -> None:
    try:
        bws = _WSClient(_browser_ws())
        bws.send_text(json.dumps({"id": 1, "method": "Target.closeTarget",
                                  "params": {"targetId": target_id}}))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 登录态
# ---------------------------------------------------------------------------
def is_logged_in() -> bool:
    """检测独立 profile 里是否已登录抖音（命中登录 Cookie 即视为已登录）。"""
    with _LOCK:
        return _is_logged_in_unlocked()


def _storage_get_cookies() -> list[dict]:
    """浏览器级读 Cookie（不新建标签页，轮询开销极小）。"""
    ws = None
    try:
        ws = _WSClient(_browser_ws())
        ws.send_text(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                _, raw = ws.recv_msg()
            except RuntimeError:
                break
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if m.get("id") == 1:
                return (m.get("result") or {}).get("cookies") or []
    except Exception:
        return []
    finally:
        try:
            if ws is not None:
                ws.sock.close()
        except Exception:
            pass
    return []


def _has_login_cookie(cookies) -> bool:
    for c in cookies or []:
        if "douyin.com" in (c.get("domain") or "").lower() and c.get("name") in _LOGIN_COOKIE_SET:
            return True
    return False


def _snapshot_has_login() -> bool:
    """直接读落盘 Cookie 快照判断是否已登录，**不启动 Chrome**。

    🔴 2026-11-13 根治「视频工坊把用户 Chrome 绑架得打不开」：
    原来本函数一进来就 `launch_chrome()` 去探测登录态，前端 `douyin_login_status`
    轮询每次都会把本 App 拉起的系统 Chrome（无头 / 屏幕外）常驻在后台。macOS 单
    实例激活机制下，用户点 Dock 上的 Chrome 图标只会激活这个无窗口实例 → 表现为
    「Chrome 打不开」。登录态快照本就在解析 / 扫码成功时落盘（`_COOKIE_SNAPSHOT`），
    直接读文件即足以判定，完全无需为「探测」拉起一个常驻浏览器。
    """
    if not os.path.exists(_COOKIE_SNAPSHOT):
        return False
    try:
        with open(_COOKIE_SNAPSHOT, encoding="utf-8") as f:
            ck = (json.load(f) or {}).get("cookies") or []
    except Exception:
        return False
    return any(c.get("name") in _LOGIN_COOKIE_SET for c in ck)


def _is_logged_in_unlocked() -> bool:
    # 🔴 2026-11-13 根治：只读快照，绝不启动 Chrome（详见 _snapshot_has_login）。
    # 解析 / 扫码成功时 cookie 已落盘；未登录 / 游客态无快照即视为未登录。
    return _snapshot_has_login()


def login_status() -> bool:
    """查询抖音登录态（仅用于可选登录入口/诊断，**不据此做任何自动动作**）。

    🔴🔴 2026-11-03 实测：抖音给**游客**也下发 `sid_tt / sessionid / uid_tt / sid_guard`，
    也就是说「按 cookie 名判登录」在游客态恒为真。所以：
      · 绝不能因为这个 true 就去 close_chrome（曾导致用户点「登录抖音」后窗口被自己关掉、
        根本来不及扫码）；
      · 界面也不要据此显示「已登录」。
    解析本身不依赖登录（游客态可用），这里只保留一个尽力而为的查询能力。
    """
    return is_logged_in()


def open_login_page() -> dict:
    """打开抖音登录页（展示二维码），用户在手机抖音 App 扫码登录。"""
    with _LOCK:
        # 扫码登录必须弹出可见窗口 ⇒ 需要系统 Chromium 浏览器（内嵌 headless-shell 无窗口）。
        # 没有任何系统浏览器时返回 ok:false，由路由层转成友好提示（不抛异常、不依赖 yt_dlp）。
        if not _system_browser():
            return {
                "ok": False,
                "error": "登录抖音需要先安装一个 Chromium 内核浏览器",
                "hint": "扫码登录要在看得见窗口里展示二维码，本机未检测到 Chrome / Edge / "
                        "Brave / Arc / Opera / Vivaldi / QQ浏览器 等。请先安装其一；"
                        "仅下载公开视频无需登录，不受影响。",
                "category": "douyin_no_system_browser",
            }
        # 解析用的是无头实例（看不见窗口）⇒ 登录必须先切成有头可见实例
        _ensure_mode(headless=False)
        _set_window_visible(True)
        # 已经有抖音页就复用，别每次点按钮都叠一个新 tab
        existing = next((t for t in _page_targets()
                         if "douyin.com" in (t.get("url") or "")), None)
        if existing:
            target_id = existing.get("id") or ""
            _bring_to_front(target_id)
        else:
            target_id = _create_target(DOUYIN_HOME)
            _bring_to_front(target_id)
        return {
            "ok": True,
            "hint": "已在系统 Chrome 打开抖音登录页，请用手机抖音 App 扫码登录；"
                    "登录成功后即可直接粘贴抖音链接解析下载。",
            "target_id": target_id,
        }


def _bring_to_front(target_id: str) -> None:
    """把既有的抖音 tab 提到最前（用户点「登录抖音」时要能一眼看到二维码）。"""
    if not target_id:
        return
    ws = None
    try:
        ws_url = _target_ws(target_id)
        if not ws_url:
            return
        ws = _WSClient(ws_url)
        ws.send_text(json.dumps({"id": 1, "method": "Page.bringToFront"}))
        time.sleep(0.2)
    except Exception:
        pass
    finally:
        try:
            if ws is not None:
                ws.sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 核心：解析抖音 → 捕获 douyinvod 直链
# ---------------------------------------------------------------------------
def _classify_douyinvod(url: str) -> str | None:
    """返回 'video' / 'audio' / 'combined' / None（非抖音流）。"""
    if "douyinvod.com" not in url:
        return None
    u = url.lower()
    if "media-audio" in u:
        return "audio"
    if "media-video" in u:
        return "video"
    # 形如 v11-weba.douyinvod.com/.../xxx.mp4 且不含 media- 前缀：通常是混合轨
    if u.endswith(".mp4") or "video" in u:
        return "combined"
    return "video"


def resolve(url: str, timeout: int = 90) -> dict:
    """本地 Chrome + CDP 捕获抖音视频/音轨直链，返回与 downloader._douyin_info
    同构的 data dict：{video_url, audio_url, video_has_audio, title, ...}。

    未登录 / 未捕获到流 → 抛 ResolveError（调用方负责转译前端文案）。
    """
    with _LOCK:
        _ACTIVE_TASKS[0] += 1
        _mark_active()
        try:
            return _resolve_unlocked(url, timeout)
        finally:
            _ACTIVE_TASKS[0] -= 1
            _mark_active()


def _resolve_unlocked(url: str, timeout: int = 90) -> dict:
    from downloader import ResolveError

    if not _bundled_chromium() and not os.path.exists(CHROME):
        raise ResolveError(
            "本地未找到 Chrome / Edge 浏览器",
            "抖音解析需要一个 Chromium 内核浏览器（Chrome 或 Edge）来捕获真实视频流"
            "（Safari 内核不行）。装好 Chrome 或 Edge 后重新点解析即可。",
            category="douyin_no_chrome")
    # 🔴🔴 2026-11-03 实测推翻旧结论：**游客态（完全不登录、全新空 profile）也能抓到流**，
    # 同一视频 10.6s 成功、直链与登录态一致（br=1380）。原来的「必须登录」是从服务器侧
    # 失败经验照搬过来的过度限制，已删除 —— 用户首次使用不该被扫码拦住。
    # 登录降级为**可选**：只有私密 / 关注可见 / 部分受限视频才需要（见下方错误文案）。
    headless = _headless_enabled()
    try:
        _ensure_mode(headless=headless)
        # 快路优先：详情接口 ~1s 拿到最高档无水印直链（见 _detail 注释）。
        # 失败（图文 / 私密 / 风控）静默回退到抓流老路，用户无感。
        data = _detail(url, timeout=min(40, max(15, timeout // 2)))
        if data:
            return data
        return _capture(url, timeout)
    except ResolveError as e:
        # 兜底：无头实例被抖音识别/拦截（极少见）时，退回有头可见实例再抓一次。
        # 只对「能连上但没抓到流」重试，登录态/没装 Chrome 这类错误重试也没意义。
        if headless and getattr(e, "category", "") in ("douyin_login_stale", "parse_failed"):
            _log("无头未抓到流(%s)，回退有头重试" % e.category)
            try:
                _ensure_mode(headless=False)
                return _capture(url, timeout)
            except Exception:
                raise e from None
        raise


# ── 详情接口快路（2026-11-03）───────────────────────────────────────────────
# 老路径是「等页面真的开始拉视频流，再从网络事件里抄地址」：慢（6~10s），而且只能
# 拿到播放器**当前选的那一档**（常见 540/720），时长/封面也拿不到（duration=None）。
# 抖音的详情接口一次给出全部码率档位（实测 11~20 档，最高 1080P）、真实时长、封面、
# 无水印源，接口本身 0.25s 就返回。
#
# 🔴 但**不能自己发这个请求**：裸 HTTP 调 /aweme/v1/web/aweme/detail/ 恒被 Argus
# 拦成 403「Uifid Not Found」——缺的是 a_bogus 签名（前端 JS 现算，且必须与设备
# 指纹同源）。所以让**页面自己发**：fetch 由页面 JS 触发，签名自动带上，我们只把
# JSON 取回来。这是「用真浏览器」和「用接口」的结合点，不需要逆向签名算法。
_AWEME_ID_RE = re.compile(r"/(?:video|note|slides)/(\d{15,})")


def _aweme_id(url: str) -> str:
    """从各种抖音链接形态里抠作品 ID（分享长链 / ?modal_id= / 短链展开后的长链）。"""
    m = _AWEME_ID_RE.search(url or "")
    if m:
        return m.group(1)
    m = re.search(r"[?&]modal_id=(\d{15,})", url or "")
    return m.group(1) if m else ""


def _gallery_images(aweme: dict) -> list[dict]:
    """图文作品（多图 / 图集）的图片清单；非图文返回空列表。

    🔴 图文作品 `aweme_type=68`，**同时也带 `video.play_addr`** —— 那是抖音把几张图
    串成幻灯片配 BGM 合成的 720×720 视频。若直接按视频处理，用户粘图文链接会下到
    一个「幻灯片视频」而不是图片（2026-11-03 实测就是这个行为）。所以必须先判图文。

    图片地址形态（实测）：
      https://p3-pc-sign.douyinpic.com/tos-cn-p-0015/<hash>~tplv-dy-aweme-images:q75.webp?<签名>
    - `url_list` 给 3 条同源备用，任意一条可用；
    - **不需要 Referer**（与视频直链不同，实测裸下 200）；
    - 去掉 `~tplv-...` 转码段或换成 `.jpeg` **一律 403** —— 签名把转码参数一起绑了，
      拿不到「原始无损」，只有网页同版 q75 webp。不要再试这条路。
    """
    out: list[dict] = []
    for im in (aweme.get("images") or []):
        if not isinstance(im, dict):
            continue
        urls = [u for u in (im.get("url_list") or []) if u]
        if not urls:
            continue
        out.append({
            "urls": urls,
            "w": int(im.get("width") or 0),
            "h": int(im.get("height") or 0),
        })
    return out


def _pick_track(aweme: dict) -> dict | None:
    """从 aweme_detail 里挑一条最好的直链（默认封顶 1080P）。

    抖音的档位名形如 `normal_1080_0` / `low_540_0`，档位对象本身**不带**
    width/height（实测是 None），只有 `play_addr` 里带真实像素（竖屏 1080P 是
    1080x1440）。所以两套尺寸都要留：`gh` 是档位标签（短边，用来判封顶），
    `w/h` 是真实像素（用来显示）。🔴 不能拿真实长边（1440）去判 1080P 上限，
    否则竖屏 1080P 会被误判超限、全部降档。
    """
    v = aweme.get("video") or {}
    cands: list[dict] = []
    for it in (v.get("bit_rate") or []):
        pa = it.get("play_addr") or {}
        urls = [u for u in (pa.get("url_list") or []) if u]
        if not urls:
            continue
        gear = it.get("gear_name") or ""
        m = re.search(r"(\d{3,4})", gear)
        cands.append({
            "urls": urls,
            "w": int(pa.get("width") or 0),
            "h": int(pa.get("height") or 0),
            "gh": int(m.group(1)) if m else 0,
            "br": int(it.get("bit_rate") or 0),
            "gear": gear,
        })
    if not cands:
        # 极少数老视频没有 bit_rate，退到 play_addr 单条
        pa = v.get("play_addr") or {}
        urls = [u for u in (pa.get("url_list") or []) if u]
        if not urls:
            return None
        return {"urls": urls, "w": int(pa.get("width") or 0),
                "h": int(pa.get("height") or 0), "gh": 0, "br": 0, "gear": ""}
    capped = [c for c in cands if c["gh"] and c["gh"] <= 1080]
    pool = capped or cands
    pool.sort(key=lambda c: (c["gh"], c["br"]), reverse=True)
    return pool[0]


_DETAIL_JS = """(async () => {
  const q = new URLSearchParams({device_platform:'webapp',aid:'6383',
    channel:'channel_pc_web',pc_client_type:'1',version_code:'170400',
    version_name:'17.4.0',cookie_enabled:'true',platform:'PC',downlink:'10',
    aweme_id:'%s'});
  try {
    const r = await fetch('/aweme/v1/web/aweme/detail/?' + q.toString(),
                          {credentials:'include'});
    if (r.status !== 200) return 'S' + r.status;
    const t = await r.text();
    return t.indexOf('aweme_detail') > 0 ? t : 'BAD';
  } catch (e) { return 'ERR'; }
})()"""


def _eval_js(ws, mid: int, expr: str, wait: float = 12.0):
    """在已挂接的标签页里跑一段 JS 并取回值（失败/超时返回 None）。"""
    try:
        ws.send_text(json.dumps({"id": mid, "method": "Runtime.evaluate",
                                 "params": {"expression": expr,
                                            "awaitPromise": True,
                                            "returnByValue": True}}))
    except Exception:
        return None
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            _, raw = ws.recv_msg()
        except Exception:
            return None
        try:
            m = json.loads(raw)
        except ValueError:
            continue
        if m.get("id") == mid:
            return (m.get("result", {}).get("result") or {}).get("value")
    return None


def _detail(url: str, timeout: int = 40) -> dict | None:
    """快路：在抖音页面上下文里调详情接口，拿最高档无水印直链。

    成功返回与 `_capture` 同构的 data dict；失败（图文 / 私密 / 风控 / 超时）
    返回 None，由调用方回退到抓流。**这里绝不抛错** —— 快路失败不该让用户看到
    报错，老路还有机会。
    """
    uid = _aweme_id(url)
    if not uid:
        return None
    target_id = None
    ws = None
    try:
        target_id = _create_target("https://www.douyin.com/")
        ws_url = None
        for _ in range(30):
            ws_url = _target_ws(target_id)
            if ws_url:
                break
            time.sleep(0.3)
        if not ws_url:
            return None
        ws = _WSClient(ws_url)
        try:
            ws.send_text(json.dumps({"id": 1, "method": "Runtime.enable"}))
        except Exception:
            pass
        # 首屏的 __ac_nonce 挑战没跑完时接口会 403，隔几百毫秒就通了，
        # 所以立刻开始试、失败 400ms 后重来（典型 1.2s 内拿到）。
        txt = None
        mid = 10
        deadline = time.time() + timeout
        while time.time() < deadline:
            mid += 1
            v = _eval_js(ws, mid, _DETAIL_JS % uid)
            if isinstance(v, str) and v.startswith("{"):
                txt = v
                break
            time.sleep(0.4)
        if not txt:
            return None
        aw = (json.loads(txt) or {}).get("aweme_detail") or {}
        if not aw:
            return None
        # 图文作品先判：它也有 video.play_addr（抖音合成的幻灯片视频），
        # 按视频走会莫名其妙下一个 720×720 的视频而不是图片。
        gal = _gallery_images(aw)
        if gal:
            title = (aw.get("desc") or "").strip()
            first = gal[0]
            _log("图文作品命中[%d 张] 首图 %dx%d" % (len(gal), first.get("w") or 0, first.get("h") or 0))
            try:
                _save_cookie_snapshot()
            except Exception:
                pass
            return {
                "is_gallery": True,
                # gallery 保留每条图片的全部备用地址（实测每张 3 条同源），
                # 单条下载失败时下载层换源重试；images 只给前端展示用第一张。
                "gallery": [{"urls": list(g["urls"]), "w": g["w"], "h": g["h"]} for g in gal],
                "images": [g["urls"][0] for g in gal],
                "image_count": len(gal),
                "video_url": "",
                "audio_url": "",
                "video_has_audio": False,
                "video_id": str(aw.get("aweme_id") or uid),
                "title": (title[:120] if title else "抖音图文"),
                "duration": None,
                "thumbnail": first["urls"][0],
                "webpage_url": url,
                "height": 0,
                "width": 0,
            }
        tr = _pick_track(aw)
        if not tr:
            return None
        vinfo = aw.get("video") or {}
        h = tr.get("h") or int(vinfo.get("height") or 0) or 720
        w = tr.get("w") or int(vinfo.get("width") or 0) or (h * 9 // 16)
        dur = aw.get("duration")
        try:
            dur_s = round(int(dur) / 1000.0, 3) if dur else None
        except Exception:
            dur_s = None
        title = (aw.get("desc") or "").strip()
        _log("详情接口命中[%s] %dx%d" % (tr.get("gear") or "?", w, h))
        # 抓流成功后顺手刷新登录 Cookie 快照（登录 Cookie 可能刚被抖音续期）
        try:
            _save_cookie_snapshot()
        except Exception:
            pass
        return {
            "video_url": tr["urls"][0],
            "audio_url": "",
            "video_has_audio": True,
            "video_id": str(aw.get("aweme_id") or uid),
            "title": (title[:120] if title else "抖音视频"),
            "duration": dur_s,
            "thumbnail": ((vinfo.get("cover") or {}).get("url_list") or [""])[0],
            "webpage_url": url,
            "height": h,
            "width": w,
        }
    except Exception as _e:
        _log("详情接口未命中：%s: %s" % (type(_e).__name__, str(_e)[:80]))
        return None
    finally:
        if ws is not None:
            try:
                ws.sock.close()
            except Exception:
                pass
        if target_id:
            try:
                _close_target(target_id)
            except Exception:
                pass


def _capture(url: str, timeout: int = 90) -> dict:
    """在已就绪的 Chrome 实例里开 tab、导航、抓 douyinvod 流。形态由 _ensure_mode 决定。"""
    from downloader import ResolveError

    target_id = _create_target(url)
    ws_url = None
    for _ in range(30):
        ws_url = _target_ws(target_id)
        if ws_url:
            break
        time.sleep(0.3)
    if not ws_url:
        raise ResolveError("抖音解析失败", "无法挂接 Chrome 标签页", category="parse_failed")

    videos: list[str] = []
    audios: list[str] = []
    combined: list[str] = []
    title = "抖音视频"
    captured_at = 0.0
    _mid = [10]

    def _send(method, params=None):
        _mid[0] += 1
        _send_txt = json.dumps({"id": _mid[0], "method": method,
                                **({"params": params} if params else {})})
        try:
            ws.send_text(_send_txt)
        except Exception:
            pass

    try:
        import urllib.parse as _up
        ws = _WSClient(ws_url)
        _send("Page.enable")
        _send("Network.enable")
        _send("Runtime.enable")
        # 确保导航到目标（createTarget 已带 url，但稳妥起见再 navigate 一次）
        _send("Page.navigate", {"url": url})
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                _, raw = ws.recv_msg()
            except RuntimeError as e:
                raise ResolveError("抖音解析失败", "Chrome 连接中断：%s" % e,
                                  category="parse_failed") from None
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            method = m.get("method") or ""
            if method == "Network.responseReceived":
                resp = m.get("params", {}).get("response", {})
                rurl = resp.get("url", "")
                kind = _classify_douyinvod(rurl)
                if kind == "video" and rurl not in videos:
                    videos.append(rurl)
                    captured_at = time.time()
                    _log("捕获视频流[%d]" % len(videos))
                elif kind == "audio" and rurl not in audios:
                    audios.append(rurl)
                    captured_at = time.time()
                    _log("捕获音轨流[%d]" % len(audios))
                elif kind == "combined" and rurl not in combined:
                    combined.append(rurl)
                    captured_at = time.time()
                    _log("捕获混合流[%d]" % len(combined))
                # 顺手抓标题
                if not videos and not audios and len(combined) == 0:
                    pass
            elif method == "Page.frameNavigated":
                # 标题在 DOM 里，简单用 frame 的 title 兜底（不够准，下游会覆盖）
                pass
            # 同时抓 document title 通过 Runtime 太重，跳过；标题后续用 URL 兜底
            # 一旦拿到「视频+音频」或「混合轨」，稳定 3s 收尾再退（确保两轨都抓全）
            if captured_at and (time.time() - captured_at > 3):
                if (videos and audios) or combined:
                    break
        # 抓页面标题做文件名（解析完成后再补一刀，不阻塞主流程）
        try:
            _title_id = _mid[0] + 1
            _send("Runtime.evaluate",
                  {"expression": "document.title || ''", "returnByValue": True})
            _tdead = time.time() + 3
            while time.time() < _tdead:
                try:
                    _, raw = ws.recv_msg()
                except RuntimeError:
                    break
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                if m.get("id") == _title_id:
                    val = ((m.get("result") or {}).get("result") or {}).get("value")
                    if isinstance(val, str):
                        _t = val.strip()
                        _t = re.sub(r'\s*[-_|]\s*抖音.*$', '', _t).strip()
                        if _t:
                            title = _t[:120]
                    break
        except Exception:
            pass
    finally:
        try:
            ws.sock.close()
        except Exception:
            pass
        _close_target(target_id)

    # 抓流成功后顺手刷新登录 Cookie 快照（登录 Cookie 可能刚被抖音续期）
    try:
        _save_cookie_snapshot()
    except Exception:
        pass

    # 优先混合轨（单直链），其次 视频+音频 分离轨
    if combined:
        return {
            "video_url": combined[0],
            "audio_url": "",
            "video_has_audio": True,
            "video_id": "",
            "title": title,
            "duration": None,
            "thumbnail": "",
            "webpage_url": url,
            "height": 720, "width": 1280,
        }
    if videos and audios:
        return {
            "video_url": videos[0],
            "audio_url": audios[0],
            "video_has_audio": False,
            "video_id": "",
            "title": title,
            "duration": None,
            "thumbnail": "",
            "webpage_url": url,
            "height": 720, "width": 1280,
        }
    # 只有视频（极少，可能音频流还没发出来）：当作混合轨兜底
    if videos:
        return {
            "video_url": videos[0],
            "audio_url": "",
            "video_has_audio": True,
            "video_id": "",
            "title": title,
            "duration": None,
            "thumbnail": "",
            "webpage_url": url,
            "height": 720, "width": 1280,
        }
    # 🔴 已登录却抓不到流：最常见是"会话在服务端已过期"（本地 Cookie 还在，界面显示已登录，
    # 但抖音把我们当未登录给登录墙）。这种情况必须引导重新扫码，否则用户会卡在
    # "永远显示已登录、永远解析失败"的死角里。另可能是私密/已删除/合集/直播页。
    # 🔴 游客态抓不到流的三种常见原因：① 该视频本身要求登录（私密/关注可见/付费）
    # ② 链接是已删除 / 合集 / 直播页 ③ 页面加载超时。给出「可选登录」入口即可，
    # 不要把话说成「必须先登录」——绝大多数公开视频游客就能下。
    raise ResolveError(
        "抖音没抓到视频流",
        "这条视频可能需要登录才能看（私密 / 关注可见 / 付费内容），也可能是已删除、合集"
        "或直播页。可点「登录抖音」扫码后再试，或换一个单个视频的分享链接。",
        category="douyin_login_stale")


def close_chrome() -> None:
    """退出本地 Chrome（App 退出时调用，释放调试端口）。

    跨进程安全：Chrome 可能由上一个 App 进程启动（本进程没有其 Popen 句柄），
    故优先用 CDP `Browser.close`（同一 profile/端口，任何进程都能关），再退本地句柄。
    """
    proc = _PROC.get("proc")
    # 0) 关之前先落 Cookie 快照：抖音登录 Cookie 是会话级的，Chrome 一退就被清掉，
    #    不存快照 = 用户下次还得重新扫码。
    if _chrome_running():
        try:
            _save_cookie_snapshot()
        except Exception:
            pass
    # 1) CDP Browser.close：跨进程生效
    if _chrome_running():
        try:
            ws = _WSClient(_browser_ws())
            ws.send_text(json.dumps({"id": 1, "method": "Browser.close"}))
            time.sleep(0.5)
        except Exception:
            pass
    # 2) 本地句柄兜底（本进程启动的情况）
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            # 🔴 os.getpgid/os.killpg 是 POSIX-only，Windows 上直接 AttributeError
            # ⇒ Windows 用「按 profile 精确 taskkill」兜底。
            if sys.platform == "win32":
                _kill_by_profile()
            else:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
    _PROC["proc"] = None


# ── 进程退出兜底（2026-11-03）───────────────────────────────────────────────
# 🔴 桌面外壳的退出走 `os._exit(0)`，**atexit 不会执行** ⇒ GUI 侧另在
# desktop_launcher._kill_douyin_engine() 显式收尾（覆盖前端退出按钮 / Cmd+Q /
# Dock Quit）。这里只覆盖「server 进程自己正常结束」的场景：开发态调试、
# CLI 调用、uvicorn 优雅停机 —— 否则测试跑完会留下一堆 headless 进程。
# 注册 `_kill_chrome`（纯进程操作，不发网络请求）而不是 `close_chrome`
# （后者要先写 Cookie 快照，在解释器退出阶段做文件/网络 IO 不可靠）。
import atexit as _atexit

_atexit.register(_kill_chrome)
