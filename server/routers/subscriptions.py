"""server/routers/subscriptions.py — 由 server/app.py 按域抽取（Phase 1）。
handler 通过 `app.<name>` 访问共享内核（globals/helper/导入）。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录对应文件。

安全审计 P0-2：订阅源与下载任务同属用户私有数据，四个端点全部按归属隔离——
列表/删除/手动检查只作用于本人（或历史遗留）订阅，新建订阅记录创建者归属。
"""
import app
from fastapi import APIRouter, Request
from stats import record_event


def _cloud_report(resource: str, title: str = "") -> None:
    """本地功能用量补上报授权中心（不参与配额闸门，只统计）。"""
    try:
        from routers.cloud_account import report_feature_usage_cloud
        report_feature_usage_cloud(resource, title=title)
    except Exception:  # noqa: BLE001
        pass

router = APIRouter()

def _owned_or_404(sub_id: str, request: Request):
    """取订阅并校验归属：非本人订阅一律 404（不返回 403，避免订阅 id 可枚举）。"""
    sub = app.sub_store.get(sub_id)
    if not sub:
        raise app.HTTPException(status_code=404, detail="订阅不存在")
    owner = getattr(sub, "owner_id", "") or ""
    if owner and owner != app._task_owner(request):
        raise app.HTTPException(status_code=404, detail="订阅不存在")
    return sub

@router.get("/api/subscriptions")
def list_subscriptions(request: Request) -> dict:
    subs = app.sub_store.list_for_owner(app._task_owner(request))
    return {"subscriptions": [s.to_public_dict() for s in subs], "enabled": app.SUB_ENABLED}

@router.post("/api/subscriptions")
def add_subscription(payload: app.SubscribeRequest, request: Request) -> dict:
    if not app.SUB_ENABLED:
        raise app.HTTPException(status_code=403, detail="当前部署未启用订阅功能")
    if not app.downloader.is_valid_quality(payload.quality):
        raise app.HTTPException(status_code=400, detail="不支持的清晰度选项")
    url, platform = app.parse_source(payload.url)  # 校验为已知公开平台
    # 安全审计 2026-10-11（验证轮 P3-2）：`parse_source` 对非已知平台的链接会回退到
    # `_generic_platform` 并放行 —— 它做的是「功能白名单」而非「网络可达性白名单」，
    # 内部地址（169.254.169.254 云元数据、127.0.0.1、私有网段）不会被它拦截。
    # 而下方 `probe_channel`（yt-dlp extract_flat）会真实发起 HTTP 请求，
    # 故这里必须再过一遍 SSRF 护栏，与 /api/download 口径一致；否则任意登录用户
    # 都能让本机去请求云元数据 / 内网服务（VPS 多租户部署下尤其危险）。
    app._assert_safe_url(url)
    sid = app.uuid.uuid4().hex[:app.TASK_ID_LENGTH]
    # 首次添加只记录基线（不下载历史视频），之后发布的新视频才自动下载
    items = app.subs_mod.probe_channel(url, payload.cookie, payload.proxy, limit=app.SUBSCRIBE_PROBE_LIMIT)
    baseline = [it["id"] for it in items][:200]
    sub = app.subs_mod.Subscription(
        id=sid, url=url, name=payload.name or platform.name,
        platform=platform.name, quality_key=payload.quality,
        quality_label=app.downloader.quality_label(payload.quality),
        cookie=payload.cookie, proxy=payload.proxy, auto_check=payload.auto_check,
        last_video_ids=baseline, last_checked=app.time.time(), created_at=app.time.time(),
        owner_id=app._task_owner(request),
    )
    app.sub_store.add(sub)
    try:
        record_event("subscribe")
    except Exception:  # noqa: BLE001 — 统计绝不影响订阅创建
        pass
    _cloud_report("subscribe")
    return sub.to_public_dict()

@router.delete("/api/subscriptions/{sub_id}")
def remove_subscription(sub_id: str, request: Request) -> dict:
    _owned_or_404(sub_id, request)
    if not app.sub_store.remove(sub_id):
        raise app.HTTPException(status_code=404, detail="订阅不存在")
    return {"deleted": True}

@router.post("/api/subscriptions/{sub_id}/check")
def check_subscription_route(sub_id: str, request: Request) -> dict:
    sub = _owned_or_404(sub_id, request)
    try:
        result = app._run_subscription_check(sub)
    except Exception as exc:  # noqa: BLE001
        app.logger.exception("订阅 %s 手动检查失败", sub_id)
        raise app.HTTPException(status_code=502, detail=f"探查频道失败：{str(exc)[:200]}")
    return result
