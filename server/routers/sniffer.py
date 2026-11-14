"""CDP 浏览器嗅探路由（2026-09-27，对标 DataTool 悬浮球）。

- connect/disconnect/status/items：工坊 UI 的嗅探面板用
- send：页面悬浮球 outbox 通道失败时的兜底回传入口（正常路径走 CDP
  outbox 轮询，不经过页面 fetch —— https 页面往回环发 fetch 受
  Private Network Access 约束，响应须带 Allow-Private-Network 头）
- picked：前端轮询取走悬浮球里用户点了「下载」的项目
CORS 预检由 app.py 的全局 CORSMiddleware（默认 "*"）处理，这里不重复。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request, Response

import cdp_sniffer  # noqa: E402 - 与 app.py 同级（server 目录在 sys.path），路由包内不能用相对导入

router = APIRouter()


def _cloud_report(resource: str, title: str = "") -> None:
    """功能用量补上报授权中心（让云端用量榜能看到嗅探被多少人用过）。"""
    try:
        from routers.cloud_account import report_feature_usage_cloud
        report_feature_usage_cloud(resource, title=title)
    except Exception:  # noqa: BLE001
        pass


_PNA_HEADERS = {
    "Access-Control-Allow-Private-Network": "true",
    "Access-Control-Allow-Origin": "*",
}


def _pna(resp: Response) -> Response:
    resp.headers.update(_PNA_HEADERS)
    return resp


def _record_ext_ua(request) -> None:
    """记下扩展请求自带的 User-Agent（设备画像精确判定浏览器用，2026-10-13 #4）。

    扩展发往本机的请求带的是**它所在浏览器的 UA**，这是「浏览器判定精确化」最可靠
    且无需改扩展的信号。尽力而为：任何异常都不影响端点本身。
    """
    try:
        ua = ""
        try:
            ua = (request.headers.get("user-agent") or "") if request is not None else ""
        except Exception:  # noqa: BLE001
            ua = ""
        if not ua:
            return
        import extension_sync
        extension_sync.record_client_ua(ua)
    except Exception:  # noqa: BLE001
        pass


@router.post('/api/sniffer/connect')
def sniffer_connect(payload: dict = Body(default={})) -> dict:
    port = int(payload.get("port") or 9222)
    if not (1024 <= port <= 65535):
        raise HTTPException(status_code=400, detail="端口范围 1024-65535")
    launch = bool(payload.get("launch"))
    try:
        return cdp_sniffer.SNIFFER.start(port=port, launch=launch)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post('/api/sniffer/disconnect')
def sniffer_disconnect() -> dict:
    return cdp_sniffer.SNIFFER.stop()


@router.get('/api/sniffer/status')
def sniffer_status() -> dict:
    return cdp_sniffer.SNIFFER.status()


@router.get('/api/sniffer/items')
def sniffer_items(limit: int = 100) -> dict:
    return {"items": cdp_sniffer.SNIFFER.items(limit=max(1, min(limit, 200)))}


@router.post('/api/sniffer/ext-ping')
def sniffer_ext_ping(payload: dict = None, request: Request = None,  # noqa: RUF013
                     response: Response = None) -> dict:  # noqa: RUF013
    """扩展心跳（2026-10-01）：service worker 启动 + alarms 每分钟一次。

    面板据此显示「扩展已连接 ✓」——用户装完扩展立刻能确认状态，不用靠猜。
    1.0.36 起心跳带 {version}，桌面端与包内扩展版本比对 → 旧版提示更新。
    1.0.44 起**响应带回 reload_to**（2026-10-02 零点击自动更新）：App 把新版写进
    用户的扩展加载目录后，在这里告诉扩展「磁盘上已经是 X 版了」→ 扩展自己
    `chrome.runtime.reload()`。解压版扩展 Chrome 不会自动更新，这是唯一不需要
    用户手动覆盖目录 / 点 ↻ 的路径（详见 routers/extension.py 顶部注释）。
    心跳不带任何敏感信息，仅本机/回环场景使用。
    2026-10-13：顺手记录请求 UA（`_record_ext_ua`），供设备画像精确判定浏览器。
    """
    if response is not None:
        _pna(response)
    _record_ext_ua(request)
    ver, captured, pushed = "", 0, 0
    seen, media, last_mime = 0, 0, ""
    push_ok, push_err, push_page, push_last_err, page_saw = 0, 0, 0, "", 0
    if isinstance(payload, dict):
        raw = payload.get("version")
        if isinstance(raw, str):
            ver = raw
        captured = payload.get("captured") or 0
        pushed = payload.get("pushed") or 0
        seen = payload.get("seen") or 0
        media = payload.get("media") or 0
        last_mime = payload.get("last_mime") or ""
        # v1.0.40：落盘的推送记账 + 页面哨兵计数（旧版扩展不带这些键 → 保持 0，不报错）
        push_ok = payload.get("push_ok") or 0
        push_err = payload.get("push_err") or 0
        push_page = payload.get("push_page") or 0
        push_last_err = payload.get("push_last_err") or ""
        page_saw = payload.get("page_saw") or 0
    cdp_sniffer.SNIFFER.mark_ext_seen(ver, captured, pushed, seen, media, last_mime,
                                      push_ok, push_err, push_page, push_last_err, page_saw)
    # 零点击自动更新（2026-10-02）：心跳顺手带回「磁盘上已有新版」。
    # 目录同步与安全边界都在 routers/extension.py::maybe_sync 里，这里只透传结果；
    # 任何异常都不得影响心跳本身（否则扩展会以为桌面端掉线）。
    reload_to, auto_on = "", False
    try:
        from routers import extension as _ext_rtr
        info = _ext_rtr.maybe_sync(ver)
        reload_to = str(info.get("reload_to") or "")
        auto_on = bool(info.get("auto"))
    except Exception:  # noqa: BLE001 - 自动更新是尽力而为
        pass
    return {"ok": True, "reload_to": reload_to, "auto_update": auto_on}


@router.post('/api/sniffer/ext-push')
def sniffer_ext_push(payload: dict = Body(...), request: Request = None,  # noqa: RUF013
                     response: Response = None) -> dict:  # noqa: RUF013
    """扩展自动推送嗅探条目（2026-10-01）：扩展每嗅到**新**媒体 URL 就推一条。

    没有这条链路时，扩展嗅到的条目只存在扩展本地，桌面面板列表（读服务端
    items 库）永远是空的，用户会以为「没有嗅探到」。推送幂等：服务端按
    URL 全量去重，重复推只加 count。
    """
    if response is not None:
        _pna(response)
    _record_ext_ua(request)
    rows = payload.get("items") if isinstance(payload, dict) else None
    added = cdp_sniffer.SNIFFER.add_ext_items(rows if isinstance(rows, list) else [])
    return {"ok": True, "added": added}


@router.post('/api/sniffer/send')
def sniffer_send(payload: dict = Body(...), response: Response = None) -> dict:  # noqa: RUF013
    """悬浮球 / 浏览器扩展提交下载项（仅本机/回环场景使用）。

    返回的 send_id 是**回执凭据**：本条只是入队（必然成功），真正的建任务发生在
    桌面端进程里。调用方（扩展）拿 send_id 轮询 /api/sniffer/result，才能知道
    桌面端是「已加入下载」还是失败（未登录 / 不支持 / 超配额），而不是一律显示成功。

    可选字段 quality（2026-10-01 用户反馈「目前没法选择分辨率」）：扩展 popup 的
    清晰度下拉随本条上报，白名单净化后由桌面端建任务时使用；缺省/非法则桌面端用
    嗅探面板上的默认清晰度。
    """
    if response is not None:
        _pna(response)
    try:
        item = cdp_sniffer.SNIFFER.add_manual(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _cloud_report("sniffer")
    return {"ok": True, "picked": True, "send_id": item["send_id"],
            "url": item["url"][:200]}


@router.get('/api/sniffer/result')
def sniffer_result(send_id: str = "", response: Response = None) -> dict:  # noqa: RUF013
    """扩展查询回执：pending / ok / error(带原因) / unknown。**绝不限流**（扩展 ~0.9s 轮询）。"""
    if response is not None:
        _pna(response)
    return cdp_sniffer.SNIFFER.send_result(send_id)


@router.post('/api/sniffer/send-result')
def sniffer_send_result(payload: dict = Body(...), response: Response = None) -> dict:  # noqa: RUF013
    """桌面端前端写回建任务结果（仅本机/回环场景使用）。"""
    if response is not None:
        _pna(response)
    ok = bool(payload.get("ok"))
    cdp_sniffer.SNIFFER.report_result(
        str(payload.get("send_id") or ""), ok, str(payload.get("message") or ""))
    return {"ok": True}


@router.get('/api/sniffer/picked')
def sniffer_picked(request: Request, response: Response = None) -> dict:  # noqa: RUF013
    # 借这条 3s 一次的既有轮询，把**桌面端自己**的登录态告知服务端：扩展没有桌面端
    # 会话令牌，只有这样才能在点下载之前提示「桌面端未登录」（见 mark_desktop_auth）。
    user_id = None
    try:
        from user_membership import get_current_user_id
        user_id = get_current_user_id(request)
        cdp_sniffer.SNIFFER.mark_desktop_auth(user_id)
    except Exception:  # noqa: BLE001 - 登录态信号是尽力而为，绝不影响出队
        pass
    if response is not None:
        _pna(response)
    # ⚠️ 防偷条目（2026-09-28）：只有带有效令牌的桌面端才允许出队。匿名轮询
    # （Chrome 旧页面仍加载着 desktop-app.js / 普通浏览器）一律返回空、**不得**
    # take_picked()，否则会抢走扩展发来的下载条目，导致「点下载桌面端没反应」。
    # 桌面端桌面端 request() 总会带 Bearer（来自 WKWebView localStorage），与 Chrome
    # 的独立存储天然区分，因此令牌即身份判据。
    if user_id:
        return {"items": cdp_sniffer.SNIFFER.take_picked()}
    return {"items": []}
