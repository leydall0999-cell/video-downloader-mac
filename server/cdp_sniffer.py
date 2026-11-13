"""CDP 浏览器嗅探（对标竞品 DataTool 的「悬浮球」能力，2026-09-27）。

原理：连接用户浏览器的 Chrome DevTools Protocol（--remote-debugging-port），
Network.enable 监听每个标签页的请求/响应，按 mimeType + URL 后缀判定媒体资源
（HLS/DASH 清单、mp4/webm 直链），去重后列在工坊 UI；同时在页面右下角注入
悬浮球（Shadow DOM，不污染页面样式），用户点「下载」的项目经
window.__VDL_SNIFF.outbox 由嗅探端 Runtime.evaluate 轮询取回 ——
**不走页面 fetch，完全绕开目标站点的 CSP connect-src 限制**（YouTube 等站点
会拦掉往 127.0.0.1 发的 fetch，这是走 outbox 而不是 API 回传的原因）。

线程模型：独立后台线程 + 独立 asyncio loop（FastAPI 的 def 路由直接调用
同步方法，互不干扰）。每个 page target 一条 websocket，两条协程：
reader（收事件）+ ticker（1.5s：轮询 outbox、刷新悬浮球列表）；
外层监视协程每 3s 拉 /json 发现/回收标签页。

浏览器启动：Chrome 136+ 禁止默认 profile 开 remote debugging，
因此必须用独立 user-data-dir（~/.video-downloader/cdp-profile）启动，
不动用户现有会话。
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid
from collections import OrderedDict
from typing import Any
from urllib.parse import urlsplit

try:
    import websockets  # uvicorn[standard] 自带
except ImportError:  # pragma: no cover
    websockets = None

MAX_ITEMS = 200          # 环形上限：URL 全量去重，超限弹最旧
TICKER_INTERVAL = 1.5    # outbox 轮询 / 悬浮球刷新
WATCH_INTERVAL = 1.5     # /json 监视（发现新标签页；间隔越小，错过页面加载期请求的窗口越小）
CDP_HTTP_TIMEOUT = 3.0

# 回执通道预算（见 __init__ 注释）：扩展每 ~0.9s 轮询一次，重建任务正常 < 10s。
SEND_TTL = 600.0         # send_id / 回执保留 10 分钟，超时清理
SEND_MAX = 200           # 回执与待回执表的环形上限
DESKTOP_AUTH_TTL = 30.0  # 桌面端登录态信号有效期（前端 3s 轮询一次 picked，30s 未续期即视为未知）

# 来源方可指定的清晰度白名单（与 downloader.QUALITY_PRESETS / BEST_KEY / AUDIO_KEY 对齐）。
# 用户实测反馈（2026-10-01）：「目前没法选择分辨率」——扩展/悬浮球发来的条目原本被前端
# 写死成 best，用户无从选择。现在来源方（扩展 popup）可带上 quality，桌面端据此建任务。
# 不在此表内（含空串）一律置 ""，由桌面端用面板上的默认清晰度兜底。
ALLOWED_QUALITY = frozenset({
    "best", "2160", "1440", "1080", "720", "480", "360",
    "audio", "webm", "m4a",
})

# 来源方可声明的条目类型白名单（与 add_ext_items 的 kind_hint 同一套语义）。
# 背景（2026-10-02 真机实测）：扩展 popup 的「解析并下载」提交的是**页面 URL**
# （YouTube 等靠 yt-dlp 解析），而 classify_media 只认 .mp4/.m3u8 这类**后缀**
# ——页面 URL 判不出，若直接落回 "media"，桌面端 sniffQuality 会把它当「直链」
# 而丢弃用户选的清晰度。故与 kind_hint 同规矩：服务端判定优先，判不出才采信来源方。
ALLOWED_KIND = frozenset({"media", "playlist", "segment", "page"})

# ---------------------------------------------------------------------------
# 媒体判定（纯函数，便于离线测试）
# ---------------------------------------------------------------------------

_PLAYLIST_SUFFIX_RE = None  # 延迟编译
_MIME_PLAYLIST = ("application/vnd.apple.mpegurl", "application/x-mpegurl", "application/dash+xml", "mpegurl")
_MIME_MEDIA_PREFIX = ("video/", "audio/")
_MEDIA_SUFFIXES = (".mp4", ".webm", ".m4v", ".mov", ".flv", ".mkv", ".mp3", ".m4a", ".aac", ".wav", ".ogg")
_SEGMENT_SUFFIXES = (".m4s", ".ts", ".mp4")  # mp4 同时是直链与分片形态，按 mime 区分
_IGNORED_SUFFIXES = (".vtt", ".srt", ".ass", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".css", ".js", ".html", ".json", ".xml", ".txt", ".ico")

# 站点内部 UI / 交互资源：主机正则 → 该主机上「永远不是用户内容」的路径正则。
# 2026-09-27 用户实测补充。实证案例：YouTube 搜索页会加载自己的语音搜索提示音
# https://www.youtube.com/s/search/audio/{success,failure,no_input,open}.mp3
# （响应 audio/mpeg），被嗅探成「直链」；复制粘贴到工坊后按 youtube:tab 页面解析
# → 报「视频解析失败」，点「下载」也只会建出必然失败的任务。
# **与 extension/sniff-core.js 的 NOISE_HOST_PATHS 逐条对齐，改一边必须改另一边。**
_NOISE_HOST_PATHS = (
    (re.compile(r"(^|\.)youtube\.com$"), re.compile(r"^/(?:s/search|youtubei)/")),
    (re.compile(r"(^|\.)youtube\.com$"), re.compile(r"^/(?:ptracking|generate_204)$")),
    # 本站（视频工坊网页版）内部转发流：网盘代取 /api/cloud-drive/download、
    # 媒体中继 /api/media/proxy、直链下载等都是**一次性签名/代理流**，浏览器嗅探到
    # 发给桌面端后必然复不下第二次（2026-10-25 实测：报「版权保护」的误分类任务）。
    # 本站媒体一律用站内下载 UI，嗅探直接丢弃。含所有子域（hk./vdl-N.）。
    (re.compile(r"(^|\.)hanyuxz\.top$"), re.compile(r"^/api/")),
)


def is_noise_url(url: str) -> bool:
    """站点内部 UI/接口资源（播放器音效、内部 API）→ True，一律不当作可下载媒体。

    **跨模块单源**：链接校验（platforms._is_station_internal_url）也复用它，
    改规则只需改这里一处 + 扩展侧 sniff-core.js 的 NOISE_HOST_PATHS。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    path = parts.path or ""
    for host_re, path_re in _NOISE_HOST_PATHS:
        if host_re.search(host) and path_re.search(path):
            return True
    return False


def _path_suffix(url: str) -> str:
    """取 URL 路径的小写后缀（含点）。查询串被剥掉；无后缀返回 ''。"""
    try:
        path = urlsplit(url).path
    except ValueError:
        return ""
    # yt-dlp 风格的无后缀签名 URL（如 googlevideo/videoplayback?...）返回空
    base = os.path.basename(path)
    if "." not in base:
        return ""
    return "." + base.rsplit(".", 1)[1].lower()


def _as_int(v: Any) -> int:
    """宽松取非负整数（扩展/CDP 可能给字符串、float 或 None）；非法 → 0。

    0 一律表示「未知」——没有 0 字节的媒体，所以不需要区分「0 字节」与「不知道」。
    """
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _as_float(v: Any) -> float:
    """宽松取正浮点（时长，秒）；非法/非正 → 0.0（0 表示「未知」）。"""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return 0.0
    return n if n > 0 else 0.0


def _bare_hash_name(url: str) -> bool:
    """basename 是「可选前导点 + 纯十六进制（≥16 位）」裸名（无真实扩展名）。

    站点 HLS/fMP4 分片的常见命名，如 /.b9cd747a55d43e6eae830ba1e1cf2
    （2026-10-26 pomo.mom 实测：几百 KB 的分片被当成「直链」刷满面板）。
    真正的整片直链几乎总带真实扩展名（.mp4/.mkv/...）或可读文件名。
    与 extension/sniff-core.js 的 bareHashName() 逐条对齐，改一边必须改另一边。
    """
    try:
        path = urlsplit(url).path
    except ValueError:
        return False
    base = os.path.basename(path) if path else ""
    if base.startswith("."):
        base = base[1:]
    if not base or "." in base:   # 有真实扩展名的不算
        return False
    return len(base) >= 16 and all(c in "0123456789abcdefABCDEF" for c in base)


def classify_media(url: str, mime: str = "") -> str:
    """判定 CDP 资源是否值得展示。返回 "playlist" | "media" | "segment" | ""。

    - playlist：m3u8 / mpd 清单 —— 可直接交给下载链路的**一等公民**
    - media：mp4/webm/音频直链
    - segment：m4s/ts 分片 —— 只聚合计数（用户中途才开嗅探时清单已错过，
      提示「先开启嗅探再播放」），嗅到清单后该占位会被清单取代
    - 空：与媒体无关（字幕/图片/接口 JSON 一律过滤）
    """
    mime = (mime or "").split(";")[0].strip().lower()
    suffix = _path_suffix(url)
    if is_noise_url(url):
        return ""
    if suffix in _IGNORED_SUFFIXES:
        return ""
    if mime.startswith("image/") or mime in ("text/html", "text/css", "application/javascript", "text/javascript"):
        return ""
    if mime in _MIME_PLAYLIST or suffix in (".m3u8", ".mpd") or "mpegurl" in mime:
        return "playlist"
    if mime == "application/dash+xml":
        return "playlist"
    if mime.startswith(_MIME_MEDIA_PREFIX):
        # video/mp4 可能是整片直链也可能是 fMP4 分片：.m4s 一定是分片；
        # 纯 .mp4 + video/mp4 按直链算（整片直链远多于逐段 .mp4 分片）。
        # video/mp2t 是 HLS 的 TS 传输流（整片裸 TS 直链现实中几乎不存在，
        # 且这类站点必带 m3u8 清单）；「点+纯哈希」裸名是站点分片命名 ——
        # 两者都不该进「直链」面板（2026-10-26 pomo.mom 实测）。
        if suffix == ".m4s" or mime in ("video/mp2t", "audio/mp2t") or _bare_hash_name(url):
            return "segment"
        return "media"
    if suffix in (".m4s", ".ts"):
        # 无 mime（requestWillBeSent 阶段）也认分片
        return "segment"
    if suffix in _MEDIA_SUFFIXES:
        return "media"
    return ""


def _short_origin(url: str) -> str:
    try:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}" if p.netloc else url[:60]
    except ValueError:
        return url[:60]


# ---------------------------------------------------------------------------
# 悬浮球注入脚本（在目标页面上下文执行；Shadow DOM + outbox，绕 CSP）
# ---------------------------------------------------------------------------

_BALL_JS = r"""
(function () {
  if (window.__VDL_SNIFF) { window.__VDL_SNIFF.render(); return; }
  var S = { items: [], outbox: [] };
  window.__VDL_SNIFF = S;
  var host = document.createElement('div');
  host.setAttribute('data-vdl-sniff', '1');
  host.style.cssText = 'all:initial;position:fixed;right:18px;bottom:18px;z-index:2147483647;';
  function mount() {
    if (!document.body) { return false; }
    document.body.appendChild(host); return true;
  }
  if (!mount()) { document.addEventListener('DOMContentLoaded', mount); }
  var root = host.attachShadow ? host.attachShadow({mode: 'closed'})
                               : host.createShadowRoot();
  var css = '*{box-sizing:border-box;font:13px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;}' +
    '.ball{width:46px;height:46px;border-radius:50%;background:linear-gradient(135deg,#6c5ce7,#00b894);' +
    'box-shadow:0 4px 14px rgba(0,0,0,.35);cursor:pointer;display:flex;align-items:center;justify-content:center;' +
    'color:#fff;font-size:20px;position:relative;}' +
    '.badge{position:absolute;top:-4px;right:-4px;min-width:18px;height:18px;border-radius:9px;' +
    'background:#e74c3c;color:#fff;font-size:11px;display:none;align-items:center;justify-content:center;padding:0 4px;}' +
    '.panel{display:none;margin-bottom:8px;width:330px;max-height:340px;overflow:auto;background:#fff;' +
    'border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.3);padding:10px;color:#222;}' +
    '.panel h4{margin:0 0 8px;font-size:13px;color:#333;}' +
    '.row{border-bottom:1px solid #f0f0f0;padding:6px 0;}' +
    '.row .u{color:#555;font-size:12px;word-break:break-all;max-height:32px;overflow:hidden;}' +
    '.row button{margin-top:4px;border:0;border-radius:5px;background:#6c5ce7;color:#fff;' +
    'padding:3px 10px;cursor:pointer;font-size:12px;}' +
    '.empty{color:#999;padding:8px 0;}';
  var st = document.createElement('style'); st.textContent = css; root.appendChild(st);
  var panel = document.createElement('div'); panel.className = 'panel'; root.appendChild(panel);
  var ball = document.createElement('div'); ball.className = 'ball'; root.appendChild(ball);
  ball.innerHTML = '<span>⬇</span><span class="badge"></span>';
  var badge = ball.querySelector('.badge');
  ball.addEventListener('click', function () {
    panel.style.display = (panel.style.display === 'block') ? 'none' : 'block';
  });
  S.render = function () {
    var n = S.items.length;
    badge.style.display = n ? 'flex' : 'none';
    badge.textContent = n > 99 ? '99+' : String(n);
    if (panel.style.display !== 'block') return;
    if (!n) { panel.innerHTML = '<h4>视频工坊嗅探</h4><div class="empty">播放视频后这里会出现可下载的流</div>'; return; }
    var h = '<h4>视频工坊嗅探（' + n + '）</h4>';
    S.items.slice(0, 20).forEach(function (it, i) {
      var kind = it.kind === 'playlist' ? 'HLS/DASH' : '直链';
      h += '<div class="row"><div class="u">[' + kind + '] ' + it.url.slice(0, 120) + '</div>' +
           '<button data-i="' + i + '">下载</button></div>';
    });
    panel.innerHTML = h;
    panel.querySelectorAll('button').forEach(function (b) {
      b.addEventListener('click', function () {
        var it = S.items[Number(b.getAttribute('data-i'))];
        if (it) { S.outbox.push({ url: it.url, referer: it.referer || '', mime: it.mime || '', page_url: it.page_url || '' }); b.textContent = '已加入 ✓'; }
      });
    });
  };
  S.setItems = function (items) { S.items = items || []; S.render(); };
  S.render();
})();
"""

_OUTBOX_READ_JS = (
    "(function(){try{return JSON.stringify(window.__VDL_SNIFF"
    " ? window.__VDL_SNIFF.outbox.splice(0) : []);}catch(e){return '[]';}})()"
)



# ---------------------------------------------------------------------------
# 嗅探器主体
# ---------------------------------------------------------------------------

class CDPSniffer:
    """单例状态机。线程安全：items/outbox 有锁，ws 连接只在嗅探线程里动。"""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_evt: threading.Event = threading.Event()
        self._port = 0
        self._launched_proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._items: OrderedDict[str, dict] = OrderedDict()   # url -> item
        self._picked: list[dict] = []                          # 悬浮球回传的下载请求
        # ---- 回执通道（2026-09-27）：浏览器扩展点「下载」后必须知道桌面端到底做没做 ----
        # 扩展 → /api/sniffer/send（这条只是入队，必然成功）→ 桌面端 picked 轮询取走 →
        # 建任务。若桌面端未登录/不支持/超配额，建任务会失败，但失败发生在桌面端进程里，
        # 扩展无从得知，于是按钮显示「已发送 ✓」而用户什么也没等到（用户实测抱怨）。
        # 这里补一条回执：入队时发 send_id，桌面端把建任务结果写回，扩展轮询取结果。
        self._sent_ids: OrderedDict[str, float] = OrderedDict()   # send_id -> ts（待回执）
        self._results: OrderedDict[str, dict] = OrderedDict()     # send_id -> 回执
        self._desktop_auth: tuple[float, bool] | None = None      # (ts, 桌面端是否带有效登录态)
        self._ext_seen: float = 0.0                                # 扩展最近一次心跳 ts（0=从未见过）
        self._ext_version: str = ""                                # 扩展自报版本号（心跳携带，空=旧版扩展）
        self._ext_tele: tuple[int, int] = (0, 0)                   # (captured, pushed) 扩展遥测快照
        self._ext_diag: tuple[int, int, str] = (0, 0, "")          # (seen, media, last_mime) 捕获诊断
        # v1.0.40：扩展侧**落盘**的推送记账 (ok, err, page, last_err)。
        # 原来的 pushed 是 SW 内存值，扩展一被挂起就归零，会把「推了」误读成「根本没推」
        # —— 2026-10-01 YouTube 直推失灵时就卡在这个假象上。
        self._ext_push: tuple[int, int, int, str] = (0, 0, 0, "")
        self._ext_page_saw: int = 0                                # 内容脚本哨兵报来的视频页数
        self._error = ""
        self._state = "idle"                                   # idle / running / error

    # ---- 供路由层调用的同步接口 ----

    @property
    def state(self) -> str:
        return self._state

    def status(self) -> dict:
        with self._lock:
            # 桌面端登录态：由桌面端前端轮询 picked 时带的 Authorization 决定（见
            # mark_desktop_auth）。过期（>DESKTOP_AUTH_TTL）返回 None=未知——App 关掉
            # 界面后信号会自然失效，扩展据此显示「无法确认」而不是谎报未登录。
            # 注意：本值只可能是 True 或 None（见 mark_desktop_auth 的防污染约定：
            # 匿名轮询绝不改写为 False），故扩展侧「未登录」红条在当前设计下只会在
            # Token 过期/无信号时出现，不会因普通浏览器伪装轮询而误报。
            logged_in = None
            if self._desktop_auth is not None:
                ts, ok = self._desktop_auth
                if time.time() - ts <= DESKTOP_AUTH_TTL:
                    logged_in = ok
            return {
                "state": self._state,
                "port": self._port,
                "items": len(self._items),
                "picked": len(self._picked),
                "error": self._error,
                "supported": websockets is not None,
                "desktop_logged_in": logged_in,
                # 扩展在线信号（2026-10-01）：心跳 ≤5 分钟内算在线。扩展 SW 被
                # Chrome 挂起后 alarms 最长 1 分钟才唤醒一次，留足余量防误报离线。
                "ext_online": bool(self._ext_seen) and time.time() - self._ext_seen <= 300,
                # 扩展自报版本（2026-10-01）：面板与桌面端包内版本比对，旧版则提示更新。
                # 空串=用户装的还是 1.0.35 及更早（心跳不带版本），同样触发更新提示。
                "ext_version": self._ext_version,
                # 扩展遥测（2026-10-01）：本地已捕获条数 / 推送尝试数——「嗅探不到」时
                # captured>0 而 items=0 = 断在推送层；captured=0 = 断在捕获层（页面没重播等）
                "ext_captured": self._ext_tele[0],
                "ext_pushed": self._ext_tele[1],
                # 捕获诊断（2026-10-01）：扩展侧观察到的响应总数 / 判成媒体数 / 最近 Content-Type
                "ext_seen": self._ext_diag[0],
                "ext_media": self._ext_diag[1],
                "ext_last_mime": self._ext_diag[2],
                # 推送记账（v1.0.40）：跨 SW 重启累加 —— ext_pushed 是内存值，不作判据
                "ext_push_ok": self._ext_push[0],
                "ext_push_err": self._ext_push[1],
                "ext_push_page": self._ext_push[2],
                "ext_push_last_err": self._ext_push[3],
                # 页面哨兵（v1.0.40）：内容脚本报来的视频页数，>0 说明页面侧通道活着
                "ext_page_saw": self._ext_page_saw,
            }

    def mark_desktop_auth(self, user_id: str | None) -> None:
        """桌面端前端每次轮询 picked 时调用：记录**它的**登录态（不是请求方的）。

        扩展自己没有桌面端会话令牌，问 /api/sniffer/status 时永远拿不到登录态；
        而桌面端前端每 3s 就来轮询一次 picked 且带着自己的 Bearer 令牌 —— 借这条
        既有流量把「桌面端登没登录」告诉服务端，扩展即可在点下载前就提示用户。

        ⚠️ 防污染（2026-09-28）：匿名轮询（无 Bearer，user_id is None）一律**不写入**。
        否则任何打开 http://127.0.0.1:8321 的普通浏览器一旦加载了 desktop-app.js，
        会以匿名身份每 3s 把登录态覆盖成 False，导致扩展红条误报「未登录」。
        只有带有效令牌的桌面端才有权更新该信号；匿名请求直接忽略，信号维持上次有效值，
        过期（>DESKTOP_AUTH_TTL）后由 status() 自然回落到 None（=「无法确认」）。
        """
        if not user_id:
            return
        with self._lock:
            self._desktop_auth = (time.time(), True)

    def mark_ext_seen(self, version: str = "", captured: int = 0, pushed: int = 0,
                      seen: int = 0, media: int = 0, last_mime: str = "",
                      push_ok: int = 0, push_err: int = 0, push_page: int = 0,
                      push_last_err: str = "", page_saw: int = 0) -> None:
        """扩展心跳（2026-10-01）：可见时间 + 自报版本 + 捕获/推送遥测 + 捕获诊断 + 推送记账。

        推送记账（push_ok/push_err/push_page/push_last_err，v1.0.40）由扩展落 storage.local，
        跨 SW 重启累加 —— 判断「视频页到底有没有交给桌面端」必须看这组值，不能看 pushed。
        """
        with self._lock:
            self._ext_seen = time.time()
            if version and isinstance(version, str):
                self._ext_version = version.strip()[:20]
            try:
                self._ext_tele = (max(0, int(captured)), max(0, int(pushed)))
                self._ext_diag = (max(0, int(seen)), max(0, int(media)),
                                  str(last_mime or "")[:60])
                self._ext_push = (max(0, int(push_ok)), max(0, int(push_err)),
                                  max(0, int(push_page)), str(push_last_err or "")[:120])
                self._ext_page_saw = max(0, int(page_saw))
            except (TypeError, ValueError):
                pass

    def report_result(self, send_id: str, ok: bool, message: str = "") -> bool:
        """桌面端把「建任务结果」写回给扩展（send_id 来自 /api/sniffer/send 的响应）。"""
        if not send_id:
            return False
        with self._lock:
            self._results[send_id] = {
                "ok": bool(ok),
                "message": (message or "").strip()[:300],
                "at": time.time(),
            }
            self._results.move_to_end(send_id)
            self._prune_sends()
        return True

    def send_result(self, send_id: str) -> dict:
        """扩展查询回执：pending（桌面端还没处理）/ ok / error / unknown（过期或没这条）。"""
        if not send_id:
            return {"state": "unknown"}
        with self._lock:
            self._prune_sends()
            hit = self._results.get(send_id)
            if hit:
                return {
                    "state": "ok" if hit["ok"] else "error",
                    "message": hit["message"],
                }
            if send_id in self._sent_ids:
                return {"state": "pending"}
        return {"state": "unknown"}

    def _prune_sends(self) -> None:
        """清理过期/超量的 send_id 与回执（调用方持锁）。"""
        cutoff = time.time() - SEND_TTL
        while self._sent_ids:
            sid, ts = next(iter(self._sent_ids.items()))
            if ts >= cutoff and len(self._sent_ids) <= SEND_MAX:
                break
            self._sent_ids.pop(sid, None)
        while self._results:
            sid, row = next(iter(self._results.items()))
            if row["at"] >= cutoff and len(self._results) <= SEND_MAX:
                break
            self._results.pop(sid, None)

    def items(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = list(self._items.values())
        return rows[-limit:][::-1]  # 最新在前

    def take_picked(self) -> list[dict]:
        with self._lock:
            rows, self._picked = self._picked, []
        return rows

    def add_manual(self, payload: dict) -> dict:
        """悬浮球/浏览器扩展直接提交的下载项（outbox 轮询失败时的兜底入口）。

        - cookie：MV3 扩展从 webRequest 捕获的页面 Cookie（嗅探直链常带签名
          且要求会话，桌面端下载任务直接透传给 yt-dlp；截断 8192 防御异常头）
        - source：提交来源标记（'manual' 悬浮球兜底 / 'extension' 浏览器扩展）
        - quality：来源方指定的清晰度（2026-10-01 用户反馈「没法选择分辨率」）。
          只在 ALLOWED_QUALITY 内透传，非法/缺省一律置 ""，桌面端用面板默认值兜底。
        - kind：来源方声明的条目类型（2026-10-02 真机实测补）。页面 URL 判不出类型时
          采信 ALLOWED_KIND 内的 hint，否则桌面端会当成「直链」丢掉上面的 quality。
        """
        url = (payload.get("url") or "").strip()
        if not url:
            raise ValueError("url is required")
        quality = (payload.get("quality") or "").strip().lower()
        if quality not in ALLOWED_QUALITY:
            quality = ""
        # 服务端判定优先；判不出（页面 URL 无媒体后缀）才采信来源方 hint，且仅限白名单。
        kind = classify_media(url, payload.get("mime") or "")
        hint = (payload.get("kind") or "").strip().lower()
        if not kind and hint in ALLOWED_KIND:
            kind = hint
        item = {
            "url": url,
            "mime": (payload.get("mime") or "").strip(),
            "kind": kind or "media",
            "referer": (payload.get("referer") or "").strip(),
            "page_url": (payload.get("page_url") or "").strip(),
            "page_title": payload.get("page_title") or "",
            # 媒体原名（2026-11-13）：popup 点「下载」走 /api/sniffer/send → add_manual，
            # 桌面端 take_picked 后用它建任务，避免任务名/落盘名变成 URL basename 数字串。
            "title": (payload.get("title") or "").strip()[:200],
            "cookie": (payload.get("cookie") or "").strip()[:8192],
            "quality": quality,
            "first_seen": time.time(),
            "count": 1,
            "source": (payload.get("source") or "manual").strip()[:32],
            # 标题/时长/大小（2026-10-02）：popup / 悬浮球提交时可能带上，缺省 0 = 未知。
            "send_id": uuid.uuid4().hex[:12],
        }
        _sz, _du = _as_int(payload.get("size")), _as_float(payload.get("duration"))
        if _sz:
            item["size"] = _sz
        if _du:
            item["duration"] = _du
        with self._lock:
            self._picked.append(item)
            self._sent_ids[item["send_id"]] = time.time()
            self._prune_sends()
        return item

    def start(self, port: int = 9222, launch: bool = False) -> dict:
        if websockets is None:
            raise RuntimeError("websockets 库不可用（uvicorn[standard] 未安装）")
        if self._thread and self._thread.is_alive():
            return self.status()
        if not self._http_alive(port):
            if launch:
                self._launch_browser(port)
                self._wait_http(port, timeout=12.0)
            if not self._http_alive(port):
                self._state, self._error = "error", (
                    f"127.0.0.1:{port} 上没有调试浏览器；请开启「以调试模式启动浏览器」"
                    "或让 Chrome 带 --remote-debugging-port 重启")
                return self.status()
        self._stop_evt.clear()
        self._port = port
        self._error = ""
        self._state = "running"   # 预置：线程刚起时 status() 查询不至于显示 idle
        self._thread = threading.Thread(target=self._run, name="vdl-cdp-sniffer", daemon=True)
        self._thread.start()
        return self.status()

    def stop(self) -> dict:
        self._stop_evt.set()
        proc, self._launched_proc = self._launched_proc, None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=6.0)
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass
        self._state, self._port = "idle", 0
        return self.status()

    # ---- 内部 ----

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._tasks: set[asyncio.Task] = set()
        try:
            self._loop.run_until_complete(self._browser_session())
        except Exception as exc:  # noqa: BLE001 - 后台线程兜底
            self._state, self._error = "error", f"嗅探线程异常: {exc}"
        finally:
            try:
                self._loop.close()
            except Exception:  # noqa: BLE001
                pass

    async def _browser_session(self) -> None:
        """browser 级 CDP 会话（flatten 模式）。

        用 Target.setAutoAttach 在**标签页创建瞬间**挂上并立刻 Network.enable ——
        赶在页面首个请求之前，直链导航型页面（打开 URL 就播、1s 内请求完毕）
        也能抓到。旧方案（轮询 /json 逐 tab 建连）要 2~3s，必然错过这类页面。
        """
        import websockets as _ws
        self._state = "running"
        ws_url = await asyncio.to_thread(self._browser_ws_url, self._port)
        pages: dict[str, dict] = {}      # sessionId -> {title, url}
        pendings: dict[str, dict] = {}   # sessionId -> {requestId: meta}
        try:
            async with _ws.connect(ws_url, max_size=None, open_timeout=8) as ws:
                self._ws_id = 0
                await self._send(ws, "", "Target.setAutoAttach", {
                    "autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
                last_ticker = 0.0
                while not self._stop_evt.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                    except asyncio.TimeoutError:
                        raw = None
                    if raw is not None:
                        self._on_browser_message(json.loads(raw), pages, pendings, ws)
                    now = time.time()
                    if now - last_ticker >= TICKER_INTERVAL:
                        last_ticker = now
                        for sid in list(pages):
                            await self._tick_sid(ws, sid)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if not self._stop_evt.is_set():
                self._state, self._error = "error", f"浏览器连接断开: {exc}"[:200]

    async def _send(self, ws: Any, sid: str, method: str, params: dict | None = None) -> None:
        self._ws_id = (getattr(self, "_ws_id", 0) + 1) & 0x7FFFFFFF
        msg: dict[str, Any] = {"id": self._ws_id, "method": method}
        if params:
            msg["params"] = params
        if sid:
            msg["sessionId"] = sid
        await ws.send(json.dumps(msg, ensure_ascii=False))

    def _on_browser_message(self, msg: dict, pages: dict, pendings: dict, ws: Any) -> None:
        method = msg.get("method") or ""
        params = msg.get("params") or {}
        sid = msg.get("sessionId") or ""
        if method == "Target.attachedToTarget":
            ti = params.get("targetInfo") or {}
            new_sid = params.get("sessionId") or ""
            if ti.get("type") == "page" and new_sid:
                pages[new_sid] = {"title": ti.get("title") or "", "url": ti.get("url") or ""}
                self._spawn(self._enable_page(ws, new_sid))
            return
        if method == "Target.targetInfoChanged":
            ti = params.get("targetInfo") or {}
            if sid in pages:
                pages[sid]["title"] = ti.get("title") or pages[sid]["title"]
                pages[sid]["url"] = ti.get("url") or pages[sid]["url"]
            return
        if not method and "id" in msg:
            # Runtime.evaluate 的响应：outbox 读取那条会带回用户点击的下载项
            value = ((msg.get("result") or {}).get("result") or {}).get("value")
            if isinstance(value, str) and value.startswith("[") and value != "[]":
                try:
                    rows = json.loads(value)
                except ValueError:
                    return
                if isinstance(rows, list) and rows:
                    with self._lock:
                        self._picked.extend(rows)
            return
        if sid not in pages:
            return
        pending = pendings.setdefault(sid, {})
        if method == "Network.requestWillBeSent":
            req = params.get("request") or {}
            rid = params.get("requestId") or ""
            if rid:
                pending[rid] = {
                    "url": req.get("url") or "",
                    "headers": req.get("headers") or {},
                    "page_url": params.get("documentURL") or "",
                }
                if len(pending) > 800:  # 防内存：只留最近的 requestId
                    for k in list(pending)[: -400]:
                        pending.pop(k, None)
            return
        if method == "Network.responseReceived":
            rid = params.get("requestId") or ""
            resp = params.get("response") or {}
            meta = pending.get(rid) or {}
            url = resp.get("url") or meta.get("url") or ""
            if not url:
                return
            kind = classify_media(url, resp.get("mimeType") or "")
            if not kind:
                return
            headers = meta.get("headers") or {}
            referer = headers.get("Referer") or headers.get("referer") or ""
            # 大小（2026-10-02）：CDP 响应头里的 Content-Length（键名大小写不定 → 遍历比）。
            rh = resp.get("headers") or {}
            clen = 0
            for _k, _v in rh.items():
                if str(_k).lower() == "content-length":
                    clen = _as_int(_v)
                    break
            self._register(url=url, mime=resp.get("mimeType") or "", kind=kind,
                           referer=referer, page_url=meta.get("page_url") or "",
                           page_title=pages[sid].get("title") or "", size=clen)
            if len(pending) > 800:
                for k in list(pending)[: -400]:
                    pending.pop(k, None)

    async def _enable_page(self, ws: Any, sid: str) -> None:
        """新 tab 挂上后立刻 enable + 注悬浮球（导航请求前生效）。"""
        try:
            await self._send(ws, sid, "Network.enable")
            await self._send(ws, sid, "Page.addScriptToEvaluateOnNewDocument",
                             {"source": _BALL_JS})
            await self._send(ws, sid, "Runtime.evaluate",
                             {"expression": _BALL_JS, "returnByValue": True})
        except Exception:  # noqa: BLE001
            pass

    def _spawn(self, coro) -> None:
        """惰性创建 task：无运行中事件循环（如单测直调消息处理）时直接丢弃。"""
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _tick_sid(self, ws: Any, sid: str) -> None:
        """低频轮询：读 outbox + 把最新 items 推给悬浮球（未注入的补注）。"""
        try:
            await self._send(ws, sid, "Runtime.evaluate",
                             {"expression": _OUTBOX_READ_JS, "returnByValue": True})
            rows = self.items(limit=30)
            payload = json.dumps([{k: r.get(k) for k in ("url", "mime", "kind", "referer", "page_url")}
                                  for r in rows], ensure_ascii=False)
            # addScriptToEvaluateOnNewDocument 对「已加载完的当前文档」不生效，这里兜底
            await self._send(ws, sid, "Runtime.evaluate", {"expression":
                "window.__VDL_SNIFF ? window.__VDL_SNIFF.setItems(" + payload
                + ") : (function(){" + _BALL_JS + " window.__VDL_SNIFF.setItems("
                + payload + ");})()"})
        except Exception:  # noqa: BLE001 - ticker 失败不打断嗅探
            pass

    def _register(self, *, url: str, mime: str, kind: str, referer: str,
                  page_url: str, page_title: str, source: str = "cdp",
                  cookie: str = "", size: int = 0, duration: float = 0.0,
                  title: str = "") -> None:
        # size / duration（2026-10-02 用户「把嗅探到的视频信息也加上：标题、时长、大小」）：
        #   size     = 响应头 Content-Length（字节），扩展 webRequest / CDP responseReceived 都取得到；
        #   duration = 页面 <video>.duration（秒），只有**页面侧内容脚本**拿得到（扩展 pagewatch.js）。
        # title（2026-11-13「从插件发过来是一串数字，要原名」）：**媒体原名**，由扩展
        #   从页面 DOM 采集（下载弹窗里的「名称：xxx」）。与 page_title（浏览器标签页
        #   标题，SPA 站点恒为站点名）是两个概念，不可混用。直链 URL 常是纯数字 ID /
        #   哈希，原名只在页面上；且扩展补推的时机可能比嗅探晚一步，所以这里对 title
        #   采用**后到即覆盖**，确保不会停在 basename 上。前端/下载统一 `title or page_title`。
        # 0 一律表示「未知」（没有 0 字节的媒体、也没 0 秒的视频），前端据此隐藏该项。
        with self._lock:
            if url in self._items:
                row = self._items[url]
                row["count"] += 1
                # 扩展补发的标题/cookie 可能比 CDP 首记更全，顺手回填。
                # ⚠️ 视频页（page）条目例外：标题**后到即覆盖**。SPA 换页那一刻 tab.title
                # 还是**上一页**的（YouTube 要等新页数据加载完才改标题），首推必然带旧标题；
                # 扩展会在标题 settle 后补推正确标题（background.js::refreshPageTitle）。
                # 若这里仍坚持「只在空时回填」，条目就永远停在**上一个视频**的标题上 ——
                # 用户看到的就是「最新嗅探到的是上一个视频」（2026-10-02 实测）。
                # 页面 URL 恒定、标题只是给人看的标签，后到的更准，覆盖是安全的。
                if page_title and row.get("kind") == "page":
                    if page_title != row.get("page_title"):
                        row["page_title"] = page_title
                elif page_title and not row.get("page_title"):
                    row["page_title"] = page_title
                # 原名后到即覆盖（扩展补推 / 用户点了下载才报出名字，都属正常时序）
                if title:
                    row["title"] = title
                if cookie and not row.get("cookie"):
                    row["cookie"] = cookie
                # 时长/大小：page 条目（SPA 视频页）的时长要等播放器就绪才准、换页还会变 →
                # 与 page_title 同规则「后到即覆盖」；媒体/清单条目的值来自响应头，
                # 基本只来一次 → 空时回填（免得后到的分片把整体大小冲掉）。
                if duration and (row.get("kind") == "page" or not row.get("duration")):
                    row["duration"] = float(duration)
                if size and not row.get("size"):
                    row["size"] = int(size)
                return
            item = {
                "url": url, "mime": mime, "kind": kind, "referer": referer,
                "page_url": page_url, "page_title": page_title,
                "first_seen": time.time(), "count": 1, "source": source,
                "title": title,
            }
            if cookie:
                item["cookie"] = cookie
            if size:
                item["size"] = int(size)
            if duration:
                item["duration"] = float(duration)
            if kind == "playlist":
                # 清单到手：淘汰同来源页的分片占位
                for k in [k for k, v in self._items.items()
                          if v.get("kind") == "segment" and v.get("page_url") == page_url]:
                    self._items.pop(k, None)
            elif kind == "segment":
                # 同来源页已有占位则只计数
                for v in self._items.values():
                    if v.get("kind") == "segment" and v.get("page_url") == page_url:
                        v["count"] += 1
                        return
                item["url"] = url          # 占位保留第一条分片地址便于排查
                item["kind"] = "segment"
            self._items[url] = item
            while len(self._items) > MAX_ITEMS:
                self._items.popitem(last=False)

    def add_ext_items(self, rows: list[dict]) -> int:
        """浏览器扩展自动推送的嗅探条目并入同一 items 库（2026-10-01）。

        扩展侧（MV3 webRequest）已做过媒体判定，这里按同源规则再分类一次；
        服务端判不出但扩展带了 kind_hint（media/playlist/segment）则采信 hint。
        返回实际入库条数（URL 非法 / 与媒体无关的丢弃）。
        """
        added = 0
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url") or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                continue
            mime = str(row.get("mime") or "")
            # 服务端判定优先；判不出时采信扩展 hint（media/playlist/segment 为原有语义，
            # page 为 2026-10-01 新增：YouTube 等 UMP/SABR 站点推页面链接，
            # 桌面端点「下载」时交给 yt-dlp 解析，含 web_safari 免 POT 回退）。
            kind = classify_media(url, mime)
            if not kind and str(row.get("kind_hint") or "") in ("media", "playlist", "segment", "page"):
                kind = str(row.get("kind_hint") or "")
            if kind not in ("playlist", "media", "segment", "page"):
                continue
            self._register(
                url=url, mime=mime, kind=kind,
                referer=str(row.get("referer") or ""),
                page_url=str(row.get("page_url") or ""),
                page_title=str(row.get("page_title") or ""),
                source="ext", cookie=str(row.get("cookie") or ""),
                # 媒体原名（2026-11-13）：ext-push 自动推送与页面采集的成果，
                # _register 对 title「后到即覆盖」，补推只改名不加条目。
                title=str(row.get("title") or "").strip()[:200],
                # 标题/时长/大小（2026-10-02）：扩展按条目带上来，缺省 0 = 未知。
                size=_as_int(row.get("size")),
                duration=_as_float(row.get("duration")),
            )
            added += 1
        return added

    # ---- 浏览器探测 / 启动 ----

    @staticmethod
    def _browser_ws_url(port: int) -> str:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version",
                                    timeout=CDP_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        url = data.get("webSocketDebuggerUrl") or ""
        if not url:
            raise RuntimeError("浏览器未提供 browser 级调试端点")
        return url

    @staticmethod
    def _list_pages(port: int) -> list[dict]:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/json", method="GET")
        with urllib.request.urlopen(req, timeout=CDP_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return [t for t in data if t.get("type") == "page"]

    @staticmethod
    def _http_alive(port: int) -> bool:
        try:
            CDPSniffer._list_pages(port)
            return True
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _wait_http(port: int, timeout: float = 12.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if CDPSniffer._http_alive(port):
                return True
            time.sleep(0.4)
        return False

    def _launch_browser(self, port: int) -> None:
        system = platform.system()
        candidates: list[str] = []
        if system == "Darwin":
            candidates = [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            ]
        elif system == "Windows":  # pragma: no cover
            candidates = [
                os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            ]
        else:  # Linux
            for name in ("google-chrome", "chromium", "chromium-browser"):
                p = shutil.which(name)
                if p:
                    candidates.append(p)
        exe = next((c for c in candidates if os.path.exists(c)), "")
        if not exe:
            raise RuntimeError("本机没有找到 Chrome/Chromium/Edge，无法以调试模式启动浏览器")
        profile = os.path.expanduser("~/.video-downloader/cdp-profile")
        os.makedirs(profile, exist_ok=True)
        self._launched_proc = subprocess.Popen(
            [exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
             "--no-first-run", "--no-default-browser-check", "--start-maximized",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=(system != "Windows"))
        time.sleep(1.0)


SNIFFER = CDPSniffer()
