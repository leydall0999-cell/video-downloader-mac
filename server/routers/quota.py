"""server/routers/quota.py — 免费用户配额闸门 HTTP 层。

对应设计文档 docs/llm-cloud-fallback-design.md §12：
- 终身云端事件 3 次（不可恢复）
- 每日 auto 运行 1 次（自然日重置）
- 单条上传视频 ≤ 30 分钟（免费用户；会员不限）
会员解除全部限制。

会员判定复用 app.current_member_store（与 /api/member 共用状态文件）。
配额状态持久化在 ~/.video-downloader/quota.json（与 membership 同目录），
由 pipeline 侧 quota.py 与本文共享同一 JSON schema。
"""
from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter, Request

router = APIRouter()


def _is_member(request: Optional[Request] = None) -> bool:
    """当前用户是否为会员（VIP会员或 AI 会员任一活跃即视为会员）。"""
    try:
        import app as _app
        store = _app.current_member_store(request) if request is not None else _app.member_store
        st = store.status()
        return bool(st.get("download_member", {}).get("active")) or \
               bool(st.get("ai_member", {}).get("active"))
    except Exception:
        return False


def _cloud_token(request: Optional[Request] = None) -> str:
    """取当前用户的云端账号 token（供 QuotaManager 做跨端原子记账）。

    🔴 2026-10-06：免费云端额度改为账号级中心记账后，这里是「本机身份 → 云端
    身份」的桥。拿不到 token（未登录云端 / 本机专属账号）时返回空串，
    QuotaManager 会 fail-open 沿用本机计数 —— 与旧口径一致，不误伤。

    必须用 `current_member_store(request)` 而不是全局 `app.member_store`：
    已登录本地账号时前者返回 per-user store（带该用户云端 token），
    后者是云端账号写入的全局 store（可能是别人的身份）。参见
    `membership.MembershipStore._cloud_token` 的同源回落逻辑。
    """
    try:
        import app as _app
        store = _app.current_member_store(request) if request is not None else _app.member_store
        return str(store._cloud_token() or "")
    except Exception:
        return ""


def get_quota_manager(request: Optional[Request] = None):
    """构造注入会员判定 + 云端 token 的 QuotaManager。"""
    from quota import QuotaManager
    return QuotaManager(is_member_fn=lambda: _is_member(request),
                        token_fn=lambda: _cloud_token(request))


# ── 本机引擎就绪探测（预检要用）──────────────────────────────────────── #
_LOCAL_READY_CACHE: dict = {"ts": 0.0, "val": False}
_LOCAL_READY_TTL = 60.0   # 秒；探测含 subprocess，缓存避免预检/面板高频重复调用


def _local_engine_ready(engine: str = "") -> bool:
    """本机 AI 引擎（MLX）当前是否可用：运行时找得到 + 至少一个可用权重目录。

    engine='cloud'（用户明确选纯云端）时本机能力不参与判定，直接返回 False
    —— 调用方据此把这条任务判为「需要云端」。探测只做 find_spec 与目录扫描、
    绝不加载模型，结果缓存 60 秒。
    """
    global _LOCAL_READY_CACHE
    eng = (engine or "").strip().lower()
    if eng == "cloud":
        return False
    now = time.time()
    if now - float(_LOCAL_READY_CACHE.get("ts") or 0) < _LOCAL_READY_TTL:
        return bool(_LOCAL_READY_CACHE.get("val"))
    ok = False
    try:
        import llm_config
        cfg = llm_config.get_llm_config()
        if not eng:
            eng = str(cfg.get("engine") or "auto").strip().lower()
        if eng != "cloud":
            rt = llm_config.local_runtime_status(cfg.get("mlx_python") or "")
            models = llm_config.list_local_models()
            ok = bool(rt.get("available")) and bool(models)
    except Exception:
        ok = False
    _LOCAL_READY_CACHE = {"ts": now, "val": ok}
    return ok


def resolve_engine(request: Optional[Request] = None) -> str:
    """当前生效的解说引擎档位（auto / cloud）。读不到时按 auto 处理。"""
    try:
        import llm_config
        eng = str(llm_config.get_llm_config().get("engine") or "auto").strip().lower()
    except Exception:
        eng = "auto"
    # 历史配置里可能残留 mlx / ollama（旧版三档），统一归一到 auto（本机优先）
    return "cloud" if eng == "cloud" else "auto"


def precheck_commentary(
    request: Optional[Request] = None,
    duration_sec: float = 0.0,
    engine: str = "",
) -> dict[str, Any]:
    """解说任务前置预检（返回结构化结论，不抛异常；供「选完文件立刻提示」用）。"""
    qm = get_quota_manager(request)
    eng = (engine or "").strip().lower() or resolve_engine(request)
    res = qm.precheck_commentary(
        duration_sec, local_engine_ready=_local_engine_ready(eng), engine=eng,
        resource="cloud_commentary",
    )
    st = qm.status()
    res["engine"] = eng
    res["is_member"] = st["is_member"]
    res["free_max_duration_sec"] = st["free_max_duration_sec"]
    # 🔴 2026-10-06 按功能独立终身额度：解说走 cloud_commentary 资源键。
    # 旧字段 lifetime_cloud_remaining 保留（默认 resource 值）向后兼容前端；
    # cloud_lifetime_remaining 是 per-resource 字典，前端升级后用它渲染 4 个独立行。
    res["lifetime_cloud_remaining"] = st["cloud_lifetime_remaining"].get("cloud_commentary")
    res["cloud_lifetime_remaining"] = st["cloud_lifetime_remaining"]
    # 🔴 2026-10-12 按时间计费后，预检必须把「本次将扣多少积分」一并告知：
    #    用户选完视频就能看到「50 积分/半小时」折算到本片的实际价钱，
    #    而不是等扣费失败（402）或任务跑完才知道花了多少。
    #    口径与 charge_commentary_credits 完全同源（同一组函数），绝不两套算价。
    try:
        _op = _commentary_credit_op(eng, duration_sec)
        res["credit_op"] = _op
        res["credit_units"] = commentary_billing_units(duration_sec)
        res["credit_cost"] = commentary_credit_total(_op, duration_sec)
    except Exception:
        # 估算失败不阻断预检本身（扣费点仍会在任务入口兜底）
        pass
    return res


def precheck_or_raise(
    request: Optional[Request] = None,
    duration_sec: float = 0.0,
    engine: str = "",
    vision: bool = False,
) -> dict[str, Any]:
    """前置预检：不通过则抛 403，detail 为结构化对象（message/hint/category/subscribe）。

    统一改造点：**所有**会启动解说任务的入口都必须先过这里，而不是等任务跑完
    才因额度不足失败（那会白等十几分钟并产出废片）。

    🔴 2026-10-05：在此**追加 AI 积分扣费**（解说此前完全无积分门禁，是最贵的功能）。
    扣费点选这里而不是各个 endpoint，因为本函数已是全部解说入口的统一收口 ——
    新增入口只要照旧调 `precheck_or_raise` 就自动被计费覆盖，不会漏。

    扣哪一档取决于实际会跑哪个引擎（`llm_script.py:2364` 会在长片时把
    auto 自动切成 cloud，所以长片按云端价）：
      - 本机 MLX          → `commentary_local_mlx`
      - 云端网关 / 大模型  → `commentary_llm`
    `vision=True`（用户主动开「画面理解」）时**额外**扣 `commentary_vision`。
    不足时抛 **402 + MEMBER_QUOTA|**（与会员墙其余部分同契约）。
    """
    res = precheck_commentary(request, duration_sec, engine=engine)
    if not res.get("allowed"):
        from fastapi import HTTPException
        raise HTTPException(
            status_code=403,
            detail={
                "message": res.get("reason") or "当前无法开始这次解说",
                "hint": res.get("hint") or "",
                "category": "quota",
                "code": res.get("code") or "denied",
                "subscribe": True,
            },
        )
    # 🔴 2026-10-06 按功能独立「每日免费次数」：视频解说走 cloud_commentary 资源键，
    # 与在线转码 / 在线去水印 / 在线字幕处理各自独立计每日次数（用户要求「独立功能
    # 独立计数」）。免费档默认 3 次/日、会员档 200 次/日（见 membership.FREE_DAILY_LIMITS
    # / DAILY_QUOTA_LIMITS），门禁顺序与字幕提取同构：先日配额 → 再 AI 积分 → 最后落账。
    _mstore = app_module().current_member_store(request)
    _dq = _mstore.quota_state("cloud_commentary")
    _billing = commentary_billing(request, res.get("engine") or engine, duration_sec, vision)
    if not _dq.get("allowed"):
        # 🔴 2026-11-11 实测修复：日免费次数只该约束「免费额度」，不能把**已经持有
        #    AI 积分**的用户挡死。事故场景：后台把 cloud_commentary 免费额度配成
        #    0 次/日（plans.json free_quota.daily_free_limits）⇒ 用户账户里躺着 220
        #    积分、预检也算出 credit_cost=50，却仍被 402「今日视频解说免费额度
        #    已用尽（0/日）」永久拦住 —— 付费资源被免费次数这一层拦成死路，
        #    连一次都没跑过（used=0），积分根本走不到扣费那一步。
        #    正解：免费次数不满足时，只要积分够付本次账单就放行（等价于「没有免费
        #    次数，按积分付费」）；积分也不够才按原文案 402。
        from fastapi import HTTPException
        _paid_ok = False
        try:
            _assert_commentary_affordable(request, _billing, _mstore)
            _paid_ok = True
        except HTTPException:
            _paid_ok = False
        if not _paid_ok:
            _limit = int(_dq.get("limit", 0))
            raise HTTPException(
                status_code=402,
                detail={
                    "message": f"今日视频解说免费额度已用尽（{_limit}/日）",
                    "hint": "开通会员可解锁无限次/日，并享满速生成",
                    "category": "quota", "code": "MEMBER_QUOTA", "subscribe": True,
                },
            )
    # 🔴 2026-10-23「失败不扣积分」：**真扣费从「任务前」挪到「产出成功后」**。
    # 事故背景：百度网盘接入期间转写因 av 19 与 faster-whisper 1.2.1 不兼容，
    # 1 秒内崩溃（解说管线退出 1），但积分已在预检这里扣掉、失败路径**没有任何退款**
    # ⇒ 用户两次重试被扣 320 积分、一个成片都没拿到。
    # 现在预检只做**只读**余额校验（余额不足仍在开跑前 402，不让用户白等十几分钟），
    # 真正落账由 app.py `_commentary_run_http` 的两个成功分支调
    # `charge_commentary_on_success()` 执行 —— 没产出就不扣。
    billing = _billing
    _assert_commentary_affordable(request, billing, _mstore)
    res["billing"] = billing
    return res


def _charge_optional(request: Optional[Request], op: str, label: str) -> None:
    """按需扣一项积分；不足抛 402（与主线同样的契约，前端统一弹会员中心）。"""
    import membership as _mem
    try:
        store = app_module().current_member_store(request) if request is not None \
            else app_module().member_store
    except Exception:
        return
    msg = _mem.gate_message(store, op, reason=op)
    if msg:
        from fastapi import HTTPException
        raise HTTPException(status_code=402, detail={
            "message": msg, "hint": f"{label}会消耗 AI 积分",
            "category": "quota", "code": "MEMBER_QUOTA", "subscribe": True,
        })


def app_module():
    """延迟 import app（避免 routers 层在导入期就绑死 app 模块）。"""
    import app as _a
    return _a


def _commentary_credit_op(engine: str, duration_sec: float) -> str:
    """解说这次该按哪个 op 计费（长片会被自动转云端 → 按云端价）。"""
    eng = (engine or "").strip().lower()
    local_ready = _local_engine_ready(eng)
    # auto 且本机就绪 → 通常跑本机；但长片会被 llm_script.py:2364 切云端。
    # 这里按「本机就绪且片子不长」判本机，长片按云端（宁多收不少收）。
    if local_ready and eng != "cloud":
        try:
            from llm_script_limits import LOCAL_MAX_VIDEO_SEC  # type: ignore
            long_clip = float(duration_sec or 0) > float(LOCAL_MAX_VIDEO_SEC)
        except Exception:
            long_clip = float(duration_sec or 0) > 1800.0
        if not long_clip:
            return "commentary_local_mlx"
    return "commentary_llm"


# 🔴 2026-10-12 定档：视频解说**按时间**计费 —— 每半小时 50 积分。
#    此前一律按次收 40：一条 8 分钟短片与一条 2 小时长片同价，而长片的真实成本
#    （转写 + 脚本 + 修复重试）是短片的十几倍，等于短片用户在补贴长片。
COMMENTARY_BILLING_UNIT_SEC: float = 1800.0      # 一个计费单位 = 半小时


def commentary_billing_units(duration_sec: float) -> int:
    """折算计费单位数：不足半小时按半小时（向上取整），时长未知按 1 个单位收。"""
    try:
        dur = max(0.0, float(duration_sec or 0.0))
    except (TypeError, ValueError):
        dur = 0.0
    return max(1, int(-(-dur // COMMENTARY_BILLING_UNIT_SEC)))


def commentary_credit_total(op: str, duration_sec: float) -> int:
    """解说本次应收积分 = 每半小时单价 × 计费单位数。

    单价仍取 `credit_cost(op)`（后台 plans.json 覆盖层照旧生效），只是把它重新
    解释为「每半小时的价」，因此后台改价依旧对所有用户即时生效、无需重新构建。
    """
    from membership import credit_cost
    unit_price = int(credit_cost(op))
    if unit_price <= 0:
        return 0
    return unit_price * commentary_billing_units(duration_sec)


def charge_commentary_credits(
    request: Optional[Request] = None,
    engine: str = "",
    duration_sec: float = 0.0,
) -> dict[str, Any]:
    """按实际引擎 **× 实际时长** 扣 AI 积分；不足抛 402。返回扣费结果供调用方忽略。"""
    import app as _app
    op = _commentary_credit_op(engine, duration_sec)
    units = commentary_billing_units(duration_sec)
    total = commentary_credit_total(op, duration_sec)
    store = _app.current_member_store(request) if request is not None else _app.member_store
    if total <= 0:
        return {"ok": True, "spent": 0, "free": True, "op": op, "units": units}
    from membership import gate_message
    msg = gate_message(store, op, reason=f"commentary:{op}", cost=total)
    if msg:
        from fastapi import HTTPException
        raise HTTPException(status_code=402,
                            detail=f"MEMBER_QUOTA|{msg}")
    return {"ok": True, "op": op, "spent": total, "units": units}


def commentary_billing(request: Optional[Request], engine: str, duration_sec: float,
                       vision: bool = False) -> dict[str, Any]:
    """算本次解说**应收什么**（只算不扣，零副作用）。

    🔴 2026-10-23「失败不扣积分」：本函数只产出账单，真正落账是
    `charge_commentary_on_success()`。分工的原因写在 `precheck_or_raise` 里。
    """
    op = _commentary_credit_op(engine, duration_sec)
    units = commentary_billing_units(duration_sec)
    total = commentary_credit_total(op, duration_sec)
    vision_cost = 0
    if vision:
        try:
            from membership import credit_cost
            vision_cost = max(0, int(credit_cost("commentary_vision")))
        except Exception:  # noqa: BLE001 — 查不到价按免费处理，绝不误拦用户
            vision_cost = 0
    owner = ""
    try:
        if request is not None:
            owner = app_module()._task_owner(request)
    except Exception:  # noqa: BLE001
        owner = ""
    return {"op": op, "units": int(units), "cost": int(total),
            "vision": bool(vision), "vision_cost": int(vision_cost), "owner": owner}


def _assert_commentary_affordable(request: Optional[Request], billing: dict[str, Any],
                                  store: Any = None) -> None:
    """只读校验：余额不够立刻 402（**不扣分、不消耗试用名额**）。

    等价于旧的 `charge_commentary_credits` 的「拦人」那一半，只是不动账 ——
    用的是 `membership.can_afford`（明确保证零副作用的那个），绝不能用
    `gate_message` 当探针（它会真扣、还会烧掉一次性试用名额）。
    """
    if not billing:
        return
    try:
        import membership as _mem
    except Exception:  # noqa: BLE001
        return
    try:
        st = store if store is not None else (
            app_module().current_member_store(request) if request is not None
            else app_module().member_store)
    except Exception:  # noqa: BLE001 — 取不到 store 就放行，交给产出后扣费兜底
        return
    for _op, _cost in ((billing.get("op"), billing.get("cost")),
                       ("commentary_vision", billing.get("vision_cost"))):
        if not _op or int(_cost or 0) <= 0:
            continue
        msg = _mem.can_afford(st, str(_op), total=int(_cost))
        if msg:
            from fastapi import HTTPException
            raise HTTPException(status_code=402, detail=f"MEMBER_QUOTA|{msg}")


def charge_commentary_on_success(billing: dict[str, Any]) -> dict[str, Any]:
    """**产出成功之后**才扣费（解说积分 + 画面理解 + 每日次数）。

    🔴 2026-10-23：这是全部解说扣费**唯一**的落账点。任务失败（转写崩溃 / 超时 /
    没找到脚本或成片）时它根本不会被调用 ⇒「没出片就不扣积分」。
    幂等由调用方保证（job["billing"]["charged"]），重复调用会重复扣。
    任何异常一律吞掉并记日志：记账失败绝不能把已经产出的成片作废。
    """
    import logging
    bill = dict(billing or {})
    op = str(bill.get("op") or "")
    cost = int(bill.get("cost") or 0)
    vision_cost = int(bill.get("vision_cost") or 0)
    if cost <= 0 and vision_cost <= 0:
        return {"ok": True, "charged": []}
    try:
        from membership import gate_message
        from user_membership import store_for_owner
        st = store_for_owner(str(bill.get("owner") or ""))
        charged: list[str] = []
        for _op, _cost in ((op, cost), ("commentary_vision", vision_cost)):
            if not _op or _cost <= 0:
                continue
            msg = gate_message(st, _op, reason=f"commentary:{_op}", cost=_cost)
            if msg:
                # 余额在任务运行期间被别处花掉：照常交付成片，只记日志告警
                logging.getLogger("quota").warning(
                    "解说产出后扣费未成功（余额已不足，成片照常交付）op=%s cost=%s: %s",
                    _op, _cost, msg)
            else:
                charged.append(_op)
        try:
            st.use_daily("cloud_commentary", 1)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "charged": charged}
    except Exception:  # noqa: BLE001
        logging.getLogger("quota").exception("解说产出后扣费失败（不影响成片交付）")
        return {"ok": False, "charged": []}


def assert_upload_allowed(request: Optional[Request], duration_sec: float,
                          vision: bool = False) -> dict[str, Any]:
    """上传前服务端校验（兼容旧调用）：等价于 precheck_or_raise。

    历史上这里只判「时长 > 30 分钟」，漏了「时长合法但必须走云端、而云端额度
    已耗尽」这一致命分支（用户实测的废片场景就是后者）。现统一收口到预检。

    `vision` 透传给预检：上传入口勾了「画面理解」同样要扣 `commentary_vision`
    （这两个入口不走 `precheck_or_raise`，不传会漏扣）。

    🔴 2026-10-23：返回**账单**（而不是 None），上传类入口必须把它存进
    `commentary_jobs[job_id]["billing"]` —— 否则产出成功时没有账单可落账，
    这条链路就变成免费（漏扣）。
    """
    return (precheck_or_raise(request, duration_sec, vision=vision) or {}).get("billing") or {}


@router.get("/api/quota/status")
def quota_status(request: Request) -> dict[str, Any]:
    """返回配额状态，供设置页展示「剩余免费云端 X/3、今日 auto Y/1」。"""
    return get_quota_manager(request).status()
