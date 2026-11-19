"""server/lo_engine.py — LibreOffice 引擎「按需下载 / 安装 / 探测」。

为什么按需：LibreOffice 解压约 800MB，打包进 App 会把安装包撑大近一倍。
所以做成「首次要用时提示下载 → 装到 App 自己的数据目录 → 以后一直复用」，
不动 /Applications、不需要管理员密码。

下载源用清华镜像（国内快）；下载完用 hdiutil verify 校验 dmg；
装完撕掉 com.apple.quarantine 隔离标记，否则子进程调用会被系统拦。
"""
import os
import platform
import plistlib
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request

VERSION = "26.8.3"
# 我们自己的云端（R2，出站免费）；下载失败自动回退清华镜像。
CLOUD_BASE = "https://download.hanyuxz.top/engines"
_MIRROR = "https://mirrors.tuna.tsinghua.edu.cn/libreoffice/libreoffice/stable/%s/mac/%s/"
_FILENAME = "LibreOffice_%s_MacOS_%s.dmg"

_lock = threading.Lock()
_state = {"status": "idle", "progress": 0, "done": 0, "total": 0, "error": ""}
_dock_patched = set()  # 已补过 LSUIElement 的 app 路径，避免轮询时反复读写 plist


def _arch() -> str:
    m = platform.machine().lower()
    return "aarch64" if m in ("arm64", "aarch64") else "x86_64"


def download_urls() -> list:
    """按优先级返回下载地址：自家 R2 优先，清华镜像兜底。"""
    a = _arch()
    name = _FILENAME % (VERSION, a)
    return [
        CLOUD_BASE.rstrip("/") + "/" + name,
        (_MIRROR % (VERSION, a)) + name,
    ]


def engine_root() -> str:
    return os.path.join(
        os.path.expanduser("~"), "Library", "Application Support",
        "VideoDownloader", "engines",
    )


def bundled_path() -> str:
    return os.path.join(engine_root(), "LibreOffice.app")


def system_path() -> str:
    p = "/Applications/LibreOffice.app"
    return p if os.path.isdir(p) else ""


def _soffice_in(app: str) -> str:
    p = os.path.join(app, "Contents", "MacOS", "soffice")
    return p if os.path.isfile(p) else ""


def _patch_no_dock(app: str) -> bool:
    """给引擎 Info.plist 补 LSUIElement=YES，让 headless 转换不在 Dock 冒图标。

    macOS 只要 .app 被启动就会在 Dock 显示图标（即使 --headless 无窗口），
    除非 Info.plist 声明自己是后台程序（LSUIElement）。从 dmg 装出来的
    LibreOffice 没有这个键，所以每次转换都会弹一下 Dock。

    注意：改 plist 会让原代码签名失效，但引擎是直接 exec 的 Mach-O、
    不走 LaunchServices/Gatekeeper（且安装时已撕掉 quarantine），实测仍可运行
    （`soffice --version` 正常）。故此处不重签——重签反而会把官方签名降级。
    失败不影响安装/转换，仅打日志。
    """
    if not app or app in _dock_patched:
        return False
    plist = os.path.join(app, "Contents", "Info.plist")
    try:
        with open(plist, "rb") as fh:
            data = plistlib.load(fh)
        if data.get("LSUIElement") is True:
            _dock_patched.add(app)
            return True
        data["LSUIElement"] = True
        with open(plist, "wb") as fh:
            plistlib.dump(data, fh)
        _dock_patched.add(app)
        print("[lo_engine] 已为 LibreOffice 补 LSUIElement（Dock 不再显示图标）", flush=True)
        return True
    except Exception as exc:
        print("[lo_engine] 补 LSUIElement 失败（不影响转换）: %s" % exc, flush=True)
        return False


def installed_app() -> str:
    for app in (bundled_path(), system_path()):
        if app and _soffice_in(app):
            # 存量引擎（老版本已下载好的）也补一次，保证升级不到安装流程的用户
            # 同样不再 Dock 冒图标；幂等，已设置会直接返回。
            if app == bundled_path():
                _patch_no_dock(app)
            return app
    return ""


def soffice_bin() -> str:
    app = installed_app()
    return _soffice_in(app) if app else ""


def status() -> dict:
    with _lock:
        s = dict(_state)
    app = installed_app()
    s.update({
        "installed": bool(app),
        "path": app,
        "arch": _arch(),
        "url": download_urls()[0],
        "size_mb": 286,
    })
    return s


def _set(**kw):
    with _lock:
        _state.update(kw)


def _download(dmg_path: str, url: str):
    req = urllib.request.Request(
        url, headers={"User-Agent": "VideoDownloader/1.0 (Macintosh)"}
    )
    # 禁系统代理：部分代理会让 Python 的 TLS 直接 EOF（历史踩坑）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        _set(total=total, progress=1)
        done = 0
        t0 = time.time()
        with open(dmg_path, "wb") as fh:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if total:
                    _set(done=done, progress=max(1, min(88, int(done * 88 / total))))
                # 慢源降级：下了 8MB 还在 600KB/s 以下，就别耗着，切备用源
                elapsed = time.time() - t0
                if done > 8 * 1024 * 1024 and elapsed > 6 and done / elapsed < 600 * 1024:
                    raise RuntimeError("该下载源过慢（%.0fKB/s）" % (done / elapsed / 1024))


def _detach_all():
    try:
        names = os.listdir("/Volumes")
    except OSError:
        return
    for name in names:
        if name.lower().startswith("libreoffice"):
            subprocess.run(
                ["hdiutil", "detach", os.path.join("/Volumes", name), "-quiet"],
                capture_output=True, timeout=120,
            )


def _attach(dmg: str) -> str:
    out = subprocess.run(
        ["hdiutil", "attach", dmg, "-nobrowse", "-quiet"],
        capture_output=True, text=True, timeout=240,
    )
    if out.returncode != 0:
        raise RuntimeError("挂载安装包失败：%s" % (out.stderr or "")[:200])
    for name in os.listdir("/Volumes"):
        if name.lower().startswith("libreoffice"):
            return os.path.join("/Volumes", name)
    raise RuntimeError("挂载成功但找不到安装卷")


def _install_from_volume(vol: str):
    src = os.path.join(vol, "LibreOffice.app")
    if not os.path.isdir(src):
        raise RuntimeError("安装包里没有 LibreOffice.app")
    root = engine_root()
    os.makedirs(root, exist_ok=True)
    dst = os.path.join(root, "LibreOffice.app")
    if os.path.isdir(dst):
        shutil.rmtree(dst, ignore_errors=True)
    subprocess.run(["ditto", src, dst], check=True, timeout=900)
    subprocess.run(
        ["xattr", "-dr", "com.apple.quarantine", dst],
        capture_output=True, timeout=180,
    )
    # 装完立即补 LSUIElement：否则每次 headless 转换都会在 Dock 冒图标
    _patch_no_dock(dst)


def _run():
    dmg = os.path.join(tempfile.gettempdir(), "vdl_libreoffice_%s_%s.dmg" % (VERSION, _arch()))
    try:
        _set(status="downloading", progress=0, done=0, total=0, error="")
        ok = False
        last_err = ""
        for url in download_urls():
            try:
                _download(dmg, url)
                if os.path.isfile(dmg) and os.path.getsize(dmg) > 1024 * 1024:
                    ok = True
                    break
            except Exception as exc:
                last_err = str(exc)[:160]
                print("[lo_engine] 下载失败(%s): %s" % (url, last_err), flush=True)
                try:
                    os.remove(dmg)
                except OSError:
                    pass
        if not ok:
            raise RuntimeError("下载失败：%s" % (last_err or "所有下载源都不可用"))
        _set(status="installing", progress=90)
        _detach_all()
        vol = _attach(dmg)
        try:
            _install_from_volume(vol)
        finally:
            subprocess.run(["hdiutil", "detach", vol, "-quiet"], capture_output=True, timeout=120)
            try:
                os.remove(dmg)
            except OSError:
                pass
        if not installed_app():
            raise RuntimeError("安装完成但找不到可执行文件")
        _set(status="done", progress=100)
    except Exception as exc:
        _set(status="failed", error=str(exc)[:300], progress=0)
        try:
            os.remove(dmg)
        except OSError:
            pass


def install_async() -> bool:
    """启动后台下载安装；已在跑返回 False。"""
    with _lock:
        if _state["status"] in ("downloading", "installing"):
            return False
        _state.update(status="downloading", progress=0, done=0, total=0, error="")
    threading.Thread(target=_run, name="vdl-lo-install", daemon=True).start()
    return True
