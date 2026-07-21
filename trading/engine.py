# -*- coding: utf-8 -*-
"""二期自动交易引擎：APScheduler 四触发点编排 + 影子模式分流。

物理意图（四触发点的真实业务节奏）：
  eod_plan  15:35 T-1 晚：扫颈线法信号 → build_orders → save_plan（confirmed=False）
              → push 钉钉（待研究员确认）。本阶段绝不下单（机器只产计划，人审是闸）。
  pre_open  09:22 T 日开盘前：① 撤昨日遗留未成交单 ② 读已确认计划
              → 注入动态白名单（过关5）→ 挂限价买 + 止盈限价卖（逐单 try-except 兜底）。
  stop_loss 每 5min 盘中：查 gw 真实持仓 + 现价，跌破止损价 → 发卖出单（qty 必须来自
              gw 持仓，绝不硬编码——live 卖错数量 = 致命）。
  post_close 15:30 盘后：对账（run_reconcile）+ 清动态白名单。熔断连线见 TODO（本 task 不做）。

============================================================================
⚠️ 不变量（Task5 M2 风险官要求 · 绝对红线）
============================================================================
本引擎**必须独立进程运行**（``python -m trading``，由 ``trading/__main__.py`` 起常驻
AsyncIOScheduler），**绝不可被 server lifespan 嵌入 server 进程**。

Why 独立进程是硬约束：
- ``trading.dynamic_whitelist._DYNAMIC`` 是模块级全局（当日计划标的临时注入），
  只在 engine 进程内有效——这是设计预期（见 dynamic_whitelist.py 模块 docstring）。
- 若 engine 与 server 同进程：engine 在 pre_open 注入的 _DYNAMIC 会污染 server 的
  手动下单路径（Cockpit/前端），导致 server 手动下单越过静态 env 白名单（前视污染），
  破坏「server 行为与改造前完全一致」的向后兼容红线。
- 因此 ``server/main.py`` 的 lifespan **不应** import 本模块、不应构造 TradingEngine。
  入口唯一在 ``trading/__main__.py``（Task 10）。

============================================================================
影子模式（AUTO_TRADE_MODE=dry_run，默认）红线
============================================================================
- pre_open / stop_loss_monitor 走 ``_submit`` → trading_service.submit_order 的
  ``dry_run=(_mode()=="dry_run")`` 分流，命中即返 ``{"state":"DRY_RUN"}`` 不真下单。
- 未跑满 TRADE_SHADOW_MIN_DAYS（≥5）禁切 live 的告警由 ``trading/__main__.py``
  启动期处理（Task 10），本引擎内 ``_mode()`` 仅忠实读 env，不重复告警逻辑。
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Mapping, Optional

from trading import (
    calendar,
    circuit_breaker,
    dynamic_whitelist,
    reconcile_job,
    signal_runner,
    trading_plan,
)
from trading.signal_runner import build_orders_from_signals

logger = logging.getLogger(__name__)


# ============================================================================
# 环境读取辅助
# ============================================================================
def _mode() -> str:
    """当前交易模式：dry_run（默认·影子）/ live。

    Why 默认 dry_run：spec 红线，未显式 AUTO_TRADE_MODE=live 一律按影子处理，
    宁可漏挂单也不在未观测足够天数时盲发真单（live 前必修见报告 follow-up）。
    """
    return os.getenv("AUTO_TRADE_MODE", "dry_run")


def _trade_cfg() -> dict:
    """交易参数（从 env 读，缺省值与颈线法 v6 基线对齐）。

    Why env 化：实盘调参不改代码（十二期自动化原则），且独立进程的 env 与 server
    解耦（server 不应感知这些参数，进一步隔离两端状态）。
    """
    return {
        "pos_cap": float(os.getenv("TRADE_POS_CAP", "0.05")),
        # 颈线法 id_cfg 默认；实盘从 NecklineConfig 读（本引擎薄编排，不重算）
        "stop_atr_mult": float(os.getenv("TRADE_STOP_ATR_MULT", "2.0")),
        "tp_h_mult": float(os.getenv("TRADE_TP_H_MULT", "2.0")),
        # 海龟 trailing 止损参数（compute_stop_price 用，本 task stop_loss_monitor 直接用计划 stop_price）
        "grace": int(os.getenv("TRADE_STOPLOSS_GRACE_DAYS", "5")),
        "step": float(os.getenv("TRADE_STOPLOSS_STEP_ATR", "0.1")),
        "floor": float(os.getenv("TRADE_STOPLOSS_FLOOR", "0.5")),
    }


def get_gateway():
    """惰性取交易网关单例（透传 trading_service.get_gateway）。

    Why 透传不重造：网关单例的装配优先级（EMT > QMT > None）与懒构造策略已在
    trading_service.get_gateway 固化，本引擎薄编排不重复，避免双单例漂移。
    本函数独立出来便于测试 monkeypatch（engine.get_gateway）隔离真实网关副作用。
    """
    from server.services.trading_service import get_gateway as _svc_get_gw
    return _svc_get_gw()


async def _submit(order, *, confirm: bool = True) -> dict:
    """下单分流（dry_run 据 _mode）。

    透传 trading_service.submit_order，其契约：
    - dry_run 命中 → 返 {"order_id":"", "state":"DRY_RUN", "message":<reason>}（不真下单）
    - 真单成功   → 返 {"order_id":<seq>, "state":<OrderState.name 字符串>, "message":...}
    - 挡板命中（非 dry_run）→ **raise RuntimeError**（调用方必须 try-except 兜底）

    Why dry_run 用 _mode() 而非参数注入：pre_open/stop_loss 都是「影子即整批不真单」
    语义，_mode 是进程级开关，逐单传参反而引入「单只切 live」的误操作面。
    """
    from server.services.trading_service import submit_order as svc_submit
    return await svc_submit(order, dry_run=(_mode() == "dry_run"), confirm=confirm)


# ============================================================================
# 触发点 1：eod_plan —— T-1 晚扫信号、落计划、推钉钉（不真下单）
# ============================================================================
async def eod_plan(date: str, signals: list, atr_map: dict, capital: float) -> dict:
    """T-1 晚：颈线法信号 → 计划落盘（confirmed=False） → 推钉钉等研究员确认。

    物理意图：机器批量扫信号易受数据瑕疵/前视偏差/极端行情误判，T-1 晚必须给人
    一次否决机会——故本函数只产计划不下单（spec §2 确认闸红线）。

    Args:
        date:     T 日（计划生效日），如 "2026-07-22"。
        signals:  NecklineMethodStrategy.scan_at 返回的 trade dict 列表。
        atr_map:  {symbol: ATR}，缺 ATR 的标的在 build 阶段被跳过（不抛）。
        capital:  总资金（仓位 cap 计算基准）。

    Returns:
        {"date":..., "n_orders":..., "mode":...}，n_orders=0 亦正常（当日无信号）。

    嵌套 orders 结构（scope #1，与 Task8 push_plan_to_dingtalk + save_plan 全链路一致）：
        [{"order":{symbol,qty,side,price}, "stop_price":..., "take_profit":...}, ...]
    """
    cfg = _trade_cfg()
    # 信号 → PlannedOrder（仓位整手 + 止损/止盈价）；缺数据跳过不抛
    orders = build_orders_from_signals(
        signals,
        capital=capital,
        pos_cap=cfg["pos_cap"],
        atr_map=atr_map,
        stop_cfg={"stop_atr_mult": cfg["stop_atr_mult"], "tp_h_mult": cfg["tp_h_mult"]},
    )
    # 序列化为嵌套 dict（Task8 契约硬约束：order + stop_price + take_profit 三段）
    order_dicts = [
        {
            "order": {
                "symbol": o.order.symbol,
                "qty": o.order.qty,
                "side": o.order.side,
                "price": o.order.price,
            },
            "stop_price": o.stop_price,
            "take_profit": o.take_profit,
        }
        for o in orders
    ]
    # 落盘 confirmed=False（pre_open 会检查此位）+ 推钉钉等确认
    trading_plan.save_plan(date, order_dicts)
    trading_plan.push_plan_to_dingtalk(date, order_dicts)
    logger.info("eod_plan 完成 date=%s n_orders=%d mode=%s", date, len(orders), _mode())
    return {"date": date, "n_orders": len(orders), "mode": _mode()}


# ============================================================================
# 触发点 2：pre_open —— T 日开盘前：撤昨日单 + 检查确认闸 + 挂当日买单
# ============================================================================
async def pre_open(date: str) -> dict:
    """T 日开盘前：撤昨日遗留未成交单 → 读已确认计划 → 注入白名单 → 逐单挂单。

    物理意图与时序（顺序不可调）：
        ① 撤昨日未成交（scope #2）：避免昨日挂单与新计划叠加导致超额成交。
           须在确认闸检查通过后、挂新单前执行（否则没确认也撤，破坏昨日已确认单）。
        ② 确认闸检查（spec §2 红线）：未确认 → 一律不挂，返「计划未确认」。
        ③ 注入动态白名单（Task5）：让当日计划标过关5，但仅在本 engine 进程内生效
           （独立进程不变量，见模块 docstring）。
        ④ 逐单挂单 + try-except 兜底（scope #7）：单标的挡板命中 raise 不炸整批。

    Args:
        date: T 日（如 "2026-07-22"）。

    Returns:
        {"submitted":<成功挂单数>, "mode":..., "reason"?:...}。
    """
    plan = trading_plan.load_plan(date)
    if plan is None:
        return {"submitted": 0, "reason": "无计划"}
    if not plan.get("confirmed"):
        # 未确认绝不挂单（spec 红线）：宁可漏挂，不挂研究员未审核的单。
        return {"submitted": 0, "reason": "计划未确认，跳过挂单"}

    # ① 撤昨日未成交（scope #2）：仅在确认闸通过后才撤，避免误撤昨日已确认单。
    gw = get_gateway()
    if gw is None:
        # gw 未装配：影子模式仍可挂 DRY_RUN（dry_run 命中不触达 gw）；真单模式下
        # 挂单也会因 gw=None 抛 RuntimeError 由下方 try-except 吞掉，故这里只 warning。
        logger.warning("pre_open 撤昨日单跳过：交易网关未装配（gw=None）")
    else:
        try:
            n_cancelled = await circuit_breaker.cancel_all_open_orders(gw)
            logger.info("pre_open 撤昨日未成交单 %s 笔", n_cancelled)
        except Exception:
            # 撤单失败不阻塞挂单主路径（单笔失败已在 cancel_all 内被吞，此处兜整体异常）
            logger.exception("pre_open 撤昨日单整体异常（继续挂新单）")

    # ② 注入动态白名单（Task5）：仅 engine 进程生效，server 进程不受影响。
    symbols = {o["order"]["symbol"] for o in plan["orders"]}
    dynamic_whitelist.inject_dynamic_whitelist(symbols)

    # ③ 逐单挂单 + raise 兜底（scope #7）
    from trading.execution_gateway import OrderRequest
    n_submitted = 0
    for o in plan["orders"]:
        od = o["order"]
        order_req = OrderRequest(
            symbol=od["symbol"], qty=od["qty"], side=od["side"], price=od["price"],
        )
        try:
            result = await _submit(order_req, confirm=True)
        except Exception as exc:
            # 挡板命中（资金不足/涨跌停/不在白名单等）会 raise RuntimeError
            # （trading_service.submit_order 契约）——必须逐单吞，一只拒单不炸整批。
            logger.warning("pre_open 挂单失败 symbol=%s 原因=%s", od["symbol"], exc)
            continue
        # state 是 OrderState.name 字符串；REJECTED/FAILED 视为未挂成功
        if result.get("state") not in ("REJECTED", "FAILED"):
            n_submitted += 1
        else:
            logger.warning("pre_open 挂单未成功 symbol=%s state=%s msg=%s",
                           od["symbol"], result.get("state"), result.get("message"))

    logger.info("pre_open 完成 date=%s submitted=%d/%d mode=%s",
                date, n_submitted, len(plan["orders"]), _mode())
    return {"submitted": n_submitted, "mode": _mode()}


# ============================================================================
# 触发点 3：stop_loss_monitor —— 盘中持仓跌破止损价 → 卖出（qty 来自 gw 持仓）
# ============================================================================
async def stop_loss_monitor(
    stop_prices: Optional[Mapping[str, float]] = None,
    *,
    gw: Any = None,
) -> dict:
    """盘中止损监控：拉 gw 真实持仓 + 现价，跌破止损价即发卖出单。

    ⚠️ live 安全红线（scope #3）：卖出 qty **必须**来自 gw._fetch_broker_positions()
    返回的真实持仓，**绝不硬编码**——硬编码 100 会导致实盘卖错数量（致命）。

    Args:
        stop_prices: {symbol: stop_price}。None 时由调用方（TradingEngine._stoploss）
                     从活跃计划读；本函数聚焦决策与下单，不耦合计划存储。
        gw:          网关实例（测试注入）；None 时内部 get_gateway()。

    Returns:
        盘中：{"checked":N, "stop_triggered":M, "mode":...}
        非盘中：{"checked":0, "reason":"非盘中时段..."}
        无 gw：{"checked":0, "reason":"...网关..."}

    边界与决策（Grill Me）：
    - 非盘中时段直接返 no-op（calendar.is_intraday_session）——午休/盘后不监控，
      避免无流动性时段挂单致滑点失控。
    - gw._fetch_broker_positions 过滤了 can_use_volume==0 的 T+1 冻结仓——本语义
      与「只能卖可卖仓」天然对齐（T+1 当日买入不可卖）。
    - 现价来源：优先 gw.get_price(symbol)；缺该方法时跳过该标的（不猜价）。
    """
    # ① 盘中时段判定（Task1）
    if not calendar.is_intraday_session(datetime.now()):
        return {"checked": 0, "reason": "非盘中时段（9:30-11:30 / 13:00-15:00），跳过止损监控"}

    # ② 取网关与持仓
    if gw is None:
        gw = get_gateway()
    if gw is None:
        logger.warning("stop_loss_monitor 跳过：交易网关未装配（gw=None）")
        return {"checked": 0, "reason": "交易网关未装配，无法查持仓"}
    if stop_prices is None or not stop_prices:
        return {"checked": 0, "reason": "无止损价配置（stop_prices 空）"}

    try:
        positions = await gw._fetch_broker_positions()  # {symbol: 可卖持仓 qty}
    except Exception:
        # 持仓查询失败绝不下卖出单（敞口未明即操作 = 盲卖，违反风控）
        logger.exception("stop_loss_monitor 查持仓失败（拒发任何卖出单）")
        return {"checked": 0, "reason": "查持仓异常，拒发卖出单"}

    # ③ 逐标的：拉现价 → 决策 → 下卖出单（qty 来自持仓 dict）
    from trading.execution_gateway import OrderRequest
    n_triggered = 0
    n_checked = 0
    for sym, qty in positions.items():
        sp = stop_prices.get(sym)
        if sp is None or qty <= 0:
            continue
        # 现价：优先 gw.get_price；缺该方法记 warning 跳过（不猜价）
        get_price = getattr(gw, "get_price", None)
        if get_price is None:
            logger.warning("stop_loss_monitor 跳过 %s：网关未实现 get_price", sym)
            continue
        try:
            price = await get_price(sym) if asyncio.iscoroutinefunction(get_price) else get_price(sym)
        except Exception:
            logger.exception("stop_loss_monitor 拉现价失败 %s（跳过）", sym)
            continue
        n_checked += 1
        if price is None:
            continue
        if price <= sp:
            # 跌破止损价：发卖出单。qty 来自 gw 真实持仓（绝不硬编码——scope #3 红线）。
            try:
                result = await _submit(
                    OrderRequest(symbol=sym, qty=qty, side="sell", price=price),
                    confirm=True,
                )
            except Exception as exc:
                # 挡板 raise（如断线 lock_down）：单只失败不阻塞其他标的止损
                logger.warning("stop_loss_monitor 卖出失败 symbol=%s qty=%s 原因=%s",
                               sym, qty, exc)
                continue
            if result.get("state") not in ("REJECTED", "FAILED"):
                n_triggered += 1
                logger.warning(
                    "【止损触发】%s 卖出 %s 股 @%s（止损价 %s，mode=%s）",
                    sym, qty, price, sp, _mode(),
                )

    logger.info("stop_loss_monitor 完成 checked=%d triggered=%d mode=%s",
                n_checked, n_triggered, _mode())
    return {"checked": n_checked, "stop_triggered": n_triggered, "mode": _mode()}


# ============================================================================
# 触发点 4：post_close —— 盘后对账 + 清动态白名单（熔断连线留 follow-up）
# ============================================================================
async def post_close(
    date: str,
    *,
    gw: Any = None,
    local_positions: Optional[Mapping[str, float]] = None,
    tolerance: float = 0.0,
) -> dict:
    """盘后：对账（run_reconcile） + 清动态白名单。

    Args:
        date:            T 日。
        gw:              网关（None 时内部 get_gateway）。
        local_positions: 本地理论持仓 {symbol: qty}；None 则跳过对账。
        tolerance:       持仓偏差容忍度（默认 0 零容忍）。

    Returns:
        {"date":..., "drift":bool}（drift=True 表示有偏差，run_reconcile 已告警）。

    ⚠️ follow-up（live 前必修，本 task 显式不做的部分）：
        本函数**不做**熔断连线（check_daily_loss_limit + cancel_all_open_orders +
        emergency_halt）。原因：daily loss 熔断需要 start_equity / curr_equity 两个
        基线值，plan/spec 未给 equity 数据源（trading_service 当前无 get_equity 公开
        接口），无来源的熔断是伪熔断（用 None/0 触发 = 永远不触发 或 误触发）。
        TODO(live 前必修)：定 equity 来源（如 gw.query_asset 或新增 get_equity 接口）
        后，在此处串联：
            1) check_daily_loss_limit(start_equity, curr_equity) → True 即熔断
            2) circuit_breaker.cancel_all_open_orders(gw) 撤所有未终态单
            3) trading_service.emergency_halt() 置 lock_down + 告警
        无上述三步，post_close 不算完成 live 准入。
    """
    result: dict = {"date": date}
    if gw is None:
        gw = get_gateway()

    # 对账：gw + local 齐全才跑（缺一不可，否则伪对账）
    if gw is not None and local_positions is not None:
        try:
            rec = await reconcile_job.run_reconcile(gw, local_positions, tolerance)
            # drift 判定：not is_ok 综合了 drifted/only_local/only_broker（Task7 契约）
            result["drift"] = not rec.is_ok
        except Exception:
            logger.exception("post_close 对账异常（不影响清白名单）")
            result["drift"] = True  # 异常视作有偏差（保守，触发人工排查）
    else:
        logger.info("post_close 跳过对账：gw=%s local_positions=%s",
                    "有" if gw is not None else "无",
                    "有" if local_positions is not None else "无")

    # 清动态白名单（Task5）：保证下一交易日从干净状态开始（防止昨日标的污染今日白名单）
    try:
        dynamic_whitelist.clear_dynamic_whitelist()
    except Exception:
        logger.exception("post_close 清动态白名单异常")

    logger.info("post_close 完成 date=%s drift=%s", date, result.get("drift"))
    return result


# ============================================================================
# TradingEngine：APScheduler 四 cron 装配（独立常驻进程 python -m trading）
# ============================================================================
class TradingEngine:
    """APScheduler 编排容器（四 cron 触发点装配 + start/shutdown 生命周期）。

    ⚠️ 不变量（再次强调，见模块 docstring）：本类实例**只在 ``python -m trading``
    独立进程内构造**，绝不在 server 进程内实例化（否则 dynamic_whitelist._DYNAMIC
    模块级全局会污染 server 手动下单路径，破坏 server 行为向后兼容）。

    四 cron（Task4 已配 env，缺省值对齐 A 股交易日历）：
        eod_plan   15:35 周一-五  T-1 晚扫信号 + 落计划 + 推钉钉
        pre_open   09:22 周一-五  T 日开盘前撤昨日 + 挂当日单
        stop_loss  */5  9-14 周一-五  盘中每 5 分钟止损监控
        post_close 15:30 周一-五  盘后对账 + 清白名单

    每个 job 先过 calendar.is_trading_day 判交易日（节假日整体跳过）。
    """

    def __init__(self) -> None:
        """装配 AsyncIOScheduler + 四 cron job（不 start）。"""
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger

        self.sched = AsyncIOScheduler()
        # 四 job 注册：id 显式命名便于 get_jobs 自检与外部调试
        self.sched.add_job(
            self._eod, CronTrigger.from_crontab(
                os.getenv("ENGINE_EOD_PLAN_CRON", "35 15 * * 1-5")),
            id="eod_plan",
        )
        self.sched.add_job(
            self._pre_open, CronTrigger.from_crontab(
                os.getenv("ENGINE_PRE_OPEN_CRON", "22 9 * * 1-5")),
            id="pre_open",
        )
        self.sched.add_job(
            self._stoploss, CronTrigger.from_crontab(
                os.getenv("ENGINE_STOPLOSS_CRON", "*/5 9-14 * * 1-5")),
            id="stop_loss",
        )
        self.sched.add_job(
            self._post_close, CronTrigger.from_crontab(
                os.getenv("ENGINE_POST_CLOSE_CRON", "30 15 * * 1-5")),
            id="post_close",
        )

    # ----- cron 包装：交易日判定 + 转调 async 触发函数 -----
    async def _eod(self) -> None:
        """cron 包装：节假日跳过，交易日调 eod_plan。

        信号扫描的 NecklineMethodStrategy 装配由 Task 10 ``__main__`` 注入（本类
        聚焦调度，不耦合策略实例化）；当前默认空信号占位，待 Task 10 接通。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        if not calendar.is_trading_day(today):
            logger.info("eod_plan 跳过：今日非交易日 %s", today)
            return
        # TODO(Task 10): 注入 NecklineMethodStrategy + 拉当日 universe → signals + atr_map
        await eod_plan(today, signals=[], atr_map={}, capital=float(os.getenv("TRADE_CAPITAL", "1_000_000")))

    async def _pre_open(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if not calendar.is_trading_day(today):
            logger.info("pre_open 跳过：今日非交易日 %s", today)
            return
        await pre_open(today)

    async def _stoploss(self) -> None:
        """cron 包装：止损监控（盘中时段判定在 stop_loss_monitor 内）。

        TODO(Task 10/follow-up)：从活跃计划 / 持仓状态机读当日 stop_prices map 注入。
        当前 stop_prices=None → stop_loss_monitor 内部返「无止损价配置」no-op。
        """
        await stop_loss_monitor(stop_prices=None)

    async def _post_close(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if not calendar.is_trading_day(today):
            logger.info("post_close 跳过：今日非交易日 %s", today)
            return
        await post_close(today)

    # ----- 生命周期 -----
    def start(self) -> None:
        """启动 scheduler（阻塞主线程进入事件循环由 ``__main__`` 负责）。"""
        self.sched.start()
        logger.warning("TradingEngine 已启动（mode=%s）——独立常驻进程运行", _mode())

    def shutdown(self) -> None:
        """优雅停机（wait=False：不等 pending job，进程退出场景）。"""
        self.sched.shutdown(wait=False)
        logger.info("TradingEngine 已停机")
