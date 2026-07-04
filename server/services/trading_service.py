# -*- coding: utf-8 -*-
"""实盘交易服务：QMT 网关单例装配 + status/positions/emergency_halt 业务逻辑。

设计红线（Why 这样切分）：
- 单例装配在模块级 lazy：get_qmt_gateway() 首次调用时读环境变量构造，缺凭证/无
  xtquant 返 None。不在 import 期构造（避免无 xtquant 机器 import 即崩）；不在
  lifespan 自动 connect（connect 是同步阻塞 C++ 调用，会拖慢启动；由 Cockpit
  视图或调度器按需 connect）。
- status 四态严格镜像网关：unavailable（无单例）/ disconnected（未 connect）/
  live（已连接）/ vetoed_by_risk（断线锁定）。前端心跳灯完全镜像，绝不虚假繁荣。
- emergency_halt 幂等：lock_down 一旦置位，重复调用不再重复撤单（避免对同一批
  未终态订单发多次撤单指令，防柜台风控误判）。

Why 模块级 import fire_and_forget：emergency_halt 投递告警走 fire_and_forget，
模块级暴露该名字便于测试 monkeypatch 屏蔽告警副作用（起 daemon thread）。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from core.notifier import NotificationManager, fire_and_forget

logger = logging.getLogger(__name__)

# 模块级单例（lazy：首次 get_qmt_gateway 调用时构造）
_gateway_singleton: Optional[object] = None


def get_qmt_gateway() -> Optional[object]:
    """懒构造 QmtExecutionGateway 单例。

    环境变量 QMT_USERDATA_PATH / QMT_ACCOUNT_ID 齐全 → 构造单例（不 connect）；
    缺凭证 / 无 xtquant → 返 None（Cockpit 走 unavailable 降级态）。

    Why 懒构造不在 import 期：xtquant 是 Windows 专用 C++ 扩展，开发机/CI 无该包时
    import QmtExecutionGateway 会触发 ImportError；放函数内 + try/except 让无 xtquant
    环境也能正常 import trading_service（仅 get_qmt_gateway 返 None）。
    """
    global _gateway_singleton
    if _gateway_singleton is not None:
        return _gateway_singleton
    if not (os.environ.get("QMT_USERDATA_PATH") and os.environ.get("QMT_ACCOUNT_ID")):
        logger.info("QMT 凭证未配置，trading_service 走 unavailable 模式")
        return None
    try:
        from trading.qmt_gateway import QmtExecutionGateway
        _gateway_singleton = QmtExecutionGateway()
        logger.info("QMT 网关单例已构造（未 connect）account=%s",
                    os.environ.get("QMT_ACCOUNT_ID"))
        return _gateway_singleton
    except Exception as e:
        logger.warning("QMT 网关构造失败（无 xtquant?），走 unavailable：%s", e)
        return None


def get_status() -> dict:
    """四态探测：unavailable / disconnected / live / vetoed_by_risk。

    锁定优先于连接：即便 _connected=True，只要 is_locked=True 即视为风控否决
    （断线瞬间 _connected 可能未被 on_disconnected 翻转，但 _lock_down 已率先置位）。
    """
    gw = get_qmt_gateway()
    if gw is None:
        return {"connected": False, "locked": False, "mode": "unavailable"}
    locked = bool(getattr(gw, "is_locked", False))
    connected = bool(getattr(gw, "_connected", False))
    if locked:
        return {"connected": connected, "locked": True, "mode": "vetoed_by_risk"}
    if connected:
        return {"connected": True, "locked": False, "mode": "live"}
    return {"connected": False, "locked": False, "mode": "disconnected"}


async def get_positions() -> list:
    """聚合底层真实持仓 → [{symbol, qty, market_value, pnl}]。

    pnl = market_value - open_cost（累计浮盈；XtPosition 不带昨收，无法算"今日"盈亏，
    务实口径见 spec 偏差记录）。第一版 market_value/pnl 走 None（未查行情，前端中性灰），
    仅返 symbol/qty，避免引入额外行情查询接口。
    未连接/锁定 → raise RuntimeError（路由层转 409）；无网关 → raise（路由层转 503）。
    """
    gw = get_qmt_gateway()
    if gw is None:
        raise RuntimeError("QMT 网关未装配（unavailable）")
    if getattr(gw, "is_locked", False) or not getattr(gw, "_connected", False):
        raise RuntimeError("QMT 网关未连接或已锁定，拒绝对账")
    raw = await gw._fetch_broker_positions()   # {stock_code: volume}
    if not raw:
        return []
    return [
        {"symbol": str(sym), "qty": float(qty), "market_value": None, "pnl": None}
        for sym, qty in raw.items()
    ]


def emergency_halt() -> dict:
    """一键熔断：置 lock_down + 告警。幂等。

    幂等规则：lock_down 已为 True 时直接返"已处于熔断态"，不重复处理。
    Why 本期不主动撤单：撤所有未终态订单需遍历 _orders + 逐个 cancel_order（async），
    与同步 emergency_halt 语义冲突；本期仅置 lock_down（后续 submit_order 见此标志即
    拒，等效"停止一切新发单"的熔断语义）。撤单留待调度器单独触发。

    无网关 → raise RuntimeError（路由层转 503）。
    """
    gw = get_qmt_gateway()
    if gw is None:
        raise RuntimeError("QMT 网关未装配（unavailable），无法熔断")

    if getattr(gw, "_lock_down", False):
        return {"halted": True, "message": "已处于熔断态（lock_down 已置位，跳过重复处理）"}

    # 置断线锁定：后续 submit_order/cancel_order 见此标志即拒（既有网关契约）
    gw._lock_down = True   # type: ignore[attr-defined]
    try:
        gw._connected = False   # type: ignore[attr-defined]  # 熔断即视为不可发单
    except Exception:
        pass

    # 钉钉最高级别告警（fire_and_forget，失败不影响熔断语义）
    try:
        fire_and_forget(
            NotificationManager.get_default().notify_risk_event(
                "【紧急熔断】人工触发 emergency_halt，网关已锁定，禁止后续发单", "ERROR"
            )
        )
    except Exception as e:
        logger.warning("熔断告警投递失败（不影响熔断语义）：%s", e)

    logger.critical("【紧急熔断】已触发，网关锁定")
    return {"halted": True, "message": "熔断已触发：网关锁定，后续发单一律拒绝"}
