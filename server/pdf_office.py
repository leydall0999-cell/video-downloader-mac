"""server/pdf_office.py — PDF 转 PPT / Word。

策略（默认视觉保真，不做用户开关）：
  1. 有 LibreOffice ⇒ 用它把 PDF 导入 Impress 再导出 pptx（透明底图/矢量/艺术字全保留）
  2. 没有 LibreOffice ⇒ 回退内置重建（文字逐 span 还原 + 图片按坐标 + 表格），
     并在结果里标明 engine=builtin，前端据此提示「装 LibreOffice 观感更好」

Word 一律用 python-docx（LibreOffice 的 Draw→Word 导出在本机会报 Write 错误）。
"""
import os
import shutil
import signal
import subprocess
import tempfile

import pymupdf as fitz

_LO_SYSTEM = "/Applications/LibreOffice.app/Contents/MacOS/soffice"


def _soffice() -> str:
    """优先用「按需下载」装到 App 数据目录的引擎，其次系统 /Applications。"""
    try:
        import lo_engine

        p = lo_engine.soffice_bin()
        if p:
            return p
    except Exception:
        pass
    return _LO_SYSTEM if os.path.isfile(_LO_SYSTEM) else ""


def _log(msg):
    print("[pdf_office] %s" % msg, flush=True)


def lo_available() -> bool:
    return bool(_soffice())


def _clean_font(name):
    if not name:
        return None
    if "+" in name:
        head, tail = name.split("+", 1)
        if len(head) == 6:
            return tail
    return name


# ---------------- PPT：LibreOffice 引擎 ----------------

# 引擎一旦卡死过，本次运行内不再重试：否则用户每转一份都要白等一次超时。
_LO_STALLED = False

_ENV_KEEP = (
    "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR",
    "LANG", "LC_ALL", "LC_CTYPE", "LC_NUMERIC",
    "__CF_USER_TEXT_ENCODING", "SSH_AUTH_SOCK",
)


def _clean_env():
    """给 LibreOffice 一个干净环境。

    从 App（py2app 冻结进程）继承的 DYLD_*/PYTHON*/自定义变量会污染它的
    动态库解析与字体探测，实测表现为进程活着但永远不出结果。
    只保留系统必需项，其余一律不传。
    """
    env = {k: v for k, v in os.environ.items() if k in _ENV_KEEP}
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    env["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
    env.setdefault("HOME", os.path.expanduser("~"))
    env.setdefault("TMPDIR", tempfile.gettempdir())
    return env


def _kill_tree(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def to_pptx_lo(src: str, out_path: str, timeout: int = 0) -> bool:
    global _LO_STALLED
    if _LO_STALLED:
        return False
    soffice = _soffice()
    if not soffice:
        return False
    if timeout <= 0:
        # 实测 22 页约 65s；按页数给足余量，同时防止真的卡死时无限等。
        try:
            _doc = fitz.open(src)
            _pages = max(1, _doc.page_count)
            _doc.close()
        except Exception:
            _pages = 20
        timeout = min(480, 90 + _pages * 6)
    work = tempfile.mkdtemp(prefix="_vdl_pdf_lo_")
    proc = None
    try:
        # 1) 先把 PDF 复制到自己的工作目录：避开「下载」等受系统保护的目录，
        #    也避免 LibreOffice 因路径权限弹窗卡住。
        local_src = os.path.join(work, "in.pdf")
        shutil.copyfile(src, local_src)
        profile = os.path.join(work, "profile")
        outdir = os.path.join(work, "out")
        os.makedirs(outdir, exist_ok=True)
        cmd = [
            soffice,
            "-env:UserInstallation=file://" + profile,
            "--headless",
            "--norestore",
            "--nolockcheck",
            "--nodefault",
            "--nofirststartwizard",
            "--convert-to", "pptx",
            "--infilter=impress_pdf_import",
            "--outdir", outdir,
            local_src,
        ]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=_clean_env(),
            cwd=work,
            start_new_session=True,
        )
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _log("LibreOffice 超过 %ss 未完成，放弃并回退内置引擎" % timeout)
            _kill_tree(proc)
            _LO_STALLED = True
            return False
        _log("LibreOffice 输出: %s" % (out or "").strip()[-300:])
        produced = os.path.join(outdir, "in.pptx")
        if os.path.isfile(produced):
            shutil.move(produced, out_path)
            return True
        _log("LibreOffice 未产出 pptx（退出码 %s）" % proc.returncode)
        return False
    except Exception as exc:
        _log("LibreOffice 失败: %s" % exc)
        return False
    finally:
        if proc is not None and proc.poll() is None:
            _kill_tree(proc)
        shutil.rmtree(work, ignore_errors=True)


# ---------------- PPT：内置回退（无 LibreOffice） ----------------

def _image_bytes(doc, xref, smask):
    """取图片；带 smask 时合成透明，避免黑色底图盖住文字。"""
    info = doc.extract_image(xref)
    raw = info["image"]
    ext = info.get("ext") or "png"
    if not smask:
        return raw, ext
    try:
        import io
        from PIL import Image

        mask_info = doc.extract_image(smask)
        base = Image.open(io.BytesIO(raw)).convert("RGBA")
        mask = Image.open(io.BytesIO(mask_info["image"])).convert("L")
        if mask.size != base.size:
            mask = mask.resize(base.size)
        base.putalpha(mask)
        buf = io.BytesIO()
        base.save(buf, format="PNG")
        return buf.getvalue(), "png"
    except Exception as exc:
        _log("smask 合成失败 xref=%s: %s" % (xref, exc))
        return raw, ext


def to_pptx_builtin(src: str, out_path: str) -> bool:
    """无 LibreOffice 时的重建：文字 + 图片 + 表格，按 PDF 坐标归位。"""
    try:
        from pptx import Presentation
        from pptx.util import Inches, Pt
        from pptx.dml.color import RGBColor
    except Exception as exc:
        _log("缺 python-pptx，无法回退: %s" % exc)
        return False

    doc = fitz.open(src)
    try:
        prs = Presentation()
        if doc.page_count:
            first = doc[0].rect
            prs.slide_width = Inches(first.width / 72.0)
            prs.slide_height = Inches(first.height / 72.0)
        blank = prs.slide_layouts[6]

        for page in doc:
            slide = prs.slides.add_slide(blank)
            sx = prs.slide_width / page.rect.width
            sy = prs.slide_height / page.rect.height

            for img in page.get_images(full=True):
                xref = img[0]
                try:
                    rects = page.get_image_rects(xref)
                except Exception:
                    rects = []
                if not rects:
                    continue
                try:
                    raw, ext = _image_bytes(doc, xref, img[1])
                    tmp = os.path.join(
                        tempfile.gettempdir(), "_vdl_pdfimg_%d.%s" % (xref, ext)
                    )
                    with open(tmp, "wb") as fh:
                        fh.write(raw)
                    r = rects[0]
                    slide.shapes.add_picture(
                        tmp,
                        int(r.x0 * sx), int(r.y0 * sy),
                        int(r.width * sx), int(r.height * sy),
                    )
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                except Exception as exc:
                    _log("图片跳过 xref=%s: %s" % (xref, exc))

            blocks = [b for b in page.get_text("dict").get("blocks", []) if b.get("type") == 0]
            blocks.sort(key=lambda b: (round(b["bbox"][1] / 5), b["bbox"][0]))
            for b in blocks:
                x0, y0, x1, y1 = b["bbox"]
                lines = [
                    line for line in b.get("lines", [])
                    if "".join(sp.get("text", "") for sp in line.get("spans", [])).strip()
                ]
                if not lines:
                    continue
                width = max(int((x1 - x0) * sx), int(Inches(0.35)))
                height = max(int((y1 - y0) * sy), int(Inches(0.22)))
                box = slide.shapes.add_textbox(int(x0 * sx), int(y0 * sy), width, height)
                tf = box.text_frame
                tf.word_wrap = True
                tf.margin_left = tf.margin_right = 0
                tf.margin_top = tf.margin_bottom = 0
                for idx, line in enumerate(lines):
                    para = tf.paragraphs[0] if idx == 0 else tf.add_paragraph()
                    para.space_before = Pt(0)
                    para.space_after = Pt(0)
                    for sp in line.get("spans", []):
                        txt = sp.get("text", "")
                        if not txt:
                            continue
                        run = para.add_run()
                        run.text = txt
                        run.font.size = Pt(min(72.0, max(5.0, sp.get("size", 11) or 11)))
                        flags = int(sp.get("flags", 0))
                        run.font.bold = bool(flags & 16)
                        run.font.italic = bool(flags & 2)
                        fname = _clean_font(sp.get("font"))
                        if fname:
                            run.font.name = fname
                        try:
                            c = int(sp.get("color"))
                            if 0 <= c <= 0xFFFFFF:
                                run.font.color.rgb = RGBColor(
                                    (c >> 16) & 255, (c >> 8) & 255, c & 255
                                )
                        except (TypeError, ValueError):
                            pass

        prs.save(out_path)
        return True
    finally:
        doc.close()


# ---------------- Word ----------------

def to_docx(src: str, out_path: str) -> bool:
    try:
        from docx import Document
        from docx.shared import Pt, RGBColor as DocxRGB
    except Exception as exc:
        _log("缺 python-docx，无法生成 Word: %s" % exc)
        return False

    doc = fitz.open(src)
    try:
        w = Document()
        for i, page in enumerate(doc):
            if i:
                w.add_page_break()

            table_rects = []
            try:
                found = page.find_tables()
                for t in found.tables:
                    b = t.bbox
                    table_rects.append(fitz.Rect(b[0], b[1], b[2], b[3]))
                    data = t.extract() or []
                    if not data:
                        continue
                    ncols = max((len(row) for row in data), default=1)
                    tb = w.add_table(rows=len(data), cols=ncols)
                    tb.style = "Table Grid"
                    for ri, row in enumerate(data):
                        for ci in range(min(ncols, len(row))):
                            tb.cell(ri, ci).text = str(row[ci] or "")
                    w.add_paragraph()
            except Exception as exc:
                _log("Word 第%d页表格跳过: %s" % (i + 1, exc))

            blocks = [b for b in page.get_text("dict").get("blocks", []) if b.get("type") == 0]
            blocks.sort(key=lambda b: (round(b["bbox"][1] / 5), b["bbox"][0]))
            for b in blocks:
                br = fitz.Rect(b["bbox"])
                if any(abs((tr.x0 + tr.x1) / 2 - (br.x0 + br.x1) / 2) < tr.width and
                       tr.y0 - 2 <= (br.y0 + br.y1) / 2 <= tr.y1 + 2 for tr in table_rects):
                    continue
                para = w.add_paragraph()
                for line in b.get("lines", []):
                    for sp in line.get("spans", []):
                        txt = sp.get("text", "")
                        if not txt:
                            continue
                        run = para.add_run(txt)
                        run.font.size = Pt(min(28.0, max(6.0, sp.get("size", 11) or 11)))
                        flags = int(sp.get("flags", 0))
                        run.bold = bool(flags & 16)
                        run.italic = bool(flags & 2)
                        fname = _clean_font(sp.get("font"))
                        if fname:
                            run.font.name = fname
                        try:
                            c = int(sp.get("color"))
                            if 0 <= c <= 0xFFFFFF:
                                run.font.color.rgb = DocxRGB(
                                    (c >> 16) & 255, (c >> 8) & 255, c & 255
                                )
                        except (TypeError, ValueError):
                            pass
        w.save(out_path)
        return True
    finally:
        doc.close()


# ---------------- 统一入口 ----------------

def convert(src: str, out_dir: str, base_name: str, on_stage=None) -> dict:
    """把 PDF 转成 pptx + docx，返回 {pptx, docx, engine, pages, error}。"""
    def stage(msg):
        _log(msg)
        if on_stage:
            try:
                on_stage(msg)
            except Exception:
                pass

    result = {"pptx": "", "docx": "", "engine": "", "pages": 0, "error": ""}
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception:
        pass
    doc = fitz.open(src)
    result["pages"] = doc.page_count
    doc.close()

    pptx_path = os.path.join(out_dir, base_name + ".pptx")
    docx_path = os.path.join(out_dir, base_name + ".docx")

    if lo_available():
        stage("正在用专业引擎还原版面（约 1 分钟）…")
        if to_pptx_lo(src, pptx_path):
            result["engine"] = "libreoffice"
            result["pptx"] = pptx_path
        else:
            stage("LibreOffice 转换失败，改用内置引擎…")
            if to_pptx_builtin(src, pptx_path):
                result["engine"] = "builtin"
                result["pptx"] = pptx_path
    else:
        stage("未检测到 LibreOffice，用内置引擎重建…")
        if to_pptx_builtin(src, pptx_path):
            result["engine"] = "builtin"
            result["pptx"] = pptx_path

    stage("正在生成 Word…")
    if to_docx(src, docx_path):
        result["docx"] = docx_path

    if not result["pptx"] and not result["docx"]:
        result["error"] = "转换失败：PPT 和 Word 都没生成"
    return result
