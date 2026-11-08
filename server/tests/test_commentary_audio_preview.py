# -*- coding: utf-8 -*-
"""本地音乐试听路由守卫：静态路由必须先于 /api/commentary/{job_id} 注册。

根因：Starlette 按注册顺序匹配，`/api/commentary/{job_id}` 会把字面量
`audio-preview` 当成任务号吃掉，返回 404「解说任务不存在或已过期」，
前端 <audio> 拿到 JSON 后报「The operation is not supported.」。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SERVER = REPO / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

os.environ.setdefault("VDL_COMMENTARY_ENABLED", "false")

from fastapi.testclient import TestClient  # noqa: E402
import app as m  # noqa: E402

PASS = 0
FAIL = 0


def ok(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("  FAIL", label, extra)


def main():
    src = (SERVER / "routers/commentary.py").read_text(encoding="utf-8")
    a = src.index('@router.get("/api/commentary/audio-preview")')
    b = src.index('@router.get("/api/commentary/{job_id}")')
    ok("静态路由注册早于动态路由", a < b, f"static={a} dynamic={b}")

    client = TestClient(m.app)
    home = Path.home()
    tmp = Path(tempfile.mkdtemp(prefix="vdl_audio_preview_", dir=str(home)))
    p = tmp / "probe.mp3"
    blob = b"ID3" + b"\x00" * 128
    p.write_bytes(blob)
    try:
        r = client.get("/api/commentary/audio-preview", params={"path": str(p)})
        ok("真打接口返回 200", r.status_code == 200, f"got={r.status_code} body={r.text[:80]}")
        ok("不是「任务不存在」的 404", r.status_code != 404 or "解说任务" not in r.text)
        ok("Content-Type 为 audio/mpeg", "audio/mpeg" in r.headers.get("content-type", ""))
        ok("字节逐字一致", r.content == blob)

        for label, path, want in (
            ("相对路径被拦", "abc.mp3", 400),
            ("家目录外被拦", "/etc/hosts", 400),
            ("不存在的音频", str(tmp / "missing.mp3"), 404),
        ):
            rr = client.get("/api/commentary/audio-preview", params={"path": path})
            ok(label, rr.status_code == want, f"got={rr.status_code}")

        bad = tmp / "not_audio.txt"
        bad.write_text("x", encoding="utf-8")
        rr = client.get("/api/commentary/audio-preview", params={"path": str(bad)})
        ok("非音频扩展名被拦", rr.status_code == 400, f"got={rr.status_code}")

        openapi = client.get("/openapi.json").json()
        ok("openapi 仍登记该端点",
           "/api/commentary/audio-preview" in openapi.get("paths", {}))
    finally:
        for f in tmp.iterdir():
            try:
                f.unlink()
            except OSError:
                pass
        try:
            tmp.rmdir()
        except OSError:
            pass

    print(f"\n=== test_commentary_audio_preview.py: {PASS} 通过 / {FAIL} 失败 ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
