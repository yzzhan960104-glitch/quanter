# -*- coding: utf-8 -*-
"""掘金终端（7002 网关）只读观测代理（W4-B，2026-08-28 全库评审清偿；
2026-08-29 cockpit 多腿方案升级为双腿形态）。

物理定位：
    QMT 退役 P3 删除了 /api/v1/trading/* 路由与 QMT 网关，cockpit 三卡（心跳/资金/
    成交流水）与 /jobs 视图失去数据源——前端轮询死路由 404+toast 轰炸。本路由把
    掘金终端 7002 REST API（Bearer=策略目录 runtime.json token，skill=
    .agents/skills/goldminer-terminal 实测面）以【只读代理】形态接回 server，
    前端零凭证（token 只活在 server 进程，绝不入前端 bundle——client.ts 的
    VITE_API_TOKEN 内网假设不扩散到终端 token）。

双腿形态（2026-08-29 方案 docs/superpowers/plans/2026-08-29-cockpit-multi-leg.md）：
    - /legs：腿目录（gc.LEGS 单源 + runtime 三键 + 7002 策略名映射）；
    - asset/positions/orders/trades 加 ?leg=main|exp（缺省 main——旧前端零改动）；
    - /ab：当日对照（ops.emquant_ab_compare.compare 单源消费，结构化 JSON）；
    - /audit：terminal_audit 事件流下钻（experiments.db 只读，按腿账户过滤）。

边界红线：
    - 只代理 GET（持仓/资金/委托/成交流水/策略列表/账户状态）；下单/撤单/启停/
      风控配置族是 skill 纪律禁区（须用户显式指令），绝不进本路由。
    - 单源纪律：7002 调用与 runtime.json 读取全部经 ops.gm_ops_common（与三件套
      同源）；对照判定逻辑在 ops.emquant_ab_compare——本文件只做 HTTP 形态适配。
    - 仿真绩效不进 Web（认识论纪律）：/ab 只出工程闸+信号/订单结构 diff，
      永不做收益率归一化对比。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from ops import emquant_ab_compare as ab
from ops import gm_ops_common as gc

# 鉴权在 main.py 挂载层统一施加（require_read_cookie，同 logs_router 范式）——
# 本模块不自带依赖，保持与其它 v1 路由一致的"挂载点单源鉴权"结构。
router = APIRouter(prefix="/gm", tags=["gm-readonly"])

_AB_DB = Path(__file__).resolve().parents[4] / "experiment" / "experiments.db"


def _leg(key: str) -> gc.LegDef:
    """leg key → LegDef；非法 key → 404（防手滑拿错腿的早失败）。"""
    for leg in gc.LEGS:
        if leg.key == key:
            return leg
    raise HTTPException(404, f"未知腿 {key!r}（合法值：{[l.key for l in gc.LEGS]}）")


def _leg_cfg(key: str) -> dict:
    """腿 → runtime.json（token/account_id）。缺文件=腿未部署 → 502 带处置指引。"""
    d = gc.leg_strategy_dir(_leg(key))
    try:
        return gc.runtime_config(d)
    except Exception as e:
        raise HTTPException(502, f"腿 {key} 的 runtime.json 不可读（未部署/目录异位）：{e}") from e


def _proxy(path: str, leg: str = "main") -> JSONResponse:
    """GET 7002 → 透传 JSON。非 200/连接失败 → 502 带中文排障信息（不裸抛）。"""
    cfg = _leg_cfg(leg)
    status, payload = gc.api_get(path.format(account=cfg.get("account_id") or ""),
                                 str(cfg.get("token") or ""), timeout=4.0)
    if status != 200 or payload is None:
        return JSONResponse(status_code=502, content={
            "detail": f"掘金 7002 网关不可用（status={status}，leg={leg}，path={path}）——"
                      "终端未开/网关断连，处置见 goldminer-terminal skill"})
    return JSONResponse(payload)


@router.get("/legs", summary="腿目录：双腿注册表 + runtime 三键 + 策略名（前端腿选择器唯一数据源）")
async def legs() -> JSONResponse:
    """LegSelector/DualAssetCard 数据源。exp 未部署时天然只返回 main（注册表语义）。"""
    token = ""
    try:
        token = str(gc.runtime_config().get("token") or "")   # 终端 token 腿间同值，主腿缺省取
    except Exception:
        pass                                                   # 未部署态也返回腿骨架（前端降级展示）
    _, strategies = gc.api_get("/v3/strategies", token, timeout=4.0)
    name_map = {s.get("strategy_id"): s.get("name")
                for s in ((strategies or {}).get("data") or []) if isinstance(s, dict)}
    out = []
    for leg in gc.active_legs():
        d = gc.leg_strategy_dir(leg)
        try:
            cfg = gc.runtime_config(d)
        except (OSError, ValueError):
            cfg = {}
        sid = str(cfg.get("strategy_id") or "")
        out.append({"key": leg.key, "label": leg.label,
                    "role": "incumbent" if leg.key == "main" else "challenger",
                    "account_id": str(cfg.get("account_id") or "") or None,
                    "strategy_id": sid or None,
                    "strategy_name": name_map.get(sid) or None})
    return JSONResponse({"legs": out})


@router.get("/overview", summary="终端总览：策略列表 + 账户连接状态（全局，不按腿）")
async def overview() -> JSONResponse:
    """StatusCard 数据源：策略 stage/进程态 + 账户通道连接（两次 7002 聚合）。
    双腿都在策略列表里（NECK/NECK-EXP），前端按 strategy_id 映射腿——本端点保持
    单腿时代形态，不引入按腿参数。"""
    token = str(gc.runtime_config().get("token") or "")
    s1, strategies = gc.api_get("/v3/strategies", token, timeout=4.0)
    s2, accounts = gc.api_get("/v3/account-statuses", token, timeout=4.0)
    if s1 != 200:
        return JSONResponse(status_code=502, content={
            "detail": f"掘金 7002 网关不可用（status={s1}）——终端未开/网关断连"})
    return JSONResponse({"strategies": (strategies or {}).get("data") or [],
                         "account_statuses": (accounts or {}).get("data") or []
                         if s2 == 200 else None})


@router.get("/asset", summary="资金：nav/available/frozen/market_value（按腿）")
async def asset(leg: str = Query("main", description="main|exp")) -> JSONResponse:
    return _proxy("/v3/account-trade/cash/{account}", leg)


@router.get("/positions", summary="持仓：volume/vwap/fpnl/available（按腿）")
async def positions(leg: str = Query("main")) -> JSONResponse:
    return _proxy("/v3/account-trade/positions/{account}", leg)


@router.get("/orders", summary="当日委托（含状态/拒因，按腿）")
async def orders(leg: str = Query("main")) -> JSONResponse:
    return _proxy("/v3/account-trade/orders/{account}", leg)


@router.get("/trades", summary="成交流水（execrpts：成交价量，按腿）")
async def trades(leg: str = Query("main")) -> JSONResponse:
    return _proxy("/v3/account-trade/execrpts/{account}", leg)


@router.get("/round", summary="当前 A/B 轮次档案透传（emquant/config/ab_round.json 只读）")
async def round_profile() -> JSONResponse:
    """RoundCard 数据源。轮次档案手工维护（README runbook 钉死字段）；档案缺失 →
    404 带指引（实验视图展示占位态,不炸）。"""
    import json as _json
    p = Path(__file__).resolve().parents[4] / "emquant" / "ab_round.json"
    if not p.exists():
        raise HTTPException(404, "轮次档案 ab_round.json 缺失——按 emquant/ab_round.json 模板创建")
    return JSONResponse(_json.loads(p.read_text(encoding="utf-8")))


@router.get("/ab", summary="双腿当日对照（结构化：工程闸+信号diff+订单diff+红旗）")
async def ab_daily(date: str | None = Query(None, description="YYYY-MM-DD，缺省今天")) -> JSONResponse:
    """AbDailyCard 数据源。对照判定单源=ops.emquant_ab_compare.compare；只出工程闸
    与结构 diff，永不出收益率对比（认识论红线）。单腿在役 → 200 带 single_leg 标记
    （前端展示占位，不报错——exp 未部署期 cockpit 不受损）。"""
    day = date or f"{datetime.now():%Y-%m-%d}"
    main_leg, exp_leg = _leg("main"), _leg("exp")
    if exp_leg not in gc.active_legs():
        return JSONResponse({"day": day, "single_leg": True,
                             "detail": "实验腿未部署（GM_EXP_STRATEGY_DIR 未设）——无对照面"})
    try:
        main_acc = str(gc.runtime_config(gc.leg_strategy_dir(main_leg)).get("account_id") or "")
        exp_acc = str(gc.runtime_config(gc.leg_strategy_dir(exp_leg)).get("account_id") or "")
    except (OSError, ValueError) as e:
        return JSONResponse(status_code=502, content={"detail": f"腿 runtime 不可读：{e}"})
    payload = ab.compare(day, ab.fetch_rows(day, main_acc, _AB_DB),
                         ab.fetch_rows(day, exp_acc, _AB_DB), main_acc, exp_acc)
    payload["ok"] = not payload["flags"]
    return JSONResponse(payload)


@router.get("/audit", summary="事件流下钻：terminal_audit 按腿/日期/事件过滤（只读）")
async def audit(leg: str = Query("main"), date: str = Query(None, description="YYYY-MM-DD，缺省今天"),
                event: str | None = Query(None), limit: int = Query(500, ge=1, le=5000)) -> JSONResponse:
    """AuditExplorer 数据源。experiments.db 以只读模式打开（file:…?mode=ro——
    server 进程绝不写台账库，写权独属 ingest cron）。腿→账户映射经 runtime.json，
    不接受裸 account_id 参数（防手滑拿错账户）。"""
    day = date or f"{datetime.now():%Y-%m-%d}"
    if not _AB_DB.exists():
        return JSONResponse({"rows": [], "detail": "台账库未建（ingest 15:40 首跑后可用）"})
    try:
        account = str(_leg_cfg(leg).get("account_id") or "")
    except HTTPException:
        raise
    uri = f"file:{_AB_DB.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as con:
        rows = con.execute(
            "SELECT ts, event, detail FROM terminal_audit"
            " WHERE account_id=? AND ts LIKE ?" + (" AND event=?" if event else "") +
            " ORDER BY ts DESC LIMIT ?",
            (account, day + "%", *([event] if event else []), limit)).fetchall()
    import json as _json
    out = []
    for ts, ev, detail in rows:
        try:
            d = _json.loads(detail) if detail else {}
        except ValueError:
            d = {"raw": detail}
        out.append({"ts": ts, "event": ev, "detail": d})
    return JSONResponse({"day": day, "leg": leg, "rows": out})
