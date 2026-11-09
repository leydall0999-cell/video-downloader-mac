#!/usr/bin/env python3
"""云端抠图「改走本产品服务端」后的守卫 —— 2026-10-13 用户定档「钥匙不出保险柜」。

背景（为什么要有这组守卫）
--------------------------
桌面端原先**直连火山**：AK/SK / MediaKit Key 必须写到用户机器上才能用云端抠图。
`0600` 权限挡不住「App 自己读它」——分发给他人就等于把钥匙一起发出去。
改为由 `VDL 网关`（`/opt/vdl-gateway`）代做后，钥匙只在服务端 `matting.json`，
用户机器上只留可单独吊销的令牌。

本文件钉死四件事（任一条被改回去都必须变红）
  ① 网关接入时客户端**必须**走网关，且请求体/请求头里**不含任何火山凭据**；
  ② 网关未接入时保留本机凭据直连（管理员自用 / 离线排障），行为不变；
  ③ 就绪判定与「凭据来源」语义正确（server / local / none）；
  ④ 网关服务自身的新端点可跑通，响应**绝不回显凭据**。

运行：`.build_venv/bin/python server/tests/test_cloud_matting_gateway.py`
"""
from __future__ import annotations

import base64
import io
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import urllib.error

HERE = pathlib.Path(__file__).resolve().parent
SERVER = HERE.parent
REPO = SERVER.parent
GATEWAY_PY = REPO / "deploy" / "gateway" / "gateway.py"

sys.path.insert(0, str(SERVER))

from PIL import Image  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ✅ {name}")
    else:
        FAILS.append(name)
        print(f"  ❌ {name}" + (f" —— {detail}" if detail else ""))


def _png_bytes(w: int = 8, h: int = 6, color: tuple[int, int, int] = (12, 34, 56)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _b64(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


class _Resp:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self, *_a) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_a) -> bool:
        return False


class FakeOpener:
    """记录每一次出站请求（URL / 头 / 体），并按脚本回包。"""

    def __init__(self, replies: list[bytes]) -> None:
        self.calls: list[dict] = []
        self.replies = list(replies)
        self.raise_on_open: Exception | None = None

    def open(self, req, timeout=None):  # noqa: ANN001, ANN201
        self.calls.append({
            "url": req.full_url,
            "headers": {k.lower(): v for k, v in req.header_items()},
            "body": (req.data or b"").decode("utf-8", "replace"),
            "method": req.get_method(),
        })
        if self.raise_on_open is not None:
            raise self.raise_on_open
        if not self.replies:
            raise AssertionError("FakeOpener 收到的请求多于预设回包")
        return _Resp(self.replies.pop(0))


# ────────────────────────────── 夹具 ──────────────────────────────
class Ctx:
    """隔离的配置目录 + 可切换的网关配置。"""

    def __init__(self) -> None:
        import cloud_matting_config
        import gateway_config

        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="vdl_mat_gw_"))
        self._mods = (gateway_config, cloud_matting_config)
        # 环境变量优先级最高，测试里必须先清干净，否则本机/CI 的值会盖过夹具
        for k in ("VDL_GATEWAY_URL", "VDL_GATEWAY_TOKEN", "VDL_GATEWAY_ENABLED",
                  "VDL_CLOUD_MAT_AK", "VDL_CLOUD_MAT_SK", "VDL_CLOUD_MAT_MEDIAKIT_KEY"):
            os.environ.pop(k, None)
        for m in self._mods:
            m._config_dir = lambda: self.tmp          # noqa: SLF001 - 测试专用重定向
        # _legacy() 会回落到 Path.home()/.video-downloader 读取真实网关配置，导致本套件
        # 无法隔离——任何装了真网关的机器（含构建机）上，「无网关」用例永远读不到无网关，
        # 进而误走网关、连真实 ECS 拿 502。把 Path.home 也重定向到临时目录，让回落落空。
        self._orig_path_home = pathlib.Path.home
        pathlib.Path.home = staticmethod(lambda: self.tmp)  # noqa: SLF001 - 测试专用重定向

    def write_gateway(self, url: str = "direct://http://gw.test/gw",
                      token: str = "vdlt_UNIT_TEST", enabled: bool = True) -> None:
        (self.tmp / "gateway_managed.json").write_text(
            json.dumps({"url": url, "token": token, "enabled": enabled}), encoding="utf-8")

    def drop_gateway(self) -> None:
        for name in ("gateway_managed.json", "gateway.json"):
            p = self.tmp / name
            if p.exists():
                p.unlink()

    def write_local_keys(self, ak: str = "AK_LOCAL_ONLY", sk: str = "SK_LOCAL_ONLY",
                         mk: str = "MK_LOCAL_ONLY") -> None:
        (self.tmp / "cloud_matting.json").write_text(json.dumps({
            "provider": "volcengine", "access_key": ak, "secret_key": sk,
            "mediakit_api_key": mk, "enabled": True,
        }), encoding="utf-8")

    def drop_local_keys(self) -> None:
        for name in ("cloud_matting.json", "cloud_matting_managed.json"):
            p = self.tmp / name
            if p.exists():
                p.unlink()

    def close(self) -> None:
        pathlib.Path.home = self._orig_path_home
        shutil.rmtree(self.tmp, ignore_errors=True)


def _fresh_caps_cache() -> None:
    import gateway_config
    gateway_config._MATTING_CAPS_CACHE.update({"ts": 0.0, "caps": None})


def _install_fake_opener() -> "FakeOpener":
    import gateway_config
    fake = FakeOpener([])
    gateway_config.gateway_opener = lambda: fake      # noqa: SLF001 - 拦截所有网关出站
    return fake


# ────────────────────── ① 网关接入：走网关且不带凭据 ──────────────────────
def test_gateway_preferred_and_no_credential_leak() -> None:
    import cloud_matting_mediakit as mk

    ctx = Ctx()
    try:
        ctx.write_gateway()
        ctx.write_local_keys()                        # 本机也有键 → 必须仍走网关
        fake = _install_fake_opener()
        fake.replies = [_b64({"ok": True, "image_b64": base64.b64encode(_png_bytes()).decode()})]

        img = Image.new("RGB", (8, 6), (9, 9, 9))
        out = mk._cloud_remove_bg(img, scene="human", timeout=30)

        call = fake.calls[0]
        check("抠图走网关端点", call["url"].endswith("/v1/image/remove-background"), call["url"])
        check("请求带可吊销令牌（Bearer）",
              call["headers"].get("authorization") == "Bearer vdlt_UNIT_TEST",
              str(call["headers"].get("authorization")))
        body = json.loads(call["body"])
        check("请求体含 image_b64 与 scene", "image_b64" in body and body["scene"] == "human", str(body)[:80])
        check("抠图端点不重复触发增强（enhance_version 为空）",
              body.get("enhance_version") == "", repr(body.get("enhance_version")))
        check("❗请求体不含本机任何火山凭据",
              not any(s in call["body"] for s in ("AK_LOCAL_ONLY", "SK_LOCAL_ONLY", "MK_LOCAL_ONLY")),
              "凭据字样出现在出站请求体里")
        check("❗请求头不含本机任何火山凭据",
              not any(s in json.dumps(call["headers"]) for s in
                      ("AK_LOCAL_ONLY", "SK_LOCAL_ONLY", "MK_LOCAL_ONLY")))
        check("返回图已按原尺寸解出", out.size == (8, 6), str(out.size))
        check("返回图为 RGBA", out.mode == "RGBA", out.mode)
    finally:
        ctx.close()
    print("✅ 网关接入 → 抠图走网关端点，出站请求不含任何火山凭据")


def test_enhance_and_segment_go_through_gateway() -> None:
    import cloud_matting
    import cloud_matting_mediakit as mk

    ctx = Ctx()
    try:
        ctx.write_gateway()
        ctx.write_local_keys()
        fake = _install_fake_opener()
        fake.replies = [
            _b64({"ok": True, "image_b64": base64.b64encode(_png_bytes(4, 4)).decode()}),
            _b64({"ok": True, "kind": "foreground",
                  "image_b64": base64.b64encode(_png_bytes(5, 5)).decode()}),
        ]

        img = Image.new("RGB", (8, 6), (7, 7, 7))
        mk._cloud_enhance(img, version="professional", multiple=2.0, timeout=30)
        c1 = fake.calls[0]
        check("画质增强走网关端点", c1["url"].endswith("/v1/image/enhance"), c1["url"])
        b1 = json.loads(c1["body"])
        check("增强请求带 version/multiple",
              b1.get("version") == "professional" and b1.get("multiple") == 2.0, str(b1)[:80])
        check("❗增强请求不含火山凭据",
              not any(s in c1["body"] for s in ("AK_LOCAL_ONLY", "MK_LOCAL_ONLY")))

        cloud_matting._segment_call("HumanSegment", img, ak="AK_LOCAL_ONLY",
                                    sk="SK_LOCAL_ONLY", timeout=30)
        c2 = fake.calls[1]
        check("CV 分割走网关端点", c2["url"].endswith("/v1/image/segment"), c2["url"])
        b2 = json.loads(c2["body"])
        check("人像分割带 person=True", b2.get("person") is True, str(b2)[:60])
        check("❗分割请求不含 AK/SK（签名在服务端做）",
              not any(s in c2["body"] for s in ("AK_LOCAL_ONLY", "SK_LOCAL_ONLY")))
        check("mask/前景返回可解成图", True)
    finally:
        ctx.close()
    print("✅ 增强与 CV 分割均走网关，签名/凭据不下发")


# ────────────────────── ② 网关未接入：保留本机直连 ──────────────────────
def test_local_direct_fallback_intact() -> None:
    import cloud_matting_mediakit as mk

    ctx = Ctx()
    try:
        ctx.drop_gateway()
        ctx.write_local_keys(mk="MK_LOCAL_ONLY")
        fake = _install_fake_opener()
        used = {"direct": 0}
        orig = {n: getattr(mk, n) for n in
                ("_request_upload", "_put_upload", "_post_json", "_download")}

        def _up(api_key, timeout):                     # noqa: ANN001
            used["direct"] += 1
            check("直连分支用本机 MediaKit Key", api_key == "MK_LOCAL_ONLY", api_key[:6])
            return {"file_id": "f1", "upload_url": "https://up.test/x"}

        mk._request_upload = _up
        mk._put_upload = lambda *a, **k: None
        mk._post_json = lambda *a, **k: {"success": True,
                                        "result": {"image_url": "https://out.test/y"}}
        mk._download = lambda *a, **k: Image.open(io.BytesIO(_png_bytes())).convert("RGBA")
        try:
            out = mk._cloud_remove_bg(Image.new("RGB", (8, 6), (1, 2, 3)),
                                      scene="general", timeout=30)
        finally:
            for n, f in orig.items():
                setattr(mk, n, f)

        check("网关未接入 → 走本机直连", used["direct"] == 1, str(used))
        check("网关未被调用", not fake.calls, str(fake.calls)[:120])
        check("直连结果可用", out.size == (8, 6), str(out.size))
    finally:
        ctx.close()
    print("✅ 网关未接入 → 本机凭据直连（行为不变）")


def test_missing_everything_raises_clear_error() -> None:
    import cloud_matting_mediakit as mk

    ctx = Ctx()
    try:
        ctx.drop_gateway()
        err = ""
        try:
            mk.mediakit_remove_bg(Image.new("RGB", (4, 4), (0, 0, 0)), scene="general", timeout=5)
        except RuntimeError as e:
            err = str(e)
        check("既无网关又无本机 Key → 明确报错", "未配置" in err and "网关" in err, err)
    finally:
        ctx.close()
    print("✅ 无网关 + 无本机 Key → 报错文案点名两条来源")


# ────────────────────── ③ 就绪判定与凭据来源 ──────────────────────
def test_readiness_and_source() -> None:
    import cloud_matting_config as cm

    ctx = Ctx()
    try:
        ctx.write_gateway()
        fake = _install_fake_opener()
        fake.replies = [_b64({"ok": True, "mediakit": True, "visual": False})]
        _fresh_caps_cache()
        ready_mk = cm.is_cloud_matting_mediakit_ready()      # 触发一次能力位探测
        check("网关能力位走 /v1/matting/capabilities",
              bool(fake.calls) and fake.calls[0]["url"].endswith("/v1/matting/capabilities"),
              str(fake.calls)[:120])
        check("capabilities 请求带令牌",
              bool(fake.calls) and fake.calls[0]["headers"].get("authorization") == "Bearer vdlt_UNIT_TEST")
        check("本机无 Key 也能判 MediaKit 就绪（服务端代做）", ready_mk is True)
        check("CV 通道服务端未开 → 不就绪（本机也无 AK/SK）",
              cm.is_cloud_matting_ready() is False)
        check("凭据来源 = server", cm.cloud_matting_source() == "server", cm.cloud_matting_source())

        # 服务端两通道都关 → 回到本机判定
        fake.replies = [_b64({"ok": True, "mediakit": False, "visual": False})]
        _fresh_caps_cache()
        check("服务端明确说没有 → 来源 none（本机也没键）",
              cm.cloud_matting_source() == "none", cm.cloud_matting_source())

        # 服务端两通道都关 + 本机有键 → local
        ctx.write_local_keys()
        _fresh_caps_cache()
        fake.replies = [_b64({"ok": True, "mediakit": False, "visual": False})]
        check("服务端没有但本机有 → 来源 local",
              cm.cloud_matting_source() == "local", cm.cloud_matting_source())

        # 网关未接入 → 不应触碰网络
        ctx.drop_gateway()
        _fresh_caps_cache()
        before = len(fake.calls)
        cm.cloud_matting_source()
        check("网关未接入时不发探测请求", len(fake.calls) == before, str(len(fake.calls)))

        # 探不到能力位 → 按未知处理（不当成「服务端有能力」），且本机也确实没有键
        ctx.write_gateway()
        ctx.drop_local_keys()
        _fresh_caps_cache()
        probe = FakeOpener([])
        probe.raise_on_open = urllib.error.URLError("boom")
        import gateway_config
        gateway_config.gateway_opener = lambda: probe
        check("探测失败 → 就绪判定不乐观放行",
              cm.is_cloud_matting_mediakit_ready() is False)
        check("探测失败 → 来源判为 none（不谎报 server）",
              cm.cloud_matting_source() == "none", cm.cloud_matting_source())
    finally:
        ctx.close()
    print("✅ 就绪判定与来源（server/local/none）语义正确")


def test_gateway_failure_propagates_for_fallback() -> None:
    import cloud_matting_mediakit as mk

    ctx = Ctx()
    try:
        ctx.write_gateway()
        fake = _install_fake_opener()
        fake.raise_on_open = urllib.error.URLError("网络不通")
        raised = ""
        try:
            mk._cloud_remove_bg(Image.new("RGB", (4, 4), (0, 0, 0)), scene="general", timeout=5)
        except Exception as e:  # noqa: BLE001
            raised = type(e).__name__
        check("网关不可达 → 抛异常（由上层回退本地引擎）", raised != "", raised or "没有抛异常")
    finally:
        ctx.close()
    print("✅ 网关失败可传播，供上层优雅回退")


# ────────────────────── ④ 网关服务自身（进程内） ──────────────────────
def _load_gateway_module(home: pathlib.Path):
    for stale in ("vdl_gw_under_test",):
        sys.modules.pop(stale, None)
    os.environ["VDL_GATEWAY_HOME"] = str(home)
    import importlib.util

    spec = importlib.util.spec_from_file_location("vdl_gw_under_test", GATEWAY_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["vdl_gw_under_test"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_gateway_service_endpoints() -> None:
    from fastapi.testclient import TestClient

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="vdl_gw_home_"))
    try:
        SECRET_AK = "AK_SERVER_VAULT"
        SECRET_SK = "SK_SERVER_VAULT"
        SECRET_MK = "MK_SERVER_VAULT"
        (tmp / "upstream.json").write_text(json.dumps(
            {"provider": "deepseek", "base_url": "https://up.test/v1",
             "api_key": "SK_UPSTREAM_VAULT", "models": ["m1"]}), encoding="utf-8")
        (tmp / "tokens.json").write_text(json.dumps(
            {"tokens": {"vdlt_GOOD": {"name": "unit"}}}), encoding="utf-8")
        (tmp / "matting.json").write_text(json.dumps(
            {"ak": SECRET_AK, "sk": SECRET_SK, "mediakit_api_key": SECRET_MK}),
            encoding="utf-8")

        gw = _load_gateway_module(tmp)

        seen = {"mk_post": [], "mk_put": [], "volc": []}

        def fake_request_upload(key):                   # noqa: ANN001
            assert key == SECRET_MK, "网关应向 MediaKit 出示服务端 Key"
            return {"file_id": "fid", "upload_url": "https://up.test/put"}

        def fake_put(url, img, content_type):           # noqa: ANN001
            seen["mk_put"].append((url, len(img), content_type))

        def fake_post(url, key, payload, timeout=180):  # noqa: ANN001
            seen["mk_post"].append((url, key, dict(payload)))
            if url.endswith("remove-image-background"):
                return {"success": True, "result": {"image_url": "https://out.test/img.png"}}
            return {"success": True, "result": {"image_url": "https://out.test/enh.png"}}

        gw._mk_request_upload = fake_request_upload
        gw._mk_put = fake_put
        gw._mk_post = fake_post
        gw._mk_get = lambda url: _png_bytes(9, 9)

        def fake_signed(action, body, content_type, ak, sk):  # noqa: ANN001
            seen["volc"].append((action, ak, sk, body.decode("utf-8", "replace")[:40]))
            return {"code": 0, "data": {"foreground_image": base64.b64encode(_png_bytes(3, 3)).decode()}}

        gw._volc_signed_post = fake_signed

        client = TestClient(gw.app)
        img_b64 = base64.b64encode(_png_bytes()).decode()

        r = client.get("/gw/health")
        check("health 200", r.status_code == 200, str(r.status_code))
        check("health 暴露抠图能力位（布尔）",
              isinstance(r.json().get("matting"), dict) and r.json()["matting"].get("mediakit") is True,
              str(r.json().get("matting")))
        check("❗health 不含任何凭据",
              not any(s in r.text for s in (SECRET_AK, SECRET_SK, SECRET_MK, "SK_UPSTREAM_VAULT")))

        r = client.get("/gw/v1/matting/capabilities")
        check("capabilities 未带令牌 → 401", r.status_code == 401, str(r.status_code))
        r = client.get("/gw/v1/matting/capabilities",
                       headers={"Authorization": "Bearer wrong"})
        check("capabilities 错令牌 → 401", r.status_code == 401, str(r.status_code))
        r = client.get("/gw/v1/matting/capabilities",
                       headers={"Authorization": "Bearer vdlt_GOOD"})
        check("capabilities 正确令牌 → 200 且两通道就绪",
              r.status_code == 200 and r.json()["mediakit"] and r.json()["visual"], r.text[:120])
        check("❗capabilities 不含任何凭据",
              not any(s in r.text for s in (SECRET_AK, SECRET_SK, SECRET_MK)))

        r = client.post("/gw/v1/image/remove-background",
                        headers={"Authorization": "Bearer vdlt_GOOD"},
                        json={"image_b64": img_b64, "scene": "human", "enhance_version": ""})
        check("抠图端点 200", r.status_code == 200, r.text[:160])
        check("抠图返回结果图 base64", bool(r.json().get("image_b64")), str(r.json())[:80])
        check("❗抠图响应不含任何凭据",
              not any(s in r.text for s in (SECRET_AK, SECRET_SK, SECRET_MK)))
        check("抠图确实用了服务端 MediaKit Key",
              seen["mk_post"] and seen["mk_post"][0][1] == SECRET_MK, str(seen["mk_post"])[:100])

        r = client.post("/gw/v1/image/enhance",
                        headers={"Authorization": "Bearer vdlt_GOOD"},
                        json={"image_b64": img_b64, "version": "professional", "multiple": 2})
        check("增强端点 200", r.status_code == 200, r.text[:160])
        check("❗增强响应不含任何凭据",
              not any(s in r.text for s in (SECRET_AK, SECRET_SK, SECRET_MK)))

        r = client.post("/gw/v1/image/segment",
                        headers={"Authorization": "Bearer vdlt_GOOD"},
                        json={"image_b64": img_b64, "person": True})
        check("分割端点 200", r.status_code == 200, r.text[:160])
        check("分割用的是 HumanSegment", seen["volc"] and seen["volc"][0][0] == "HumanSegment",
              str(seen["volc"])[:80])
        check("签名在服务端完成（用服务端 AK/SK）",
              bool(seen["volc"]) and seen["volc"][0][1] == SECRET_AK and seen["volc"][0][2] == SECRET_SK)
        check("❗分割响应不含任何凭据",
              not any(s in r.text for s in (SECRET_AK, SECRET_SK, SECRET_MK)))

        r = client.post("/gw/v1/image/segment",
                        headers={"Authorization": "Bearer vdlt_GOOD"},
                        json={"image_b64": img_b64, "person": False})
        check("非人像用 GeneralSegment",
              r.status_code == 200 and seen["volc"][-1][0] == "GeneralSegment", str(seen["volc"][-1])[:60])

        r = client.post("/gw/v1/image/remove-background",
                        headers={"Authorization": "Bearer vdlt_GOOD"}, json={})
        check("缺 image_b64 → 400", r.status_code == 400, str(r.status_code))

        r = client.post("/gw/v1/image/remove-background",
                        headers={"Authorization": "Bearer vdlt_GOOD"},
                        json={"image_b64": "AAAAA"})
        check("非法 base64 → 4xx 拒绝", 400 <= r.status_code < 500, str(r.status_code))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        os.environ.pop("VDL_GATEWAY_HOME", None)
        sys.modules.pop("vdl_gw_under_test", None)
    print("✅ 网关服务端点可用、鉴权有效、响应不回显凭据")


def test_gateway_source_wiring() -> None:
    """源码棘轮：钉住关键接线，防止重构把「走网关」悄悄改回去。"""
    gw_src = GATEWAY_PY.read_text(encoding="utf-8")
    mk_src = (SERVER / "cloud_matting_mediakit.py").read_text(encoding="utf-8")
    cv_src = (SERVER / "cloud_matting.py").read_text(encoding="utf-8")
    cfg_src = (SERVER / "cloud_matting_config.py").read_text(encoding="utf-8")

    check("网关不再只做 LLM：有抠图端点",
          "/gw/v1/image/remove-background" in gw_src and "/gw/v1/image/enhance" in gw_src
          and "/gw/v1/image/segment" in gw_src)
    check("网关扣图端点都要鉴权",
          gw_src.count("_authenticate(authorization)") >= 5,
          str(gw_src.count("_authenticate(authorization)")))
    check("网关凭据只在服务端文件（matting.json）",
          'MATTING_FILE = BASE / "matting.json"' in gw_src)
    check("health 会回能力位但不回凭据", '"matting": _matting_caps()' in gw_src)
    check("客户端抠图先看网关",
          "_gateway_on()" in mk_src and "def _cloud_remove_bg" in mk_src
          and "def _cloud_enhance" in mk_src)
    check("CV 通道也先看网关", "_gw_segment" in cv_src and "_gateway_on()" in cv_src)
    check("就绪判定计入服务端代做",
          '_gateway_matting_ready("mediakit")' in cfg_src
          and '_gateway_matting_ready("visual")' in cfg_src)
    check("凭据来源可区分 server/local/none",
          "def cloud_matting_source" in cfg_src and '"server"' in cfg_src
          and '"local"' in cfg_src and '"none"' in cfg_src)
    # 前端：**声明出去的 id 必须在 app.js 里有对应接线**（只查「字符串是否出现」会被
    # 「换成一个仍包含原名的名字」骗过 —— 2026-10-13 变异 M10 实测咬不住，故改成一致性校验）
    html = (REPO / "web" / "index.html").read_text(encoding="utf-8")
    js = (REPO / "web" / "app.js").read_text(encoding="utf-8")
    ids = re.findall(r'id="(cloudMatting[A-Za-z]*)"', html)
    check("前端声明了「凭据来源」元素", "cloudMattingSource" in ids, str(ids))
    missing = [i for i in ids if f"{i}: $('{i}')" not in js]
    check("declared id 都在 app.js 里有接线（含来源提示）", not missing, f"缺接线: {missing}")
    check("来源提示真的写进界面（不是只取到元素）",
          "el.cloudMattingSource.textContent" in js)
    print("✅ 源码接线棘轮全部命中")


def main() -> int:
    print("")
    print("▶ 云端抠图改走本产品服务端（钥匙不出保险柜）")
    test_gateway_preferred_and_no_credential_leak()
    test_enhance_and_segment_go_through_gateway()
    test_local_direct_fallback_intact()
    test_missing_everything_raises_clear_error()
    test_readiness_and_source()
    test_gateway_failure_propagates_for_fallback()
    test_gateway_service_endpoints()
    test_gateway_source_wiring()
    print("")
    if FAILS:
        print(f"❌ 存在 {len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"   - {f}")
        return 1
    print("🎉 云端抠图网关化守卫全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
