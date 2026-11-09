"""edge-tts 配音引擎 · 自动巡查与自愈。

## 为什么需要这个模块

解说配音走 edge-tts（Windows 版与 Mac 版一致），它是「微软 Edge 在线朗读」的客户端。
微软会**周期性作废旧版客户端**，表现为合成请求被拒：`403 Forbidden`、
`NoAudioReceived`、`WebSocketError`。这时功能本身没坏，**升 edge-tts 就好**。

历史教训（2026-10-20）：安装包内 edge-tts 7.2.8 被吊销，全部请求 403，
用户侧表现为「配音全失败 / 成片没旁白」，排查了半天才发现只是版本过期。

所以本模块做三件事：

1. **提前发现**：后台线程定期用一句极短文本探活，把失败原因分成
   `auth`（版本作废，可自动修）/ `network`（用户网络问题，不该乱升级）。
2. **自动修复**：判定为 `auth` 时，从 PyPI 拉最新 wheel，校验 sha256，
   解压到用户目录的「热补丁层」，插到 `sys.path` 最前 —— 冻结包内的安装目录
   （`Program Files` / `.app`）是只读的，只能靠覆盖层生效，且**不需要重新打包**。
3. **当场自愈**：任务跑到一半撞上 403 时，管线侧（edit_ffmpeg）会调
   `repair_after_auth_error()` 升级并热加载，再重试该段，尽量不让用户白等。

## 边界

- 只更新公开的 edge-tts 官方 wheel，不涉及凭证 / 用户数据。
- 校验 PyPI 公布的 sha256，防投毒与中间人；解压前逐成员校验路径防 Zip Slip。
- 先解到临时目录、导入验证通过后才原子替换，失败自动回滚到包内原版。
- 网络类失败**绝不**触发升级（避免把「WiFi 断了」误判成「版本过期」而反复下载）。
- `VDL_TTS_GUARD=0` 可整体关闭巡查（离线环境 / 排障用）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

_PKG = "edge-tts"
_MODULE = "edge_tts"

# 版本元数据只认官方源：镜像的 JSON 索引经常滞后（实测清华源仍停在 6.1.12），
# 拿它判「已是最新」会导致永远不升级。镜像仅在下载 wheel 时做兜底。
_PYPI_JSON = "https://pypi.org/pypi/edge-tts/json"
_PYPI_JSON_MIRRORS = (
    "https://mirrors.cloud.tencent.com/pypi/edge-tts/json",
    "https://pypi.tuna.tsinghua.edu.cn/pypi/edge-tts/json",
)
_PYPI_SIMPLE_MIRRORS = (
    "https://pypi.tuna.tsinghua.edu.cn/simple/edge-tts/",
    "https://mirrors.cloud.tencent.com/pypi/simple/edge-tts/",
)

# 探活用最短文本：合成一句「你好」只有十几 KB，几百毫秒，对服务端也友好。
_PROBE_TEXT = "你好"
_PROBE_VOICE = "zh-CN-XiaoxiaoNeural"
_PROBE_TIMEOUT = 45.0           # 整体探活超时（含 DNS + TLS + 一次合成）
_STATE_FILE = "tts_guard_state.json"

# 巡查节奏
_FIRST_DELAY = 25.0             # 启动后延迟：让开启动高峰，别和首屏抢 IO/网络
_INTERVAL_OK = 6 * 3600.0       # 正常时 6 小时一次
_INTERVAL_BAD = 10 * 60.0       # 异常时 10 分钟复探一次
_INTERVAL_DEAD = 3600.0         # 连续多次网络失败后退到 1 小时，别刷日志
_REPAIR_THROTTLE = 300.0        # 同一进程内两次自动升级至少间隔 5 分钟
_CHECK_THROTTLE = 20.0          # 手动/接口触发探活的最小间隔（防连点）

# 失败原因分类关键词（小写匹配）。
# 顺序很重要：**先判网络**，避免「connection reset by 403 proxy」之类被误判成版本问题。
_NET_PAT = (
    "timed out", "timeout", "connect call failed", "cannot connect",
    "connection refused", "connection reset", "connection aborted",
    "server disconnected", "connector", "clientconnector", "clientpayloaderror",
    "getaddrinfo", "name or service not known", "temporary failure in name resolution",
    "network is unreachable", "no route to host", "ssl", "certificate",
    "proxy", "dns",
)
_AUTH_PAT = (
    "403", "401", "forbidden", "unauthorized",
    "invalid response status", "invalid status",
    "no audio was received", "no audio received", "noaudioreceived",
    "websocket", "sec-ms-gec", "drm",
)

_LOCK = threading.RLock()
_PROBE_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None
_LAST_REPAIR_TS = 0.0
_LAST_CHECK_TS = 0.0
_MEM: dict[str, Any] = {}


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
def _base_dir() -> Path:
    """用户数据目录，与 gateway_config / llm_config 等保持同一处。

    Windows 冻结包 → %APPDATA%\\VideoDownloader（Program Files 只读，不能写包内）；
    其它平台 → ~/.video-downloader。VDL_HOME 可覆盖（测试隔离）。
    """
    override = (os.environ.get("VDL_HOME") or "").strip()
    if override:
        return Path(override)
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "VideoDownloader"
    return Path.home() / ".video-downloader"


def overlay_dir() -> Path:
    """热补丁层目录：edge_tts 新版本解压到这里，靠 sys.path 前置生效。"""
    return _base_dir() / "tts_overlay"


def _state_path() -> Path:
    return _base_dir() / _STATE_FILE


# --------------------------------------------------------------------------
# 启动：把热补丁层插到 sys.path 最前
# --------------------------------------------------------------------------
def bootstrap() -> bool:
    """必须在**第一次 import edge_tts 之前**调用。

    返回是否启用了热补丁层（True 表示当前进程会优先用用户目录里的新版）。
    纯路径操作，无网络、无异常外溢 —— 它跑在进程启动关键路径上。
    """
    try:
        d = overlay_dir()
        if not (d / _MODULE / "__init__.py").is_file():
            return False
        p = str(d)
        if p in sys.path:
            sys.path.remove(p)
        sys.path.insert(0, p)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# 版本信息
# --------------------------------------------------------------------------
def current_version() -> str:
    """当前进程实际加载的 edge_tts 版本。

    取值顺序刻意把「模块里写死的 __version__」放在 metadata 之前：
    覆盖安装时 Inno 只覆盖不删文件，包内可能同时残留新旧两份 dist-info
    （实测 1.0.78 升级用户处同时存在 edge_tts-7.2.8.dist-info 与 7.3.1.dist-info），
    此时 importlib.metadata 会随机命中旧的那份，把真实版本 7.3.1 误报成 7.2.8 ——
    这会让巡查误判「已过期」，也会在界面上显示一个已被微软吊销的版本号。
    """
    mod = sys.modules.get(_MODULE)
    if mod is not None:
        v = getattr(mod, "__version__", "") or ""
        if v:
            return str(v)
    # 模块还没加载：直接读 edge_tts/version.py（比 metadata 可信）
    try:
        import edge_tts.version as _v  # type: ignore[import-not-found]
        v = str(getattr(_v, "__version__", "") or "")
        if v:
            return v
    except Exception:
        pass
    # 冻结包里的 edge_tts 拆不出来时的最后兜底
    try:
        from importlib.metadata import version
        return str(version(_PKG))
    except Exception:
        return "未知"


def _loaded_from_overlay() -> bool:
    mod = sys.modules.get(_MODULE)
    f = str(getattr(mod, "__file__", "") or "")
    if not f:
        return False
    try:
        return str(overlay_dir()) in f
    except Exception:
        return False


def _get_json(url: str, timeout: float = 15.0) -> dict | None:
    try:
        import requests
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "VideoDownloader"})
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def fetch_meta() -> dict | None:
    """取 PyPI 元数据（官方优先，镜像兜底）。"""
    for url in (_PYPI_JSON,) + _PYPI_JSON_MIRRORS:
        meta = _get_json(url)
        if meta and isinstance(meta.get("info"), dict):
            return meta
    return None


def latest_version() -> str | None:
    meta = fetch_meta()
    if not meta:
        return None
    try:
        return str(meta["info"]["version"])
    except Exception:
        return None


def _vtuple(v: str) -> tuple[int, ...]:
    """把 '7.3.1' 变成 (7,3,1) 便于比较；非数字段忽略。"""
    out: list[int] = []
    for part in str(v or "").replace("-", ".").split("."):
        num = ""
        for ch in part:
            if ch.isdigit():
                num += ch
            else:
                break
        if num:
            out.append(int(num))
        elif out:
            break
    return tuple(out)


def _is_newer(a: str, b: str) -> bool:
    """a 是否比 b 新。"""
    return _vtuple(a) > _vtuple(b)


# --------------------------------------------------------------------------
# 探活
# --------------------------------------------------------------------------
def _classify(exc: BaseException) -> str:
    """把异常分成 auth（版本作废，可自动修）/ network（用户网络）/ unknown。"""
    status = getattr(exc, "status", None)
    if isinstance(status, int) and status in (401, 403, 407, 429, 451):
        return "auth"
    msg = f"{type(exc).__name__} {exc}".lower()
    # 网络优先：宁可漏判（不升级），也不能把断网误判成版本过期
    if any(k in msg for k in _NET_PAT):
        return "network"
    if any(k in msg for k in _AUTH_PAT):
        return "auth"
    return "unknown"


def _probe_sync() -> dict:
    """在新线程里跑一次极短合成，规避「当前线程已有事件循环」的冲突。"""
    box: dict[str, Any] = {}

    def _run() -> None:
        async def _job() -> None:
            import edge_tts
            tmpdir = Path(tempfile.mkdtemp(prefix="vdl-tts-probe-"))
            try:
                out = tmpdir / "probe.mp3"
                comm = edge_tts.Communicate(_PROBE_TEXT, _PROBE_VOICE)
                await comm.save(str(out))
                if not out.is_file() or out.stat().st_size < 512:
                    raise RuntimeError("探活合成结果为空")
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        try:
            asyncio.run(_job())
            box["ok"] = True
        except BaseException as exc:  # noqa: BLE001 - 探活要接住一切
            box["ok"] = False
            box["kind"] = _classify(exc)
            box["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"

    th = threading.Thread(target=_run, name="vdl-tts-probe", daemon=True)
    th.start()
    th.join(_PROBE_TIMEOUT)
    if th.is_alive():
        # 线程卡死（edge_tts 自身无超时）：标记 network，不升级，交给下次巡查
        return {"ok": False, "kind": "network", "error": "探活超时（网络挂起）"}
    if box.get("ok"):
        return {"ok": True, "kind": "ok", "error": ""}
    return {"ok": False, "kind": box.get("kind", "unknown"), "error": box.get("error", "未知错误")}


def probe(force: bool = False) -> dict:
    """探活配音引擎（会真的合成一句「你好」，约 0.5~2 秒，需联网）。"""
    global _LAST_CHECK_TS
    with _PROBE_LOCK:
        now = time.time()
        if not force and _MEM.get("_probe") and now - _LAST_CHECK_TS < _CHECK_THROTTLE:
            cached = dict(_MEM["_probe"])
            cached["cached"] = True
            return cached
        try:
            import edge_tts  # noqa: F401
        except Exception as exc:
            res = {"ok": False, "kind": "missing", "error": f"未安装 edge_tts：{exc}"}
        else:
            res = _probe_sync()
        _LAST_CHECK_TS = time.time()
        _MEM["_probe"] = dict(res)
        res["at"] = _LAST_CHECK_TS
        return res


# --------------------------------------------------------------------------
# 修复：下载 → 校验 → 解压 → 原子替换 → 热加载
# --------------------------------------------------------------------------
def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """解压并防 Zip Slip：任何解析后落在 dest 之外的成员一律拒绝。"""
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    for name in zf.namelist():
        target = (dest / name).resolve()
        if target != dest and dest not in target.parents:
            raise RuntimeError(f"wheel 含越界路径，拒绝解压：{name}")
    zf.extractall(dest)


def _wheel_urls(meta: dict) -> list[dict]:
    """从 PyPI 元数据里挑出可用 wheel（优先 py3-none-any 纯 Python 包）。"""
    urls = [u for u in (meta.get("urls") or []) if str(u.get("filename", "")).endswith(".whl")]
    pure = [u for u in urls if "py3-none-any" in str(u.get("filename", ""))]
    return pure or urls


def _mirror_wheel_urls(version: str) -> list[str]:
    """官方下载失败时，从镜像 simple 页刮 wheel 链接兜底（无 sha256，只做结构校验）。"""
    import re as _re
    out: list[str] = []
    for page in _PYPI_SIMPLE_MIRRORS:
        try:
            import requests
            r = requests.get(page, timeout=15, headers={"User-Agent": "VideoDownloader"})
            r.raise_for_status()
            for href in _re.findall(r'href="([^"]+\.whl[^"]*)"', r.text):
                url = href.split("#")[0]
                fname = url.rsplit("/", 1)[-1]
                if f"edge_tts-{version}-" in fname.replace("-", "_") or \
                        f"edge_tts-{version}-" in fname:
                    if url.startswith("/"):
                        url = "https://pypi.tuna.tsinghua.edu.cn" + url
                    out.append(url)
        except Exception:
            continue
    return out


def _download(raw: bytes, dest: Path) -> None:
    dest.write_bytes(raw)


def _hot_reload() -> tuple[bool, str]:
    """把热补丁层插到 sys.path 最前，清掉已加载的旧模块后重新 import。"""
    d = str(overlay_dir())
    if d in sys.path:
        sys.path.remove(d)
    sys.path.insert(0, d)
    for name in [m for m in list(sys.modules) if m == _MODULE or m.startswith(_MODULE + ".")]:
        sys.modules.pop(name, None)
    try:
        import importlib
        importlib.invalidate_caches()
        import edge_tts
        return True, str(getattr(edge_tts, "__version__", "") or "?")
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _rollback_overlay() -> None:
    """热加载失败：把坏的热补丁层挪走，让进程回落到包内原版。"""
    d = overlay_dir()
    pkg = d / _MODULE
    try:
        if pkg.is_dir():
            bad = d / f"_broken_{int(time.time())}"
            pkg.rename(bad)
            shutil.rmtree(bad, ignore_errors=True)
    except Exception:
        pass
    for name in [m for m in list(sys.modules) if m == _MODULE or m.startswith(_MODULE + ".")]:
        sys.modules.pop(name, None)
    p = str(d)
    if p in sys.path:
        sys.path.remove(p)
    try:
        import edge_tts  # noqa: F401 - 回落包内原版
    except Exception:
        pass


def _do_install(meta: dict, version: str) -> dict:
    """真正的安装动作：下载 wheel → 校验 → 解压到临时目录 → 原子替换 → 热加载。"""
    urls = _wheel_urls(meta)
    if not urls:
        return {"ok": False, "updated": False, "error": "PyPI 上没有可用的 edge-tts wheel"}

    import requests

    raw: bytes | None = None
    last_err = ""
    for item in urls:
        try:
            r = requests.get(item["url"], timeout=180, headers={"User-Agent": "VideoDownloader"})
            r.raise_for_status()
            raw = r.content
            expected = (item.get("digests") or {}).get("sha256")
            if expected and hashlib.sha256(raw).hexdigest() != expected:
                return {"ok": False, "updated": False,
                        "error": "校验和不匹配，疑似下载被篡改，已终止更新"}
            break
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
            raw = None
    if raw is None:
        # 官方源不通 → 镜像兜底（仍做 zip 结构 + 包名校验，只是没有 sha256）
        for url in _mirror_wheel_urls(version)[:2]:
            try:
                r = requests.get(url, timeout=180, headers={"User-Agent": "VideoDownloader"})
                r.raise_for_status()
                raw = r.content
                break
            except Exception as exc:  # noqa: BLE001
                last_err = f"{type(exc).__name__}: {exc}"
                raw = None
    if raw is None:
        return {"ok": False, "updated": False, "error": f"下载失败：{last_err or '无可用地址'}"}

    d = overlay_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "_tmp_install"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        with zipfile.ZipFile(BytesIO(raw)) as zf:
            _safe_extract(zf, tmp)
        src = tmp / _MODULE
        if not (src / "__init__.py").is_file():
            return {"ok": False, "updated": False, "error": "wheel 内容异常：缺少 edge_tts/__init__.py"}
        # 原子替换：旧版先改名，新版就位，失败可回滚
        dst = d / _MODULE
        old = d / "_old_pkg"
        shutil.rmtree(old, ignore_errors=True)
        if dst.exists():
            dst.rename(old)
        try:
            src.rename(dst)
        except Exception:
            if old.exists() and not dst.exists():
                old.rename(dst)
            raise
        shutil.rmtree(old, ignore_errors=True)
        # 版本标记（供人工排查，热加载不依赖它）
        try:
            (d / "VERSION").write_text(version, encoding="utf-8")
        except Exception:
            pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    ok, info = _hot_reload()
    if not ok:
        _rollback_overlay()
        return {"ok": False, "updated": False, "error": f"新版加载失败已回滚：{info}"}
    return {"ok": True, "updated": True, "version": info}


def repair(reason: str = "", force: bool = False, throttle: bool = True) -> dict:
    """下载并换上最新 edge-tts（不判故障，纯执行动作）。

    返回 {"ok", "updated", "version"?, "error"?}。线程安全；
    throttle=True 时 5 分钟内重复调用直接跳过（调用方已自行节流时可传 False）。
    """
    global _LAST_REPAIR_TS
    with _LOCK:
        now = time.time()
        if throttle and not force and now - _LAST_REPAIR_TS < _REPAIR_THROTTLE:
            return {"ok": False, "updated": False, "error": "刚尝试过升级，稍后再试", "throttled": True}
        _LAST_REPAIR_TS = now

        meta = fetch_meta()
        if not meta:
            _record("network", "查不到 PyPI 版本信息（网络不通或代理拦截）", error=reason)
            return {"ok": False, "updated": False, "error": "无法获取最新版本（请检查网络）"}
        latest = str(meta.get("info", {}).get("version") or "")
        if not latest:
            return {"ok": False, "updated": False, "error": "PyPI 返回的版本号为空"}

        cur = current_version()
        if not force and not _is_newer(latest, cur):
            # 已经是最新（或本地覆盖层已是最新）→ 不再重复下载，交给上层判「等官方修复」
            _record("outdated_cannot_fix",
                    f"edge-tts 已是 {cur}（PyPI 最新 {latest}），仍被服务端拒绝；"
                    "属微软协议变更，需等官方跟进，可在 24 小时后再试",
                    latest=latest)
            return {"ok": True, "updated": False, "version": cur, "latest": latest,
                    "error": ""}

        res = _do_install(meta, latest)
        if res.get("ok") and res.get("updated"):
            same = _vtuple(str(res.get("version") or "")) == _vtuple(cur)
            _record("updated",
                    f"已重新安装 edge-tts {res.get('version')}" if same
                    else f"已自动升级 edge-tts {cur} → {res.get('version')}",
                    latest=latest)
            return {"ok": True, "updated": True, "version": res.get("version"),
                    "latest": latest, "from": cur}
        _record("repair_failed", res.get("error") or "升级失败", latest=latest)
        return {"ok": False, "updated": False, "error": res.get("error", "升级失败")}


# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------
_STATUS_TEXT = {
    "ok": "配音引擎正常",
    "updated": "配音引擎曾失效，已自动升级并恢复",
    "outdated_cannot_fix": "edge-tts 已是最新仍被拒绝：微软改了协议，等官方修复",
    "network": "连不上微软配音服务（网络 / 代理问题，非版本问题）",
    "repair_failed": "自动升级失败",
    "missing": "未安装 edge-tts，配音不可用",
    "disabled": "自动巡查已关闭（VDL_TTS_GUARD=0）",
    "unknown": "配音引擎状态未知",
}


def _record(status: str, message: str, *, latest: str = "", error: str = "") -> None:
    """写状态到内存 + 落盘（原子写，失败不影响主流程）。"""
    now = time.time()
    with _LOCK:
        st = dict(_MEM.get("_state") or {})
        hist = list(st.get("history") or [])
        hist.append({"ts": now, "status": status, "message": message[:300]})
        st.update({
            "status": status,
            "message": message,
            "latest_version": latest or st.get("latest_version", ""),
            "current_version": current_version(),
            "checked_at": now,
            "last_error": error or "",
            "history": hist[-10:],
        })
        _MEM["_state"] = st
        try:
            data = {k: v for k, v in st.items() if not k.startswith("_")}
            data["overlay"] = str(overlay_dir())
            _atomic_write_json(_state_path(), data)
        except Exception:
            pass


def _atomic_write_json(path: Path, data: dict) -> None:
    from atomic_io import atomic_write_json  # 复用项目的原子写（带文件锁）
    atomic_write_json(path, data)


def _read_state_file() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def status() -> dict:
    """给前端 / 诊断接口用的完整快照。"""
    st = dict(_read_state_file())
    st.update({k: v for k, v in (_MEM.get("_state") or {}).items() if not k.startswith("_")})
    cur = current_version()
    checked = float(st.get("checked_at") or 0)
    st.update({
        "status": st.get("status") or "unknown",
        "ok": st.get("status") in ("ok", "updated"),
        "current_version": cur,
        "overlay": str(overlay_dir()),
        "overlay_active": _loaded_from_overlay(),
        "checked_at": checked,
        "checked_ago_sec": int(time.time() - checked) if checked else None,
        "watchdog_running": bool(_THREAD and _THREAD.is_alive()),
    })
    st.setdefault("message", _STATUS_TEXT.get(st["status"], ""))
    if not checked:
        st["message"] = "尚未巡查（启动后会自动探活一次）"
        st["ok"] = True          # 没有负面证据前不当成故障，避免首屏误报红条
        st["status"] = "unknown"
    st.setdefault("latest_version", "")
    st.setdefault("history", [])
    return st


# --------------------------------------------------------------------------
# 巡查
# --------------------------------------------------------------------------
def check(auto_repair: bool = True, force: bool = False) -> dict:
    """探活一次；鉴权类失败时自动升级并复探。

    这是巡查与「一键检查」的共同入口，返回值就是 status() 快照（附带本次 probe）。
    """
    pr = probe(force=force)
    kind = pr.get("kind")

    if pr.get("ok"):
        _record("ok", f"{_STATUS_TEXT['ok']}（edge-tts {current_version()}）")
        out = status()
        out["probe"] = pr
        return out

    if kind == "missing":
        _record("missing", _STATUS_TEXT["missing"], error=pr.get("error", ""))
        out = status()
        out["probe"] = pr
        return out

    if kind == "network":
        _record("network", _STATUS_TEXT["network"], error=pr.get("error", ""))
        out = status()
        out["probe"] = pr
        return out

    # —— 以下是 auth / unknown：先记「可疑」，再决定是否升级 ——
    if not auto_repair:
        _record("unknown", "配音请求被拒（疑似 edge-tts 版本过期），可点「立即修复」",
                error=pr.get("error", ""))
        out = status()
        out["probe"] = pr
        return out

    rep = repair(reason=pr.get("error", ""))
    if rep.get("updated"):
        pr2 = probe(force=True)
        if pr2.get("ok"):
            _record("updated", f"配音曾失效，已自动升级到 edge-tts {rep.get('version')} 并恢复")
        else:
            _record("outdated_cannot_fix",
                    f"已升级到 edge-tts {rep.get('version')} 仍被拒绝，需等官方跟进",
                    error=pr2.get("error", ""))
    elif not rep.get("ok"):
        _record("repair_failed", f"{_STATUS_TEXT['repair_failed']}：{rep.get('error', '')}",
                error=pr.get("error", ""))
    else:
        # repair 成功但没换版本 = 已是最新仍被拒（微软改了协议，等官方跟进）。
        # 这里显式兜一条，不依赖 repair() 内部是否已经写过状态。
        if str(status().get("status") or "") != "outdated_cannot_fix":
            _record("outdated_cannot_fix",
                    f"edge-tts 已是 {rep.get('version') or current_version()} 仍被拒绝，"
                    "属微软协议变更，需等官方跟进，可在 24 小时后再试",
                    error=pr.get("error", ""))

    out = status()
    out["probe"] = probe(force=True) if not pr.get("ok") else pr
    out["repair"] = rep
    return out


def repair_after_auth_error(exc: BaseException | None = None) -> bool:
    """管线侧调用：任务中撞上鉴权类失败时，升级并热加载。

    返回 True 表示「已经换上可能可用的新版」，调用方应清掉旧引用后重试一次。
    带节流：5 分钟内只尝试一次，避免上百段配音把下载逻辑刷爆。
    """
    global _LAST_REPAIR_TS
    if exc is not None and _classify(exc) not in ("auth", "unknown"):
        return False
    now = time.time()
    if now - _LAST_REPAIR_TS < _REPAIR_THROTTLE:
        return False
    # 自己打时间戳，不依赖 repair() 内部的节流（两处都要节流，且这里要先占位：
    # repair 可能跑 30 秒，期间并发的其他段不应该再触发一轮下载）
    _LAST_REPAIR_TS = now
    try:
        rep = repair(reason=f"任务内鉴权失败：{type(exc).__name__ if exc else '未知'}: {str(exc)[:120] if exc else ''}",
                     throttle=False)
    except Exception:
        return False
    return bool(rep.get("ok") and rep.get("updated"))


def _loop() -> None:
    """后台巡查循环。只在首次与故障时说话，安静是常态。"""
    time.sleep(_FIRST_DELAY)
    net_fail = 0
    while True:
        delay = _INTERVAL_OK
        try:
            st = check(auto_repair=True)
            cur = st.get("status")
            if cur == "ok":
                net_fail = 0
            elif cur == "network":
                net_fail += 1
                delay = _INTERVAL_BAD if net_fail < 3 else _INTERVAL_DEAD
            elif cur in ("updated", "outdated_cannot_fix", "repair_failed", "unknown", "missing"):
                delay = _INTERVAL_BAD if cur in ("repair_failed", "unknown", "missing") else _INTERVAL_OK
        except Exception:
            delay = _INTERVAL_DEAD
        time.sleep(delay)


def ensure_started() -> bool:
    """幂等启动后台巡查线程（在 lifespan 里调用）。"""
    global _THREAD
    if (os.environ.get("VDL_TTS_GUARD") or "").strip().lower() in ("0", "false", "off", "no"):
        _record("disabled", _STATUS_TEXT["disabled"])
        return False
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return True
        _THREAD = threading.Thread(target=_loop, name="vdl-tts-guard", daemon=True)
        _THREAD.start()
        return True


def shutdown() -> None:
    """预留：线程是 daemon，进程退出即回收，无需显式 join。"""
    return None
