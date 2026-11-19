"""server/routers/docconvert.py — PDF 转 PPT / Word（视频工坊「文档转换」页签）。

复用 app.CONVERT_JOBS / app.executor 的后台任务机制：
  上传或本机路径 → 建 job → 线程池跑 pdf_office.convert → 前端轮询 → 下载。
视觉保真优先走 LibreOffice（装了就用），没装回退内置重建。
"""
import os

import app
import lo_engine
import pdf_office
from fastapi import APIRouter

router = APIRouter()


class DocLocalRequest(app.BaseModel):
    local_path: str = ""


def _device_of(request) -> str:
    try:
        from .core import _device_of as _core_device_of
        return _core_device_of(request)
    except Exception:
        return ""


def _safe_local_pdf(path: str) -> app.Path:
    """本机 PDF 路径校验：必须存在、是 .pdf、在用户目录或外接盘下。"""
    try:
        p = app.Path(path).expanduser().resolve()
    except Exception:
        raise app.HTTPException(status_code=400, detail="路径无效")
    if not p.is_file():
        raise app.HTTPException(status_code=404, detail="找不到这个 PDF 文件")
    if p.suffix.lower() != ".pdf":
        raise app.HTTPException(status_code=400, detail="请选择 PDF 文件")
    home = str(app.Path.home())
    if not (str(p).startswith(home) or str(p).startswith("/Volumes")):
        raise app.HTTPException(status_code=403, detail="只能转换本机文件")
    return p


def _run_doc_convert(job_id: str, src_path: str, base: str, cleanup: bool = False):
    """后台线程：调用转换核心，把结果写回 job。"""
    def on_stage(msg: str):
        with app.CONVERT_LOCK:
            job = app.CONVERT_JOBS.get(job_id)
            if job is not None:
                job["stage"] = msg

    try:
        res = pdf_office.convert(
            src_path, str(app.CONVERT_DIR), base, on_stage=on_stage
        )
        with app.CONVERT_LOCK:
            job = app.CONVERT_JOBS.get(job_id)
            if job is None:
                return
            if res["error"]:
                job["status"] = "failed"
                job["error"] = res["error"]
                return
            job["status"] = "completed"
            job["out_pptx"] = res["pptx"]
            job["out_docx"] = res["docx"]
            job["engine"] = res["engine"]
            job["pages"] = res["pages"]
            job["filename"] = os.path.basename(res["pptx"] or res["docx"])
            job["stage"] = "完成"
            # 写 sidecar：内存任务重启/过期后，下载接口靠它从磁盘恢复原始文件名/页数/设备号。
            _write_sidecar(job_id, {
                "src_name": job.get("src_name", ""),
                "device_id": job.get("device_id", ""),
                "engine": res["engine"],
                "pages": res["pages"],
                "out_pptx": res["pptx"],
                "out_docx": res["docx"],
            })
    except Exception as exc:
        with app.CONVERT_LOCK:
            job = app.CONVERT_JOBS.get(job_id)
            if job is not None:
                job["status"] = "failed"
                job["error"] = str(exc)
    finally:
        if cleanup:
            try:
                os.remove(src_path)
            except OSError:
                pass


def _new_job(src_name: str, device_id: str) -> str:
    job_id = app.uuid.uuid4().hex[:12]
    with app.CONVERT_LOCK:
        app.CONVERT_JOBS[job_id] = {
            "status": "running",
            "stage": "排队中",
            "error": "",
            "out_pptx": "",
            "out_docx": "",
            "engine": "",
            "pages": 0,
            "filename": "",
            "src_name": src_name,
            "target": "doc",
            "device_id": device_id,
        }
    return job_id


def _sidecar_path(job_id: str):
    return app.CONVERT_DIR / ("doc_%s.json" % job_id)


def _write_sidecar(job_id: str, meta: dict):
    """转换成功后落一份磁盘元数据，供内存任务丢失后恢复下载。失败不影响转换本身。"""
    try:
        import json
        _sidecar_path(job_id).write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def _disk_lookup(job_id: str, device_id: str, kind: str):
    """内存任务查不到时，从磁盘按 job_id 找回产物路径。

    返回 (out_path, src_name) 或 None。要求 sidecar 存在且 device_id 匹配，
    避免跨设备/过期任务被任意下载。产物文件必须在 CONVERT_DIR 内。
    """
    import json
    sidecar = _sidecar_path(job_id)
    meta = {}
    if sidecar.is_file():
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    # 设备校验：sidecar 里有 device_id 时必须匹配（兼容老任务缺失该字段）
    want_dev = meta.get("device_id") or ""
    if want_dev and want_dev != (device_id or ""):
        return None
    key = "out_docx" if kind == "docx" else "out_pptx"
    ext = ".docx" if kind == "docx" else ".pptx"
    # 优先用 sidecar 记录的产物路径；没有 sidecar（历史任务）则按固定命名直接找。
    # 历史任务的 job_id 本身可能已带 doc_ 前缀（base="doc_%s"%job_id），故两种命名都试。
    candidates = []
    if meta.get(key):
        candidates.append(meta[key])
    stem = job_id if job_id.startswith("doc_") else ("doc_" + job_id)
    candidates += [str(app.CONVERT_DIR / (stem + ext)), str(app.CONVERT_DIR / ("doc_" + job_id + ext))]
    try:
        base = app.Path(app.CONVERT_DIR).resolve()
        for out in candidates:
            p = app.Path(out).resolve()
            if not str(p).startswith(str(base) + os.sep):
                continue
            if p.is_file():
                return str(p), meta.get("src_name", "")
    except Exception:
        pass
    return None


@router.post("/api/doc-convert")
def create_doc_convert(
    file: app.UploadFile = app._FastAPIFile(...),
    request: app.Request = None,
) -> dict:
    """上传 PDF → 转 PPT + Word。"""
    app._check_rate_limit(request)
    name = file.filename or "document.pdf"
    if not name.lower().endswith(".pdf"):
        raise app.HTTPException(status_code=400, detail="请上传 PDF 文件")

    save_path = app.UPLOAD_TMP / ("doc_%s.pdf" % app.uuid.uuid4().hex[:12])
    written = 0
    try:
        with save_path.open("wb") as fh:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > app.UPLOAD_MAX_BYTES:
                    fh.close()
                    save_path.unlink(missing_ok=True)
                    raise app.HTTPException(status_code=413, detail="文件超过上传大小上限")
                fh.write(chunk)
    except app.HTTPException:
        raise
    except Exception as exc:
        save_path.unlink(missing_ok=True)
        raise app.HTTPException(status_code=500, detail="保存上传文件失败：%s" % exc)
    finally:
        file.file.close()

    job_id = _new_job(name, _device_of(request))
    app.executor.submit(_run_doc_convert, job_id, str(save_path),
                        "doc_%s" % job_id, True)
    return {"job_id": job_id, "status": "running", "engine_hint": "libreoffice" if pdf_office.lo_available() else "builtin"}


@router.post("/api/doc-convert/local")
def create_doc_convert_local(
    payload: DocLocalRequest,
    request: app.Request = None,
) -> dict:
    """桌面端本机 PDF 路径直接转（免上传）。"""
    app._check_rate_limit(request)
    p = _safe_local_pdf(payload.local_path)
    job_id = _new_job(p.name, _device_of(request))
    app.executor.submit(_run_doc_convert, job_id, str(p), "doc_%s" % job_id, False)
    return {"job_id": job_id, "status": "running", "engine_hint": "libreoffice" if pdf_office.lo_available() else "builtin"}


@router.get("/api/doc-convert/{job_id}")
def doc_convert_status(job_id: str, request: app.Request) -> dict:
    dev = _device_of(request)
    job = app.CONVERT_JOBS.get(job_id)
    if not job:
        # 内存任务丢失：从磁盘恢复一个"已完成"状态，前端可重新显示下载按钮
        sidecar = _sidecar_path(job_id)
        meta = {}
        if sidecar.is_file():
            try:
                import json
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        if (meta.get("device_id") or "") and meta["device_id"] != dev:
            raise app.HTTPException(status_code=404, detail="转换任务不存在")
        # 有 sidecar 用记录路径；历史任务无 sidecar 则按固定命名探测（job_id 可能已带 doc_ 前缀）
        stem = job_id if job_id.startswith("doc_") else ("doc_" + job_id)
        def _existing(meta_key, ext):
            for p in [meta.get(meta_key), str(app.CONVERT_DIR / (stem + ext)),
                      str(app.CONVERT_DIR / ("doc_" + job_id + ext))]:
                if p and app.Path(p).is_file():
                    return p
            return ""
        pptx_ok = bool(_existing("out_pptx", ".pptx"))
        docx_ok = bool(_existing("out_docx", ".docx"))
        if not pptx_ok and not docx_ok:
            raise app.HTTPException(status_code=404, detail="转换任务不存在")
        return {
            "status": "completed", "stage": "完成", "error": "",
            "engine": meta.get("engine", ""), "pages": meta.get("pages", 0),
            "src_name": meta.get("src_name", ""),
            "pptx_ready": pptx_ok, "docx_ready": docx_ok,
        }
    if job.get("device_id") and job["device_id"] != dev:
        raise app.HTTPException(status_code=404, detail="转换任务不存在")
    return {
        "status": job["status"],
        "stage": job.get("stage", ""),
        "error": job.get("error", ""),
        "engine": job.get("engine", ""),
        "pages": job.get("pages", 0),
        "src_name": job.get("src_name", ""),
        "pptx_ready": bool(job.get("out_pptx")),
        "docx_ready": bool(job.get("out_docx")),
    }


@router.get("/api/doc-convert/{job_id}/file")
def doc_convert_file(job_id: str, kind: str = "pptx", request: app.Request = None) -> app.FileResponse:
    kind = (kind or "pptx").lower()
    dev = _device_of(request)
    job = app.CONVERT_JOBS.get(job_id)

    # 路径 A：内存任务在（正常刚转换完）
    if job:
        if job.get("device_id") and job["device_id"] != dev:
            raise app.HTTPException(status_code=404, detail="转换任务不存在")
        if job["status"] != "completed":
            raise app.HTTPException(status_code=409, detail="转换尚未完成")
        out = job.get("out_docx") if kind == "docx" else job.get("out_pptx")
        src_name = job.get("src_name", "")
        if not out or not app.Path(out).exists():
            # 内存记录在但文件没了，再试磁盘回退
            disk = _disk_lookup(job_id, dev, kind)
            if not disk:
                raise app.HTTPException(status_code=410, detail="文件已清理或未生成")
            out, src_name = disk
    else:
        # 路径 B：内存任务丢了（App 重启 / 任务过期），从磁盘按 job_id 恢复
        disk = _disk_lookup(job_id, dev, kind)
        if not disk:
            raise app.HTTPException(status_code=404, detail="转换任务不存在或已过期")
        out, src_name = disk

    src_stem = app.Path(src_name).stem or "document"
    ext = ".docx" if kind == "docx" else ".pptx"
    return app.FileResponse(
        path=str(out),
        filename=src_stem + ext,
        media_type="application/octet-stream",
    )


@router.get("/api/doc-engine/status")
def doc_engine_status(request: app.Request = None) -> dict:
    """专业引擎（LibreOffice）是否就绪 / 下载进度。"""
    return lo_engine.status()


@router.post("/api/doc-engine/install")
def doc_engine_install(request: app.Request = None) -> dict:
    """按需下载并安装专业引擎（后台线程；轮询 status 看进度）。"""
    started = lo_engine.install_async()
    data = lo_engine.status()
    data["started"] = started
    return data
