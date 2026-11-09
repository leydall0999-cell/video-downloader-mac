"""server/routers/commentary.py — 由 server/app.py 按域抽取（Phase 1）。
handler 通过 `app.<name>` 访问共享内核（globals/helper/导入）。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录对应文件。
"""
import app
import os
import pathlib
import sys
import subprocess
from fastapi import APIRouter, File, Form, Request, UploadFile

router = APIRouter()

# ---------------------------------------------------------------------------
# 解说词「字数 vs 片段时长」预检
#
# 背景：渲染层（commentary-pipeline/scripts/edit_ffmpeg.py）的原速模式下
#   out_dur = win = end - start，ffmpeg 以 `-t out_dur` 收尾，
#   **旁白超出窗口的部分会被直接截断**（见该函数 1911-1918 行注释：
#   "窗口在调用方已保证 ≥ 旁白时长"）。而这个"保证"来自生成端 LLM 自觉控字数
#   ——提示词里写的是"约 40~70 字…不用怕写长"，并未按时长硬性约束。
#   人工在审核面板改长之后，这个前提即失效，且全链路无兜底。
#
# 因此在这里给出估算预算，供审核面板实时提示、渲染前自查，避免产出"话说一半被切"的废片。
# ---------------------------------------------------------------------------

# 中文 TTS 常规语速估算（字/秒）。生成端无同款常量，故在此定义并作为唯一数据源，
# 前端不再自行猜测语速，改用本模块返回的 budgets。
NARRATION_CHARS_PER_SEC = 5.0


def _narration_budget(start, end, cps=NARRATION_CHARS_PER_SEC) -> int:
    """按片段可用时长估算「装得下多少字」。

    时长缺失/非法/非正 → 返回 0，表示「不校验」（而不是给出误导性的预算）。
    """
    try:
        dur = float(end) - float(start)
    except (TypeError, ValueError):
        return 0
    if dur <= 0:
        return 0
    return max(1, int(dur * cps))


def _narration_chars(text) -> int:
    """解说词计字：去空白后长度。

    标点同样占用朗读时长，故计入；仅排除纯空白（含换行缩进）。
    """
    return len("".join(str(text or "").split()))


def _narration_over_limit(segments: list) -> list:
    """挑出「字数超过该片段时长预算」的段，供保存时回传给前端告警。

    返回 [{"index": 1-based, "chars": 实际字数, "budget": 预算字数}, ...]。
    """
    out = []
    for i, seg in enumerate(segments or []):
        if not isinstance(seg, dict):
            continue
        budget = _narration_budget(seg.get("start", 0), seg.get("end", 0))
        if not budget:
            continue
        n = _narration_chars(seg.get("narration", ""))
        if n > budget:
            out.append({"index": i + 1, "chars": n, "budget": budget})
    return out


def _plan_original_speed(segments: list, prefer_original_speed: bool = True):
    """渲染前预检：恒定原速。

    🔴 2026-10-26 用户拍板：**绝不放慢画面，解说保持完整**。
    此前逻辑：检出「旁白字数超片段时长预算」→ 返回 False → 渲染层 setpts
    慢放把窗口撑开 → 成片突然慢动作。已废除。

    现结论：渲染层原速模式本就自洽（edit_ffmpeg.py build 主链路）——
    窗口完全跟随旁白真实时长（e = s + vdur），画面**原速多播几秒**；
    触及片尾则起点前移，实在放不下整段跳过（绝不截断旁白）；相邻段
    宁可源时间轻微重叠也绝不慢放。⇒ 超标段的正确处置就是"画面多放几秒"，
    文字一字不动、画面原速、旁白完整，三方全满足。

    本函数保留只为回传超标明细（供任务留档/前端提示），**永不自动降速**；
    仅当调用方显式传 prefer_original_speed=False（目前无任何 UI 路径）才透传非原速。
    """
    over = _narration_over_limit(segments)
    return bool(prefer_original_speed), over


@router.get("/api/commentary/config")
def commentary_config_get() -> dict:
    """返回当前解说(配音/音量)设置，供前端面板回填。"""
    from commentary_config import get_commentary_config
    return get_commentary_config()


@router.get("/api/commentary/voice-sample")
def get_commentary_voice_sample() -> dict:
    """读取「我的音色」本地克隆参考样本（音频路径 + 文字稿 + 是否可用）。"""
    from commentary_config import get_voice_sample
    return get_voice_sample()


@router.post("/api/commentary/voice-sample")
def save_commentary_voice_sample(
    audio_path: str = app.Form(""),
    ref_text: str = app.Form(""),
    name: str = app.Form(""),
) -> dict:
    """保存「我的音色」样本：音频绝对路径 + 该音频里念的文字稿 + 配音名字。

    校验失败（文件不存在 / 格式不支持 / 文字稿为空）返回 400 + 中文原因，
    前端直接展示，避免把用不了的样本留到渲染时才炸。
    name（2026-09-20）：录制弹窗里可编辑的名字；留空则按音频文件名兜底。
    """
    from commentary_config import save_voice_sample
    try:
        return save_voice_sample(audio_path, ref_text, name)
    except ValueError as exc:
        raise app.HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/commentary/voice-library")
def get_commentary_voice_library() -> dict:
    """「我的音色」音色库（2026-09-20）：用户存过的全部音色 + 哪一条当前生效。

    供「🎧 全部音色」弹窗列出并切换。库只是收藏夹，读坏当空库，绝不影响渲染。
    """
    from commentary_config import list_voice_library
    return list_voice_library()


@router.post("/api/commentary/voice-library")
def add_commentary_voice_library_item(
    audio_path: str = app.Form(""),
    ref_text: str = app.Form(""),
    name: str = app.Form(""),
    activate: str = app.Form(""),
) -> dict:
    """把一段样本登记进音色库；activate=1 时同时设为当前生效样本。

    「设为当前」直接复用既有 save_voice_sample（同一套文件/格式/文字稿校验），
    不新增第二条落配置的路径。
    """
    from commentary_config import list_voice_library, save_voice_sample, upsert_voice_library
    try:
        item = upsert_voice_library(name, audio_path, ref_text)
        if str(activate or "").strip().lower() in ("1", "true", "yes", "on"):
            save_voice_sample(audio_path, ref_text, item.get("name") or name)
    except ValueError as exc:
        raise app.HTTPException(status_code=400, detail=str(exc)) from exc
    lib = list_voice_library()
    lib["item"] = item
    return lib


@router.post("/api/commentary/voice-library/delete")
def delete_commentary_voice_library_item(id: str = app.Form("")) -> dict:
    """从音色库删掉一条（只连带删 App 自己录的那个 wav，外部样本文件不动）。"""
    from commentary_config import list_voice_library, remove_voice_library
    try:
        removed = remove_voice_library(id)
    except ValueError as exc:
        raise app.HTTPException(status_code=400, detail=str(exc)) from exc
    lib = list_voice_library()
    lib["removed"] = removed
    return lib


@router.post("/api/commentary/voice-sample/record")
async def record_commentary_voice_sample(
    audio: UploadFile = File(...),
    duration: str = Form(""),
) -> dict:
    """接收「我的音色」页面内直接录制的音频并落盘（2026-09-19）。

    前端用 WebAudio 采 PCM、自己编 16bit WAV 传上来（不依赖 MediaRecorder 的编码差异），
    这里只做校验 + 落盘，返回 {"audio_path", "duration", "bytes"}；
    路径拿到后，前端仍走既有 `POST /api/commentary/voice-sample` 落配置（同一套校验）。
    """
    from commentary_config import save_recorded_voice_sample

    data = await audio.read()
    dur: float | None = None
    raw_dur = str(duration or "").strip()
    if raw_dur:
        try:
            dur = float(raw_dur)
        except ValueError:
            dur = None
    try:
        return save_recorded_voice_sample(data, audio.filename or "voice_rec.wav", dur)
    except ValueError as exc:
        raise app.HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/commentary/tts-status")
def commentary_tts_status() -> dict:
    """返回 TTS 引擎相关的本机配置/就绪状态，供前端动态推荐。

    - platform: darwin / win32 / linux
    - apple_silicon: 是否为 Apple Silicon Mac（MLX 仅在此推荐）
    - indextts_mlx_ready: 127.0.0.1:7866 是否可连接
    - qwen3tts_ready: 127.0.0.1:7871 是否可连接（Qwen3-TTS 本地语音克隆）
    - voice_sample_ready: 是否已配「我的音色」样本（克隆引擎真正生效的前提）
    - minimax_configured / siliconflow_configured: 是否已配置 API Key
    """
    import json
    import os
    import platform as _platform
    import socket
    import sys

    _plat = sys.platform
    machine = _platform.machine().lower()
    apple_silicon = (_plat == "darwin") and (machine.startswith("arm") or "aarch64" in machine)

    indextts_mlx_ready = False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.6)
            s.connect(("127.0.0.1", 7866))
            indextts_mlx_ready = True
    except Exception:
        indextts_mlx_ready = False

    qwen3tts_ready = False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.6)
            s.connect(("127.0.0.1", 7871))
            qwen3tts_ready = True
    except Exception:
        qwen3tts_ready = False

    tts_cfg: dict = {}
    try:
        tts_path = os.path.join(os.path.expanduser("~"), ".video-downloader", "tts_config.json")
        if os.path.exists(tts_path):
            with open(tts_path, encoding="utf-8") as f:
                tts_cfg = json.load(f)
    except Exception:
        tts_cfg = {}

    minimax_key = (os.environ.get("MINIMAX_API_KEY") or tts_cfg.get("minimax_api_key") or "").strip()
    siliconflow_key = (os.environ.get("SILICONFLOW_API_KEY") or tts_cfg.get("api_key") or "").strip()

    return {
        "platform": _plat,
        "machine": machine,
        "apple_silicon": apple_silicon,
        "indextts_mlx_ready": indextts_mlx_ready,
        "qwen3tts_ready": qwen3tts_ready,
        "voice_sample_ready": _voice_sample_ready(),
        "minimax_configured": bool(minimax_key),
        "siliconflow_configured": bool(siliconflow_key),
    }


def _voice_sample_ready() -> bool:
    """「我的音色」样本是否可用（读取异常一律视为未就绪，不影响状态接口本身）。"""
    try:
        from commentary_config import get_voice_sample
        return bool(get_voice_sample().get("ready"))
    except Exception:  # noqa: BLE001
        return False


# ── 本地语音克隆运行环境（按需下载安装）─────────────────────────────
# 为什么单独开三个接口：这能力要 500MB venv + 1.9GB 权重，**不进发行包**（见 server/clone_env.py），
# 所以必须能在界面上「检测 → 一键装 → 看进度 → 可取消」，而不是丢一句命令行让用户自己搞。
@router.get("/api/commentary/clone-env")
def commentary_clone_env() -> dict:
    """本机语音克隆环境状态 + 安装进度。

    ready=True 才算真能用（venv 有 mlx/mlx_audio 且权重完整）；
    needed_mb / disk_free_mb 供前端在「开始下载」前就拦住磁盘不足。
    """
    import clone_env
    return clone_env.status()


@router.post("/api/commentary/clone-env/install")
def commentary_clone_env_install(force: bool = app.Form(False)) -> dict:
    """启动环境安装（幂等；force=True 可强制重装）。"""
    import clone_env
    return clone_env.start_install(force=bool(force))


@router.post("/api/commentary/clone-env/cancel")
def commentary_clone_env_cancel() -> dict:
    """取消进行中的安装（当前步骤跑完即停，不留半包数据）。"""
    import clone_env
    return clone_env.cancel()


@router.post("/api/commentary/stash")
async def create_commentary_stash(
    file: app.UploadFile = app._FastAPIFile(...),
) -> dict:
    """上传本地视频到本机「接收站」(cache-by-hash)：按 sha256(前 16 位) 去重，
    相同内容 0 字节传输。返回 {id: "stash:<sha>", size, from_cache}。

    前端拿到 id 后用 JSON 提交（不再 multipart），worker 通过 _resolve_source
    识别 stash: 前缀 → 取真路径 → 跑流水线。"""
    if not app.COMMENTARY_ENABLED:
        raise app.HTTPException(status_code=503, detail="该实例未启用解说功能")
    suffix = app.Path(file.filename or "upload.mp4").suffix.lower() or ".mp4"
    if suffix not in app._STASH_VIDEO_EXTS:
        raise app.HTTPException(status_code=409, detail="请上传视频文件")
    name = (file.filename or "").strip() or "upload.mp4"
    sha = app.hashlib.sha256()
    # 流式落盘到 .part 文件：同名 dest 已存在就提前 return(from_cache)
    # 我们故意先把 dest 路径算出来，存在=可能命中（按大小兜底校验）
    try:
        await file.seek(0)
    except Exception:
        pass
    # 同一进程可能并发多份 stash 文件，每个 stash 用独立临时名（按 hash 占位）
    placeholder_ext = suffix
    tmp = app.COMMENTARY_STASH_DIR / f".part-{app.uuid.uuid4().hex[:12]}{placeholder_ext}"
    try:
        with tmp.open("wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                sha.update(chunk)
                fh.write(chunk)
    finally:
        try:
            await file.close()
        except Exception:
            pass
    sha_hex = sha.hexdigest()[:16]
    dest = app._stash_path_for(sha_hex, placeholder_ext)
    if dest.exists():
        # 内容哈希已落盘，删临时文件
        try:
            tmp.unlink()
        except OSError:
            pass
        from_cache = True
    else:
        try:
            tmp.rename(dest)
        except OSError:
            # 极端情况：另一进程刚刚才落盘——以 dest 为准
            tmp.unlink(missing_ok=True)
        from_cache = False
    stash_id = app._stash_register(sha_hex, placeholder_ext, name)
    return {
        "id": stash_id,
        "size": dest.stat().st_size if dest.exists() else 0,
        "name": name,
        "from_cache": from_cache,
    }


@router.post("/api/commentary/config")
def commentary_config_save(req: app.CommentaryConfigRequest) -> dict:
    """保存解说(配音/音量)手动可调设置，写入 commentary_config.json。"""
    from commentary_config import save_commentary_config
    normalized = save_commentary_config({
        "narration_loudness": req.narration_loudness,
        "original_duck": req.original_duck,
        "narration_boost": req.narration_boost,
    })
    return {"ok": True, "config": normalized}


@router.post("/api/commentary")
def create_commentary(payload: app.CommentaryRequest, request: app.Request) -> dict:
    if not app.COMMENTARY_ENABLED:
        raise app.HTTPException(status_code=503, detail="该实例未启用解说功能")
    if app.COMMENTARY_MODE == "http":
        if not app.COMMENTARY_ENDPOINT:
            raise app.HTTPException(status_code=503, detail="解说 worker 未配置（VDL_COMMENTARY_MODE=http 但缺少 VDL_COMMENTARY_ENDPOINT）")
    else:
        if not app.COMMENTARY_DIR or not (app.COMMENTARY_DIR / "process.py").exists():
            raise app.HTTPException(status_code=503, detail="解说管线未配置（VDL_COMMENTARY_DIR 缺失或不含 process.py）")

    src_path = app._resolve_source(payload, request)

    # 前置闸门：免费用户超时长 / 必须走云端但额度已耗尽 → 立刻拒绝，绝不进入执行阶段。
    # 历史缺口：此入口与本地拖拽入口都没有校验，用户要等转写+脚本全跑完才被告知
    # 额度不够，白等十几分钟且拿到废片（2026-09-15 用户实测反馈）。
    from routers.quota import precheck_or_raise
    # 🔴 2026-10-23「失败不扣积分」：预检已**不再真扣费**，只回一份账单（只读校验余额）。
    # 账单必须随身带进 job，跑成功后由 app._commentary_charge_on_success 落账；
    # 任务失败时那份账单就自然作废 ⇒ 没产出就不扣分。
    _pre = precheck_or_raise(request, _effective_duration(src_path, payload.trim_start, payload.trim_end,
                                                payload.drama_start_sec, payload.drama_end_sec),
                      vision=bool(payload.vision))

    job_id = app.uuid.uuid4().hex[:12]
    _title = app._commentary_title(payload, src_path, request)
    with app._commentary_lock:
        app.commentary_jobs[job_id] = {"status": "running", "error": "", "output_path": "", "progress": [],
                                   "steps": [], "logs": [], "src_path": src_path, "title": _title,
                                   "billing": (_pre or {}).get("billing") or {}}
    app.executor.submit(app._commentary_run, job_id, src_path, payload.vertical, payload.voice or app.COMMENTARY_VOICE,
                    trim_start=payload.trim_start, trim_end=payload.trim_end,
                    mode=payload.mode, commentary_type=payload.commentary_type,
                    highlight_source=payload.highlight_source,
                    intro_highlight=payload.intro_highlight,
                    skip_intro_outro=payload.skip_intro_outro,
                    no_narrate_intro_outro=payload.no_narrate_intro_outro,
                    retain_pct=payload.retain_pct, web=payload.web,
                    vision=payload.vision, tts_provider=payload.tts_provider,
                    correct_transcript=payload.correct_transcript,
                    intro_sec=payload.intro_sec, outro_sec=payload.outro_sec,
                    drama_start_sec=payload.drama_start_sec, drama_end_sec=payload.drama_end_sec,
                    one_click=payload.one_click,
                    title=_title,
                    style=payload.style,
                    style_intensity=payload.style_intensity,
                    bgm=payload.bgm, bgm_file=payload.bgm_file, bgm_volume=payload.bgm_volume,
                    subtitle_size=payload.subtitle_size, subtitle_color=payload.subtitle_color,
                    subtitle_border=payload.subtitle_border,
                    subtitle_border_color=payload.subtitle_border_color,
                    subtitle_pos=payload.subtitle_pos,
                    max_chars=payload.max_chars,
                    feather_opt=payload.feather_opt,
                    export_jianying=payload.export_jianying)
    return {"job_id": job_id, "status": "running"}

@router.post("/api/commentary/feather-detect")
def feather_detect(frames: list[app.UploadFile] = app._FastAPIFile(default=[]),
                   vertical: bool = app.Form(False)) -> dict:
    """探测「烧进画面的原字幕」所在横带（前端「原字幕羽化」面板用）。

    入参：前端从预览视频抽的若干帧 JPEG（多文件字段 frames，通常 10 帧）。
    出参：带占画面高的比例 + 命中帧数，供预览层画带、用户拖拽/输入微调。

    **不调 LLM、不进执行队列、不扣配额**：纯 PIL 图像处理（4 帧 480px 图约 10ms）。
    ⚠️ 算法与管线 `_prepare_feather` 同源，两侧常量必须同步（见 subtitle_band.py 顶部）。
    """
    if not app.COMMENTARY_ENABLED:
        raise app.HTTPException(status_code=503, detail="该实例未启用解说功能")
    from io import BytesIO

    from PIL import Image

    from subtitle_band import detect_band_ratio

    imgs = []
    for f in frames or []:
        try:
            raw = f.file.read()
            if not raw:
                continue
            with Image.open(BytesIO(raw)) as im:
                im.load()
                imgs.append(im.copy())      # copy：with 退出后底图即关闭
        except Exception:
            continue                        # 单帧坏图不该让整个探测失败
    if not imgs:
        raise app.HTTPException(status_code=400, detail="没有收到可解析的帧图")
    res = detect_band_ratio(imgs)
    res["vertical"] = bool(vertical)
    return res

@router.post("/api/commentary/suggest-range")
def suggest_range(payload: app.CommentaryRequest, request: app.Request) -> dict:
    """轻量探测片头/片尾边界，供前端「起点/终点」预填建议值（2026-09-20）。

    只跑 ffmpeg 静音/黑场/静止探测（秒级、零 LLM 成本）。探测不到（有声有色的
    片头）→ ok=False，前端保持「自动检测」占位，渲染时管线再做全量检测
    （含视觉集数卡识别）。探测到的值前端当普通输入值提交 → 所见即所用。
    不进执行队列、不扣配额、不写任务。
    """
    if not app.COMMENTARY_ENABLED:
        raise app.HTTPException(status_code=503, detail="该实例未启用解说功能")
    src = app._resolve_source(payload, request)
    return app._suggest_intro_outro(src)

@router.post("/api/commentary/upload")
def create_commentary_upload(
    file: app.UploadFile = app._FastAPIFile(...),
    vertical: bool = app.Form(False),
    voice: str = app.Form(""),
    trim_start: float = app.Form(0.0),
    trim_end: float = app.Form(0.0),
    mode: str = app.Form("highlights"),
    vision: bool = app.Form(False),
    tts_provider: str = app.Form(""),
    correct_transcript: str = app.Form(""),
    title: str = app.Form(""),
    intro_sec: float = app.Form(None),
    outro_sec: float = app.Form(None),
    drama_start_sec: float = app.Form(None),
    drama_end_sec: float = app.Form(None),
    export_jianying: str = app.Form(""),
    feather_opt: str = app.Form(""),
    request: app.Request = None,
) -> dict:
    """上传本地视频 → 直接生成解说成片。

    安全审计 2026-10-13（F-04）：补 `request` 形参 —— 否则下方计费/配额闸门只能传
    `None`，落到**全局** member_store，登录用户的解说费用不计入本人账户。
    """
    if not app.COMMENTARY_ENABLED:
        raise app.HTTPException(status_code=503, detail="该实例未启用解说功能")
    suffix = app.Path(file.filename or "upload.mp4").suffix.lower() or ".mp4"
    if suffix not in {".mp4", ".mkv", ".mov", ".webm", ".avi"}:
        raise app.HTTPException(status_code=409, detail="请上传视频文件")
    work_dir = app._commentary_work_dir()
    dest = work_dir / f"upload{suffix}"
    try:
        with dest.open("wb") as fh:
            app.shutil.copyfileobj(file.file, fh)
    except Exception as e:
        raise app.HTTPException(status_code=500, detail=f"保存上传文件失败：{e}")
    finally:
        file.file.close()

    # 服务端二次校验：免费用户上传 > 30 分钟视频直接拦截（前端主拦截，此处防绕过；探测失败则 fail-open）
    _bill = {}   # 预检失败会抛，异常分支下保持空账 ⇒ 不会误扣
    try:
        from routers.quota import assert_upload_allowed
        _dur = _effective_duration(dest, trim_start, trim_end,
                                   drama_start_sec, drama_end_sec)
        _bill = assert_upload_allowed(request, _dur, vision=bool(vision))   # 只拿账单，产出成功才扣（2026-10-23）
    except app.HTTPException:
        raise
    except Exception:
        pass

    job_id = app.uuid.uuid4().hex[:12]
    # 片名前缀优先级（用户 2026-08-25 明确）：显式 title → 上传前的名字(有意义时)
    # → ffprobe 读 mp4 自带标题 → v<6hex> 短码。
    # 保证上传《少帅.mp4》→ 片名「少帅」；源文件叫 upload.mp4 这种无语义名不会上片名。
    stem = app.Path(file.filename).stem if file.filename else ""
    final_title = (title
                   or (stem if app._meaningful_stem(stem) else "")
                   or app._probe_video_title(dest)
                   or "")
    final_title = (final_title or "").strip()
    if not final_title:
        import secrets as _secrets
        final_title = "v" + _secrets.token_hex(3)
    # 保留用户上传时的原始文件名，UI「源视频: ...」优先展示它而不是 upload.mp4 这种占位名
    src_filename = (file.filename or "").strip() or app.Path(file.filename or "upload.mp4").name
    with app._commentary_lock:
        app.commentary_jobs[job_id] = {"status": "running", "error": "", "output_path": "", "progress": [],
                                   "steps": [], "logs": [], "src_path": str(dest), "title": final_title,
                                   "src_filename": src_filename, "billing": _bill or {}}
    app.executor.submit(app._commentary_run, job_id, str(dest), vertical, voice or app.COMMENTARY_VOICE,
                    trim_start=trim_start, trim_end=trim_end, mode=mode,
                    vision=vision, tts_provider=tts_provider,
                    correct_transcript=correct_transcript,
                    intro_sec=intro_sec, outro_sec=outro_sec,
                    drama_start_sec=drama_start_sec, drama_end_sec=drama_end_sec,
                    title=final_title,
                    src_filename=src_filename,
                    feather_opt=feather_opt,
                    export_jianying=export_jianying)
    return {"job_id": job_id, "status": "running"}

@router.post("/api/commentary/script-only/upload")
def create_script_only_upload(
    file: app.UploadFile = app._FastAPIFile(...),
    vertical: bool = app.Form(False),
    voice: str = app.Form(""),
    trim_start: float = app.Form(0.0),
    trim_end: float = app.Form(0.0),
    mode: str = app.Form("highlights"),
    commentary_type: str = app.Form("deep_hl"),
    highlight_source: str = app.Form("ai"),
    intro_highlight: bool = app.Form(False),
    skip_intro_outro: bool = app.Form(False),
    no_narrate_intro_outro: bool = app.Form(True),
    intro_sec: float = app.Form(None),
    outro_sec: float = app.Form(None),
    drama_start_sec: float = app.Form(None),
    drama_end_sec: float = app.Form(None),
    retain_pct: float = app.Form(None),
    web: bool = app.Form(False),
    one_click: bool = app.Form(False),
    style: str = app.Form("none"),
    style_intensity: int = app.Form(65),
    vision: bool = app.Form(False),
    tts_provider: str = app.Form(""),
    correct_transcript: str = app.Form(""),
    title: str = app.Form(""),
    export_jianying: str = app.Form(""),
    request: app.Request = None,
) -> dict:
    """上传本地视频 → 只生成脚本不渲染成片。

    安全审计 2026-10-13（F-04）：补 `request` 形参 —— 否则下方计费/配额闸门只能传
    `None`，落到**全局** member_store，登录用户的解说费用不计入本人账户。
    """
    if not app.COMMENTARY_ENABLED:
        raise app.HTTPException(status_code=503, detail="该实例未启用解说功能")
    if app.COMMENTARY_MODE == "http":
        raise app.HTTPException(status_code=400, detail="脚本审核模式暂不支持 HTTP worker，请使用 local 模式")
    suffix = app.Path(file.filename or "upload.mp4").suffix.lower() or ".mp4"
    if suffix not in {".mp4", ".mkv", ".mov", ".webm", ".avi"}:
        raise app.HTTPException(status_code=409, detail="请上传视频文件")
    work_dir = app._commentary_work_dir()
    dest = work_dir / f"upload{suffix}"
    try:
        with dest.open("wb") as fh:
            app.shutil.copyfileobj(file.file, fh)
    except Exception as e:
        raise app.HTTPException(status_code=500, detail=f"保存上传文件失败：{e}")
    finally:
        file.file.close()

    # 服务端二次校验：免费用户上传 > 30 分钟视频直接拦截（前端主拦截，此处防绕过；探测失败则 fail-open）
    _bill = {}   # 预检失败会抛，异常分支下保持空账 ⇒ 不会误扣
    try:
        from routers.quota import assert_upload_allowed
        _dur = _effective_duration(dest, trim_start, trim_end,
                                   drama_start_sec, drama_end_sec)
        _bill = assert_upload_allowed(request, _dur, vision=bool(vision))   # 只拿账单，产出成功才扣（2026-10-23）
    except app.HTTPException:
        raise
    except Exception:
        pass

    job_id = app.uuid.uuid4().hex[:12]
    src_path = str(dest)
    # 片名前缀优先级（用户 2026-08-25 明确）：显式 title → 上传前的名字(有意义时)
    # → ffprobe 读 mp4 自带标题 → v<6hex> 短码。
    # 保证上传《少帅.mp4》→ 片名「少帅」；源文件叫 upload.mp4 这种无语义名不会上片名。
    stem = app.Path(file.filename).stem if file.filename else ""
    final_title = (title
                   or (stem if app._meaningful_stem(stem) else "")
                   or app._probe_video_title(dest)
                   or "")
    final_title = (final_title or "").strip()
    if not final_title:
        import secrets as _secrets
        final_title = "v" + _secrets.token_hex(3)  # e.g. v3a8f1b
    # 保留用户上传时的原始文件名，UI「源视频: ...」优先展示它而不是 upload.mp4 这种占位名
    src_filename = (file.filename or "").strip() or app.Path(file.filename or "upload.mp4").name
    with app._commentary_lock:
        app.commentary_jobs[job_id] = {"status": "running", "error": "", "output_path": "", "script_path": "",
                                   "progress": [], "steps": [], "logs": [], "src_path": src_path,
                                   "title": final_title, "src_filename": src_filename,
                                   "billing": _bill or {}}
    app.executor.submit(app._commentary_run, job_id, src_path, vertical, voice or app.COMMENTARY_VOICE, script_only=True,
                    trim_start=trim_start, trim_end=trim_end, mode=mode,
                    commentary_type=commentary_type, highlight_source=highlight_source,
                    intro_highlight=intro_highlight, skip_intro_outro=skip_intro_outro,
                    no_narrate_intro_outro=no_narrate_intro_outro,
                    retain_pct=retain_pct, web=web, one_click=one_click,
                    vision=vision, tts_provider=tts_provider,
                    correct_transcript=correct_transcript,
                    intro_sec=intro_sec, outro_sec=outro_sec,
                    drama_start_sec=drama_start_sec, drama_end_sec=drama_end_sec,
                    title=final_title,
                    style=style,
                    style_intensity=style_intensity,
                    src_filename=src_filename,
                    export_jianying=export_jianying)
    return {"job_id": job_id, "status": "running"}

@router.get("/api/commentary/diagnostics")
def commentary_diagnostics() -> dict:
    """返回解说运行环境诊断信息，让桌面用户一眼看清 python / ffprobe 是否就绪。"""
    return {
        "enabled": app.COMMENTARY_ENABLED,
        "mode": app.COMMENTARY_MODE,
        "dir": str(app.COMMENTARY_DIR) if app.COMMENTARY_DIR else None,
        "python": app.COMMENTARY_RT.python,
        "python_ok": app.COMMENTARY_RT.python_ok,
        "deps_ok": app.COMMENTARY_RT.deps_ok,
        "ffmpeg_dir": app.COMMENTARY_RT.ffmpeg_dir,
        "ffprobe_ok": app.COMMENTARY_RT.ffprobe_ok,
        "ready": app.COMMENTARY_RT.ready(),
        "issues": app.COMMENTARY_RT.issues,
        "frozen": getattr(app.sys, "frozen", False),
        # 配音引擎（edge-tts）健康状态：微软会周期性作废旧版，这里给出「是否正常 /
        # 是否已自动升级 / 是否已是最新仍被拒」，前端据此提示，不用用户自己猜。
        "tts_guard": _tts_guard_state(),
    }


def _tts_guard_state() -> dict:
    try:
        return app.tts_guard.status()
    except Exception as exc:  # noqa: BLE001 - 诊断接口本身不能因为子系统异常而 500
        return {"ok": False, "status": "unknown", "message": f"无法读取配音引擎状态：{exc}"}


@router.get("/api/commentary/tts-guard")
def commentary_tts_guard() -> dict:
    """配音引擎（edge-tts）巡查状态。

    背景：解说配音走 edge-tts，微软会周期性作废旧版客户端（403 / NoAudioReceived）。
    这不是功能坏了 —— 应用内会自动巡查并在判定版本过期时自动升级，
    本接口把「当前版本 / 最近一次探活结果 / 是否已自动修复」摊开给用户看。
    """
    return {"ok": True, "state": _tts_guard_state()}


@router.post("/api/commentary/tts-guard/check")
def commentary_tts_guard_check(repair: bool = True) -> dict:
    """立刻探活一次（真合成一句「你好」，约 1~3 秒；判定版本过期会顺带自动升级）。"""
    try:
        st = app.tts_guard.check(auto_repair=bool(repair), force=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"探活失败：{exc}", "state": _tts_guard_state()}
    return {"ok": True, "state": st}


@router.post("/api/commentary/tts-guard/repair")
def commentary_tts_guard_repair() -> dict:
    """一键修复：强制下载最新 edge-tts 并热加载（不等故障判定）。"""
    try:
        r = app.tts_guard.repair(reason="用户手动修复", force=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"修复失败：{exc}", "state": _tts_guard_state()}
    return {"ok": bool(r.get("ok")), "result": r, "state": _tts_guard_state()}


def _effective_duration(src_path, trim_start: float = 0.0, trim_end: float = 0.0,
                        drama_start_sec: float | None = None,
                        drama_end_sec: float | None = None) -> float:
    """源视频「实际参与解说的时长」= 外层裁剪 ∩ 正剧范围（秒）。

    预检必须按真实处理的时长判定：用户把 32 分钟的片子裁掉 5 分钟头尾后只剩
    27 分钟，按总时长判会误拦。探测失败返回 0（调用方一律 fail-open）。

    🔴 2026-09-19 修正两处口径：
      ① 旧实现把 trim_start/trim_end 当「从头/从尾裁掉的秒数」（dur − (ts + te)），
         而 app._commentary_run 把它们当「绝对起止秒」——同一个字段两套语义。前端传的是
         **整片终点**，于是这里恒算出 0（预检形同失效）。现统一按 [起点, 终点] 区间算。
      ② 界面上的「正剧范围」（drama_* 绝对秒）现在会真正决定喂给管线的区间
         （见 app._fold_commentary_range），预检当然也要按它判——否则用户设了 15 分钟
         仍会被按 45 分钟拦下。
    """
    dur = float(app._probe_video_duration(src_path) or 0.0)
    if dur <= 0:
        return 0.0
    ts, te = app._fold_commentary_range(dur, trim_start, trim_end,
                                        drama_start_sec, drama_end_sec)
    return max(0.0, (te - ts) if te > ts else dur)


@router.get("/api/commentary/precheck")
def commentary_precheck(
    request: Request,
    duration_sec: float = 0.0,
    file_id: str = "",
    trim_start: float = 0.0,
    trim_end: float = 0.0,
    drama_start_sec: float = 0.0,
    drama_end_sec: float = 0.0,
    engine: str = "",
) -> dict:
    """解说前置预检 —— 前端「选完视频」立刻调用，不通过就当场提示。

    这是「不能等做完才说额度不够」的核心保证：免费用户的时长上限与云端额度
    必须在任务开始前判定完。历史缺陷是只在两个上传入口做校验，本地拖拽
    （stash → script-only）与下载库入口完全没有闸门，于是免费用户跑一条超能力
    的 40 分钟片，会在转写与脚本全部跑完后才因额度不足失败——白等十几分钟，
    产出的是废片。

    前端本地文件场景自行读元数据传 duration_sec（零成本、无需上传）；
    下载库 / 接收站缓存场景传 file_id，由服务端 ffprobe 探测。
    探测失败一律 fail-open（宁可放过，不误拦），真正的兜底在任务入口。

    engine 由前端传「界面上当前选中的档位」（auto / cloud），而不是只读后端已
    保存的配置：用户可能先切档位、再去选素材，此时还没点保存，若按已保存值判定
    就会出现两种误导——界面上是「本机优先」却被判成「将消耗云端额度」（本机明明
    可用，甚至会被误以为额度已耗尽而拦下），或反之界面是「纯云端」却提示不花钱。
    传空则回落到已保存配置。
    """
    from routers.quota import precheck_commentary as _pre
    dur = float(duration_sec or 0.0)
    if dur > 0:
        # 前端传来的是源视频总时长，这里折算出真实参与解说的时长。
        # 🔴 2026-09-19：旧实现按「ts+te 是要裁掉的秒数」算（前端传的是整片终点，
        #    于是恒等于 0、预检形同失效）；现统一走 app._fold_commentary_range，
        #    与真正喂给管线的区间同一套口径（含「正剧范围」）。
        _ts, _te = app._fold_commentary_range(dur, trim_start, trim_end,
                                              drama_start_sec, drama_end_sec)
        dur = max(0.0, (_te - _ts) if _te > _ts else dur)
    elif file_id:
        try:
            src = app._resolve_source(app.CommentaryRequest(file_id=file_id))
            dur = _effective_duration(src, trim_start, trim_end,
                                      drama_start_sec, drama_end_sec)
        except Exception:
            dur = 0.0
    return _pre(request, dur, engine=engine)


@router.get("/api/commentary/list")
def commentary_list() -> dict:
    """按修改时间倒序列出所有已生成的解说成片。

    每条返回：
    - id / name / size / mtime：成片元数据
    - bgm_state: {bgm, volume, source, ts}（manifest 缺失视为空 → 走默认 off）
    - bgm_previews: ["soft","light","epic"] 的子集 — 仅返回实际存在的预览 mp3
      （build() 渲染时已落 sidecar；旧版渲染可能为空）
    前端按 bgm_state 拆 draft/final；预览/试听通过 /api/commentary/bgm-preview/{cid}/{kind}。
    """
    import pathlib as _pl
    items = []
    seen: set[str] = set()
    for root in app._commentary_roots():
        for p in root.iterdir():
            if not p.is_file() or p.suffix.lower() not in (".mp4", ".mkv", ".mov", ".webm"):
                continue
            # 只列「成品」：渲染时会在同目录额外落一份无音乐原片 sidecar
            # `<成片名>.nomusic.mp4`（供渲染后单独换/加背景音乐），它不是独立成片。
            # 2026-09-15 用户反馈：历史里出现「...-解说完成2026...nomusic.mp4」两条同名记录，
            # 且数量翻倍 —— 根因就是这里把 sidecar 也当成了成片。按 stem 后缀剔除。
            if p.stem.lower().endswith(app._COMMENTARY_SIDECAR_STEMS):
                continue
            key = str(p.resolve())
            if key in seen:
                continue
            seen.add(key)
            cid = app.base64.urlsafe_b64encode(key.encode("utf-8")).decode("ascii")
            state = app._read_bgm_state(p) or {}
            # 哪些内置风格的 preview mp3 实际存在
            previews = []
            try:
                base = _pl.Path(key)
                stem = base.stem  # 去后缀的 basename（同名 .previews）
                previews_dir = base.parent / (stem + ".previews")
                if previews_dir.is_dir():
                    for kind in ("soft", "light", "epic"):
                        if (previews_dir / f"{kind}.mp3").is_file():
                            previews.append(kind)
            except Exception:
                pass
            items.append({
                "id": cid,
                "name": p.name,
                "size": p.stat().st_size,
                "mtime": int(p.stat().st_mtime),
                "bgm_state": {
                    "bgm": state.get("bgm", "off"),
                    "volume": float(state.get("volume", 0.18) or 0.18),
                    "source": state.get("source", ""),
                    "ts": int(state.get("ts", 0) or 0),
                },
                "bgm_previews": previews,
            })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"items": items}

@router.get("/api/commentary/file/{cid}")
def commentary_file_by_id(cid: str) -> app.FileResponse:
    p = app._decode_commentary_id(cid)
    return app.FileResponse(str(p), filename=p.name, media_type="video/mp4")

@router.delete("/api/commentary/file/{cid}")
def commentary_delete_by_id(cid: str) -> dict:
    """把已生成的解说成片移入系统回收站，拒绝直接硬删用户资产。"""
    p = app._decode_commentary_id(cid)
    allowed_roots = app._commentary_roots()
    if not allowed_roots:
        raise app.HTTPException(status_code=503, detail="解说输出目录未配置")
    try:
        resolved = p.resolve()
    except Exception:
        resolved = p
    in_allowed_root = any(
        resolved == root.resolve() or root.resolve() in resolved.parents
        for root in allowed_roots
    )
    if not in_allowed_root:
        raise app.HTTPException(status_code=403, detail="文件路径不在解说输出目录内")
    if not resolved.is_file():
        raise app.HTTPException(status_code=404, detail="成片文件不存在")
    if not app.retention_mod.trash_available():
        raise app.HTTPException(status_code=503, detail="系统回收站不可用，拒绝直接删除")
    # 先记录大小：移动成片到回收站后原路径已不存在，再 stat 会抛异常导致 500。
    try:
        file_size = resolved.stat().st_size
    except Exception:
        file_size = 0
    if not app.retention_mod.move_to_trash(resolved):
        raise app.HTTPException(status_code=500, detail="移入回收站失败")
    return {"deleted": True, "trashed": True, "name": p.name, "size": file_size}

@router.put("/api/commentary/file/{cid}")
def commentary_rename_by_id(cid: str, payload: app.CommentaryRenameReq) -> dict:
    """重命名已生成的解说成片（仅改文件名，不移动目录，保留原扩展名）。"""
    p = app._decode_commentary_id(cid)
    allowed_roots = app._commentary_roots()
    if not allowed_roots:
        raise app.HTTPException(status_code=503, detail="解说输出目录未配置")
    resolved = p.resolve()
    in_allowed_root = any(
        resolved == root.resolve() or root.resolve() in resolved.parents
        for root in allowed_roots
    )
    if not in_allowed_root:
        raise app.HTTPException(status_code=403, detail="文件路径不在解说输出目录内")
    if not resolved.is_file():
        raise app.HTTPException(status_code=404, detail="成片文件不存在")

    new_name = (payload.name or "").strip()
    if not new_name:
        raise app.HTTPException(status_code=400, detail="新文件名不能为空")
    # 去掉可能用于路径穿越的字符
    new_name = new_name.replace("/", "_").replace("\\", "_").replace("..", "_").strip()
    if not new_name:
        raise app.HTTPException(status_code=400, detail="新文件名非法")
    # 保留原扩展名（若新名未带扩展名）
    if "." not in new_name:
        new_name = new_name + p.suffix
    # 防止覆盖已有文件：重名自动追加 (1)/(2)…
    dest = resolved.parent / new_name
    n = 1
    while dest.exists():
        stem, suffix = new_name.rsplit(".", 1) if "." in new_name else (new_name, "")
        dest = resolved.parent / f"{stem} ({n}){('.' + suffix) if suffix else ''}"
        n += 1
    try:
        resolved.rename(dest)
    except Exception as e:
        raise app.HTTPException(status_code=500, detail=f"重命名失败：{e}")
    new_cid = app.base64.urlsafe_b64encode(str(dest.resolve()).encode("utf-8")).decode("ascii")
    return {"renamed": True, "id": new_cid, "name": dest.name}

@router.post("/api/commentary/file/{cid}/save")
def commentary_save_to_downloads(cid: str) -> dict:
    """把已生成的解说成片复制到「下载」文件夹。

    桌面版 WebView 的 <a download> 在 cocoa/WKWebView 下不会触发本机保存，
    而 pywebview 原生 Api 也未暴露 save_commentary_file 桥接；因此由本地
    FastAPI 后端（与 app 同机同用户运行，有权限写 ~/Downloads）直接复制文件，
    前端点击「下载」时调用本接口即可真正把成片落到下载目录。
    """
    p = app._decode_commentary_id(cid)
    if not p.is_file():
        raise app.HTTPException(status_code=404, detail="成片文件不存在")
    downloads = app.Path.home() / "Downloads"
    try:
        downloads.mkdir(parents=True, exist_ok=True)
    except Exception:
        raise app.HTTPException(status_code=500, detail="无法访问下载文件夹")
    # 避免覆盖已有同名文件：dst 已存在则追加 (1)/(2)…
    stem, suffix = p.stem, p.suffix
    dst = downloads / p.name
    n = 1
    while dst.exists():
        dst = downloads / f"{stem} ({n}){suffix}"
        n += 1
    try:
        app.shutil.copy2(str(p), str(dst))
    except Exception as e:
        raise app.HTTPException(status_code=500, detail=f"复制失败：{e}")
    return {"saved": True, "path": str(dst), "name": dst.name}

@router.get("/api/commentary/audio-preview")
def commentary_audio_preview(path: str):
    """提供本地音乐试听流（用户在桌面桥选完文件后，前端调本端点拉 mp3 进 <audio>）。

    安全：路径必须
    - 以 / 开头（绝对路径）
    - 位于当前用户家目录下（避免误读到系统目录）
    - 文件存在且扩展名在允许的音频列表（mp3/m4a/wav/aac/flac/ogg）
    """
    import pathlib as _pl
    if not path or not path.startswith("/"):
        raise app.HTTPException(status_code=400, detail="path 必须为绝对路径")
    home = _pl.Path.home()
    try:
        ap = _pl.Path(path).resolve()
        ap.relative_to(home)  # 必须在家目录下
    except ValueError:
        raise app.HTTPException(status_code=400, detail="path 必须在用户家目录下")
    except OSError:
        raise app.HTTPException(status_code=400, detail="path 无法解析")
    if not ap.is_file():
        raise app.HTTPException(status_code=404, detail="文件不存在")
    if ap.suffix.lower() not in (".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg"):
        raise app.HTTPException(status_code=400, detail=f"不支持的音频扩展名 {ap.suffix}")
    media = {
        ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav",
        ".aac": "audio/aac", ".flac": "audio/flac", ".ogg": "audio/ogg",
    }[ap.suffix.lower()]
    return app.FileResponse(path=str(ap), filename=ap.name, media_type=media)


@router.get("/api/commentary/{job_id}")
def commentary_status(job_id: str) -> dict:
    with app._commentary_lock:
        job = app.commentary_jobs.get(job_id)
    if not job:
        raise app.HTTPException(status_code=404, detail="解说任务不存在或已过期")
    # script_ready 也算就绪态（脚本已生成，等待人工确认后渲染）
    ready = job["status"] in ("completed", "script_ready")
    steps = job.get("steps") or []
    if not steps:
        # 旧任务或 HTTP 模式可能还没初始化步骤，兜底生成一个简单时间线
        steps = [{"name": "处理中", "status": "running" if job["status"] == "running" else "done",
                  "detail": "", "created_at": app.time.time(), "updated_at": app.time.time()}]
    result = {"job_id": job_id, "status": job["status"], "error": job.get("error", ""),
              "ready": ready, "progress": job.get("progress", []),
              "steps": steps, "logs": job.get("logs", []),
              "eta_remaining": job.get("eta_remaining"),
              "eta_done_at": job.get("eta_done_at"),
              "started_at": job.get("started_at"),
              "source_duration": job.get("source_duration"),
              "src_filename": job.get("src_filename", "")}
    if job.get("script_path"):
        result["script_path"] = job["script_path"]
    return result

@router.get("/api/commentary/{job_id}/file")
def commentary_file(job_id: str) -> app.FileResponse:
    with app._commentary_lock:
        job = app.commentary_jobs.get(job_id)
    if job and job["status"] == "completed" and job.get("output_path"):
        path = app.Path(job["output_path"])
        if path.exists():
            return app.FileResponse(path=str(path), filename=path.name, media_type="application/octet-stream")
        # 任务标记完成但成片文件已丢失/被清理 → 410 Gone，不应再走 cid 解码分支（否则误报 409）
        raise app.HTTPException(status_code=410, detail="成片文件已清理或丢失")
    # 兼容「已生成成片列表」卡片的 cid（base64 编码的文件路径标识）：
    # 桌面版桥接 save_commentary_file(cid) 会请求 /api/commentary/{cid}/file，
    # 命中本路由；此处按 cid 解码并校验后直接返回文件，使列表卡片也能下载。
    try:
        p = app._decode_commentary_id(job_id)
    except Exception:
        raise app.HTTPException(status_code=409, detail="成片尚未就绪或标识无效")
    return app.FileResponse(str(p), filename=p.name, media_type="application/octet-stream")

@router.post("/api/commentary/script-only")
def create_script_only(payload: app.CommentaryRequest, request: app.Request) -> dict:
    """只做转写+解说词生成，不渲染成片。返回 job_id 供前端轮询，
    拿到 script.json 后展示可编辑解说词面板。"""
    if not app.COMMENTARY_ENABLED:
        raise app.HTTPException(status_code=503, detail="该实例未启用解说功能")
    if app.COMMENTARY_MODE == "http":
        if not app.COMMENTARY_ENDPOINT:
            raise app.HTTPException(status_code=503, detail="解说 worker 未配置")
        raise app.HTTPException(status_code=400, detail="脚本审核模式暂不支持 HTTP worker，请使用 local 模式")

    src_path = app._resolve_source(payload, request)

    # 前置闸门（本地拖拽 / 下载库主路径）：这是「不能等做完才说额度不够」的关键点——
    # 免费用户跑一条超能力的片子，若在此放行，会白等十几分钟后才失败并产出废片。
    from routers.quota import precheck_or_raise
    # 同上：只拿账单不扣费，成功产出后才落账（2026-10-23）
    _pre = precheck_or_raise(request, _effective_duration(src_path, payload.trim_start, payload.trim_end,
                                                payload.drama_start_sec, payload.drama_end_sec),
                      vision=bool(payload.vision))

    job_id = app.uuid.uuid4().hex[:12]
    _title = app._commentary_title(payload, src_path, request)
    with app._commentary_lock:
        app.commentary_jobs[job_id] = {"status": "running", "error": "", "output_path": "", "script_path": "",
                                   "progress": [], "src_path": src_path, "title": _title,
                                   "billing": (_pre or {}).get("billing") or {}}
    app.executor.submit(app._commentary_run, job_id, src_path, payload.vertical, payload.voice or app.COMMENTARY_VOICE,
                    script_only=True, trim_start=payload.trim_start, trim_end=payload.trim_end,
                    mode=payload.mode, commentary_type=payload.commentary_type,
                    highlight_source=payload.highlight_source,
                    intro_highlight=payload.intro_highlight,
                    skip_intro_outro=payload.skip_intro_outro,
                    no_narrate_intro_outro=payload.no_narrate_intro_outro,
                    retain_pct=payload.retain_pct, web=payload.web,
                    vision=payload.vision, tts_provider=payload.tts_provider,
                    correct_transcript=payload.correct_transcript,
                    intro_sec=payload.intro_sec, outro_sec=payload.outro_sec,
                    drama_start_sec=payload.drama_start_sec, drama_end_sec=payload.drama_end_sec,
                    one_click=payload.one_click,
                    title=_title,
                    style=payload.style,
                    style_intensity=payload.style_intensity,
                    bgm=payload.bgm, bgm_file=payload.bgm_file, bgm_volume=payload.bgm_volume,
                    subtitle_size=payload.subtitle_size, subtitle_color=payload.subtitle_color,
                    subtitle_border=payload.subtitle_border,
                    subtitle_border_color=payload.subtitle_border_color,
                    subtitle_pos=payload.subtitle_pos,
                    max_chars=payload.max_chars,
                    feather_opt=payload.feather_opt,
                    export_jianying=payload.export_jianying)
    return {"job_id": job_id, "status": "running"}

@router.get("/api/commentary/script/{job_id}")
def get_script(job_id: str) -> dict:
    """获取已生成脚本文件内容（script.json）。"""
    with app._commentary_lock:
        job = app.commentary_jobs.get(job_id)
    if not job:
        raise app.HTTPException(status_code=404, detail="任务不存在或已过期")
    if job["status"] != "script_ready" or not job.get("script_path"):
        raise app.HTTPException(status_code=409, detail="脚本尚未就绪（当前状态: " + job["status"] + "）")
    script_path = app.Path(job["script_path"])
    if not script_path.exists():
        raise app.HTTPException(status_code=410, detail="脚本文件已被清理")
    try:
        data = app.json.loads(script_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise app.HTTPException(status_code=500, detail=f"读取脚本文件失败：{e}")
    segs = data.get("segments", [])
    # 每段「按可用时长估算能装多少字」，交给审核面板做实时提示。
    # 语速常量只在本模块定义，前端不再自行猜测，避免两端口径分叉。
    budgets = [_narration_budget(s.get("start", 0), s.get("end", 0))
               if isinstance(s, dict) else 0 for s in segs]
    return {"job_id": job_id, "title": data.get("title", ""),
            "voice": data.get("voice", ""),
            "segments": segs, "segment_count": len(segs), "budgets": budgets,
            # 🔴 2026-11-09：裁剪过的片子，segments 的 start/end 是**片段内**相对时间（0 起），
            # 而用户是按原片时间划的范围（如 10:00–15:00）。把绝对区间一并给出，
            # 前端据此把时间显示换算成原片位置，避免「选了 10 分钟却显示 2:11」的误判。
            # 内存 job 重启就没了 ⇒ 再回落到 script.json 顶层（管线收尾时写入的那份）。
            "range_start": float(job.get("range_start") or data.get("range_start") or 0.0),
            "range_end": float(job.get("range_end") or data.get("range_end") or 0.0),
            "src_duration": float(job.get("src_duration") or data.get("src_duration") or 0.0)}

@router.put("/api/commentary/script/{job_id}")
def update_script(job_id: str, payload: app.ScriptUpdateRequest) -> dict:
    """人工修改后提交更新脚本（写回 script.json）。
    保留原始时间戳（前端发 start/end=0），只更新 narration / voice。
    """
    with app._commentary_lock:
        job = app.commentary_jobs.get(job_id)
    if not job:
        raise app.HTTPException(status_code=404, detail="任务不存在或已过期")
    if job["status"] != "script_ready" or not job.get("script_path"):
        raise app.HTTPException(status_code=409, detail="脚本尚未就绪，无法修改（当前状态: " + job["status"] + "）")
    script_path = app.Path(job["script_path"])

    # 读现有脚本以保留时间戳
    try:
        existing = app.json.loads(script_path.read_text(encoding="utf-8"))
    except Exception:
        existing = {}
    existing_segs = existing.get("segments", [])

    # 合并：payload 里每个 seg 的 narration 写回对应 idx 的原始 seg
    merged = []
    for i, pseg in enumerate(payload.segments):
        orig = existing_segs[i] if i < len(existing_segs) else {}
        # 🔴 2026-11-09 广告口播段（ad=true，用户诉求：「方便后期加入广告对白」）：
        #    这类段的**时间由用户在审核面板里自己填**（可随意改到原片任意位置），
        #    所以必须采用 payload 的 start/end，不能像普通段那样从原脚本回填时间——
        #    否则用户把广告挪到别处、保存后被旧时间盖回去，等于白改。
        #    普通段保持原逻辑（时间只认原脚本，前端传 0 也不影响），行为零变化。
        is_ad = bool(pseg.get("ad"))
        if is_ad:
            try:
                a = max(0.0, float(pseg.get("start") or 0))
                b = max(a, float(pseg.get("end") or a))
            except (TypeError, ValueError):
                a, b = 0.0, 0.0
            start, end = a, b
        elif pseg.get("user_time"):
            # ⚖ 2026-11-09 自动校准（用户：「文字多了、匹配的时间少了，能够自动调整」）：
            #    面板把某段的结束时间往后撑开后会带 user_time=True 提交 —— 这段的时间已经是
            #    「按字数算过」的，必须采用 payload，否则保存后被旧时间盖回去，等于白校准。
            #    ⚠️ 只认显式标记，且必须是一段合法区间（b > a）：不带标记的普通段行为零变化
            #    （见 test_normal_segment_time_untouched_regression），任何旧前端都改不坏时间。
            try:
                a = max(0.0, float(pseg.get("start") or 0))
                b = max(a, float(pseg.get("end") or a))
            except (TypeError, ValueError):
                a, b = 0.0, 0.0
            if b > a:
                start, end = a, b
            else:
                start = orig.get("start", 0)
                end = orig.get("end", 0)
        else:
            start = orig.get("start", pseg.get("start", 0))
            end = orig.get("end", pseg.get("end", 0))
        item = {
            "start": start,
            "end": end,
            "narration": pseg.get("narration", orig.get("narration", "")),
            "note": pseg.get("note", orig.get("note", "")),
        }
        if is_ad:
            item["ad"] = True   # 落盘保留标记：下次打开面板这段的时间才可继续编辑
        merged.append(item)

    data = {
        "title": payload.title or existing.get("title", ""),
        "voice": payload.voice or existing.get("voice", job.get("voice", "")),
        "segments": merged,
        # 保留原始 mode（如高光解说模式），否则保存后再渲染会丢失高光标记
        "mode": existing.get("mode", ""),
        # 保留原始 options（剪辑选项：解说类型/高光来源/片头高光/联网/保留时长等），
        # 否则审核后渲染会丢失用户在面板上的剪辑选择
        "options": existing.get("options", {}),
    }
    try:
        script_path.write_text(app.json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        raise app.HTTPException(status_code=500, detail=f"写入脚本失败：{e}")

    # 更新内存中的 voice 偏好（渲染时会用到）
    if payload.voice:
        with app._commentary_lock:
            app.commentary_jobs[job_id]["voice"] = payload.voice

    # 超长预检：原速模式渲染时（out_dur = end - start，ffmpeg -t 收尾）
    # 超出预算的旁白会被静默截断，故保存后回传告警，让用户在渲染前就知道。
    over_limit = _narration_over_limit(merged)
    return {"job_id": job_id, "status": "updated",
            "segment_count": len(payload.segments), "over_limit": over_limit}

@router.post("/api/commentary/render/{job_id}")
def render_script(job_id: str, vertical: bool = app.Form(False), voice: str = app.Form(""),
                 export_jianying: str = app.Form(""),
                 bgm: str = app.Form("off"), bgm_file: str = app.Form(""),
                 bgm_volume: float = app.Form(0.18),
                 subtitle_size: float = app.Form(1.0), subtitle_color: str = app.Form("FFFFFF"),
                 subtitle_border: float = app.Form(1.0), subtitle_border_color: str = app.Form("000000"),
                 subtitle_pos: str = app.Form("bottom"),
                 max_chars: int = app.Form(0),
                 feather_opt: str = app.Form(""),
                 tts_provider: str = app.Form("")) -> dict:
    """用已审核的脚本渲染成片（process.py --edit-only）。

    剪辑选项直接沿用 script.json 中已保存的 options（生成脚本时写入、人工审核时可改），
    避免用默认值覆盖用户当初的选择（例如一键生成的全片深入+联网会被 deep_hl 默认值冲掉）。

    tts_provider（2026-09-18 新增）：🔴 此前本接口**根本没有这个参数**，于是「配音引擎」下拉
    在「生成脚本 → 渲染」这条主链路上完全无效——用户在界面上选了本地语音克隆，渲染子进程
    拿不到 VDL_TTS_PROVIDER，只能吃 ~/.video-downloader/tts_config.json 里的旧值（实测是
    siliconflow），出来的成片自然不是自己的声音。现在把界面上选的引擎如实透传给渲染子进程。
    """
    if not app.COMMENTARY_ENABLED:
        raise app.HTTPException(status_code=503, detail="该实例未启用解说功能")
    if app.COMMENTARY_MODE == "http":
        raise app.HTTPException(status_code=400, detail="脚本渲染暂不支持 HTTP worker 模式，请使用 local 模式")

    with app._commentary_lock:
        job = app.commentary_jobs.get(job_id)
    if not job:
        raise app.HTTPException(status_code=404, detail="任务不存在或已过期")
    if job["status"] != "script_ready" or not job.get("script_path"):
        raise app.HTTPException(status_code=409, detail="请先生成脚本再渲染（当前状态: " + job["status"] + "）")
    script_path = job["script_path"]
    # 反查原始视频路径：优先用脚本任务记录的 src_path，避免按 title/job_id 在 input 目录里猜错。
    src_path = job.get("src_path")
    if not src_path:
        raise app.HTTPException(status_code=410, detail="源视频路径已丢失，请重新上传")
    # 片名前缀：取脚本生成时存的「原始 title」（用户上传时算出的 final_title，
    # 优先于 script.json 里的 LLM 推断剧名），避免成片名是 hash/upload。
    # 旧任务（未存 title）从 src_path 走同样的回退链重算。
    title = (job.get("title") or "").strip()
    if not title:
        stem = app.Path(src_path).stem if src_path else ""
        title = (stem if app._meaningful_stem(stem) else "") or app._probe_video_title(src_path) or ""
        title = (title or "").strip()
        if not title:
            import secrets as _secrets
            title = "v" + _secrets.token_hex(3)
    seg_data = {}
    try:
        seg_data = app.json.loads(app.Path(script_path).read_text(encoding="utf-8")) or {}
        saved = seg_data.get("options") or {}
    except Exception:
        saved = {}

    # 渲染前统一预检：不管脚本有没有被人工改过，都按「字数 vs 片段时长」校验一遍。
    # 🔴 2026-10-26 用户拍板：**绝不放慢画面，解说保持完整**。此前检出超标段会
    #    自动放弃原速（渲染层 setpts 慢放把窗口撑开对齐旁白）⇒ 成片"突然慢动作"。
    #    现已废除该降速路径：恒原速渲染。超标段由渲染层原速逻辑自然消化——
    #    窗口跟随旁白时长（画面原速多播几秒），文字一字不动、旁白完整不截断。
    #    over_limit 仅留档/提示，不再触发任何画面变速。
    use_original_speed, over_limit = _plan_original_speed(seg_data.get("segments") or [])
    auto_adjusted = False

    commentary_type = saved.get("commentary_type", "deep_hl")
    highlight_source = saved.get("highlight_source", "ai")
    intro_highlight = bool(saved.get("intro_highlight", False))
    skip_intro_outro = bool(saved.get("skip_intro_outro", False))
    no_narrate_intro_outro = bool(saved.get("no_narrate_intro_outro", True))
    retain_pct = saved.get("retain_pct")
    intro_sec = saved.get("intro_sec")
    outro_sec = saved.get("outro_sec")
    drama_start_sec = saved.get("drama_start_sec")
    drama_end_sec = saved.get("drama_end_sec")
    web = bool(saved.get("web", False))
    one_click = bool(saved.get("one_click", False))

    # 复用父任务的裁剪参数：同一源+起止会命中确定性命名的裁剪文件，直接吃裁剪后视频渲染
    trim_start = float(job.get("trim_start", 0.0) or 0.0)
    trim_end = float(job.get("trim_end", 0.0) or 0.0)

    # 用新的 job_id 提交渲染（保留原 script 关联）
    render_job_id = app.uuid.uuid4().hex[:12]
    # 复用父任务上传时的原始文件名（render 任务不重新接收上传，沿用脚本任务的 src_filename）
    src_filename = (job.get("src_filename") or "").strip()
    with app._commentary_lock:
        app.commentary_jobs[render_job_id] = {"status": "running", "error": "", "output_path": "", "progress": [],
                                          "parent_script_job": job_id, "steps": [], "logs": [],
                                          "src_path": src_path, "title": title,
                                          "src_filename": src_filename,
                                          # 预检结论留档，便于前端轮询/事后排查
                                          "auto_adjusted": auto_adjusted, "over_limit": over_limit}
    v = voice or job.get("voice", "") or app.COMMENTARY_VOICE
    app.executor.submit(app._commentary_run, render_job_id, src_path, vertical, v, edit_only=script_path,
                    trim_start=trim_start, trim_end=trim_end,
                    commentary_type=commentary_type, highlight_source=highlight_source,
                    intro_highlight=intro_highlight, skip_intro_outro=skip_intro_outro,
                    no_narrate_intro_outro=no_narrate_intro_outro,
                    retain_pct=retain_pct, web=web, one_click=one_click,
                    intro_sec=intro_sec, outro_sec=outro_sec,
                    drama_start_sec=drama_start_sec, drama_end_sec=drama_end_sec,
                    title=title,
                    src_filename=src_filename,
                    bgm=bgm, bgm_file=bgm_file, bgm_volume=bgm_volume,
                    subtitle_size=subtitle_size, subtitle_color=subtitle_color,
                    subtitle_border=subtitle_border,
                    subtitle_border_color=subtitle_border_color,
                    subtitle_pos=subtitle_pos,
                    max_chars=max_chars,
                    feather_opt=feather_opt,
                    export_jianying=export_jianying,
                    tts_provider=tts_provider,
                    original_speed=use_original_speed)
    return {"job_id": render_job_id, "status": "running", "script_job": job_id,
            "over_limit": over_limit, "auto_adjusted": auto_adjusted}


def _run_remux_bgm(remux_id: str, output_path: str, bgm: str, bgm_file: str,
                   bgm_volume: float, bgm_clip_start=None, bgm_clip_end=None) -> None:
    """后台轻量换/加/移除 BGM：复用 _commentary_run 的同款子进程机制跑 process.py remux-bgm。

    不重渲成片，只跑 amix（秒级），成品就地替换（build 时已存无音乐 sidecar）。
    bgm_clip_start/end (秒)：锁住 BGM 文件片段——通过 ffmpeg atrim 完成。
    """
    last: list[str] = []

    def _append(line: str) -> None:
        line = (line or "").rstrip("\n")
        if not line:
            return
        last.append(line)
        if len(last) > 60:
            del last[: len(last) - 60]
        with app._commentary_lock:
            j = app.commentary_jobs.setdefault(remux_id, {})
            j["progress"] = list(last)
            app._commentary_log(j, line)

    try:
        _bundled = app._commentary_is_bundled()
        if _bundled:
            args = [sys.executable, "--vdl-commentary-worker", "remux-bgm", "--out", output_path]
        else:
            args = [app.COMMENTARY_RT.python, "-u", "process.py", "remux-bgm", "--out", output_path]
        args += ["--bgm", bgm, "--bgm-volume", str(bgm_volume)]
        if bgm == "user" and bgm_file:
            args += ["--bgm-file", bgm_file]
        if bgm_clip_start is not None:
            args += ["--bgm-clip-start", str(float(bgm_clip_start))]
        if bgm_clip_end is not None:
            args += ["--bgm-clip-end", str(float(bgm_clip_end))]
        run_env = app.COMMENTARY_RT.env()
        with app._commentary_lock:
            app.commentary_jobs.setdefault(remux_id, {})["status"] = "remuxing"
        proc = subprocess.Popen(
            args, cwd=str(app.COMMENTARY_DIR),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=run_env,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            _append(line)
        ret = proc.wait(timeout=300)
        if ret != 0:
            tail = "\n".join(last[-20:]) or "无输出"
            raise RuntimeError(f"换音乐退出码 {ret}：\n{tail}")
        with app._commentary_lock:
            rj = app.commentary_jobs.get(remux_id)
            if rj:
                rj.update(status="completed", output_path=output_path, remuxed_bgm=bgm)
            # 同步原渲染任务，方便前端仅凭 render_job 轮询即可感知换音乐完成
            parent = rj.get("parent_render_job") if rj else None
            if parent and parent in app.commentary_jobs:
                app.commentary_jobs[parent].update(remuxed_bgm=bgm, remux_job=remux_id, status="completed")
        # 写 BGM 状态 manifest：bgm=off 视为"已移除配乐"=已成片(无配乐版)，bgm!=off 视为"已加配乐"=已成片
        # 即只要走过 remux 流程，无论 on/off 都算 final（用户主动确认过配乐决策）
        try:
            app._write_bgm_state(output_path, bgm=bgm, volume=bgm_volume, source="remux")
        except Exception:
            app.logger.exception("写 remux BGM manifest 失败：%s", output_path)
    except Exception as exc:  # noqa: BLE001
        with app._commentary_lock:
            j = app.commentary_jobs.setdefault(remux_id, {})
            j["status"] = "failed"
            j["error"] = str(exc)[:800]
        app.logger.exception("换音乐任务 %s 失败", remux_id)


@router.post("/api/commentary/remux-bgm/{cid}")
def remux_bgm(cid: str, bgm: str = app.Form("off"), bgm_file: str = app.Form(""),
             bgm_volume: float = app.Form(0.18),
             bgm_clip_start: float = app.Form(None),
             bgm_clip_end: float = app.Form(None)) -> dict:
    """渲染后单独换/加/移除 BGM（轻量 amix，不重渲成片）。

    用成片文件 id（与列表/下载/删除接口同款 cid）定位成片，避免依赖内存中的
    渲染任务（可能被清理）。返回独立 remux_id 供前端轮询进度；成品就地替换，
    output_path 不变。build() 已始终保留无音乐 sidecar + 三种内置预览 mp3，
    故可随时在 off/soft/light/epic/user 间自由切换；同时支持 bgm_clip_start/end
    锁定 BGM 文件的片段（ffmpeg atrim）。

    字段：
    - bgm: "off" 移除配乐；"soft"/"light"/"epic" 内置和弦；"user" 本地文件
    - bgm_file: bgm=user 时为本地音乐绝对路径
    - bgm_volume: BGM 音量(0.02~0.6, 默认 0.18)
    - bgm_clip_start/end: BGM 文件秒级片段（None = 用整条）
    """
    if not app.COMMENTARY_ENABLED:
        raise app.HTTPException(status_code=503, detail="该实例未启用解说功能")
    if app.COMMENTARY_MODE == "http":
        raise app.HTTPException(status_code=400, detail="换音乐暂不支持 HTTP worker 模式，请使用 local 模式")
    p = app._decode_commentary_id(cid)
    if not p or not p.is_file():
        raise app.HTTPException(status_code=404, detail="成片文件不存在")
    output_path = str(p)
    remux_id = app.uuid.uuid4().hex[:12]
    with app._commentary_lock:
        app.commentary_jobs[remux_id] = {"status": "remuxing", "error": "", "output_path": output_path,
                                          "progress": [], "parent_render_job": "", "logs": [],
                                          "remuxed_bgm": ""}
    app.executor.submit(_run_remux_bgm, remux_id, output_path, bgm, bgm_file,
                         bgm_volume, bgm_clip_start, bgm_clip_end)
    return {"job_id": remux_id, "status": "remuxing", "render_job": cid}


def _bgm_preview_mp3_path(mp4_path: "pathlib.Path", kind: str) -> "pathlib.Path | None":
    """返回 `<mp4>.previews/<kind>.mp3` 路径（若不存在则 None）。"""
    try:
        import pathlib as _pl
        p = _pl.Path(str(mp4_path))
        base = p.with_suffix("") if p.suffix else _pl.Path(str(p))
        previews_dir = base.parent / (base.name + ".previews")
        candidate = previews_dir / f"{kind}.mp3"
        return candidate if candidate.is_file() else None
    except Exception:
        return None


@router.get("/api/commentary/bgm-preview/{cid}/{kind}")
def commentary_bgm_preview(cid: str, kind: str):
    """返回成片（cid）对三种内置 BGM 风格的试听 mp3（build() 时已落 sidecar）。

    kind ∈ {"soft", "light", "epic"}。返回 audio/mpeg 流；找不到 404。
    """
    if kind not in ("soft", "light", "epic"):
        raise app.HTTPException(status_code=400, detail="kind 必须为 soft/light/epic")
    p = app._decode_commentary_id(cid)
    if not p or not p.is_file():
        raise app.HTTPException(status_code=404, detail="成片不存在，无法提供试听")
    mp3 = _bgm_preview_mp3_path(p, kind)
    if not mp3:
        raise app.HTTPException(status_code=404,
                                detail=f"该成片缺少 {kind} 风格试听（可能是旧版渲染，请重新渲染）")
    return app.FileResponse(path=str(mp3), filename=f"{kind}.mp3", media_type="audio/mpeg")


@router.post("/api/commentary/voice-preview")
def voice_preview(
    voice: str = app.Form(...),
    text: str = app.Form("你好，我是视频解说员。我将为你解说这段视频。"),
    loudness: str = app.Form(None),
    boost: str = app.Form(None),
    provider: str = app.Form(""),
) -> app.FileResponse:
    """配音试听：把指定文本用指定 voice 转成 mp3 返回给前端播放。

    若带 loudness/boost（来自「配音与音量」面板的「试听当前设置」），生成的旁白会
    再做 ffmpeg 响度标准化 + 增益，使试听与成片响度一致；不传则保持纯 TTS（音色试听）。

    provider（2026-09-18 新增）：传 qwen3tts 时改走本机克隆引擎试听（用「我的音色」样本），
    此时 voice 允许不是 zh-* 音色（克隆与 edge 音色名无关），返回 wav。这样「试听听到的」
    才等于「成片会用的声音」——此前试听恒为 edge，用户永远验不出克隆有没有生效。
    """
    if not app.COMMENTARY_ENABLED:
        raise app.HTTPException(status_code=503, detail="该实例未启用解说功能")
    if app.COMMENTARY_MODE == "http":
        raise app.HTTPException(status_code=400, detail="配音试听需在 local 模式使用")
    # 1. 先校验输入（不等 COMMENTARY_DIR 挂掉）
    voice = (voice or "").strip()
    is_clone = provider.strip().lower() == "qwen3tts"
    if not is_clone and not voice.startswith("zh-"):
        raise app.HTTPException(status_code=400, detail=f"voice 必须是 zh-CN-* 音色，当前: {voice}")
    # FastAPI Form() 有 bug：空字符串会落回默认值（即使前端显式发 text=），所以加 fallback
    text = (text or "").strip()[:500] if text else ""
    if not text:
        text = "你好，我是视频解说员。我将为你解说这段视频。"
    # loudness/boost 校验（仅当显式提供时）
    if loudness not in (None, "", "off"):
        try:
            lv = float(loudness)
        except ValueError:
            raise app.HTTPException(status_code=400, detail=f"loudness 必须为数字（-18~-10）或 off，当前: {loudness}")
        if not (-18 <= lv <= -10):
            raise app.HTTPException(status_code=400, detail=f"loudness 超出范围（-18~-10），当前: {loudness}")
    if boost not in (None, ""):
        try:
            bv = float(boost)
        except ValueError:
            raise app.HTTPException(status_code=400, detail=f"boost 必须为数字（1.0~1.6），当前: {boost}")
        if not (1.0 <= bv <= 1.6):
            raise app.HTTPException(status_code=400, detail=f"boost 超出范围（1.0~1.6），当前: {boost}")
    # 2. 再检查资源可用性
    if not app.COMMENTARY_DIR or not (app.COMMENTARY_DIR / "scripts" / "voice_preview.py").exists():
        raise app.HTTPException(status_code=503, detail="解说管线未配置（VDL_COMMENTARY_DIR 缺失或不含 voice_preview.py）")

    out_dir = app._commentary_root("work") / "voice_preview"
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".wav" if is_clone else ".mp3"
    out_path = out_dir / f"{app.uuid.uuid4().hex[:12]}{suffix}"
    try:
        app._run_voice_preview(text, voice, out_path,
                               timeout=(900 if is_clone else 45),
                               loudness=loudness, boost=boost,
                               provider=("qwen3tts" if is_clone else ""))
    except RuntimeError as e:
        raise app.HTTPException(status_code=500, detail=f"试听生成失败：{e}")
    # 加过期清理保护：定时任务会清理 work/voice_preview/ 下超过 1 天的文件（用户本机 .cleanup）
    if is_clone:
        # 克隆引擎返回 wav（Qwen3-TTS 服务原始产物），浏览器 <audio> 直接可播
        return app.FileResponse(path=str(out_path), filename="preview.wav", media_type="audio/wav")
    return app.FileResponse(path=str(out_path), filename="preview.mp3", media_type="audio/mpeg")

@router.post("/api/commentary/preview/{job_id}")
def preview_segments(
    job_id: str,
    voice: str = app.Form(""),
    max_segments: int = app.Form(3),
) -> app.FileResponse:
    """用当前 voice 朗读 script.json 里前 N 段的 narration，拼接成一段 mp3 返回。
    主要给「预览全部」按钮用——按全脚本生成太长，前 3 段够判断音色和节奏。
    """
    if not app.COMMENTARY_ENABLED:
        raise app.HTTPException(status_code=503, detail="该实例未启用解说功能")
    if app.COMMENTARY_MODE == "http":
        raise app.HTTPException(status_code=400, detail="预览需在 local 模式使用")
    # 1. 先校验 job 状态（不等资源检查）
    with app._commentary_lock:
        job = app.commentary_jobs.get(job_id)
    if not job or job["status"] != "script_ready" or not job.get("script_path"):
        raise app.HTTPException(status_code=409, detail="请先生成脚本再预览（当前状态: " + (job or {}).get("status", "missing") + "）")
    script_path = app.Path(job["script_path"])
    if not script_path.exists():
        raise app.HTTPException(status_code=410, detail="脚本文件已被清理")
    try:
        data = app.json.loads(script_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise app.HTTPException(status_code=500, detail=f"读取脚本失败：{e}")
    segs = [s for s in (data.get("segments") or []) if (s.get("narration") or "").strip()]
    if not segs:
        raise app.HTTPException(status_code=409, detail="脚本里没有可朗读的 narration")
    n = max(1, min(int(max_segments), len(segs), 6))
    chosen = segs[:n]
    v = (voice or "").strip() or data.get("voice") or job.get("voice", "") or app.COMMENTARY_VOICE
    if not v.startswith("zh-"):
        raise app.HTTPException(status_code=400, detail=f"voice 必须是 zh-CN-* 音色")
    # 2. 再检查资源可用性
    if not app.COMMENTARY_DIR or not (app.COMMENTARY_DIR / "scripts" / "voice_preview.py").exists():
        raise app.HTTPException(status_code=503, detail="解说管线未配置（VDL_COMMENTARY_DIR 缺失或不含 voice_preview.py）")

    out_dir = app._commentary_root("work") / "voice_preview"
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = out_dir / app.uuid.uuid4().hex[:12]
    tmp_dir.mkdir(parents=True, exist_ok=True)
    final_mp3 = tmp_dir / "preview.mp3"
    # 逐段生成
    try:
        clips = []
        for i, seg in enumerate(chosen, 1):
            seg_mp3 = tmp_dir / f"seg{i:02d}.mp3"
            narration = (seg.get("narration") or "").strip()
            if not narration:
                continue
            try:
                app._run_voice_preview(narration, v, seg_mp3, timeout=45)
            except RuntimeError as e:
                raise RuntimeError(f"第 {i} 段朗读失败：{e}")
            clips.append(seg_mp3)
        if not clips:
            raise RuntimeError("没有可用 narration 可朗读")
        # 用 ffmpeg concat demuxer 拼接
        list_file = tmp_dir / "concat.txt"
        list_file.write_text("\n".join(f"file '{p.name}'" for p in clips), encoding="utf-8")
        concat_cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                      "-i", str(list_file), "-c", "copy", str(final_mp3)]
        proc = app.subprocess.run(concat_cmd, cwd=str(tmp_dir), capture_output=True, text=True, timeout=60,
                              env={"PATH": app.COMMENTARY_RT.ffmpeg_dir + app.os.pathsep + app.os.environ.get("PATH", "")})
        if proc.returncode != 0 or not final_mp3.exists():
            # concat 失败时退到单段：直接把第一段作为 preview（保证有声音）
            try:
                app.shutil.copyfile(clips[0], final_mp3)
            except Exception as e:
                raise RuntimeError(f"拼接失败且回退也失败：{e}; 原 stderr: {(proc.stderr or '')[:200]}")
    except RuntimeError as e:
        # 清理临时目录（保留 final_mp3 不存在路径下不会报错）
        try: app.shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception: pass
        raise app.HTTPException(status_code=500, detail=f"预览生成失败：{e}")
    # 返回临时文件，让客户端下载/播放
    return app.FileResponse(path=str(final_mp3), filename="preview.mp3", media_type="audio/mpeg")
