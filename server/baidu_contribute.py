#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""百度账号自动贡献共享池 —— 显式开关版（2026-10-30 定稿）。

合规形态（与用户确认的口径一致，替代被否决的「静默抓取/注册须知打包」方案）：
- 默认关闭；必须用户在网盘页显式开启，开启时前端弹窗明示三条：
  ① 共享的是百度账号的**完整访问权**（BDUSS=账号钥匙，可浏览/下载网盘文件）；
  ② 开启后这台机器浏览器里登录的百度账号会自动同步到共享池，供其他用户下载使用；
  ③ 可随时关闭；关闭后停止后续共享，已上云的快照随云端约 30 天过期失效。
- 开启后「像视频下载一样自动」：harvest 抓到新账号 -> 本机池 -> 自动上云。
- 贡献列表随时可见（只显示 BDUSS 哈希前缀，绝不回显明文），一键撤回。
- 凭据：内置「只写」贡献令牌 —— 云端对该令牌只放行 inject 动作且限定 baidu.com，
  不含读池/删池权限（云端 admin_cookies 同步校验）。泄露最多被人灌数据，
  云端验真 + 去重兜底。可被 ~/.videodownloader/cloud_sync.json 的
  contribution_url / contribution_token 字段或 VDL_BAIDU_CONTRIBUTION_URL/TOKEN 覆盖。
- 日志与状态文件绝不落 Cookie / 令牌明文。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# 内置默认：只写贡献令牌（占位符由发版脚本注入真实值，与云端 vdl-web 环境一致）
_DEFAULT_URL = "https://hanyuxz.top"
_DEFAULT_TOKEN = "__CONTRIB_TOKEN__"

_HOME = Path.home()
_CFG = _HOME / ".videodownloader" / "cloud_sync.json"
_STATE = _HOME / ".videodownloader" / "baidu_contribute.json"
_POOL = _HOME / ".videodownloader" / "cookie_pool" / "baidu.com.json"
_LOG = _HOME / ".videodownloader" / "baidu_contribute.log"

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _log(msg: str) -> None:
    """追加日志并裁到 200 行；只记计数/哈希，绝不记 Cookie 或令牌明文。"""
    try:
        with open(_LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " | " + msg + "\n")
        with open(_LOG) as f:
            lines = f.readlines()
        if len(lines) > 200:
            with open(_LOG, "w") as f:
                f.writelines(lines[-200:])
    except Exception:  # noqa: BLE001 — 日志失败不影响主流程
        pass


def contribution_cfg() -> tuple[str, str]:
    """返回 (贡献 URL, 只写贡献令牌)。优先级：环境变量 > 本机配置字段 > 内置默认。"""
    url = (os.environ.get("VDL_BAIDU_CONTRIBUTION_URL") or "").strip()
    token = (os.environ.get("VDL_BAIDU_CONTRIBUTION_TOKEN") or "").strip()
    if (not url or not token) and _CFG.exists():
        try:
            d = json.loads(_CFG.read_text(encoding="utf-8"))
            url = url or (d.get("contribution_url") or "").strip()
            token = token or (d.get("contribution_token") or "").strip()
        except Exception:  # noqa: BLE001
            pass
    tok = (token or _DEFAULT_TOKEN).strip()
    if not tok or tok.startswith("__CONTRIB"):
        # 未注入真实令牌的构建/开发环境：视为无凭据（push 跳过）。
        # 刻意用 startswith 而非「与占位符全等」的字面比较：发版注入是全局 sed 替换，
        # 字面比较会被一并换成真令牌，导致判断恒真、令牌被自己抹空（1.1.04 首版事故）。
        tok = ""
    return (url or _DEFAULT_URL).rstrip("/"), tok


def _load_state() -> dict[str, Any]:
    try:
        d = json.loads(_STATE.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return d
    except Exception:  # noqa: BLE001
        pass
    return {"enabled": False, "sig": "", "last_push": 0, "accounts": {}}


def _save_state(st: dict[str, Any]) -> None:
    try:
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        _STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(_STATE, 0o600)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass


def is_enabled() -> bool:
    return bool(_load_state().get("enabled"))


def set_enabled(v: bool) -> None:
    st = _load_state()
    st["enabled"] = bool(v)
    st["ts"] = int(time.time())
    if not v:
        st["revoked_at"] = st["ts"]
    _save_state(st)
    _log("enabled=%s" % bool(v))


def group_accounts() -> list[dict[str, Any]]:
    """读本机池 baidu.com.json，按 BDUSS 分组，每组取字段最全的一份快照。

    返回项只含 key（BDUSS sha256 前 12 位）/ member / parts / header / ts，
    任何对外出口（status）都必须剥掉 header。
    """
    try:
        cs = json.loads(_POOL.read_text(encoding="utf-8")).get("cookies", [])
    except Exception:  # noqa: BLE001 — 池不存在/损坏视为空
        return []
    by: dict[str, dict[str, Any]] = {}
    for c in cs if isinstance(cs, list) else []:
        h = (c.get("header") or "").strip()
        if not h:
            continue
        bd = None
        for seg in h.split(";"):
            s = seg.strip()
            if s.startswith("BDUSS="):
                bd = s.split("=", 1)[1]
                break
        if not bd:
            continue
        key = hashlib.sha256(bd.encode()).hexdigest()[:12]
        parts = len([x for x in h.split(";") if x.strip()])
        if key not in by or parts > by[key]["parts"]:
            by[key] = {"key": key, "member": bool(c.get("member")),
                       "parts": parts, "header": h, "ts": int(c.get("ts") or 0)}
    # 会员优先、字段数降序，仅用于推送顺序
    return sorted(by.values(), key=lambda x: (not x["member"], -x["parts"]))


def _post_inject(url: str, token: str, header: str) -> dict[str, Any]:
    """推一条到云端 pool-admin inject（贡献令牌：云端只放行该动作+百度域）。"""
    body = json.dumps({"action": "inject", "domain": "baidu.com",
                       "header": header, "token": token}).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/api/cookie/pool-admin", data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": _UA})
    last: Exception | None = None
    for _attempt in range(2):  # 网络抖动重试一次；4xx 不重试
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                o = json.loads(r.read().decode("utf-8", "replace") or "{}")
            return o if isinstance(o, dict) else {"ok": False, "error": "响应非对象"}
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                detail = ""
            return {"ok": False, "error": "HTTP %s %s" % (e.code, detail)}
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5)
    return {"ok": False, "error": type(last).__name__ if last else "未知错误"}


def push_accounts(force: bool = False) -> dict[str, Any]:
    """把本机全部百度账号推上云。账号集合（BDUSS）没变且上次全成功 -> 跳过。

    有失败不刷 sig，下一轮（下次 harvest / 重新开启）自动重试。
    """
    url, token = contribution_cfg()
    if not token:
        return {"pushed": 0, "failed": 0, "skipped": True, "reason": "无凭据"}
    accounts = group_accounts()
    if not accounts:
        return {"pushed": 0, "failed": 0, "skipped": True, "reason": "本机无百度登录态"}
    st = _load_state()
    sig = hashlib.sha256("|".join(sorted(a["key"] for a in accounts)).encode()).hexdigest()[:16]
    acc_state = st.get("accounts") if isinstance(st.get("accounts"), dict) else {}
    all_ok = all((acc_state.get(a["key"]) or {}).get("ok") for a in accounts)
    if not force and st.get("sig") == sig and all_ok:
        return {"pushed": 0, "failed": 0, "skipped": True, "reason": "账号集合未变化"}
    pushed = failed = 0
    now = int(time.time())
    for a in accounts:
        resp = _post_inject(url, token, a["header"])
        ok = bool(resp.get("ok"))
        acc_state[a["key"]] = {"ts": now, "ok": ok}
        if ok:
            pushed += 1
        else:
            failed += 1
        _log("push key=%s member=%s ok=%s%s" % (
            a["key"], a["member"], ok,
            "" if ok else " err=%s" % str(resp.get("error") or "")[:80]))
    if failed == 0:
        st["sig"] = sig
        st["last_push"] = now
    st["accounts"] = acc_state
    _save_state(st)
    return {"pushed": pushed, "failed": failed, "skipped": False}


def auto_push_if_enabled() -> None:
    """harvest 尾部挂钩：开关开着才推；任何异常静默（贡献是旁路，不碰主流程）。"""
    if not is_enabled():
        return
    try:
        r = push_accounts(force=False)
        if not r.get("skipped"):
            _log("auto push pushed=%s failed=%s" % (r.get("pushed"), r.get("failed")))
    except Exception as e:  # noqa: BLE001
        _log("auto push ERROR %s" % type(e).__name__)


def status() -> dict[str, Any]:
    """给前端/端点的只读状态：无任何 Cookie 明文，URL 只出主机名。"""
    st = _load_state()
    acc_state = st.get("accounts") if isinstance(st.get("accounts"), dict) else {}
    accounts = []
    for a in group_accounts():
        info = acc_state.get(a["key"]) or {}
        accounts.append({
            "key": a["key"], "member": a["member"], "parts": a["parts"],
            "pushed_ok": bool(info.get("ok")),
            "pushed_at": int(info.get("ts") or 0),
        })
    url, _tok = contribution_cfg()
    from urllib.parse import urlparse
    host = urlparse(url).netloc or url
    return {
        "enabled": bool(st.get("enabled")),
        "accounts": accounts,
        "last_push": int(st.get("last_push") or 0),
        "cloud_host": host,
    }


def revoke() -> dict[str, Any]:
    """一键撤回：停止后续自动共享（已上云快照随云端约 30 天 TTL 失效）。"""
    set_enabled(False)
    return status()
