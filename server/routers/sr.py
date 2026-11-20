"""server/routers/sr.py — 图片/视频高清修复。2026-09-12 新增，桌面端优先。

**完全本地**，不接任何云端 API、不需要 API Key、不产生按次费用。

图片两档：

- **快速档**：Pillow ``LANCZOS`` 放大 + ``UnsharpMask`` 锐化。秒级完成，任何 Mac 都流畅。
- **AI 档**：Real-ESRGAN ONNX（BSD-3-Clause，合规）分块推理，CoreML 优先，真实细节重建。

视频两档（2026-09-12 追加，走 ffmpeg 传统滤镜而非 AI）：

- **标准档**：``scale=lanczos`` + ``cas`` 锐化 + 提码率。实测 **5× 实时**（45 分钟片约 9 分钟）。
- **增强档**：叠加 ``atadenoise`` 时域降噪。实测 **2.5× 实时**（45 分钟片约 18 分钟）。
  降噪默认关闭：平台压过的低码率片问题在于压缩伪影而非随机噪声，降噪反而抹细节。

⚠️ LGPL 红线：发行版 ffmpeg 为自编译 LGPL，**没有 ``hqdn3d``（GPL 滤镜）**。
可用且已实测的替代品是 ``cas``（AMD 开源，对比度自适应锐化，质量优于 unsharp）与
``atadenoise``（时域降噪）。写滤镜前先确认 ``ffmpeg -filters`` 里有它。

设计依据（均为 2026-09-12 本机实测，非估算）：

- M1 CoreML 上 x2plus 约 2.4 s / 512×512、13.7 s / 720p；**纯 CPU（Intel Mac）再慢约 5.6 倍**。
  ⇒ Intel 上用更小的 tile（256 vs 512）与更低的输入上限（1024 vs 2048）控制内存与耗时。
- ⚠️ CoreML **绝不能**显式指定 ``MLComputeUnits=CPUAndNeuralEngine``：实测比默认配置慢 3 倍
  （ANE 对这类密集卷积支持不全，频繁回退搬运数据）。这里**不传任何 provider_options**。
- **视频 AI 超分不可行**：抽帧 1/4 下 1 分钟片仍需 29 分钟（Intel 1.9 小时），且 x2plus 是
  图片模型、逐帧独立推理无时域一致性会有帧间闪烁；轻量视频模型（animevideov3 / generalv3）
  也无可用 ONNX 权重。故本模块**只做图片**，视频增强走 ffmpeg 传统滤镜（见视频增强模块）。

⚠️ 结果下载必须接三处，缺一处＝点了没反应（WKWebView 不弹 ``<a download>``）：
① 前端 ``wireSaveConvertDownload(el.<列表>)``；② href 拦截正则认得该 URL；
③ 桌面桥 ``save_convert_file_dialog`` 能查到 ``SR_JOBS``。

handler 通过 ``app.<name>`` 访问共享内核（与 compress.py 等路由约定一致）。
"""
from __future__ import annotations

import app
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from .core import _device_of
from stats import record_event

# --------------------------------------------------------------------------- #
# 依赖守护：AI 档按需，缺失时快速档仍可正常工作
try:  # pragma: no cover - 缺失属正常运行态
    import numpy as np
except Exception:  # noqa: BLE001
    np = None
try:  # pragma: no cover
    import onnxruntime as ort
except Exception:  # noqa: BLE001
    ort = None
try:  # pragma: no cover
    from PIL import Image, ImageFilter
except Exception:  # noqa: BLE001
    Image = None
    ImageFilter = None

router = APIRouter()

# --------------------------------------------------------------------------- #
# 常量
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
MODES = {"fast", "ai"}
SCALES = {2, 4}
_DEFAULT_MODE = "fast"
_DEFAULT_SCALE = 2

# ---- 视频增强（2026-09-12 追加）-------------------------------------------- #
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".flv", ".wmv"}
VIDEO_MODES = {"standard", "enhance"}
_DEFAULT_VIDEO_MODE = "standard"
# 输入短边上限（px）。超出此分辨率的片增强收益有限、产物体积却成倍上涨：
# 720p ×2 ⇒ 1440p，按建议码率 8000k 算，45 分钟片约 2.6 GB。
# 本功能的定位是「低清老片修复」，故只接受真正低清的输入。
_MAX_INPUT_SHORT_SIDE = 540
# 放大后码率上限（kbps），防止极端输入把体积拉爆
_MAX_ENHANCE_KBPS = 8000
# 音轨可原样复制的编码（与 compress 一致），其余统一转 AAC 160k
_COPY_AUDIO_CODECS = {"aac", "mp3"}
# 码率提升倍率。放大**不产生新信息**，若按目标分辨率的建议码率「满配」，
# 480p/600k 的片放大到 960p 会被给到 4500k —— 实测产物体积涨 6.7 倍而观感几乎不变。
# 实测 1.8 倍已足够消除放大后的糊感（提码率本身就是观感提升的一部分）。
_BITRATE_LIFT = 1.8
_MIN_KBPS = 200
# 预估耗时系数（秒/秒素材，即 1 秒视频需要多少秒处理）——实测推导，用于前端报时
_ETA_STANDARD = 0.20      # 5× 实时
_ETA_ENHANCE = 0.40       # 2.5× 实时

SR_JOBS: dict = {}
_LOCK = threading.Lock()

# AI 档输入上限（长边，px）。超出会先等比缩小再超分 —— 直接用大图分块推理会让
# 输出缓冲占用数百 MB（4000×3000 ×2 ⇒ float32 约 576 MB），8GB 机器上有 OOM 风险。
_MAX_SIDE_COREML = 2048
_MAX_SIDE_CPU = 1024
_TILE_COREML = 512
_TILE_CPU = 256

# 模型缓存目录（与 matting_ai / dewatermark_ai 一致）
def _model_dir() -> Path:
    raw = os.environ.get("VDL_MODELS_DIR")
    base = Path(raw) if raw else Path.home()
    d = base / ".vdl_models" / "sr"
    d.mkdir(parents=True, exist_ok=True)
    return d


# Real-ESRGAN ONNX（BSD-3-Clause）。国内镜像在前，失败自动回退官方源。
_SR_MODELS: dict[str, dict] = {
    "x2": {
        "filename": "real_esrgan_x2.onnx",
        "min_bytes": 5_000_000,
        "urls": [
            "https://hf-mirror.com/SceneWorks/real-esrgan-onnx/resolve/main/real_esrgan_x2.onnx",
            "https://huggingface.co/SceneWorks/real-esrgan-onnx/resolve/main/real_esrgan_x2.onnx",
        ],
    },
    "x4": {
        "filename": "real_esrgan_x4.onnx",
        "min_bytes": 5_000_000,
        "urls": [
            "https://hf-mirror.com/SceneWorks/real-esrgan-onnx/resolve/main/real_esrgan_x4.onnx",
            "https://huggingface.co/SceneWorks/real-esrgan-onnx/resolve/main/real_esrgan_x4.onnx",
        ],
    },
}

_DL_LOCK = threading.Lock()
_DL: dict = {"active": False, "model": "", "pct": 0.0, "done": 0, "total": 0, "error": ""}
_SESSIONS: dict[str, object] = {}
_PROVIDER: str = ""


def _set_dl(**kw) -> None:
    with _DL_LOCK:
        _DL.update(kw)


def download_progress() -> dict:
    """供前端展示模型下载进度。"""
    with _DL_LOCK:
        return dict(_DL)


def available() -> bool:
    """AI 档是否可用（依赖齐全即可，模型会按需下载）。"""
    return np is not None and ort is not None and Image is not None


def _local_path(key: str) -> Path:
    return _model_dir() / _SR_MODELS[key]["filename"]


def _valid_cached(key: str) -> bool:
    p = _local_path(key)
    return p.exists() and p.stat().st_size > _SR_MODELS[key]["min_bytes"]


def _ensure_model(key: str, job: dict | None = None) -> Path:
    """确保模型在本地；缺失则依次尝试各镜像下载（带进度 + .part 原子落盘）。"""
    if _valid_cached(key):
        return _local_path(key)
    meta = _SR_MODELS[key]
    dest = _local_path(key)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last_err = ""
    for url in meta["urls"]:
        try:
            _set_dl(active=True, model=key, done=0, total=0, pct=0.0, error="")
            if job is not None:
                job["stage"] = "下载 AI 模型中"
            req = urllib.request.Request(url, headers={"User-Agent": "VDL/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp, tmp.open("wb") as fh:
                total = int(resp.headers.get("Content-Length") or 0)
                _set_dl(total=total)
                done = 0
                last_emit = 0.0
                while True:
                    buf = resp.read(1024 * 256)
                    if not buf:
                        break
                    fh.write(buf)
                    done += len(buf)
                    now = time.time()
                    if now - last_emit > 0.4 or (total and done >= total):
                        last_emit = now
                        pct = round(done / total * 100, 1) if total else 0.0
                        _set_dl(done=done, pct=pct)
                        if job is not None:
                            job["progress"] = min(30, int(pct * 0.3))
            if not tmp.exists() or tmp.stat().st_size < meta["min_bytes"]:
                raise RuntimeError("下载结果不完整")
            tmp.replace(dest)  # 原子落盘
            _set_dl(active=False, pct=100.0, done=dest.stat().st_size)
            return dest
        except Exception as e:  # noqa: BLE001
            last_err = str(e)[:300]
            tmp.unlink(missing_ok=True)
            _set_dl(active=False, error=last_err)
            continue
    raise RuntimeError(
        f"高清修复模型下载失败（已尝试 {len(meta['urls'])} 个源）：{last_err or '未知错误'}"
    )


def _get_session(key: str):
    """懒加载 ONNX session。

    ⚠️ 只按默认参数创建 CoreML —— 显式传 ``MLComputeUnits`` 会显著变慢（见模块 docstring）。
    """
    global _PROVIDER
    if key in _SESSIONS:
        return _SESSIONS[key], _PROVIDER
    path = _ensure_model(key)
    providers = ["CPUExecutionProvider"]
    if ort is not None and "CoreMLExecutionProvider" in ort.get_available_providers():
        providers = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    sess = ort.InferenceSession(str(path), providers=providers)
    _PROVIDER = sess.get_providers()[0]
    _SESSIONS[key] = sess
    return sess, _PROVIDER


def _coreml_active() -> bool:
    return _PROVIDER == "CoreMLExecutionProvider"


# --------------------------------------------------------------------------- #
# 两档实现


def _sr_fast(img, scale: int) -> "Image.Image":
    """快速档：Lanczos 放大 + 非锐化掩膜。

    说明：ffmpeg 的 ``cas`` 是视频滤镜；图片这里用 Pillow 的 ``UnsharpMask``
    （等价的传统锐化），无需拉起 ffmpeg 进程，秒级完成。
    """
    w, h = img.size
    out = img.resize((w * scale, h * scale), Image.LANCZOS)
    out = out.filter(ImageFilter.UnsharpMask(radius=2, percent=140, threshold=3))
    return out


def _sr_ai(img, key: str, job: dict) -> "Image.Image":
    """AI 档：Real-ESRGAN 分块推理（overlap 加权混合，避免块间接缝）。"""
    sess, provider = _get_session(key)
    input_name = sess.get_inputs()[0].name
    scale = 2 if key == "x2" else 4
    tile = _TILE_COREML if provider == "CoreMLExecutionProvider" else _TILE_CPU
    max_side = _MAX_SIDE_COREML if provider == "CoreMLExecutionProvider" else _MAX_SIDE_CPU

    rgb = img.convert("RGB")
    w0, h0 = rgb.size
    # 输入上限保护：超出先等比缩小（并在 note 中如实告知）
    if max(w0, h0) > max_side:
        ratio = max_side / float(max(w0, h0))
        nw, nh = max(1, int(w0 * ratio)), max(1, int(h0 * ratio))
        rgb = rgb.resize((nw, nh), Image.LANCZOS)
        job["note"] = (f"输入 {w0}×{h0} 超过 {max_side}px 上限，"
                       f"已先缩放到 {nw}×{nh} 再超分（输出 {nw * scale}×{nh * scale}）")

    src = np.asarray(rgb, dtype=np.float32) / 255.0
    h, w = src.shape[:2]
    H, W = h * scale, w * scale
    out = np.zeros((H, W, 3), dtype=np.float32)
    wmap = np.zeros((H, W, 1), dtype=np.float32)

    step = max(1, tile - 32)  # 32px 重叠，用加权平均消除块间接缝
    ys = list(range(0, h, step))
    xs = list(range(0, w, step))
    total = len(ys) * len(xs)
    done = 0
    t_start = time.time()

    for y in ys:
        for x in xs:
            y1, x1 = y, x
            y2, x2 = min(y + tile, h), min(x + tile, w)
            y1, x1 = max(0, y2 - tile), max(0, x2 - tile)
            chunk = src[y1:y2, x1:x2]
            ch, cw = chunk.shape[:2]
            if ch < tile or cw < tile:
                pad = np.zeros((tile, tile, 3), dtype=np.float32)
                pad[:ch, :cw] = chunk
                chunk = pad
            inp = np.transpose(chunk, (2, 0, 1))[None]
            pred = sess.run(None, {input_name: inp})[0][0]
            pred = np.transpose(pred, (1, 2, 0))
            ph, pw = ch * scale, cw * scale
            pred = pred[:ph, :pw]
            out[y1 * scale:y2 * scale, x1 * scale:x2 * scale] += pred
            wmap[y1 * scale:y2 * scale, x1 * scale:x2 * scale] += 1.0
            done += 1
            job["progress"] = 30 + int(done / total * 65)
            job["elapsed"] = round(time.time() - t_start, 1)
            if done >= 1 and total > 1:
                job["eta"] = round(job["elapsed"] / done * (total - done), 1)
            job["stage"] = f"AI 重建中（{done}/{total} 块）"

    out /= np.maximum(wmap, 1e-6)
    out = np.clip(out, 0.0, 1.0)
    return Image.fromarray((out * 255.0 + 0.5).astype(np.uint8), mode="RGB")


# --------------------------------------------------------------------------- #
# job 表与执行


def _discard_job(job_id: str) -> None:
    """清掉一次「重新修复」被顶替掉的旧任务：删产物文件 + 移除记录（2026-09-24）。

    与 ``routers/compress.py::_discard_job`` 同契约（前端点「重新修复」时把上一轮
    job_id 作为 ``replaces`` 传上来），两条硬约束同样适用：
    1. 只清理**已结束**（completed / failed）的任务 —— running 的被 pop 掉会让工作
       线程写回一个没人查询的 dict，任务变幽灵，用户永远等不到结果；
    2. 删除对象取自**本进程自己的记录**（``job["out_path"]``），不是客户端传来的
       路径，伪造 job_id 也无法越权删任意文件。
    """
    if not job_id or not app.re.fullmatch(r"[0-9a-f]{8,32}", job_id or ""):
        return
    with _LOCK:
        old = SR_JOBS.get(job_id)
        if not old or old.get("status") not in ("completed", "failed"):
            return
        SR_JOBS.pop(job_id, None)
        out_path = old.get("out_path") or ""
    if out_path:
        try:
            app.Path(out_path).unlink(missing_ok=True)
        except OSError:
            pass


def _register_job(device_id: str, src_name: str) -> str:
    job_id = app.uuid.uuid4().hex[:12]
    with _LOCK:
        SR_JOBS[job_id] = {
            "status": "running", "stage": "排队中", "progress": 0, "error": "",
            "out_path": "", "filename": "", "src_name": src_name,
            "device_id": device_id, "mode": "", "scale": 0,
            "size_before": 0, "size_after": 0,
            "w_before": 0, "h_before": 0, "w_after": 0, "h_after": 0,
            "note": "", "elapsed": 0.0, "eta": 0.0,
            "kind": "image",                    # image / video
            "src_kbps": 0, "target_kbps": 0,    # 视频增强的码率依据（前端可展示）
        }
    return job_id


def _run_sr(job_id: str, src: str, mode: str, scale: int, src_is_temp: bool) -> None:
    job = SR_JOBS.get(job_id)
    if not job:
        return
    job["mode"], job["scale"] = mode, scale
    out_path = None
    try:
        if Image is None:
            raise RuntimeError("图片处理库不可用（Pillow 缺失）")
        src_path = app.Path(src)
        job["size_before"] = src_path.stat().st_size

        with Image.open(src_path) as im:
            im.load()
            job["w_before"], job["h_before"] = im.size
            img = im.convert("RGB") if im.mode not in ("RGB", "L") else im.copy()

        if mode == "ai":
            if np is None or ort is None:
                raise RuntimeError("AI 档依赖不可用（onnxruntime / numpy 缺失），请改用快速档")
            job["stage"] = "准备 AI 模型"
            out_img = _sr_ai(img, "x2" if scale == 2 else "x4", job)
        else:
            job["stage"] = "快速放大中"
            job["progress"] = 30
            out_img = _sr_fast(img, scale)
            job["progress"] = 80

        job["w_after"], job["h_after"] = out_img.size
        ext = src_path.suffix.lower() or ".png"
        out_path = app.CONVERT_DIR / f"sr_{job_id}{ext}"
        job["stage"] = "写入文件"
        save_kwargs = {}
        if ext in (".jpg", ".jpeg"):
            save_kwargs = {"quality": 95, "subsampling": 0}
        elif ext == ".webp":
            save_kwargs = {"quality": 92}
        out_img.save(out_path, **save_kwargs)

        job["size_after"] = out_path.stat().st_size
        job["out_path"] = str(out_path)
        job["filename"] = f"{src_path.stem}_{scale}x{out_path.suffix}"
        job["status"] = "completed"
        job["stage"] = "完成"
        job["progress"] = 100
        job["elapsed"] = round(time.time() - job.get("_t0", time.time()), 1)
        try:
            record_event("sr")
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        job["status"] = "failed"
        job["error"] = str(e)[:400]
        job["stage"] = "失败"
    finally:
        if src_is_temp:
            try:
                app.Path(src).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass


def _submit_sr(src: str, mode: str, scale: int, device_id: str,
               src_name: str = "", src_is_temp: bool = False) -> str:
    job_id = _register_job(device_id, src_name or app.Path(src).name)
    with _LOCK:
        SR_JOBS[job_id]["_t0"] = time.time()
    app.executor.submit(_run_sr, job_id, src, mode, scale, src_is_temp)
    return job_id


def _submit_sr_video(src: str, mode: str, scale: int, codec: str, device_id: str,
                     src_name: str = "", src_is_temp: bool = False) -> str:
    job_id = _register_job(device_id, src_name or Path(src).name)
    with _LOCK:
        SR_JOBS[job_id]["_t0"] = time.time()
        SR_JOBS[job_id]["kind"] = "video"
    app.executor.submit(_run_sr_video, job_id, src, mode, scale, codec, src_is_temp)
    return job_id


def _validate_video_mode(mode: str) -> str:
    m = (mode or "").lower()
    return m if m in VIDEO_MODES else _DEFAULT_VIDEO_MODE


# --------------------------------------------------------------------------- #
# 视频增强（2026-09-12 追加）
#
# 与图片 AI 档不同，视频走 ffmpeg 传统滤镜：实测 AI 超分在视频上不可行
# （抽帧 1/4 下 1 分钟片仍需 29 分钟，且逐帧独立推理有帧间闪烁）。
# 传统滤镜链路实测 5× 实时（标准档）/ 2.5× 实时（增强档），任何 Mac 都能跑完。


def _ffprobe_bin() -> str:
    """解析 ffprobe 路径（与 compress._ffprobe_bin 同逻辑，独立实现免耦合）。"""
    ffmpeg = app.FFMPEG_BIN or ""
    cand = os.path.join(os.path.dirname(ffmpeg), "ffprobe") if ffmpeg else ""
    for p in (cand, shutil.which("ffprobe") or "",
              "/opt/homebrew/bin/ffprobe" if app.sys.platform == "darwin" else ""):
        if p and os.path.isfile(p):
            return p
    return "ffprobe"


def _probe_video_meta(path: str) -> dict:
    """一次 ffprobe 取视频元信息（duration / bit_rate / width / height / acodec）。

    任何字段失败均降级为 0 / 空串，绝不抛错 —— 探测失败不应中断增强主流程。
    """
    meta = {"duration": 0.0, "bit_rate": 0, "width": 0, "height": 0, "acodec": ""}
    try:
        r = subprocess.run(
            [_ffprobe_bin(), "-v", "error", "-show_entries",
             "format=duration,bit_rate:stream=codec_type,codec_name,width,height,bit_rate",
             "-of", "json", path],
            capture_output=True, text=True, timeout=25)
        data = json.loads(r.stdout or "{}")
    except Exception:  # noqa: BLE001
        return meta

    def _num(v) -> float:
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    fmt = data.get("format") or {}
    meta["duration"] = _num(fmt.get("duration"))
    fmt_br = _num(fmt.get("bit_rate"))
    video_br = audio_br = 0.0
    for st in data.get("streams") or []:
        kind = st.get("codec_type")
        if kind == "video" and not meta["width"]:
            meta["width"] = int(_num(st.get("width")))
            meta["height"] = int(_num(st.get("height")))
            video_br = _num(st.get("bit_rate"))
        elif kind == "audio" and not meta["acodec"]:
            meta["acodec"] = (st.get("codec_name") or "").lower()
            audio_br = _num(st.get("bit_rate"))
    br = video_br or max(0.0, fmt_br - audio_br) or fmt_br
    if br <= 0 and meta["duration"] > 0:
        try:
            br = Path(path).stat().st_size * 8 / meta["duration"]
        except Exception:  # noqa: BLE001
            br = 0.0
    meta["bit_rate"] = int(br)
    return meta


def _enhance_filter(mode: str, out_w: int, out_h: int) -> str:
    """构造 LGPL 可用的增强滤镜链。

    ⚠️ 只能用发行版 ffmpeg 里真实存在的滤镜（已实测）：``cas``（AMD 开源的对比度
    自适应锐化，质量优于 unsharp）与 ``atadenoise``（时域降噪）。``hqdn3d`` 是 GPL
    滤镜，LGPL 构建里被裁掉了，写上去直接失败。改滤镜前先 ``ffmpeg -filters`` 确认。

    尺寸用算好的偶数硬编码 —— yuv420p 下奇数宽/高会让编码直接报错。
    """
    chain = [f"scale={out_w}:{out_h}:flags=lanczos"]
    if mode == "enhance":
        chain.append("atadenoise=s=9")
    chain.append("cas=strength=0.4")
    return ",".join(chain)


def _estimate_eta(duration: float, mode: str) -> float:
    """按实测系数预估耗时（秒）。用于提交时立刻给用户预期，避免「点了才知道要多久」。

    系数来自 2026-09-12 实测：标准档 5× 实时、增强档 2.5× 实时（480p / VideoToolbox）。
    """
    if duration <= 0:
        return 0.0
    k = _ETA_ENHANCE if mode == "enhance" else _ETA_STANDARD
    return round(duration * k, 1)


def _enhance_video(job: dict, src: str, out_path, mode: str, scale: int,
                   codec: str = "h264") -> None:
    """视频增强主流程：放大 + 锐化（+ 降噪）+ 提码率重编码；进度经 -progress 解析。

    码率逻辑与**压缩相反**：压缩要钳制到源码率以下保证变小；增强要**提升**到目标
    分辨率的建议码率 —— 放大后像素数翻 4 倍，沿用源码率只会把放大出来的细节压糊，
    「提码率」本身就是观感提升的一部分。
    """
    meta = _probe_video_meta(src)
    duration = meta["duration"]
    w, h = meta["width"], meta["height"]
    if not w or not h:
        raise RuntimeError("无法读取视频分辨率（文件可能已损坏或不是视频）")
    out_w = max(2, (w * scale) // 2 * 2)
    out_h = max(2, (h * scale) // 2 * 2)

    from codec_utils import res_cap_kbps, rate_controlled_args
    src_kbps = int(meta["bit_rate"] / 1000) if meta["bit_rate"] else 0
    # 该分辨率的合理码率上限（放大后不该超过真·该分辨率片子的水平）
    cap = res_cap_kbps(min(out_w, out_h))
    if (codec or "").lower() == "hevc":
        cap = int(cap * 0.75)
    cap = min(cap, _MAX_ENHANCE_KBPS)
    if src_kbps > 0:
        # 在源码率基础上提升，再用分辨率上限兜住 —— 既不浪费体积，也不变相压缩
        target = max(int(src_kbps * _BITRATE_LIFT), int(src_kbps * 1.2))
        target = min(target, cap)
    else:
        target = max(int(cap * 0.5), _MIN_KBPS)   # 探测不到码率时的兜底
    job["src_kbps"] = src_kbps
    job["target_kbps"] = target
    job["eta"] = _estimate_eta(duration, mode)

    cmd = [app.FFMPEG_BIN, "-y", "-nostdin", "-i", src]
    cmd += ["-vf", _enhance_filter(mode, out_w, out_h)]
    cmd += rate_controlled_args(app.FFMPEG_BIN, codec=codec, target_kbps=target,
                                pix_fmt="yuv420p")
    if (codec or "").lower() == "hevc":
        cmd += ["-tag:v", "hvc1"]
    cmd += ["-movflags", "+faststart"]
    cmd += (["-c:a", "copy"] if meta["acodec"] in _COPY_AUDIO_CODECS
            else ["-c:a", "aac", "-b:a", "160k"])
    cmd += ["-progress", "pipe:1", "-nostats", str(out_path)]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    # stderr 用独立线程抽干：既避免管道写满把 ffmpeg 卡死，又能在失败时给出真实原因
    err_lines: list = []

    def _drain(pipe) -> None:
        try:
            for ln in pipe:
                err_lines.append(ln.rstrip())
                if len(err_lines) > 60:
                    del err_lines[:30]
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_drain, args=(proc.stderr,), daemon=True).start()
    job["stage"] = "增强中"
    started = time.time()
    try:
        for line in proc.stdout:
            if duration > 0 and line.startswith("out_time_us="):
                try:
                    cur_us = float(line.split("=", 1)[1].strip() or 0)
                except ValueError:
                    continue
                if cur_us > 0:
                    pct = min(99, int(cur_us / 1e6 / duration * 100))
                    job["progress"] = pct
                    el = time.time() - started
                    job["elapsed"] = round(el, 1)
                    # 进度 >=3% 后按当前速率外推（前期样本抖动大，不外推）
                    if pct >= 3:
                        job["eta"] = round(max(0.0, el * (100 - pct) / pct), 1)
    finally:
        rc = proc.wait()
    if rc != 0:
        tail = " | ".join(err_lines[-6:])[-400:]
        raise RuntimeError(f"ffmpeg 增强失败（rc={rc}），编码={codec}，目标码率={target}k"
                           + (f"；{tail}" if tail else ""))
    job["w_before"], job["h_before"] = w, h
    job["w_after"], job["h_after"] = out_w, out_h
    job["progress"] = 100
    job["elapsed"] = round(time.time() - started, 1)
    job["eta"] = 0.0


def _defer_video(delay: float, job_id: str, src: str, mode: str, scale: int,
                 codec: str, src_is_temp: bool) -> None:
    """延后重投视频任务（定时器线程不占线程池 worker —— 与 compress 同范式）。

    等待转码名额期间绝不持有 worker：否则提交 2 个视频就能把与下载/抠图共用的
    8 worker 池占满，其它功能全部饿死。
    """
    def _retry() -> None:
        try:
            app.executor.submit(_run_sr_video, job_id, src, mode, scale, codec, src_is_temp)
        except RuntimeError:
            pass                      # 线程池已关闭（应用退出中）→ 放弃重投

    t = threading.Timer(max(0.05, delay), _retry)
    t.daemon = True
    t.start()


def _run_sr_video(job_id: str, src: str, mode: str, scale: int, codec: str,
                  src_is_temp: bool) -> None:
    """后台线程：执行视频增强并回写状态。"""
    job = SR_JOBS.get(job_id)
    if not job:
        return
    job["mode"], job["scale"] = mode, scale
    # 闸门判断放在 try 之外（compress 同款教训）：排队分支绝不能走到 finally
    # 的临时文件清理，否则排队中就把用户上传的源文件删了。
    try:
        from .compress import _TRANSCODE_SEM as _sem
    except Exception:  # noqa: BLE001
        _sem = None
    if _sem is not None and not _sem.acquire(blocking=False):
        job["stage"] = "排队中"
        _defer_video(0.5, job_id, src, mode, scale, codec, src_is_temp)
        return
    acquired = _sem is not None
    try:
        src_path = Path(src)
        job["size_before"] = src_path.stat().st_size
        out_path = app.CONVERT_DIR / f"sr_{job_id}.mp4"
        _enhance_video(job, src, out_path, mode, scale, codec=codec)
        job["size_after"] = out_path.stat().st_size
        job["out_path"] = str(out_path)
        job["filename"] = f"{src_path.stem}_{scale}x_enhanced.mp4"
        job["note"] = (f"码率 {job.get('src_kbps', 0)}k → {job.get('target_kbps', 0)}k"
                       if job.get("src_kbps") else "")
        job["status"] = "completed"
        job["stage"] = "完成"
        job["progress"] = 100
        record_event("sr_video", {"mode": mode, "scale": scale, "codec": codec})
    except Exception as e:  # noqa: BLE001
        job["status"] = "failed"
        job["error"] = str(e)[:400]
        job["stage"] = "失败"
    finally:
        if acquired and _sem is not None:
            _sem.release()
        if src_is_temp:
            try:
                Path(src).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# 校验与路由


def _validate_mode(mode: str) -> str:
    m = (mode or "").lower()
    if m not in MODES:
        return _DEFAULT_MODE
    return m


def _validate_scale(scale: int) -> int:
    try:
        s = int(scale)
    except Exception:  # noqa: BLE001
        return _DEFAULT_SCALE
    return s if s in SCALES else _DEFAULT_SCALE


class LocalSrRequest(BaseModel):
    """桌面端本地文件高清修复请求。"""
    local_path: str
    mode: str = _DEFAULT_MODE     # fast / ai
    scale: int = _DEFAULT_SCALE   # 2 / 4
    replaces: str = ""            # 「重新修复」时带上上一轮 job_id，由后端回收旧产物


@router.post("/api/sr/local")
def sr_local(payload: LocalSrRequest, request: app.Request) -> dict:
    """桌面版专用：本机绝对路径直接处理（免上传）。"""
    app._check_rate_limit(request)
    _gate = app.app_compute_gate(request, "sr", "高清修复")           # 本地算力账号级配额（免费 5 次/日）
    subscribed, free_used, free_daily = app._check_convert_quota(request)
    from .convert import _resolve_safe_local_path
    resolved = _resolve_safe_local_path(payload.local_path)
    if resolved.suffix.lower() not in IMAGE_EXTS:
        raise app.HTTPException(status_code=409, detail="仅支持图片（PNG/JPG/WebP/BMP）")
    mode = _validate_mode(payload.mode)
    scale = _validate_scale(payload.scale)
    _discard_job(payload.replaces)     # 重新修复：先回收上一轮产物，再登记新任务
    job_id = _submit_sr(str(resolved), mode, scale, _device_of(request),
                        src_name=resolved.name, src_is_temp=False)
    app.app_compute_count(request, _gate)           # 任务成功创建才计费
    record_event("sr_submit", {"mode": mode, "scale": scale, "src": "local"})
    return {"job_id": job_id, "status": "running", "mode": mode, "scale": scale,
            "quota": {"subscribed": subscribed, "free_used": free_used,
                      "free_daily": free_daily}}


class LocalSrVideoRequest(BaseModel):
    """桌面端本地视频高清修复请求。"""
    local_path: str
    mode: str = _DEFAULT_VIDEO_MODE  # standard / enhance
    scale: int = 2                   # 2 / 4
    codec: str = "h264"              # h264 / hevc
    replaces: str = ""               # 「重新修复」时带上上一轮 job_id，由后端回收旧产物


@router.post("/api/sr/video/local")
def sr_video_local(payload: LocalSrVideoRequest, request: app.Request) -> dict:
    """桌面版专用：本机视频绝对路径直接增强（免上传）。

    **提交时先做分辨率预检**：本功能定位是「低清老片修复」，输入短边超过
    ``_MAX_INPUT_SHORT_SIDE``（540px，即 720p 以上）直接拒绝 —— 这类片放大后
    体积成倍上涨（720p×2 ⇒ 1440p，45 分钟约 2.6 GB）而观感提升有限。
    与其让用户等 20 分钟拿到一个 2.6GB 的文件，不如一开始就讲清楚。
    """
    app._check_rate_limit(request)
    # 🔴 2026-10-06 第二轮拆池：视频超分与图片「高清修复」拆成两份额度
    # （一个键 = 一个功能；否则超分会把图片修复的次数也用掉）。
    _gate = app.app_compute_gate(request, "sr_video", "视频超分")      # 本地算力账号级配额（免费 5 次/日）
    subscribed, free_used, free_daily = app._check_convert_quota(request)
    from .convert import _resolve_safe_local_path
    resolved = _resolve_safe_local_path(payload.local_path)
    if resolved.suffix.lower() not in VIDEO_EXTS:
        raise app.HTTPException(status_code=409, detail="仅支持视频文件（MP4/MOV/MKV/WebM 等）")
    mode = _validate_video_mode(payload.mode)
    scale = _validate_scale(payload.scale)
    codec = "hevc" if (payload.codec or "").lower() == "hevc" else "h264"

    meta = _probe_video_meta(str(resolved))
    short_side = min(meta["width"], meta["height"]) if (meta["width"] and meta["height"]) else 0
    if short_side <= 0:
        raise app.HTTPException(status_code=409, detail="无法读取视频分辨率，请确认文件未损坏")
    if short_side > _MAX_INPUT_SHORT_SIDE:
        raise app.HTTPException(
            status_code=409,
            detail=(f"该视频已是 {meta['width']}x{meta['height']}，分辨率较高。"
                    f"本功能面向低清片修复（短边 ≤{_MAX_INPUT_SHORT_SIDE}px），"
                    f"高分辨率片放大后体积成倍上涨而观感提升有限"))
    if scale == 4 and short_side > 360:
        raise app.HTTPException(
            status_code=409,
            detail=(f"×4 放大仅支持短边 ≤360px 的视频（当前 {short_side}px）。"
                    f"请改用 ×2，或先用格式转换把视频缩小"))

    _discard_job(payload.replaces)     # 重新修复：先回收上一轮产物，再登记新任务
    job_id = _submit_sr_video(str(resolved), mode, scale, codec, _device_of(request),
                              src_name=resolved.name, src_is_temp=False)
    record_event("sr_submit", {"mode": mode, "scale": scale, "src": "local", "kind": "video"})
    return {"job_id": job_id, "status": "running", "mode": mode, "scale": scale,
            "eta": _estimate_eta(meta["duration"], mode),
            "src_w": meta["width"], "src_h": meta["height"],
            "quota": {"subscribed": subscribed, "free_used": free_used,
                      "free_daily": free_daily}}


@router.get("/api/sr/video/limits")
def sr_video_limits(request: app.Request) -> dict:
    """视频增强的能力边界（前端据此做禁用/提示，避免用户提交了才被拒）。"""
    return {"max_input_short_side": _MAX_INPUT_SHORT_SIDE,
            "ex4_max_short_side": 360,
            "modes": sorted(VIDEO_MODES), "scales": sorted(SCALES),
            "eta_per_sec": {"standard": _ETA_STANDARD, "enhance": _ETA_ENHANCE},
            "exts": sorted(VIDEO_EXTS)}


@router.get("/api/sr/model/status")
def sr_model_status(request: app.Request) -> dict:
    """AI 档可用性 + 模型缓存状态 + 当前推理后端（前端据此提示预估耗时）。"""
    cached = {k: _valid_cached(k) for k in _SR_MODELS}
    return {"available": available(), "cached": cached,
            "provider": _PROVIDER or ("coreml" if (ort and "CoreMLExecutionProvider"
                                                   in ort.get_available_providers()) else "cpu"),
            "download": download_progress(),
            "max_side": _MAX_SIDE_COREML if _coreml_active() or not _PROVIDER else _MAX_SIDE_CPU}


@router.get("/api/sr/{job_id}")
def sr_status(job_id: str, request: app.Request) -> dict:
    """查询进度。

    ⚠️ 这里**故意不做限流**（与 compress / convert / matting 对齐，2026-09-11 教训）：
    前端每 1.5 s 轮询一次，AI 档一张图可能跑几十秒到几分钟，若把轮询计入 30 次/小时的
    配额，任务跑到一半就会被 429 掐断，进度条永久冻死。**只有提交类端点才限流。**
    """
    with _LOCK:
        job = SR_JOBS.get(job_id)
        if not job:
            raise app.HTTPException(status_code=404, detail="高清修复任务不存在或已过期")
        return dict(job)


@router.get("/api/sr/{job_id}/file")
def sr_file(job_id: str, request: app.Request) -> app.FileResponse:
    """下载结果（同 status：只读取件，不计入限流配额）。"""
    with _LOCK:
        job = SR_JOBS.get(job_id)
    if not job or job.get("status") != "completed" or not job.get("out_path"):
        raise app.HTTPException(status_code=404, detail="高清修复结果不存在")
    p = app.Path(job["out_path"])
    if not p.is_file():
        raise app.HTTPException(status_code=404, detail="结果文件已被清理")
    return app.FileResponse(p, filename=job.get("filename") or p.name,
                            media_type="application/octet-stream")
