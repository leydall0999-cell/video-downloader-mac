"""server/routers/bgm_cloud.py — 在线配乐曲库（桌面端）。

链路：系统后台上传曲子 → ECS 静态目录 hanyuxz.top/bgm/ → 本机按需下载 →
复用既有 bgm=user + bgm_file 本地绝对路径渲染（edit_ffmpeg._mux_bgm）。

安全边界：
  · 清单与音频的域名/前缀全部硬编码，客户端只传曲目 id，绝不接受任意 URL（防 SSRF）；
  · 文件名必须是 16 位十六进制 + 音频白名单后缀；
  · 落盘前校验 sha256 与后台清单一致、30MB 上限、MP3 魔数；
  · 清单与音频都落在 ~/.video-downloader/bgm/（避开 macOS TCC 保护目录）；
  · 拉不到云端时回退本机缓存清单，已下载的曲子断网仍可试听/使用。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException

router = APIRouter()

BGM_LIBRARY_URL = "https://hanyuxz.top/bgm/library.json"
BGM_BASE_URL = "https://hanyuxz.top/bgm/"
BGM_MAX_BYTES = 30 * 1024 * 1024
BGM_TIMEOUT = 20.0
BGM_FILE_RE = re.compile(r"^[0-9a-f]{16}\.(?:mp3|m4a|wav|aac|flac|ogg)$")
BGM_ID_RE = re.compile(r"^[0-9a-f]{16}$")
BGM_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_BGM_LOCK = threading.RLock()


def _bgm_dir() -> Path:
    """曲库落盘目录。放在应用数据目录下，启动路径不碰 TCC 保护目录。"""
    d = Path.home() / ".video-downloader" / "bgm"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _library_path() -> Path:
    return _bgm_dir() / "library.json"


def _read_cached_library() -> dict:
    try:
        data = json.loads(_library_path().read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("tracks"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"version": 1, "base_url": BGM_BASE_URL, "updated_at": "", "tracks": []}


def _save_cached_library(lib: dict) -> None:
    p = _library_path()
    tmp = p.with_name(".library.json.tmp")
    tmp.write_text(json.dumps(lib, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(p))


def _fetch_url(url: str, timeout: float = BGM_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "VDL-BGM/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(BGM_MAX_BYTES + 1)


def _normalize_tracks(lib: dict) -> list[dict]:
    """只保留可安全落盘的条目：不合规文件名直接丢弃，不给路径穿越留口子。"""
    out: list[dict] = []
    for t in lib.get("tracks", []):
        if not isinstance(t, dict):
            continue
        fname = str(t.get("file") or "")
        if not BGM_FILE_RE.match(fname):
            continue
        try:
            size = int(t.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        out.append({
            "id": str(t.get("id") or ""),
            "name": str(t.get("name") or fname)[:60],
            "category": str(t.get("category") or "")[:30],
            "file": fname,
            "size": size,
            "duration": t.get("duration"),
            "sha256": str(t.get("sha256") or ""),
            "license": str(t.get("license") or "")[:200],
        })
    return out


def _local_state(track: dict) -> tuple[bool, str]:
    """本地已有且 sha256 吻合 → (True, 绝对路径)。不一致视为未下载（重下会覆盖）。"""
    p = _bgm_dir() / track["file"]
    expected = track.get("sha256") or ""
    if not p.is_file() or not BGM_SHA_RE.match(expected):
        return False, ""
    try:
        if p.stat().st_size > BGM_MAX_BYTES:
            return False, ""
        if hashlib.sha256(p.read_bytes()).hexdigest() != expected:
            return False, ""
    except OSError:
        return False, ""
    return True, str(p)


def _decorate(lib: dict) -> list[dict]:
    rows = []
    for t in _normalize_tracks(lib):
        local, path = _local_state(t)
        row = dict(t)
        row["url"] = BGM_BASE_URL + t["file"]      # 云端试听直链（audio 标签可跨域播放）
        row["local"] = local
        row["path"] = path
        rows.append(row)
    return rows


@router.get("/api/commentary/bgm/library")
def bgm_library() -> dict[str, Any]:
    """读云端曲库清单；失败时回退本机缓存，保证断网也能列出已下载的曲子。"""
    lib = _read_cached_library()
    offline = False
    try:
        data = json.loads(_fetch_url(BGM_LIBRARY_URL, timeout=8.0).decode("utf-8"))
        if isinstance(data, dict) and isinstance(data.get("tracks"), list):
            lib = {
                "version": data.get("version", 1),
                "base_url": BGM_BASE_URL,
                "updated_at": str(data.get("updated_at") or ""),
                "tracks": data["tracks"],
            }
            with _BGM_LOCK:
                _save_cached_library(lib)
    except Exception:  # noqa: BLE001 — 网络/解析任一失败都退回缓存
        offline = True
    return {
        "ok": True,
        "offline": offline,
        "base_url": BGM_BASE_URL,
        "updated_at": str(lib.get("updated_at") or ""),
        "tracks": _decorate(lib),
    }


@router.post("/api/commentary/bgm/fetch")
def bgm_fetch(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """按 id 下载云端音频到本机，返回可直接喂给 bgm_file 的绝对路径。"""
    tid = str((payload or {}).get("id") or "")
    if not BGM_ID_RE.match(tid):
        raise HTTPException(status_code=400, detail="曲目 id 不合法")
    lib = _read_cached_library()
    track = next((t for t in _normalize_tracks(lib) if t["id"] == tid), None)
    if not track:
        raise HTTPException(status_code=404, detail="曲库里没有这首曲子，请先刷新曲库")
    cached, path = _local_state(track)
    if cached:
        return {"ok": True, "cached": True, "path": path,
                "name": track["name"], "sha256": track["sha256"]}

    expected = track["sha256"]
    if not BGM_SHA_RE.match(expected):
        raise HTTPException(status_code=409, detail="曲库清单缺少校验值，拒绝下载")
    url = BGM_BASE_URL + track["file"]
    try:
        blob = _fetch_url(url, timeout=BGM_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"下载失败：{exc}") from exc
    if not blob or len(blob) > BGM_MAX_BYTES:
        raise HTTPException(status_code=413, detail="音频为空或超过 30MB 上限")
    if hashlib.sha256(blob).hexdigest() != expected:
        raise HTTPException(status_code=409, detail="音频校验不通过（云端文件已变化），请刷新曲库")
    if track["file"].endswith(".mp3") and not (
        blob[:3] == b"ID3" or blob[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")
    ):
        raise HTTPException(status_code=409, detail="这不是有效的 MP3 文件")

    d = _bgm_dir()
    target = d / track["file"]
    with _BGM_LOCK:
        tmp = d / ("." + track["file"] + ".tmp")
        tmp.write_bytes(blob)
        os.replace(str(tmp), str(target))       # 原子落盘，杜绝半截文件
    return {"ok": True, "cached": False, "path": str(target),
            "name": track["name"], "sha256": expected}
