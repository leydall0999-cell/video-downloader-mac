"""P2 批次回归测试（安全审计 2026-10-11 的 🟡/🟢 级发现）。

覆盖：
  · P2-14 扫码分享 `upload_path` 接受任意绝对路径并外传公网短链
  · P2-15 文件变网页 `build` 未鉴权、未限路径（盲读 + 同目录写产物）
  · P2-16 aria2 JSON-RPC 无 rpc-secret 且开 rpc-allow-origin-all（任意写文件）
  · P2-17 CF 隧道下 nginx `$remote_addr` 为 127.0.0.1 ⇒ 全站限流被整体豁免
  · P2-18 `purge_expired` 把「下载超过 TTL 的进行中任务」就地删除；降级不复核状态
  · P2-19 `pause` 置非法态 `pausing`（不在 TaskStatus），合并阶段暂停被忽略后卡死
  · P2-21 证件照 make/render 未进登录门禁；文件变网页同样未进
  · （P2-22 前端 esc 转义由 node 守卫 test_esc_escaping.mjs 覆盖）

设计约束：临时目录 + monkeypatch，不联网、不碰真实家目录。
"""
import json
import os
import sys
import tempfile
import types
from pathlib import Path

_SERVER_DIR = str(Path(__file__).resolve().parent.parent)
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402
import tasks as tasks_mod  # noqa: E402
import torrent as torrent_mod  # noqa: E402
from routers import core as C  # noqa: E402


class _On:
    """显式指定补丁目标模块（值本身可能是 tuple，故不能用 (obj, val) 约定）。"""

    def __init__(self, mod, value):
        self.mod = mod
        self.value = value


class _patch:
    def __init__(self, **attrs):
        self._attrs = attrs
        self._orig = {}

    def __enter__(self):
        for k, v in self._attrs.items():
            obj = v.mod if isinstance(v, _On) else server_app
            val = v.value if isinstance(v, _On) else v
            self._orig[k] = (obj, k, getattr(obj, k))
            setattr(obj, k, val)
        return self

    def __exit__(self, *exc):
        for _k, (obj, name, old) in self._orig.items():
            setattr(obj, name, old)
        return False


def _tmpdir_prefix() -> tuple[Path, str]:
    """造一个位于系统临时区的目录，并返回可注入白名单的前缀。"""
    d = Path(tempfile.mkdtemp(prefix="vdl_p2_")).resolve()
    return d, str(d) + os.sep


def _err(exc) -> int:
    return int(getattr(exc, "status_code", 0))


# ============================================================ P2-14/15 路径护栏

def test_shareable_path_blocks_secrets_and_system_dirs():
    d, prefix = _tmpdir_prefix()
    normal = d / "video.mp4"
    normal.write_bytes(b"x")
    secret_dir = d / ".ssh"
    secret_dir.mkdir(parents=True, exist_ok=True)
    secret = secret_dir / "id_rsa"
    secret.write_text("PRIVATE KEY")
    keyfile = d / "server.pem"
    keyfile.write_text("KEY")

    with _patch(_SHAREABLE_PATH_PREFIXES=_On(server_app, (prefix,))):
        # 正常媒体文件：放行
        assert server_app._assert_shareable_local_path(str(normal)) == normal
        # 隐藏目录（.ssh 等）：拒绝
        for bad in (secret,):
            try:
                server_app._assert_shareable_local_path(str(bad))
                raise AssertionError(f"{bad} 不应被允许外传")
            except server_app.HTTPException as e:
                assert _err(e) == 403, e.status_code
        # 密钥类文件名：拒绝
        try:
            server_app._assert_shareable_local_path(str(keyfile))
            raise AssertionError("*.pem 不应被允许外传")
        except server_app.HTTPException as e:
            assert _err(e) == 403, e.status_code


def test_shareable_path_blocks_user_library_dirs():
    """验证轮补充（P3-2）：`~/Library/**`（钥匙串、浏览器 Cookies 库、应用容器）
    落在 /Users 白名单内且不是点目录，只按前缀拦 /Library/Keychains 会漏。"""
    d, prefix = _tmpdir_prefix()
    for rel in ("Library/Keychains/login.keychain-db",
                "Library/Application Support/Google/Chrome/Default/Cookies",
                "Library/Cookies/Cookies.binarycookies",
                "AppData/Roaming/Microsoft/Credentials/xyz"):
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("SECRET")
        with _patch(_SHAREABLE_PATH_PREFIXES=_On(server_app, (prefix,))):
            try:
                server_app._assert_shareable_local_path(str(p))
                raise AssertionError(f"{rel} 不应被允许外传")
            except server_app.HTTPException as e:
                assert _err(e) == 403, (rel, e.status_code)
    # 对照组：普通目录不受影响
    ok = d / "Movies" / "clip.mp4"
    ok.parent.mkdir(parents=True, exist_ok=True)
    ok.write_bytes(b"x")
    with _patch(_SHAREABLE_PATH_PREFIXES=_On(server_app, (prefix,))):
        assert server_app._assert_shareable_local_path(str(ok)) == ok


def test_shareable_path_blocks_outside_user_dirs():
    """不在白名单前缀内的路径（如 /etc/hosts）一律拒绝。"""
    try:
        server_app._assert_shareable_local_path("/etc/hosts")
        raise AssertionError("/etc/hosts 不应被允许")
    except server_app.HTTPException as e:
        assert _err(e) == 403, e.status_code


def test_shareable_path_rejects_missing_and_empty():
    for bad in ("", "   ", "/nonexistent/nope.mp4"):
        try:
            server_app._assert_shareable_local_path(bad)
            raise AssertionError(f"{bad!r} 不应通过")
        except server_app.HTTPException as e:
            assert _err(e) == 400, e.status_code


def test_share_upload_path_returns_403_for_secret():
    """端点级：走 share/upload_path 传密钥文件必须被拒（而不是默默上传）。"""
    from routers import share as share_router
    d, prefix = _tmpdir_prefix()
    secret_dir = d / ".ssh"
    secret_dir.mkdir(parents=True, exist_ok=True)
    secret = secret_dir / "id_rsa"
    secret.write_text("PRIVATE KEY")
    with _patch(_SHAREABLE_PATH_PREFIXES=_On(server_app, (prefix,))):
        resp = share_router.share_upload_path({"path": str(secret)})
    assert resp.status_code == 403, resp.status_code
    body = json.loads(bytes(resp.body).decode("utf-8"))
    assert body["error"] == "path_not_allowed", body


def test_pagetool_build_rejects_secret_path():
    from routers import pagetool
    d, prefix = _tmpdir_prefix()
    secret_dir = d / ".config"
    secret_dir.mkdir(parents=True, exist_ok=True)
    secret = secret_dir / "token"
    secret.write_text("TOKEN")
    with _patch(_SHAREABLE_PATH_PREFIXES=_On(server_app, (prefix,))):
        try:
            pagetool.build_from_paths(pagetool.BuildReq(paths=[str(secret)]))
            raise AssertionError("隐藏目录下的文件不应被渲染")
        except pagetool.HTTPException as e:
            assert _err(e) == 403, e.status_code


# ============================================================ P2-16 aria2 rpc-secret

def _capture_aria2_start():
    """启动一次 aria2 管理器，捕获命令行与第一次 RPC 的请求体。"""
    captured = {"args": None, "payload": None}

    class _Proc:
        def __init__(self, *a, **k):
            captured["args"] = list(a[0])

        def poll(self):
            return None

    def _fake_urlopen(req, timeout=None):
        captured["payload"] = json.loads(bytes(req.data).decode("utf-8"))

        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def read(self_inner):
                return b'{"jsonrpc":"2.0","id":"vdl","result":"ok"}'

        return _Resp()

    with _patch(_find_aria2c=_On(torrent_mod, lambda: "/usr/bin/aria2c")), \
         _patch(Popen=_On(torrent_mod.subprocess, _Proc)), \
         _patch(urlopen=_On(torrent_mod.urllib.request, _fake_urlopen)):
        mgr = torrent_mod.Aria2TorrentManager(Path(tempfile.mkdtemp(prefix="vdl_aria_")))
        mgr._start_daemon()
        mgr._rpc("aria2.tellStatus", ["gid1", []])
    return captured, mgr


def test_aria2_starts_with_rpc_secret_and_no_origin_all():
    captured, mgr = _capture_aria2_start()
    args = captured["args"]
    assert any(a.startswith("--rpc-secret=") for a in args), args
    assert not any("rpc-allow-origin-all" in a for a in args), args
    assert "--rpc-listen-all=false" in args, args
    secret = [a.split("=", 1)[1] for a in args if a.startswith("--rpc-secret=")][0]
    assert len(secret) >= 16, secret
    assert mgr._rpc_secret == secret


def test_aria2_rpc_carries_token_as_first_param():
    captured, mgr = _capture_aria2_start()
    params = captured["payload"]["params"]
    assert params[0] == "token:" + mgr._rpc_secret, params
    assert params[1:] == ["gid1", []], params


def test_aria2_secret_is_random_per_start():
    _, m1 = _capture_aria2_start()
    _, m2 = _capture_aria2_start()
    assert m1._rpc_secret and m2._rpc_secret
    assert m1._rpc_secret != m2._rpc_secret, "两次启动用了同一把密钥"


# ============================================================ P2-17 限流客户端 IP

def _req(headers=None, host="127.0.0.1"):
    return types.SimpleNamespace(headers=dict(headers or {}),
                                 client=types.SimpleNamespace(host=host))


def test_client_ip_prefers_cf_connecting_ip():
    ip = server_app._client_ip(_req({"cf-connecting-ip": "203.0.113.9",
                                     "x-real-ip": "127.0.0.1"}))
    assert ip == "203.0.113.9", ip


def test_client_ip_tunnel_case_skips_loopback_proxy_values():
    """CF 隧道：X-Real-IP 与 XFF 最右都是 127.0.0.1，必须回退到真实客户端。"""
    ip = server_app._client_ip(_req({"x-real-ip": "127.0.0.1",
                                     "x-forwarded-for": "198.51.100.7, 127.0.0.1"}))
    assert ip == "198.51.100.7", ip
    assert server_app._is_loopback_ip(ip) is False


def test_client_ip_desktop_direct_stays_loopback():
    """桌面 App 直连：无代理头、socket 即环回 → 仍按本机处理（限流豁免）。"""
    ip = server_app._client_ip(_req({}, host="127.0.0.1"))
    assert ip == "127.0.0.1"
    assert server_app._is_loopback_ip(ip) is True


def test_client_ip_x_real_ip_wins_over_spoofed_xff():
    """nginx 会覆盖 X-Real-IP：伪造 XFF 无法改变限流桶键。"""
    ip = server_app._client_ip(_req({"x-real-ip": "198.51.100.9",
                                     "x-forwarded-for": "1.2.3.4"}))
    assert ip == "198.51.100.9", ip


# ============================================================ P2-18 TTL 清理

def _mk_task(tstore, title, status, owner="d:aaa"):
    t = tstore.create(url="https://e.com/v", title=title, platform="bilibili",
                      quality="1080p", owner_id=owner)
    tstore.update(t.id, status=status)
    t.status = status
    t.created_at = 0.0            # 远古时间 → 超过任何 TTL
    return t


def test_purge_expired_never_touches_active_tasks():
    """进行中的任务（含 paused/pausing）无论多老都不得被清理。"""
    tmp = Path(tempfile.mkdtemp(prefix="vdl_p2_tasks_"))
    tstore = tasks_mod.TaskStore(tmp / "t", state_dir=tmp / "s")
    active = [_mk_task(tstore, s, s) for s in
              ("pending", "downloading", "merging", "paused", "pausing")]
    demoted = tstore.purge_expired(ttl=1)
    assert demoted == 0
    for t in active:
        assert tstore.get(t.id) is not None, f"{t.status} 任务被误删"
        assert t.workdir.exists(), f"{t.status} 任务的工作目录被误删"


def test_purge_expired_demotes_completed_and_removes_terminal():
    tmp = Path(tempfile.mkdtemp(prefix="vdl_p2_tasks_"))
    tstore = tasks_mod.TaskStore(tmp / "t", state_dir=tmp / "s")
    done = _mk_task(tstore, "done", "completed")
    failed = _mk_task(tstore, "failed", "failed")
    demoted = tstore.purge_expired(ttl=1)
    assert demoted == 1, demoted
    got = tstore.get(done.id)
    assert got is not None and got.file_expired is True, "到期成品应降级为历史条目"
    assert tstore.get(failed.id) is None, "失败任务（终态）应连记录一起清掉"


def test_demote_skips_task_no_longer_completed():
    """竞态：快照后被「重试」拉回进行中的任务，不得再降级（否则清掉在途分片）。"""
    tmp = Path(tempfile.mkdtemp(prefix="vdl_p2_tasks_"))
    tstore = tasks_mod.TaskStore(tmp / "t", state_dir=tmp / "s")
    t = _mk_task(tstore, "retried", "completed")
    part = t.workdir / "in.part"
    part.write_bytes(b"partial")
    # 模拟 purge_expired 取快照之后、真正降级之前用户点了「重试」
    tstore.update(t.id, status="pending")
    t.status = "pending"
    assert tstore._demote_completed(t.id) == 0, "状态已变仍执行了降级"
    assert t.file_expired is False
    assert part.exists(), "在途分片被清掉了"


# ============================================================ P2-19 pausing 状态

def test_pausing_is_a_declared_task_status():
    assert "pausing" in tasks_mod.TaskStatus.__args__, tasks_mod.TaskStatus.__args__


class _FakeTask:
    def __init__(self, status="pausing"):
        self.id = "t1"
        self.status = status
        self.pause_requested = False
        self.steps = []
        self.quality_key = "best"

    def add_step(self, *a, **k):
        pass

    def log(self, *a, **k):
        pass

    @property
    def is_finished(self):
        return self.status in ("completed", "failed", "canceled")


def test_resume_accepts_pausing_state():
    """合并阶段暂停不会被进度回调消费，任务会停在 pausing —— 必须能「继续」拉回。"""
    task = _FakeTask("pausing")
    calls = []
    with _patch(_require_owned_task=_On(server_app, lambda tid, req: task),
                _download_gate_error=_On(C, lambda req: None),
                store=_On(server_app, types.SimpleNamespace(update=lambda *a, **k: None)),
                scheduler=_On(server_app, types.SimpleNamespace(submit=lambda *a, **k: calls.append(a)))):
        r = C.resume_task("t1", _req())
    assert r.get("resumed") is True, r
    assert task.pause_requested is False
    assert calls, "未重新提交下载作业"


def test_pause_on_pausing_is_idempotent():
    task = _FakeTask("pausing")
    with _patch(_require_owned_task=_On(server_app, lambda tid, req: task)):
        r = C.pause_task("t1", _req())
    assert r.get("paused") is True and r.get("message") == "已暂停", r


def test_recovery_treats_pausing_as_interrupted():
    """重启恢复：pausing 必须按「中断」处理（failed + 可续传），不能原样留着。"""
    tmp = Path(tempfile.mkdtemp(prefix="vdl_p2_rec_"))
    root = tmp / "t"
    state = tmp / "s"
    tstore = tasks_mod.TaskStore(root, state_dir=state)
    tid = "a" * tasks_mod.TASK_ID_LENGTH
    (root / tid).mkdir(parents=True, exist_ok=True)
    (root / tid / "in.part").write_bytes(b"partial")
    (state / tasks_mod.STATE_FILENAME).write_text(json.dumps({
        "version": 1, "saved_at": 0, "tasks": [{
            "id": tid, "url": "https://e.com/v", "title": "t", "platform": "bilibili",
            "quality": "1080p", "quality_key": "best", "status": "pausing",
            "created_at": 1.0, "file_expired": False, "filepath": "",
        }],
    }), encoding="utf-8")
    fresh = tasks_mod.TaskStore(root, state_dir=state)
    got = fresh.get(tid)
    assert got is not None, "任务未被恢复"
    assert got.status == "failed", f"pausing 被原样恢复成 {got.status}（永久卡死）"
    assert got.resumable is True


# ============================================================ 2026-11-17 暂停/取消响应
def test_pausing_cancel_lands_immediately():
    """pausing 状态点取消必须立即落 canceled（用户实测「取消没反应」）。"""
    with tempfile.TemporaryDirectory(prefix="vdl_cancel_pausing_") as td:
        tstore = tasks_mod.TaskStore(Path(td) / "t", state_dir=Path(td) / "s")
        task = tstore.create(url="https://e.com/v", title="t", platform="e", quality="1080p")
        tstore.update(task.id, status="pausing")
        task._run_gen = 3
        assert tstore.request_cancel(task.id) is True
        got = tstore.get(task.id)
        assert got.status == "canceled", f"pausing 取消后仍停在 {got.status}"
        assert got.cancel_requested is True
        assert got._run_gen == 4, "代际未自增，孤儿线程醒来可能覆盖 canceled"


def test_downloading_cancel_defers_to_worker():
    """downloading 状态只置标志、终态仍由下载主循环落，不误伤正常取消流程。"""
    with tempfile.TemporaryDirectory(prefix="vdl_cancel_dl_") as td:
        tstore = tasks_mod.TaskStore(Path(td) / "t", state_dir=Path(td) / "s")
        task = tstore.create(url="https://e.com/v", title="t", platform="e", quality="1080p")
        tstore.update(task.id, status="downloading")
        assert tstore.request_cancel(task.id) is True
        got = tstore.get(task.id)
        assert got.cancel_requested is True
        assert got.status == "downloading", "downloading 取消不应被就地改终态"


def test_pause_force_land_default_is_five_seconds():
    """暂停兜底宽限默认 5s（2026-11-17 从 30s 收缩），防回退。"""
    src = (Path(_SERVER_DIR) / "downloader.py").read_text(encoding="utf-8")
    assert 'VDL_PAUSE_FORCE_LAND", "5"' in src, "暂停兜底宽限默认值不是 5s"


# ============================================================ P2-21 登录门禁

def test_idphoto_and_pagetool_are_login_gated():
    for path in ("/api/idphoto/make", "/api/idphoto/abc/render",
                 "/api/pagetool/build", "/api/pagetool/build-upload"):
        assert server_app._login_gated_path(path) is True, path


def test_frontend_gate_table_has_idphoto_button():
    app_js = (Path(_SERVER_DIR).parent / "web" / "app.js").read_text(encoding="utf-8")
    assert "idpBtn: '证件照制作'" in app_js, "证件照「生成」按钮未进前端门禁表"


def test_gate_does_not_cover_resolve_or_files():
    """回归：门禁不得重新把「前置步骤」纳入（用户实测要求）。"""
    for path in ("/api/resolve", "/api/fs/open"):
        assert server_app._login_gated_path(path) is False, path


if __name__ == "__main__":  # pragma: no cover
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception:
            failed += 1
            print("FAIL", fn.__name__)
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
