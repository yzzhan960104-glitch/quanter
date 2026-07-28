# -*- coding: utf-8 -*-
"""二期自动交易引擎：APScheduler 四触发点编排 + 影子模式分流。

物理意图（四触发点的真实业务节奏 · 术语对齐 T 日盘后扫盘 → T+1 执行）：
  eod_plan  19:00 T 日盘后：扫颈线法信号 → build_orders → save_plan（confirmed=False）
              → push 钉钉（待研究员确认）。本阶段绝不下单（机器只产计划，人审是闸）。
              次日（T+1 日）pre_open 才挂单执行。
              ⚠️ 非 15:35：须等 18:00 增量采集落湖 + 18:30 数据检查点② 通过，否则用
              T-1 数据算 T+1 计划（时序 bug · Task6 修复）。
  pre_open  09:22 T 日开盘前：① 撤昨日遗留未成交单 ② 读已确认计划
              → 注入动态白名单（过关5）→ 挂限价买 + 止盈限价卖（逐单 try-except 兜底）。
  stop_loss 每 5min 盘中：查 gw 真实持仓 + 现价（qmt_market_data.get_quote / xtdata），
              跌破止损价 → 发卖出单（qty 必须来自 gw 持仓，绝不硬编码——live 卖错数量 = 致命）。
              ⚠️ 止损链路依赖 xtdata 行情源（miniQMT 通道），无 xtdata 时 live 前需另接行情源（C1 follow-up）。
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
- 因此 ``presentation/server/main.py`` 的 lifespan **不应** import 本模块、不应构造 TradingEngine。
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
import json
import logging
import os
from dataclasses import replace as _dc_replace
from datetime import datetime
from typing import Any, Mapping, Optional

from trading import (
    calendar,
    dynamic_whitelist,
    qmt_market_data,
    reconcile_job,
    trading_plan,
)
from trading.io.breaker import cancel_all_open_orders as _cancel_all_open_orders
# Layer2 阶段6 follow-up #4a：signal_runner 垫片已删，直指真身 trading.compute.plan
from trading.compute.plan import build_orders_from_signals
from trading.compute.stop import should_trigger_stop, trading_days_between as _trading_days_between, compute_stop_price as _compute_stop_price
# Task 10（R-2 日内熔断）：check_daily_loss_limit 纯判定 functional core
from trading.compute.breaker import check_daily_loss_limit as _check_daily_loss_limit
# Task 10（R-2）：position_book 的 daily_equity reader/writer（snapshot/get_start）
from trading import position_book as _position_book

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

    ⚠️ stop_atr_mult 默认值对齐回测（plan Task 4 · P1-6 修复 · 2026-07-28）：
        历史默认 env=2.0 与回测 neckline/method_v0.DEFAULTS["stop_atr_mult"]=1.0 不一致，
        导致回测冠军档套实盘时止损价偏宽（颈线−2×ATR vs 颈线−1×ATR），风险敞口与
        回测预期脱钩。改默认 1.0 对齐回测基线；env 显式设置仍可覆盖（向后兼容）。

    TODO(follow-up · plan Task 14 param_iter 重跑后落地)：spec §4.6 原设计是「_trade_cfg
        加 active_experiments 参数，从主力实验 params 读 stop_atr_mult」——待 param_iter
        新冠军档稳定后单独落地（避免此时引入「读实验」耦合扩大本 task 改动面）。
    """
    return {
        "pos_cap": float(os.getenv("TRADE_POS_CAP", "0.05")),
        # 颈线法 id_cfg 默认；实盘从 NecklineConfig 读（本引擎薄编排，不重算）
        # P1-6 修复：默认 1.0 对齐回测 DEFAULTS（原 2.0 与回测不一致致风险敞口翻倍）
        "stop_atr_mult": float(os.getenv("TRADE_STOP_ATR_MULT", "1.0")),
        "tp_h_mult": float(os.getenv("TRADE_TP_H_MULT", "2.0")),
        # 地基（live-readiness Task 2）：挂单等待回踩有效期日，pre_open max_wait 窗口过滤用
        "max_wait": int(os.getenv("TRADE_MAX_WAIT", "5")),
        # 分级止盈（Task 7 · P0-3 对齐）：tp1_h_mult×H 锁利位 + tp1_portion 比例。
        # Why 默认 1.0/0.5 对齐 NecklineConfig EXEC_DEFAULTS（颈线+1.0×H 锁 50% 仓）：
        #     simulate_exit 用同口径加权两批，实盘 _place_take_profit 据此挂两张限价卖单。
        # env 缺省 → 对齐回测；显式 env 可覆盖（灰度调参用）。
        "tp1_h_mult": float(os.getenv("TRADE_TP1_H_MULT", "1.0")),
        "tp1_portion": float(os.getenv("TRADE_TP1_PORTION", "0.5")),
        # 占位（M1）：海龟 trailing 动态止损参数——grace/step/floor 三件套本 task 未实际消费，
        # compute_stop_price 盘后重算（Task2 已就绪）需 Task 10 在引擎状态层维护
        # {symbol: stop_price} 并每日/盘中更新后注入 stop_loss_monitor；本 task 的
        # stop_loss_monitor 直接用活跃计划里的静态 stop_prices，不涉及 trailing 动态更新。
        "grace": int(os.getenv("TRADE_STOPLOSS_GRACE_DAYS", "5")),
        "step": float(os.getenv("TRADE_STOPLOSS_STEP_ATR", "0.1")),
        "floor": float(os.getenv("TRADE_STOPLOSS_FLOOR", "0.5")),
        # max_holding（Task 8 · P0-4 超时平仓）：成交后超时持仓周期（交易日），对齐回测
        # strategies/neckline/backtest.py MAX_HOLDING=15。post_close 扫超期 → 次日 pre_open
        # 跌停价平仓释放资金（对齐回测「成交后 max_holding 日未达止盈收盘卖剩余」语义）。
        "max_holding": int(os.getenv("TRADE_MAX_HOLDING", "15")),
    }


# ============================================================================
# 策略数据源辅助（二期 gap② · _eod 从 data_lake 加载 universe + 单 symbol 前复权日线）
# ============================================================================
def _load_universe(lake) -> list:
    """加载创板科创可交易标的池（300/301/688/689 开头）。

    物理意图：复用 data_lake/a_shares_daily.parquet（MultiIndex date,symbol，全市场
    5 年前复权日线），按 symbol 前缀过滤创板科创。

    ⚠️ 性能不变量（Task 7b fix · 性能阻断级修复）：
        本函数**绝不 read_parquet**——lake 由调用方（``_eod``）入口一次性读入后注入，
        全创板科创 1993 个标的共用同一份 DataFrame。
        历史 bug：每个 symbol 都重读 455MB parquet（1.75s/次）→ 58 分钟纯 I/O，
        19:00 的 ``_eod`` 根本无法在合理窗口完成。复用 lake 后整体扫描降至秒级。

    Why 收窄创板科创（不扫全市场）：
        颈线法 param_iter 基线口径（记忆 neckline-paramiter-baseline）——创板科创
        20cm 涨跌幅 + 流动性结构更契合颈线法形态学假设；主板/北交所不在该策略可交易池。
        实际环境若需扩池，按实际前缀在此调整（spec 红线：本过滤口径变更需同步基线重算）。
    """
    # lake 已由 _eod 入口 read_parquet 一次注入，此处仅做 symbol 前缀过滤（零 I/O）
    syms = lake.index.get_level_values("symbol").unique().tolist()
    return [s for s in syms if s.split(".")[0].startswith(("300", "301", "688", "689"))]


def _load_df_upto(lake, symbol: str, date: str):
    """从已加载的 lake 取 symbol 截至 date 的前复权日线（严格因果 .loc[:date] · 无前视）。

    Args:
        lake:   ``_eod`` 入口一次性 ``pd.read_parquet`` 读入的 data_lake DataFrame
                （MultiIndex date,symbol）。本函数**不 read_parquet**，避免每 symbol 重读。
        symbol: 形如 "300001.SZ"（与 data_lake MultiIndex level="symbol" 一致）。
        date:   截断日（YYYY-MM-DD，_eod 传 T 日盘后日 today——见下方术语说明）。

    Returns:
        该 symbol 截至 date（含 date）的前复权日线 DataFrame（OHLCV，DatetimeIndex）；
        symbol 不在 data_lake → 返 None（调用方 None-check 跳过）。

    ⚠️ 性能不变量（Task 7b fix）：
        本函数**绝不 read_parquet**——从传入的 lake 做 xs 切片，全创板科创 universe
        复用同一份 DataFrame，1993 次 xs 从 1993 次 disk read 降为纯内存索引（毫秒级）。

    Why xs+sort_index+loc：
        - xs(level="symbol") 取单 symbol 切片（MultiIndex 标准范式）；
        - sort_index 保时间升序（ATR/MA 等时序算子前提）；
        - .loc[:date] 闭区间截断，防 today 之后的 K 线泄漏（前视偏差 = 回测致命）。
    """
    try:
        return lake.xs(symbol, level="symbol").sort_index().loc[:date]
    except KeyError:
        # symbol 不在 data_lake（新上市/退市/代码漂移）→ 返 None，调用方跳过
        return None


# ============================================================================
# plan Task 5（P0-5 cooldown 信号去重）：扫最近 cooldown 日 plan formed_at 标的集
# ============================================================================
def _load_recent_plan_symbols(days_back: int, today: str) -> set[str]:
    """扫 logs/trading_plans/plan_*.json 最近 N 自然日（含 today）含 formed_at 的 symbol 集。

    物理意图（plan Task 5 · 对齐缺口 P0-5）：
        scan_live 无跨日去重，同形态被持续突破会连续多日触发信号 → 实盘连续挂单超额成交。
        spec §4.5：_eod scan 后查最近 cooldown 日 plan formed_at，同标的丢弃。

    Why 用 formed_at 而非 order.symbol：
        formed_at（Task 2 落盘）= 信号突破日，是「该标的最近一次被识别为信号」的真实时间锚点；
        order.symbol 仅在「该标的进了 plan」时存在，但形成的信号可能被 max_wait 过滤掉未进 plan
        ——用 formed_at 才能覆盖所有「被识别为信号」的标的（无论是否最终挂单）。
        老 plan（Task 2 前，无 formed_at）兜底用 order.symbol（粗近似）。

    Why 自然日回溯而非交易日：
        cooldown 参数（exec_cfg["cooldown"]）本身是【交易日】单位（颈线法 EXEC_DEFAULTS），
        但本函数用自然日回溯是**保守上界**——自然日≥交易日数（含周末），故 cooldown=5
        交易日 ≈ 7 自然日；用 cooldown=5 自然日回溯可能漏掉周五+周末的 5 交易日窗口。
        取 days_back=cooldown+2（含周末余量）作为保守窗口，避免周末边界漏判。

    Args:
        days_back: 回溯自然日数（含 today），调用方传 cooldown+2 余量。
        today:     YYYY-MM-DD（_eod 调用时传 datetime.now()）。

    Returns:
        最近 days_back 自然日 plan 含 formed_at 的 symbol 集；plan 损坏/无 plan 返空集（保守不误杀）。
    """
    import json as _json
    from datetime import datetime as _dt, timedelta as _td
    from pathlib import Path as _Path
    syms: set[str] = set()
    try:
        plan_dir = _Path(os.getenv("TRADE_PLAN_DIR", "logs/trading_plans"))
        if not plan_dir.exists():
            return syms
        today_dt = _dt.strptime(today, "%Y-%m-%d")
        # 回溯窗口：[today - days_back + 1, today]（含 today）
        for i in range(days_back):
            d = (today_dt - _td(days=i)).strftime("%Y-%m-%d")
            p = plan_dir / f"plan_{d}.json"
            if not p.exists():
                continue
            try:
                plan = _json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                # 单 plan 损坏不影响其他 plan 扫描（保守继续，不抛）
                continue
            for o in plan.get("orders", []):
                # 优先 formed_at（真实信号突破日）；老 plan 无 formed_at 兜底用 order.symbol
                if o.get("formed_at") or (o.get("order") or {}).get("symbol"):
                    sym = (o.get("order") or {}).get("symbol")
                    if sym:
                        syms.add(sym)
    except Exception:
        # 整体异常（IO/权限）返空集，保守不误杀（让所有新信号都通过，由人审闸兜底）
        logger.exception("_load_recent_plan_symbols 异常返空集（cooldown 不去重）")
        return set()
    return syms


def _resolve_cooldown_days(experiments: list) -> int:
    """从 experiments 提取 cooldown（取所有实验里的最大值，保守去重）。

    Why 取 max 而非首个：多实验灰度场景下，不同实验可能配不同 cooldown（如新档 5、旧档 3）。
    按「最长 cooldown 跨日去重」最保守——避免新档信号被旧档短 cooldown 漏去重。
    缺失/异常返 0（不去重，向后兼容老链路）。
    """
    if not experiments:
        return 0
    try:
        cooldowns = []
        for exp in experiments:
            params = getattr(exp, "params", None) or {}
            cd = params.get("cooldown")
            if cd is not None and int(cd) > 0:
                cooldowns.append(int(cd))
        return max(cooldowns) if cooldowns else 0
    except Exception:
        logger.exception("_resolve_cooldown_days 异常返 0（不去重）")
        return 0



def get_gateway():
    """惰性取交易网关单例（透传 trading_service.get_gateway）。

    Why 透传不重造：网关单例的装配（QMT 唯一，无凭证→None）与懒构造策略已在
    trading_service.get_gateway 固化，本引擎薄编排不重复，避免双单例漂移。
    本函数独立出来便于测试 monkeypatch（engine.get_gateway）隔离真实网关副作用。
    """
    from presentation.server.services.trading_service import get_gateway as _svc_get_gw
    return _svc_get_gw()


async def _submit(order, *, confirm: bool = True) -> dict:
    """下单分流（dry_run 据 _mode）。

    透传 trading_service.submit_order，其契约：
    - dry_run 命中 → 返 {"order_id":"", "state":"DRY_RUN", "message":<reason>}（不真下单）
    - 真单成功   → 返 {"order_id":<seq>, "state":<OrderState.name 字符串>, "message":...}
    - 挡板命中（非 dry_run）→ **raise RuntimeError**（调用方必须 try-except 兜底）

    Why dry_run 用 _mode() 而非参数注入：pre_open/stop_loss 都是「影子即整批不真单」
    语义，_mode 是进程级开关，逐单传参反而引入「单只切 live」的误操作面。

    Why confirm 默认 True（I2）：引擎是**自动批量下单通道**，盘中无人工在场做二次确认；
    风控由 risk_shield 10 关挡板（资金/涨跌停/白名单/熔断 lock_down）+ T-1 确认闸
    （pre_open 必须研究员人工 confirmed=True 才挂单）+ 影子模式前置（≥5 天影子观测）
    三层保障，**而非** confirm 开关——confirm 是 server 手动下单路径的防误触开关，
    引擎通道若走 confirm=False 会导致批量挂单逐单等待人工点确认，盘中不可行。
    """
    from presentation.server.services.trading_service import submit_order as svc_submit
    return await svc_submit(order, dry_run=(_mode() == "dry_run"), confirm=confirm)


# ============================================================================
# 触发点 1：eod_plan —— T 日盘后扫信号、落计划、推钉钉（不真下单）
# ============================================================================
async def eod_plan(date: str, signals: list, atr_map: dict, capital: float) -> dict:
    """T 日盘后：颈线法信号 → 计划落盘（confirmed=False） → 推钉钉等研究员确认。

    物理意图（术语对齐物理时序 · Task 7b fix）：
        本函数由 ``_eod`` 在 **T 日盘后 19:00** 调用（Task6 时序修复：原 15:35 因增量
        采集 @18:00 尚未落湖致读到 T-1 数据，挪 19:00 等数据落湖 + 检查点② 通过），扫 T 日新突破信号，产 T+1 日
        生效计划；机器批量扫信号易受数据瑕疵/前视偏差/极端行情误判，T 日盘后必须给人
        一次否决机会——故本函数只产计划不下单（spec §2 确认闸红线）。

    Args:
        date:     T+1 日（计划生效日），如 "2026-07-22"。由 _eod 传
                  ``calendar.next_trading_day(today)``（T 日盘后算次日交易日），物理上
                  计划在 T+1 日 pre_open 挂单执行（date 与 load_plan 读取口径对齐）。
        signals:  NecklineMethodStrategy.scan_live 返回的 list[Signal]（Layer2 阶段1 后为
                  frozen dataclass，_eod 已用 dataclasses.replace 注入实验归因字段）。
        atr_map:  {symbol: ATR}，缺 ATR 的标的（_eod 已过滤）在 build 阶段被跳过（不抛）。
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
        stop_cfg={
            "stop_atr_mult": cfg["stop_atr_mult"], "tp_h_mult": cfg["tp_h_mult"],
            "max_wait": cfg["max_wait"],
            # Task 7：tp1_h_mult/tp1_portion 透传 build_orders 算 tp1 锁利位
            "tp1_h_mult": cfg["tp1_h_mult"], "tp1_portion": cfg["tp1_portion"],
        },
    )
    # 序列化为嵌套 dict（Task8 契约硬约束：order + stop_price + take_profit 三段）
    # 透传 experiment_id/experiment_weight 归因（Task5 PlannedOrder 携带）：
    # Why：report 阶段需按 experiment_id 聚合实验分组，归因字段必须随 order_dict
    # 一起落盘到 trading_plan JSON，否则 Task8 拿不到实验归因的物理基础。
    # 老计划（无归因字段）由 load_plan / report 阶段向后兼容归「未归因」桶，不在此处处理。
    order_dicts = [
        {
            "order": {
                "symbol": o.order.symbol,
                "qty": o.order.qty,
                "side": o.order.side,
                # 挂单价 round 两位（A 股报价粒度=分；颈线回踩挂单同此约束）
                "price": round(o.order.price, 2),
            },
            # 止损/止盈/颈线 round 两位四舍五入：A 股最小报价 0.01 元，算法产出的高精度
            # 浮点（如 stop=10.350317475825085）无实际意义，且污染人审/复盘可读性。
            # Why 序列化层 round 而非 compute 层：compute 是 functional core 保持全精度
            # （风控参数计算精度不折损），仅落盘+展示约束两位（IO 层关切）。
            "stop_price": round(o.stop_price, 2),
            "take_profit": round(o.take_profit, 2),
            # 颈线价（形态基准 c*）：透出供 push_plan_to_dingtalk md 显式标注「颈线」，
            # 让研究员 T-1 晚人审时一眼确认形态基准位（止损/止盈均以颈线为锚）。
            # scan_live 下 entry_price=neckline，故与 order.price 同值——显式单列是为
            # 语义清晰（挂单价=回踩位 vs 颈线=形态位），未来 entry≠neckline 时自然分叉。
            "neckline": round(o.neckline, 2),
            # 地基字段（live-readiness Task 2）：atr 供 trailing 盘后演进 / formed_at 供 pre_open
            # max_wait 窗口判定 / max_wait 有效期。精度：atr round 4 位（计算用），其余原值。
            "atr": round(o.atr, 4),
            "formed_at": o.formed_at,
            "max_wait": o.max_wait,
            # 分级止盈（Task 7 · P0-3）：tp1 锁利位 + tp1_portion 分配比例。
            # Why 落盘：_place_take_profit 盘中买单成交时读 plan.tp1/tp1_portion 拆两张卖单，
            # 必须在 plan JSON 持久化（盘中进程重启 / 切换执行机都能读回）。
            # tp1 可能为 None（老 cfg 不配 tp1_h_mult）→ JSON null，_place_take_profit 检测
            # falsy 退回 tp2 单笔全平（向后兼容）。
            "tp1": round(o.tp1, 2) if o.tp1 is not None else None,
            "tp1_portion": o.tp1_portion,
            "experiment_id": o.experiment_id,           # 透传实验归因（Task5 → Task8 链路）
            "experiment_weight": o.experiment_weight,   # 透传实验权重（Task8 加权聚合用）
            # R3 实际口径盈亏比（2026-07-27 Task 2）：从 PlannedOrder.rr 透传到 order_dict，
            # 供 push_plan_to_dingtalk md 渲染「盈亏比N.N」+ trading_plan JSON 落盘存档。
            # Why 落盘：研究员复盘时可按 rr 排序找出弱信号历史，迭代 min_rr 守卫阈值。
            "rr": round(o.rr, 2),
        }
        for o in orders
    ]
    # 落盘 + 确认闸：AUTO_CONFIRM_PLAN=true（全自动模式）→ 落盘后自动 confirm_plan 置
    # confirmed=True，pre_open 次日直接挂单（opt-out：研究员 pre_open 前可 veto 拦截）；
    # 默认 false → confirmed=False 保持人审（回复「确认」才挂，spec §2 红线，向后兼容）。
    trading_plan.save_plan(date, order_dicts)
    auto_confirmed = os.getenv("AUTO_CONFIRM_PLAN", "").lower() in ("true", "1", "yes")
    if auto_confirmed:
        trading_plan.confirm_plan(date)  # 全自动：落盘即确认，pre_open 直挂
    # 持仓段注入 QMT 真实持仓（全量口径，和持仓播报同源）：研究员钉钉人审要看券商实际
    # 仓位（含 T+1 冻结），而非 engine 记账（dry_run 空仓 + 不含 smoke 直接 broker 操作）。
    # 网关未连/异常 → None，push 内部退回 position_book 本地账本（软降级，不阻断推送）。
    broker_positions = None
    try:
        from presentation.server.services.trading_service import get_positions as _get_positions
        broker_positions = await _get_positions()
    except Exception:
        logger.warning("eod_plan 拉 QMT 持仓失败，交易计划持仓段退回本地账本")
    trading_plan.push_plan_to_dingtalk(
        date, order_dicts, broker_positions=broker_positions, auto_confirmed=auto_confirmed)
    logger.info("eod_plan 完成 date=%s n_orders=%d mode=%s auto_confirmed=%s",
                date, len(orders), _mode(), auto_confirmed)
    return {"date": date, "n_orders": len(orders), "mode": _mode(), "auto_confirmed": auto_confirmed}


# ============================================================================
# 触发点 2：pre_open —— T 日开盘前：撤昨日单 + 检查确认闸 + 挂当日买单
# ============================================================================
async def pre_open(date: str) -> dict:
    """T 日开盘前：读已确认计划 → 撤昨日遗留未成交单 → 注入白名单 → 逐单挂单。

    物理意图与时序（顺序不可调，与代码实际执行顺序一致）：
        ① 确认闸检查（spec §2 红线）：未确认 → 一律不挂，返「计划未确认」。
           **必须最先做**——确认闸未通过即不应触达任何网关写操作（含撤昨日单），
           否则会误撤昨日已确认单（研究员当日已审核，机器无权撤）。
        ② 撤昨日未成交（scope #2）：避免昨日挂单与新计划叠加导致超额成交。
           仅在 ① 确认闸通过后才撤，避免误撤昨日已确认单。
        ③ 注入动态白名单（Task5）：让当日计划标过关5，但仅在本 engine 进程内生效
           （独立进程不变量，见模块 docstring）。
        ④ 逐单挂单 + try-except 兜底（scope #7）：单标的挡板命中 raise 不炸整批。

    Args:
        date: T 日（如 "2026-07-22"）。

    Returns:
        {"submitted":<成功挂单数>, "mode":..., "reason"?:...}。

    ⚠️ gw=None 行为诚实说明（I3）：
        - **dry_run 模式**：gw=None 仍可继续挂单——submit_order 内部命中 dry_run
          分支返 ``{"state":"DRY_RUN"}`` 不触达 gw，submitted 计数正常（影子观测用）。
        - **live 模式**：gw=None 时 submit_order 会 ``raise RuntimeError``（缺网关），
          被下方逐单 try-except 吞掉，**submitted=0**（全部失败）。
        - **结论**：live 部署前**必须**确保 gateway 已连接（``get_gateway()`` 返非 None），
          否则当日计划一支也挂不上。
    """
    plan = trading_plan.load_plan(date)
    if plan is None:
        return {"submitted": 0, "reason": "无计划"}
    if not plan.get("confirmed"):
        # 未确认绝不挂单（spec 红线）：宁可漏挂，不挂研究员未审核的单。
        return {"submitted": 0, "reason": "计划未确认，跳过挂单"}

    # ② 撤昨日未成交（scope #2）：仅在确认闸（①）通过后才撤，避免误撤昨日已确认单。
    gw = get_gateway()
    if gw is None:
        # gw 未装配：影子模式仍可挂 DRY_RUN（dry_run 命中不触达 gw）；真单模式下
        # 挂单也会因 gw=None 抛 RuntimeError 由下方 try-except 吞掉，故这里只 warning。
        logger.warning("pre_open 撤昨日单跳过：交易网关未装配（gw=None）")
    else:
        try:
            n_cancelled = await _cancel_all_open_orders(gw)
            logger.info("pre_open 撤昨日未成交单 %s 笔", n_cancelled)
        except Exception:
            # 撤单失败不阻塞挂单主路径（单笔失败已在 cancel_all 内被吞，此处兜整体异常）
            logger.exception("pre_open 撤昨日单整体异常（继续挂新单）")

    # ②.5 抓日内熔断基线（Task 10 · R-2 日内熔断 · spec §5.2）：
    # 物理意图：post_close 判 -3% 熔断需要 start_equity 基线，开盘前是唯一可靠的
    # 「未受当日交易影响」时点。pre_open 在确认闸 + 撤昨日单后调 gw.query_asset
    # 抓当日开盘总资产 → snapshot_start_equity 写 daily_equity 表（幂等 INSERT OR REPLACE，
    # 进程崩溃重启重入安全）。
    # 边界（红线）：
    # - gw=None / query_asset 返 {}（未连接/锁定/超时）→ 跳过 + WARN，绝不拿 0/None
    #   写基线（否则 post_close check_daily_loss_limit(0, curr) 返 False 反永不熔断）；
    # - query_asset 异常 → 跳过 + 告警（不阻塞挂单主路径）。
    # Why 在撤单后而非前：撤单不影响总资产（仅未成交单状态变化），先后顺序无关；
    # 放后面可与撤单共用同一个 gw 引用，且「撤完昨日 → 抓今日基线」语义更清晰。
    today_eq = datetime.now().strftime("%Y-%m-%d")
    if gw is not None:
        try:
            asset = await gw.query_asset() if hasattr(gw, "query_asset") else {}
            total = (asset or {}).get("total_asset")
            if total is not None and float(total) > 0:
                _position_book.snapshot_start_equity(today_eq, float(total))
                logger.info("pre_open 日内熔断基线已抓取 date=%s start_equity=%s",
                            today_eq, float(total))
            else:
                # query_asset 返空（未连接/锁定/超时）→ 不写基线，让 post_close 跳过熔断
                logger.warning("pre_open 跳过熔断基线快照：query_asset 返空 date=%s", today_eq)
        except Exception:
            logger.exception("pre_open 抓熔断基线异常（不阻塞挂单主路径） date=%s", today_eq)
    else:
        logger.warning("pre_open 跳过熔断基线快照：gw=None date=%s", today_eq)

    # ②.6 Task 8（P0-4 max_holding）：平昨日盘后标记的超期持仓（跌停价释放资金）。
    # 物理意图：回测 max_holding 超时平仓对齐——持仓超 max_holding 日未达止盈即收盘平，
    # 实盘在【次日 pre_open】挂跌停价卖单（保证成交，接受滑点；超时释放资金不等好价位）。
    # Why 在挂新买单前：先释放超期资金占用再挂新单（资金可用度更准）；卖单与买单方向
    # 相反不冲突。qty 必须来自 gw 真实持仓（红线，同 stop_loss_monitor，绝不硬编码）。
    expired_positions = _load_expired_positions()
    if expired_positions:
        await _close_expired_positions(gw, expired_positions)

    # ③ 注入动态白名单（Task5）：仅 engine 进程生效，server 进程不受影响。
    symbols = {o["order"]["symbol"] for o in plan["orders"]}
    dynamic_whitelist.inject_dynamic_whitelist(symbols)

    # ④ 逐单挂单 + raise 兜底（scope #7）
    from trading.compute.types import OrderRequest  # Layer2 阶段6 follow-up #4b：execution_gateway 垫片已删，直指 compute.types 真身
    # plan Task 6（P0-2 max_wait 窗口）：pre_open 按 formed_at+max_wait 过滤超期信号。
    # 物理意图：回测信号后 max_wait 天窗口等回踩；实盘原口径只挂 1 天（次日 pre_open 撤昨日），
    # 改为窗口内每日可挂（回测对齐）。窗口外（trading_days > max_wait）的信号视为过期跳过。
    # 边界：order 缺 formed_at → days=0 视窗口内挂单（向后兼容老 plan）；缺 max_wait → 用 _trade_cfg 默认 5。
    today_for_max_wait = datetime.now().strftime("%Y-%m-%d")
    cfg_max_wait = int(os.getenv("TRADE_MAX_WAIT", "5"))
    n_submitted = 0
    n_expired = 0
    for o in plan["orders"]:
        od = o["order"]
        # max_wait 窗口过滤（plan Task 6）
        formed_at = o.get("formed_at")
        if formed_at:
            order_max_wait = int(o.get("max_wait") or cfg_max_wait)
            days_since = _trading_days_between(formed_at, today_for_max_wait)
            if days_since > order_max_wait:
                n_expired += 1
                logger.info("pre_open 跳过超期信号 symbol=%s formed_at=%s days=%d > max_wait=%d",
                            od["symbol"], formed_at, days_since, order_max_wait)
                continue
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

    logger.info("pre_open 完成 date=%s submitted=%d/%d expired=%d mode=%s",
                date, n_submitted, len(plan["orders"]), n_expired, _mode())
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

    ⚠️ live 止损现价依赖（C1 fix + T3 批量）：现价统一从
    ``trading.qmt_market_data.get_quotes(list(positions.keys()))`` 批量取 ``last_price``。
    **该接口底层是 xtdata.get_full_tick，仅在 miniQMT 通道可用时返回有效快照**；
    **止损链路依赖 xtdata 行情源（miniQMT 通道），无 xtdata 时 live 前必须另接行情源（live 前必修 follow-up，
    切勿在无 xtdata 行情源的环境切 live）**。
    若 ``get_quotes`` 返回的某标的 quote 为 None 或 ``last_price`` 为 None/NaN，
    则该标的跳过止损检查（无现价不能判断跌破）并记 warning，绝不发盲价卖出单。

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
    - 现价来源：``qmt_market_data.get_quote(sym)["last_price"]``；quote=None 或
      last_price=None/NaN 记 warning 跳过（不猜价、不发盲单）。
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
        positions = await gw._fetch_broker_positions()  # {symbol: {volume, ...}}（T7 扩展）
    except Exception:
        # 持仓查询失败绝不下卖出单（敞口未明即操作 = 盲卖，违反风控）
        logger.exception("stop_loss_monitor 查持仓失败（拒发任何卖出单）")
        return {"checked": 0, "reason": "查持仓异常，拒发卖出单"}

    # ③ 批量取所有持仓现价（T3 优化）：一次性 get_quotes 替代循环单只 get_quote。
    #   Why 批量：N 只持仓原 N 次 get_full_tick 线程池调用 → 1 次（原生 list 入参，
    #   xtdata.html 契约），减少 GIL 切换与 C++ 调用开销；颈线法盘中 5min 巡查场景下
    #   显著降低行情查询延迟（极端行情下高频止损检查更及时）。
    #   ⚠️ xtdata 通道（miniQMT）返快照；无 xtdata 时 live 前需另接行情源。
    from trading.compute.types import OrderRequest  # Layer2 阶段6 follow-up #4b：execution_gateway 垫片已删，直指 compute.types 真身
    quotes = await qmt_market_data.get_quotes(list(positions.keys()))
    n_triggered = 0
    n_checked = 0
    # T7：positions 现为 {sym: {volume, avg_price, ...}}（dict-of-dict），qty 取 volume 子键。
    # 旧契约 {sym: float} 已废弃；真实 QmtExecutionGateway._fetch_broker_positions 返新契约。
    for sym, pos in positions.items():
        qty = pos["volume"] if isinstance(pos, dict) else pos  # 兼容老 mock 返 float
        sp = stop_prices.get(sym)
        if sp is None or qty <= 0:
            continue
        # 现价（C1 fix + T3 批量）：从批量 dict 读，不再循环单只 get_quote。
        quote = quotes.get(sym)
        price = quote.get("last_price") if quote else None
        if price is None or price != price:  # NaN check（price != price ⟺ isNaN）
            # 现价缺失/NaN 绝不下卖出单：无价不能判断跌破（盲单 = 卖错价 = 致命）
            logger.warning("stop_loss_monitor 跳过 %s：现价缺失（quote=%s），无法判定跌破", sym, quote)
            continue
        n_checked += 1
        # 跌破判定（Layer2 阶段5 · 四缠拆解）：业务判定下推 compute.should_trigger_stop
        # 纯函数（functional core 单源），本编排层只调 compute 拿结果决定走哪条支路。
        # 物理零改：原 ``if price <= sp`` 与 should_trigger_stop(price, sp) 逐字同义
        # （都是 <= 触发，阈值线上下穿越一律触发防状态机悬挂）。
        if should_trigger_stop(price, sp):
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
# Task 8（P0-4 max_holding 超时平仓）：post_close 标记超期 + pre_open 跌停价平仓
# ============================================================================
# 超期持仓标记文件（post_close 覆盖写，pre_open 读后消费删除）。
# Why 单文件不进 db：这是一次性「次日要平」的待办标记，pre_open 消费即删，无需持久化/
# 审计（持仓周期信息在 position_book.entry_date 已存，本文件仅跨日传递「昨日盘后判定」）。
_EXPIRED_POSITIONS_PATH = os.path.join("logs", "expired_positions.json")


def _scan_expired_positions(today: str, max_holding: int) -> list[dict]:
    """扫超期持仓（holding_days > max_holding 的 {symbol→entry_date}）。

    物理意图（plan Task 8 · 对齐回测 MAX_HOLDING 超时平仓）：
        回测里成交后 max_holding 日未达任一止盈即收盘卖剩余；实盘对齐——post_close 用
        position_book.entry_date 算 holding_days（交易日口径，trading_days_between），
        >max_holding 即标超期，写 expired_positions.json 供次日 pre_open 平仓。

    边界：
        - get_entry_dates 仅返 qty!=0 且 entry_date NOT NULL 的持仓（Task 1 前老数据无
          entry_date 视作未超期，向后兼容——不盲平无周期信息的持仓）；
        - holding_days<=max_holding 视窗口内不标（含 ==：第 max_holding 日仍给足机会）。
    """
    expired: list[dict] = []
    for sym, entry_date in _position_book.get_entry_dates().items():
        holding_days = _trading_days_between(entry_date, today)
        if holding_days > max_holding:
            expired.append({
                "symbol": sym, "entry_date": entry_date,
                "holding_days": holding_days, "max_holding": max_holding,
            })
    return expired


def _write_expired_positions(date: str, expired: list[dict]) -> None:
    """post_close 覆盖写超期标记（logs/expired_positions.json）。

    覆盖写（w 模式）：每个交易日盘后重算重写，文件永远反映最新一次 post_close 结果。
    """
    os.makedirs(os.path.dirname(_EXPIRED_POSITIONS_PATH) or ".", exist_ok=True)
    payload = {"date": date, "written_at": datetime.now().isoformat(), "expired": expired}
    with open(_EXPIRED_POSITIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _load_expired_positions() -> list[dict]:
    """pre_open 读超期标记。文件不存在/损坏 → 返 []（无超期可平，软降级）。"""
    try:
        with open(_EXPIRED_POSITIONS_PATH, encoding="utf-8") as f:
            payload = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return payload.get("expired", []) if isinstance(payload, dict) else []


def _consume_expired_positions() -> None:
    """pre_open 平仓尝试后消费标记（删文件）。

    Why 无条件删：防 pre_open 崩溃重启后重复读标记 → 重复挂卖单 → 卖超（致命）。
    漏平（删了但部分标的没卖成）由人工对账兜底——风控宁可漏平也不重复卖超。
    """
    try:
        os.remove(_EXPIRED_POSITIONS_PATH)
    except FileNotFoundError:
        pass


async def _close_expired_positions(gw: Any, expired: list[dict]) -> dict:
    """平超期持仓：查 gw 真实持仓 → 跌停价挂卖 → 消费标记文件。

    风控红线（Grill Me · 同 stop_loss_monitor scope #3）：
        - 卖出 qty **必须**来自 gw._fetch_broker_positions 的真实持仓，绝不硬编码；
        - 价格优先跌停价（保证成交，超时释放资金接受滑点），无跌停价退化 last_price，
          都无则跳过（无价不发盲单=卖错价=致命）；
        - 消费时机：挂卖尝试【之后】无论成败都删标记（漏平<卖超，见 _consume）。

    Args:
        gw:      网关（dry_run 下 None → 无持仓可查，跳过+消费标记文件）。
        expired: _load_expired_positions 返回的超期列表。

    Returns:
        {"closed": <成功挂卖数>, "reason"?: ...}
    """
    from trading.compute.types import OrderRequest  # Layer2 阶段6 follow-up #4b：直指 compute.types 真身
    if gw is None:
        # dry_run 无网关无持仓可平；仍消费标记文件（切 live 前清掉 dry_run 期残留标记）
        logger.warning("跳过平超期持仓：gw=None（dry_run 无持仓可平，消费标记文件）")
        _consume_expired_positions()
        return {"closed": 0, "reason": "gw=None"}
    try:
        positions = await gw._fetch_broker_positions()
    except Exception:
        # 查持仓失败拒发卖单（敞口未明即操作=盲卖）且【保留标记】下次重试（不消费）
        logger.exception("平超期持仓查持仓失败（拒发卖单，保留标记下次重试）")
        return {"closed": 0, "reason": "查持仓异常"}
    # 批量取所有超期标的跌停价（对齐 stop_loss_monitor T3 批量模式，减 GIL/C++ 调用开销）
    try:
        quotes = await qmt_market_data.get_quotes([e["symbol"] for e in expired])
    except Exception:
        quotes = {}
        logger.exception("平超期持仓取行情异常（按无价处理，逐只跳过）")
    n_closed = 0
    for e in expired:
        sym = e["symbol"]
        pos = positions.get(sym)
        qty = pos["volume"] if isinstance(pos, dict) else pos  # 兼容老 mock 返 float
        if not qty or qty <= 0:
            continue
        quote = quotes.get(sym)
        low_limit = (quote or {}).get("low_limit")
        last_price = (quote or {}).get("last_price")
        # 跌停价优先（保证成交）；无跌停价退化 last_price；都无跳过（拒发盲单）
        price = low_limit if low_limit else last_price
        if price is None or price != price:  # NaN check（price!=price ⟺ isNaN）
            logger.warning("跳过平超期 %s：无跌停价/现价（拒发盲单，quote=%s）", sym, quote)
            continue
        try:
            result = await _submit(
                OrderRequest(symbol=sym, qty=qty, side="sell", price=price),
                confirm=True)
        except Exception as exc:
            # 挡板 raise（断线 lock_down 等）：单只失败不阻塞其他标的平仓
            logger.warning("平超期持仓失败 symbol=%s qty=%s 原因=%s", sym, qty, exc)
            continue
        if result.get("state") not in ("REJECTED", "FAILED"):
            n_closed += 1
            logger.warning("【超期平仓】%s 卖出 %s 股 @%s（holding_days=%s max_holding=%s mode=%s）",
                           sym, qty, price, e.get("holding_days"), e.get("max_holding"), _mode())
    # 消费标记文件（无论是否全部成功，避免下次 pre_open 重复挂卖单致卖超）
    _consume_expired_positions()
    logger.info("平超期持仓完成 closed=%d/%d mode=%s", n_closed, len(expired), _mode())
    return {"closed": n_closed}


def _evolve_trailing_stops(
    orders: list[dict], entry_dates: Mapping[str, str], today: str, cfg: dict,
) -> int:
    """盘后演进 plan orders 的 stop_price（海龟 trailing，holding_days 驱动）。

    物理意图（plan Task 9 · spec §5.3）：
        compute_stop_price 已实现但实盘零调用（env 读 grace/step/floor 未消费）。post_close
        盘后演进让 trailing 真正生效——对每个【已成交持仓】按 holding_days 重算【次日】固定
        stop_price：holding_days<=grace 用 base_stop（颈线-stop_mult×ATR，给趋势确认空间），
        holding_days>grace 每日收紧 step×ATR（eff_mult 递减），到 floor 卡底。盘中监控用此
        固定价不移动（spec「盘中不调整 stop」红线），故仅盘后演进一步/日。

    边界（spec §5.3）：
        - holding_days=0（今日成交 / 缺 entry_date）→ compute_stop_price 返 base_stop（零回归，
          Task 9 上线日不改变今日新成交持仓的止损）；
        - 缺 neckline/atr 的 order 跳过（无基准无法重算，老 plan 向后兼容）；
        - entry_dates 无该 symbol（未成交）→ holding_days=0（等同 base_stop）。

    Args:
        orders:      plan["orders"]（嵌套 dict，含 order.symbol/neckline/atr/stop_price）。
        entry_dates: position_book.get_entry_dates() 的 {symbol: entry_date}。
        today:       今日（holding_days 的 end）。
        cfg:         _trade_cfg()（取 stop_atr_mult/grace/step/floor）。

    Returns:
        演进成功的 order 数（缺 neckline/atr 的不计）。
    """
    n_evolved = 0
    for o in orders:
        sym = (o.get("order") or {}).get("symbol")
        neckline = o.get("neckline")
        atr = o.get("atr")
        # 缺基准（老 plan 无 neckline/atr 或数据瑕疵）跳过——不拿 None 算 stop_price
        if not sym or neckline is None or not atr:
            continue
        entry_date = entry_dates.get(sym)
        holding_days = _trading_days_between(entry_date, today) if entry_date else 0
        new_stop = _compute_stop_price(
            float(neckline), float(atr), holding_days,
            cfg["stop_atr_mult"], cfg["grace"], cfg["step"], cfg["floor"])
        o["stop_price"] = round(new_stop, 2)   # round 2 对齐 A 股 0.01 元精度
        n_evolved += 1
    return n_evolved


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
    """盘后：对账（run_reconcile） + 盘后兜底 + 日内熔断 + 清动态白名单。

    Args:
        date:            T 日。
        gw:              网关（None 时内部 get_gateway）。
        local_positions: 本地理论持仓 {symbol: qty}；None 则跳过对账。
        tolerance:       持仓偏差容忍度（默认 0 零容忍）。

    Returns:
        {"date":..., "drift":bool, "circuit_breaker":bool, "breaker_skipped"?:bool}
        - drift=True：对账有偏差（run_reconcile 已告警）
        - circuit_breaker=True：日内 -3% 熔断已触发（cancel_all + emergency_halt 已执行）
        - breaker_skipped=True：无基线跳过熔断（start_equity 未抓到，不拿 0 误触发）

    编排顺序（plan 红线 · spec §6 数据流）：
        ① reconcile（持仓对账）→ ② query_trades 兜底（Task 11 follow-up）
        → ③ 熔断（本 Task 10）→ ④ trailing（Task 9，未熔断时跑）
        → ⑤ max_holding 标记（Task 8，未熔断时跑）
        各段独立 try-except 软降级（单段异常不阻塞下一段）。

    ⚠️ 熔断三步（Task 10 · R-2 日内熔断 · spec §5.2）：
        1) get_start_equity(today) → start_equity（pre_open 快照的基线）
        2) gw.query_asset → curr_equity（盘后总资产）
        3) check_daily_loss_limit(start, curr) → True 即 cancel_all_open_orders +
           emergency_halt + ERROR 告警
        缺基线（start=None）→ 跳过 + WARN（不拿 0 触发，防 check(0,X) 永远 False 反永不熔断）。
    """
    result: dict = {"date": date}
    if gw is None:
        gw = get_gateway()

    # ① 对账：gw + local 齐全才跑（缺一不可，否则伪对账）
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

    # ③ 日内熔断三步（Task 10 · R-2 · 在 reconcile 之后）：
    # Why 在 reconcile 后：reconcile 查持仓 drift 是另一维度观测，与日内总资产 -3% 熔断
    # 互不依赖；放后面让熔断有最完整的 curr_equity（含盘后 reconcile 拉到的最新持仓估值）。
    # 各步独立 try-except 软降级：单段异常不阻塞清白名单和后续 trailing/max_holding。
    circuit_breaker_triggered = False
    breaker_skipped = False
    try:
        # 步骤 1：读 start_equity 基线（pre_open snapshot 写入 daily_equity 表）
        today_eq = datetime.now().strftime("%Y-%m-%d")
        start_equity = _position_book.get_start_equity(today_eq)
        if start_equity is None or start_equity <= 0:
            # 无基线（pre_open 未抓到 / 查询异常）→ 跳过熔断 + WARN
            # Why 显式跳过：check_daily_loss_limit(0, X) 虽返 False 但语义模糊，
            # 显式 breaker_skipped=True 让观测层知道「未判定」而非「判定未触发」。
            breaker_skipped = True
            logger.warning(
                "post_close 跳过日内熔断：无 start_equity 基线 date=%s"
                "（pre_open query_asset 失败？次日人工补基线）", today_eq)
        else:
            # 步骤 2：拉当前总资产（盘后总资产 = curr_equity）
            curr_equity = None
            if gw is not None and hasattr(gw, "query_asset"):
                try:
                    asset = await gw.query_asset()
                    curr_equity = (asset or {}).get("total_asset")
                except Exception:
                    logger.exception("post_close query_asset 异常（熔断路径降级跳过）")
            if curr_equity is None or float(curr_equity) <= 0:
                # curr 缺失同样跳过（不拿 0 触发）
                breaker_skipped = True
                logger.warning(
                    "post_close 跳过日内熔断：query_asset 返空 date=%s curr=None", today_eq)
            else:
                # 步骤 3：判定 + 触发三步（cancel_all + emergency_halt + 告警）
                triggered = _check_daily_loss_limit(
                    float(start_equity), float(curr_equity))
                if triggered:
                    logger.critical(
                        "【日内熔断】触发！date=%s start=%s curr=%s 回撤=%.2f%%"
                        "（执行 cancel_all + emergency_halt）",
                        today_eq, start_equity, curr_equity,
                        (float(curr_equity) - float(start_equity)) / float(start_equity) * 100)
                    # 撤所有未终态单（尽最大努力撤完，单笔失败不中断）
                    if gw is not None:
                        try:
                            await _cancel_all_open_orders(gw)
                        except Exception:
                            logger.exception("post_close 熔断撤单异常（继续 emergency_halt）")
                    # 置网关 lock_down + ERROR 告警
                    try:
                        from presentation.server.services.trading_service import (
                            emergency_halt as _emergency_halt,
                        )
                        _emergency_halt()
                    except Exception:
                        logger.exception("post_close emergency_halt 异常（已尽力撤单）")
                    circuit_breaker_triggered = True
                else:
                    logger.info(
                        "post_close 日内熔断未触发 date=%s 回撤=%.2f%%（阈值 -3.0%%）",
                        today_eq,
                        (float(curr_equity) - float(start_equity)) / float(start_equity) * 100)
    except Exception:
        logger.exception("post_close 日内熔断整体异常（不阻塞清白名单）")

    result["circuit_breaker"] = circuit_breaker_triggered
    if breaker_skipped:
        result["breaker_skipped"] = True

    # ④ trailing 盘后演进（Task 9 · R-3 · 未熔断时跑）：
    # Why 熔断后跳过：熔断已 lock_down，次日人工接管——此时再演进 stop 收紧可能触发
    # 额外卖出与熔断善后冲突（熔断应全场停摆，同 ⑤ max_holding 的熔断优先约束）。
    if not circuit_breaker_triggered:
        try:
            today_eq = datetime.now().strftime("%Y-%m-%d")
            plan = trading_plan.load_plan(today_eq)
            if plan and plan.get("orders"):
                entry_dates = _position_book.get_entry_dates()
                n = _evolve_trailing_stops(
                    plan["orders"], entry_dates, today_eq, _trade_cfg())
                if n:
                    # 写回 plan（保留 confirmed——trailing 是止损价演进不改人审状态；
                    # 否则 confirmed 重置 False 会让次日 _stoploss 跳过止损监控致敞口裸奔）
                    trading_plan.save_plan(
                        today_eq, plan["orders"], confirmed=plan.get("confirmed", False))
                    result["trailing_evolved"] = n
                    logger.info("post_close trailing 演进 %d 单 stop（holding_days 驱动）", n)
        except Exception:
            logger.exception("post_close trailing 演进异常（不阻塞 max_holding/清白名单）")

    # ⑤ max_holding 超期标记（Task 8 · P0-4 · 未熔断时跑）：
    # Why 熔断优先：日内 -3% 熔断已 emergency_halt + lock_down 全场停摆，此时再标超期会让
    # 次日 pre_open 平仓单与熔断善后冲突（熔断应全场停摆，不叠加平仓释放资金）。
    if not circuit_breaker_triggered:
        try:
            today_eq = datetime.now().strftime("%Y-%m-%d")
            max_holding = _trade_cfg()["max_holding"]
            expired = _scan_expired_positions(today_eq, max_holding)
            if expired:
                _write_expired_positions(today_eq, expired)
                result["expired_positions"] = len(expired)
                logger.warning(
                    "【超期持仓】%d 只 holding_days>max_holding=%d：%s（次日 pre_open 跌停价平仓）",
                    len(expired), max_holding,
                    ", ".join(f"{e['symbol']}({e['holding_days']}d)" for e in expired))
        except Exception:
            logger.exception("post_close max_holding 扫描异常（不阻塞清白名单）")

    # 清动态白名单（Task5）：保证下一交易日从干净状态开始（防止昨日标的污染今日白名单）
    try:
        dynamic_whitelist.clear_dynamic_whitelist()
    except Exception:
        logger.exception("post_close 清动态白名单异常")

    logger.info("post_close 完成 date=%s drift=%s circuit_breaker=%s breaker_skipped=%s",
                date, result.get("drift"),
                result.get("circuit_breaker"), result.get("breaker_skipped"))
    return result


# ============================================================================
# TradingEngine：APScheduler 四 cron 装配（独立常驻进程 python -m trading）
# ============================================================================
class TradingEngine:
    """APScheduler 编排容器（四 cron 触发点装配 + start/shutdown 生命周期）。

    ⚠️ 不变量（再次强调，见模块 docstring）：本类实例**只在 ``python -m trading``
    独立进程内构造**，绝不在 server 进程内实例化（否则 dynamic_whitelist._DYNAMIC
    模块级全局会污染 server 手动下单路径，破坏 server 行为向后兼容）。

    四 cron（Task4 已配 env，缺省值对齐 A 股交易日历 · 术语对齐 T 日盘后扫盘）：
        eod_plan   19:00 周一-五  T 日盘后扫信号 + 落计划 + 推钉钉（T+1 执行）
                          ⚠️ 非 15:35：18:00 增量采集 + 18:30 检查点② 通过后才扫，
                          否则读到 T-1 数据算 T+1 计划（时序 bug · Task6 修复）
        pre_open   09:22 周一-五  T 日开盘前撤昨日 + 挂当日单
        stop_loss  每 30s（IntervalTrigger，Task8：cron 不支持秒级；时段约束在 monitor 兜底）
        post_close 15:30 周一-五  盘后对账 + 清白名单

    每个 job 先过 calendar.is_trading_day 判交易日（节假日整体跳过）。
    """

    def __init__(self) -> None:
        """装配 AsyncIOScheduler + 四 job（不 start）。

        ⚠️ 触发器形态分轨（Task8）：
            eod_plan / pre_open / post_close：分钟粒度 CronTrigger（标准 5 字段）。
            stop_loss：**IntervalTrigger（秒级）**——cron 最小粒度是分钟，
            30s 巡检必须用 interval。时段约束（9:30-11:30 / 13:00-15:00）下放给
            ``stop_loss_monitor`` 内 ``calendar.is_intraday_session`` 兜底，
            非盘中由 monitor 直接 no-op（不在 trigger 层做时段过滤，避免 interval
            在午休 / 盘后空跑也只是命中 no-op，零副作用）。
        """
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        self.sched = AsyncIOScheduler()
        # 四 job 注册：id 显式命名便于 get_jobs 自检与外部调试
        self.sched.add_job(
            self._eod, CronTrigger.from_crontab(
                # ⚠️ 时序修复（Task6）：15:35 → 19:00。原 15:35 触发时 T 日增量行情
                # 尚未落湖（@18:00 sync_all_tushare 才跑增量采集 + @18:30 数据检查点②
                # 才验通过），_eod 读到的仍是 T-1 数据 → 用 T-1 收盘算 T+1 计划 = 时序 bug。
                # 挪到 19:00 既等足 18:00 增量落湖 + 18:30 检查点② 通过，又留足窗口在
                # T+1 日 09:22 pre_open 前完成扫盘 + 人审确认（confirmed=False 闸）。
                os.getenv("ENGINE_EOD_PLAN_CRON", "0 19 * * 1-5")),
            id="eod_plan",
        )
        self.sched.add_job(
            self._pre_open, CronTrigger.from_crontab(
                os.getenv("ENGINE_PRE_OPEN_CRON", "22 9 * * 1-5")),
            id="pre_open",
        )
        # stop_loss：盘中每 N 秒巡检（海龟时间驱动移动止损 grace/step/floor 在此触发）。
        # ⚠️ Task8：cron `*/5 9-14`（5min）→ IntervalTrigger(seconds=30)。
        # Why interval：cron 最小粒度是分钟，30s 必须 interval。原 `9-14` 时段约束
        # 下放给 ``stop_loss_monitor`` 内 ``calendar.is_intraday_session``（9:30-11:30 /
        # 13:00-15:00）——trigger 全天每 30s 触发，非盘中由 monitor 内 no-op 兜底。
        # ⚠️ ENGINE_STOPLOSS_INTERVAL_SECONDS：30s 目标，**spec §10 限频实测后定终值**——
        # 若 miniQMT 模拟盘连续 get_quotes+query_stock_positions 撞柜台限流，上调 60s。
        stoploss_seconds = int(os.getenv("ENGINE_STOPLOSS_INTERVAL_SECONDS", "30"))
        self.sched.add_job(
            self._stoploss,
            IntervalTrigger(seconds=stoploss_seconds),
            id="stop_loss",
        )
        self.sched.add_job(
            self._post_close, CronTrigger.from_crontab(
                os.getenv("ENGINE_POST_CLOSE_CRON", "30 15 * * 1-5")),
            id="post_close",
        )

        # 成交回调链路状态（Task 10 · 修 G5）：
        #   _tp_placed：已挂止盈的 symbol 集合（幂等防重挂——部分成交多次回报/柜台重推
        #               不应重复挂止盈卖单，否则同笔持仓挂 N 张卖单 → 超卖敞口致命）。
        #   _gw：交易网关引用（_order_direction 查 gw._orders[order_id].order_type 判买卖方向）。
        #        Task 11 在 gw.connect 注册 _on_order_update 回调时同步注入，本 task 仅声明槽位。
        #
        # ⚠️ _tp_placed 仅进程内存（不持久化）：
        #   常驻进程重启（断电/崩溃恢复/OOM kill 后 systemd 拉起）后该集合清空——若 broker
        #   断线重连后重推历史 trade 回报（miniQMT connect 时同步推送当日 _orders 与成交），
        #   已挂止盈的买单会再次命中「symbol not in _tp_placed」分支 → 重复挂止盈卖单
        #   （spec §8 幂等红线的已知缺口）。
        #   Phase2 / 生产级准入必修：把 _tp_placed 持久化到 trading_plan JSON（或独立
        #   tp_state.json），_handle_order_update 启动时加载、挂单成功后写盘。
        #   Phase1 dry_run（影子模式）：风控极低——dry_run 命中不真下单，重复挂的止盈单
        #   也走 DRY_RUN 分支不触达 broker，最多多几条「止盈单已挂」日志，无敞口风险。
        self._tp_placed: set[str] = set()
        self._gw: Any = None

    # ----- cron 包装：交易日判定 + 转调 async 触发函数 -----
    async def _eod(self) -> None:
        """cron 包装：节假日跳过；交易日 resolve 多实验 + scan_live 产信号 → eod_plan。

        物理意图（二期 gap② 策略数据源 · 术语对齐物理时序）：
            **T 日盘后 19:00 触发**——从实验配置中心读当前所有在线实验
            （status=ACTIVE+weight>0），按每实验的 strategy_name+params 装配策略实例，
            对创板科创可交易池逐 symbol 调 scan_live(df_upto 截至 T 日 today) 产
            【T 日新突破】信号，注入实验归因字段后透传 eod_plan 落盘（confirmed=False
            待研究员人审），次日（T+1 日开盘前 pre_open）挂单执行。

            ⚠️ 术语对齐（Task 7b fix · 别再误称「T-1 收盘日」）：
                传入的 ``today`` 即 T 日本身（cron 在 T 日 19:00 触发，扫 T 日盘后突破），
                计划生效日 = T+1。早期注释里的「T-1 收盘日」语义混淆，统一改为「T 日盘后」。
            ⚠️ 时序修复（Task6）：
                cron 由 15:35 挪 19:00——原 15:35 触发时 T 日增量行情（@18:00 sync_all_tushare）
                尚未落湖、@18:30 数据检查点② 未验通过，_eod 读到的是 T-1 数据，用 T-1 收盘
                算 T+1 计划 = 时序 bug。19:00 既等足数据落湖 + 检查点② 通过，又留足窗口在
                T+1 日 09:22 pre_open 前完成扫盘 + 人审确认。

        ⚠️ 性能不变量（Task 7b fix · 阻断级修复）：
            data_lake/a_shares_daily.parquet（455MB，全市场 5 年）在本函数**入口只读一次**，
            传给 _load_universe(lake) 与 _load_df_upto(lake, sym, today) 复用。
            历史 bug：每 symbol 各 read_parquet 一次（1.75s × 1993 标的 = 58 分钟纯 I/O），
            19:00 的 _eod 根本无法在合理窗口完成；复用 lake 后整体降至秒级。

        无前视契约（spec 红线）：
            df_upto 由 _load_df_upto 截断于 today（.loc[:date]），不含 today 之后任何 K 线；
            ATR 在 scan_live 内对齐 df_upto 末根计算，严格因果。

        fail-fast 红线：
            无在线实验 → 直接 return，不调 eod_plan（避免空实验下仍触发钉钉推送/落空计划）。

        Why 信号注入归因字段（experiment_id/experiment_weight）：
            signal_runner.build_orders_from_signals 已从 s.get("experiment_weight", 1.0)
            读权重、从 s.get("experiment_id") 读归因透传到 PlannedOrder——本函数只需在
            scan_live 返回的 signal dict 上补齐两字段即可复用既有归因链路（Task5/6 已就绪）。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        if not calendar.is_trading_day(today):
            logger.info("eod_plan 跳过：今日非交易日 %s", today)
            return
        # 局部 import（避免顶层拉起 experiment/strategies 子系统，保持引擎薄编排）：
        import pandas as pd
        from experiment.resolver import resolve_active
        from strategies.registry import build_strategy

        experiments = resolve_active()
        if not experiments:
            # fail-fast：无在线实验 → 不触达 eod_plan（spec §2 确认闸前置约束）
            logger.warning("_eod 无在线实验，跳过（fail-fast）")
            return

        # ⚠️ 性能红线（Task 7b fix）：data_lake 入口只读一次，全 universe 复用同一份
        # DataFrame。455MB parquet 单次 read ≈ 1.75s；历史每 symbol 重读致 1993 × 1.75s
        # ≈ 58 分钟纯 I/O，_eod 在 19:00 窗口完全无法完成。lake 复用后降为单次 disk read。
        lake = pd.read_parquet("data_lake/a_shares_daily.parquet")

        universe = _load_universe(lake)
        signals: list = []
        atr_map: dict = {}
        # 逐实验 × 逐 symbol 扫信号；单 symbol scan_live 异常仅 warn 跳过，不炸整批
        for exp in experiments:
            strategy = build_strategy(exp.strategy_name, cfg_override=exp.params)
            for sym in universe:
                df_upto = _load_df_upto(lake, sym, today)
                # 历史不足（<60 行）跳过：颈线 window+ATR 窗口需足够样本，否则识别失真
                if df_upto is None or len(df_upto) < 60:
                    continue
                try:
                    for s in strategy.scan_live(sym, df_upto, today):
                        # 注入实验归因字段（signal_runner/PlannedOrder 透传链路依赖）。
                        # Layer2 阶段1：scan_live 现返 frozen Signal dataclass，原地赋值
                        # ``s["x"]=...`` 会抛 FrozenInstanceError；用 dataclasses.replace
                        # 产出带归因的新 Signal（spec §0「参数以不可变快照锁定」红线，
                        # 止损价是实盘风险参数，跨实验串味 = 风险归因错配，故 Signal 不可变）。
                        s = _dc_replace(
                            s,
                            experiment_id=exp.experiment_id,
                            experiment_weight=exp.weight,
                        )
                        signals.append(s)
                        # ⚠️ atr 防御（Task 7b fix · Minor）：缺 atr（None/0/NaN）不建项。
                        # Why：build_orders_from_signals 算 stop_price = neckline − N×ATR，
                        # 若 atr=0.0 → stop_price = neckline，产「止损价=颈线价」的废单
                        # （等于把买入价直接挂止损价、不止损），不如让 build 阶段干净跳过。
                        if s.atr:
                            atr_map[sym] = s.atr
                except Exception as e:  # noqa: BLE001 单标的挡板（scope #7 兜底）
                    logger.warning("_eod scan_live %s 异常跳过: %s", sym, e)

        # plan Task 5（P0-5 cooldown 信号去重）：scan 后按 cooldown 过滤同标的（防连续日超额成交）。
        # 物理意图：scan_live 无跨日去重，同形态连续触发会连续多日产信号；用最近 cooldown 日
        # plan 的 formed_at 标的集做跨日去重，cooldown 窗口内的同标的新信号丢弃。
        # 边界：cooldown=0 不去重（兼容用户配置）；窗口含周末故 days_back=cooldown+2 自然日余量。
        cooldown = _resolve_cooldown_days(experiments)
        if cooldown > 0 and signals:
            recent_syms = _load_recent_plan_symbols(days_back=cooldown + 2, today=today)
            if recent_syms:
                before = len(signals)
                signals = [s for s in signals if s.symbol not in recent_syms]
                dropped = before - len(signals)
                if dropped:
                    logger.info("cooldown 去重丢弃 %d 条信号（cooldown=%d 日内已 plan）",
                                dropped, cooldown)

        # date = T+1（计划生效日）：修 date 错位 bug（2026-07-28）。原传 today（T 日），
        # 但 pre_open 次日读 load_plan(today=T+1) → 永远差一天挂不上单。改传
        # calendar.next_trading_day(today) 让落盘 date 与次日 pre_open 读取口径对齐。
        await eod_plan(
            calendar.next_trading_day(today), signals, atr_map,
            capital=float(os.getenv("TRADE_CAPITAL", "1_000_000")),
        )
        # Task12 · 持仓盈亏播报（spec §6.2 C4 / 子诉求 1<2>）：eod_plan 落盘+推钉钉后，
        # 把当前持仓逐仓浮盈 + 总资产推一次群播报，让研究员在 19:00 一次性看到「今日计划 +
        # 当前持仓盈亏全貌」。放在 eod_plan 之后、独立 try-except 软降级——盈亏播报失败
        # 绝不阻断 eod_plan 主流程（计划已落盘，研究员次日 pre_open 仍可挂单执行）。
        await self._broadcast_positions_pnl()

    async def _broadcast_positions_pnl(self) -> None:
        """播报当前持仓盈亏全貌（总资产 + 逐仓浮盈 + 盈亏汇总）。

        物理意图（spec §6.2 C4，19:00 eod_plan 收尾播报）：
            研究员在 19:00 收到 eod_plan（次日计划）后，紧接着收到一条「当前持仓 +
            浮盈」播报——一日闭环的盈亏可见性。内容三段：
              a. 总资产（gw.query_asset.total_asset）作 head；
              b. 逐仓浮盈（get_positions 富化后的 pnl，带 +/- 前缀，盲价标的显示 N/A）；
              c. 空仓特判 → 显式「空仓」（不混淆「无持仓」与「播报失败」）。

        软降级红线（绝不阻断 eod）：
            整方法 try-except 兜底——网关未连接 / query_asset 异常 / get_positions
            抛错 / 钉钉网络故障 → 仅 logger.exception 记录，不上抛、不影响 eod_plan
            已落盘的计划。播报是「锦上添花」而非「关键路径」，与 fire_and_forget 同语义。
        """
        try:
            # 局部 import：避免顶层拉起 server/infra 子系统（与 _eod 内 experiment/strategies
            # 局部 import 同口径，保持引擎薄编排）。
            from presentation.server.services.trading_service import get_positions
            from infra.notifier import NotificationManager, fire_and_forget

            gw = get_gateway()
            # query_asset 总资产：网关缺失/未连接/异常 → 走 {} 降级（head 显示 0，不阻断）。
            asset: dict = {}
            if gw is not None:
                try:
                    asset = await gw.query_asset() or {}
                except Exception as e:  # noqa: BLE001 总资产软降级
                    logger.warning("_broadcast_positions_pnl query_asset 失败（head 显示 0）：%s", e)
                    asset = {}
            total = float(asset.get("total_asset", 0.0) or 0.0)

            positions = await get_positions()

            # 汇总浮盈 + 持仓成本总额（账户浮亏率分母）：仅累加 pnl 非 None 的仓位
            # （盲价仓位跳过，避免 None + 数值 TypeError）。total_cost = Σ(avg×qty) 用于算
            # 账户浮亏率 = total_pnl / total_cost（B1 口径：相对持仓成本的投入产出比，零新存储）。
            total_pnl = 0.0
            total_cost = 0.0
            pnl_known = 0
            for p in positions:
                pnl = p.get("pnl")
                avg = p.get("avg_price")
                qty = p.get("qty")
                if pnl is not None:
                    total_pnl += float(pnl)
                    pnl_known += 1
                if avg is not None and qty is not None:
                    total_cost += float(avg) * float(qty)

            lines = [f"## 💼 持仓盈亏播报（总资产 {total:.0f}）"]
            if not positions:
                lines.append("- 空仓")
            else:
                for p in positions:
                    pnl = p.get("pnl")
                    qty = p.get("qty")
                    avg = p.get("avg_price")
                    last = p.get("last_price")
                    pct = p.get("pnl_pct")
                    # 盲价防御：avg/last/pct 任一缺失 → 只显 N/A（不猜价，量化审计红线，
                    # 与 get_positions 盲价口径一致）。齐全时展示「成本 现价 +N.N%（浮盈）」三件套。
                    if pnl is not None and avg is not None and last is not None and pct is not None:
                        lines.append(
                            f"- {p['symbol']} {qty:.0f}股 成本{avg:.2f} 现价{last:.2f} "
                            f"{pct:+.2f}%（浮盈{pnl:+.0f}）"
                        )
                    else:
                        # qty 恒为 float（get_positions 契约）；非数值 qty 会在富化阶段抛 TypeError，
                        # 不会安静流到此（同既有死代码分析），故无条件按 float 格式化。
                        lines.append(f"- {p['symbol']} {qty:.0f}股 浮盈N/A")
                # 汇总行：已估值仓位 N/总 M，累计盈亏 + 浮亏率（盲价仓位不计入累计，防误导）。
                # rate = 浮盈 / 持仓成本总额 × 100；total_cost==0（全盲价/空成本）→ 不显率，只显绝对值。
                pnl_rate = (total_pnl / total_cost * 100) if total_cost > 0 else None
                rate_str = f"（{pnl_rate:+.2f}%）" if pnl_rate is not None else ""
                lines.append(
                    f"- 汇总：已估值 {pnl_known}/{len(positions)} 仓，"
                    f"累计盈亏 {total_pnl:+.0f}{rate_str}"
                )
            msg = "\n".join(lines)

            # fire_and_forget：钉钉异步投递 daemon 线程，网络延迟不阻塞 eod 主线程。
            # notify_risk_event 用 INFO 级（持仓播报是业务流水，非风险告警，与 notify_trade_event
            # 同语义层——但本期复用 risk_event 通道避免新增通道，level=INFO 前缀 ℹ️ 区分）。
            fire_and_forget(NotificationManager.get_default().notify_risk_event(msg, "INFO"))
        except Exception:
            # 顶层兜底：任何未预期异常（含 get_gateway import 失败）都软降级，绝不阻断 eod。
            logger.exception("持仓盈亏播报失败（不影响 eod_plan 主流程）")

    async def _pre_open(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if not calendar.is_trading_day(today):
            logger.info("pre_open 跳过：今日非交易日 %s", today)
            return
        await pre_open(today)

    async def _stoploss(self) -> None:
        """IntervalTrigger 包装：止损监控（盘中时段判定在 stop_loss_monitor 内）。

        ⚠️ 交易日守卫（Task 8 fix · review I1）：
            Task 8 把 stop_loss job 从 cron ``*/5 9-14 * * 1-5`` 迁到
            ``IntervalTrigger(seconds=30)``——IntervalTrigger **无工作日过滤**，
            旧 cron 的 ``1-5``（周一至周五）约束在迁移中丢失，导致周末 9:30-15:00 时段
            也会触发本方法（``is_intraday_session`` 只查时间不查工作日，兜不住）。
            故此处显式 ``calendar.is_trading_day`` 守卫，与 ``_eod``/``_pre_open``/
            ``_post_close`` 同口径——非交易日整体跳过，不查 plan、不调 monitor。
            （周末虽无交易路径、load_plan→None→空 stop_prices→no-op，影响低；但 live
            前必修：避免无谓的 plan/monitor 调用 + docstring「时段约束下放 monitor 兜底」
            在 interval 触发器下不成立。）

        注入 stop_prices（Task 7 · 修现状 None 空转）：
            从当日活跃计划（``trading_plan.load_plan(today)``）读 ``{symbol: stop_price}``
            注入 ``stop_loss_monitor``。现状恒传 ``stop_prices=None`` → monitor 在
            「stop_prices 空」判断处直接返「无止损价配置」no-op，**盘中监控链路恒空转**
            （致命：持仓跌破止损价也不触发卖出，敞口裸奔）。

        保守降级红线（Grill Me）：
            - 计划不存在 / 未 confirmed / orders 空 / 某 order 缺 symbol 或 stop_price
              → 一律不把该标的塞进 stop_prices（宁可漏监控，不拿脏数据盲卖）。
            - 整张 stop_prices 最终为空时显式传 ``None``，让 monitor 走既定 no-op 分支
              （保守、不崩、可观测日志），绝不构造非空 map 误导下单。

        ⚠️ 现价依赖（C1 fix）：``stop_loss_monitor`` 现价走
        ``trading.qmt_market_data.get_quote``（xtdata，miniQMT 通道可用）；
        **止损链路依赖 xtdata 行情源（miniQMT 通道），无 xtdata 时需另接行情源（live 前必修 follow-up）**。

        ⚠️ Trailing stop 动态更新（follow-up）：本处注入的是计划内**静态** stop_price
            （pre_open 挂单时落盘的初始止损价）；时间驱动 trailing（海龟 grace/step/floor）
            需在盘中按持仓最高价动态更新 stop_prices map，属另一个 follow-up，不在本 task 内。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        # 交易日守卫（Task 8 fix · review I1）：IntervalTrigger 无 1-5 工作日过滤，
        # 必须显式 is_trading_day，否则周末盘中时段会空跑（与 eod/pre_open/post_close 同口径）。
        if not calendar.is_trading_day(today):
            logger.info("stop_loss 跳过：今日非交易日 %s", today)
            return
        plan = trading_plan.load_plan(today)
        stop_prices: dict[str, float] = {}
        # 仅在 confirmed 计划下抽取（confirmed=False 是人审闸——研究员未确认就不监控止损，
        # 避免研究员明确否决的计划仍触发卖出，破坏人审语义）。
        if plan and plan.get("confirmed"):
            for o in plan.get("orders", []):
                sym = (o.get("order") or {}).get("symbol")
                sp = o.get("stop_price")
                # 双重防御：symbol 缺失或 stop_price 非数（NaN/None）一律跳过——
                # stop_prices 的每一项都必须是「能拿来比价」的合法 (sym, price) 对。
                if sym and sp is not None:
                    stop_prices[sym] = sp
        # 空时显式转 None：与 stop_loss_monitor 的「stop_prices is None or empty → no-op」
        # 契约对齐，避免传 {} 时日志歧义（None=未注入计划，{}=计划无止损配置）。
        await stop_loss_monitor(stop_prices=stop_prices or None)

    async def _post_close(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if not calendar.is_trading_day(today):
            logger.info("post_close 跳过：今日非交易日 %s", today)
            return
        # gap4 fix：读本地账本 → 注入 local_positions，对账链路真跑。
        # ⚠️ 空 dict 直传（不转 None）：live 下账本空但 broker 有持仓（疑似外部单）时，
        # reconcile(local={}, broker={有}) 会报 only_broker drift——转 None 会让 post_close
        # 走跳过分支漏报（对账查漏价值丧失）。dry_run 下 gw=None 时 post_close 内部自然跳过。
        from trading import position_book
        local_positions = position_book.get_local_positions()
        await post_close(today, local_positions=local_positions)

    # ----- 成交回报 handler（Task 10 · 修 G5：成交回调链路）-----
    async def _handle_order_update(self, update: Mapping[str, Any]) -> None:
        """成交回报 handler（由 Task 11 的 ``_on_order_update`` 经 ``create_task`` 调度，
        主线程事件循环执行）。

        物理意图（spec §6.2 C1，三连）：
            on_stock_trade 回调推送 ``kind=="trade"`` 的成交回报（含真实成交价/量/时间，
            非下单时的预估价），本 handler 顺序执行三件事：
              a. ``record_live_trade`` 补写成交回报日志（CSV，Layer 6 LLM 复盘数据源）；
              b. ``notify_trade_event`` 推钉钉成交通知（fire_and_forget 异步不阻塞回调链）；
              c. 买单成交 + 未挂止盈 → ``_place_take_profit`` 挂限价止盈卖单
                 （Phase1 简化版：单一固定止盈价、全额；Phase2 升级为分级状态机复刻
                 simulate_exit 的 tp1 部分量 + tp2 剩余量）。

        幂等红线（``_tp_placed``）：
            on_stock_trade 在部分成交 / 柜台重推时会多次推送同一 order_id 的 trade 回报。
            若每次都重挂止盈卖单 → 同笔持仓挂 N 张卖单 → 超卖敞口致命。故以 symbol 为
            key 标记已挂止盈，二次回报命中即跳过（``symbol in self._tp_placed``）。

        线程安全：
            本方法 async，由主线程 ``create_task`` 调度（Task 11 用
            ``call_soon_threadsafe`` 把网关回调线程的 update 投递回主事件循环）。
            钉钉通知走 ``fire_and_forget``（独立 daemon 线程跑 asyncio.run），不阻塞
            回调链——网关回调线程若被 IM 网络延迟阻塞，会反压柜台行情推送。

        边界与降级（Grill Me）：
            - ``kind != "trade"`` 直接 return（order/order_error 由风控层负责，本 handler
              只处理真实成交）；
            - symbol 缺失或 traded_volume<=0 直接 return（脏数据/撤单回报不应触达写日志
              和挂止盈，否则会把废回报当真实成交落账）；
            - 三连各自 try-except 兜底：任一环节失败（日志写盘失败/钉钉网络故障/止盈挂单
              被风控挡板拒）只记日志，不阻塞后续环节（日志失败仍要通知，通知失败仍要挂止盈）；
            - ``_order_direction`` 返 None（查不到订单方向）时保守按 ``"TRADE"`` 落日志、
              不挂止盈（不误判买卖方向 → 不误挂止盈）。
        """
        kind = update.get("kind")
        if kind != "trade":
            return  # 仅处理成交回报（order/order_error 由风控层负责，不在本 handler 范围）
        symbol = update.get("stock_code", "")
        qty = update.get("traded_volume", 0)
        price = update.get("traded_price", 0.0)
        order_id = str(update.get("order_id", ""))
        if not symbol or qty <= 0:
            # 脏数据/撤单回报（traded_volume=0）不应落账或挂止盈，直接跳过
            return

        # 判定方向（BUY/SELL/None）——日志与挂止盈决策都依赖
        direction = self._order_direction(order_id)

        # a. 成交日志补写（用真实成交价/量，非下单预估价；Layer 6 LLM 复盘数据源）
        try:
            from presentation.server.services.trading_service import record_live_trade
            record_live_trade(
                symbol,
                direction or "TRADE",  # 方向未知时落 "TRADE"（保守中性，不误判买卖）
                float(qty),
                float(price),
                strategy="neckline",
                rationale=f"成交回报@{update.get('traded_time')}",
            )
        except Exception:
            # 日志写盘失败不阻塞通知/挂止盈（三连各自独立降级，互不阻断）
            logger.exception("成交日志补写失败 symbol=%s（不影响后续通知/挂止盈）", symbol)

        # b. 钉钉成交通知（fire_and_forget 不阻塞回调链；钉钉软降级在 _broadcast 内兜底）
        try:
            # ⚠️ 走 infra.notifier 真身（infra.notifier 是 strangler 转发垫片，broker/qmt
            # 同口径用 infra.notifier；此处直指 infra 真身，避免垫片未来下线后隐性断链）。
            from infra.notifier import NotificationManager, fire_and_forget
            fire_and_forget(NotificationManager.get_default().notify_trade_event(
                symbol, direction or "TRADE", float(qty), float(price),
            ))
        except Exception:
            logger.exception("成交通知发送失败 symbol=%s（不影响后续挂止盈）", symbol)

        # c. 买单成交 + 未挂止盈 → 挂限价止盈卖单（幂等 _tp_placed 防重挂）
        #    卖单成交（direction=="SELL"）无需挂止盈（卖出即离场，无持仓可止盈）。
        #    方向未知（None）保守不挂——宁可漏挂止盈让人工补，也不误把卖单当买单挂反方向单。
        #
        # 幂等关键设计（Why 在 _handle_order_update 标记，不在 _place_take_profit 内标记）：
        #   ``_tp_placed.add(symbol)`` 必须在**调度点**完成（即此处，调 _place_take_profit
        #   之前/之后都可，但必须在 _handle_order_update 同步路径里），**不能**下沉到
        #   ``_place_take_profit`` 内部 —— 否则：
        #     1. 部分成交重推时，_place_take_profit 还没跑完（await 中）就被第二次回报
        #        重入，_tp_placed 仍空 → 二次重挂 → 超卖；
        #     2. 单测 mock 掉 _place_take_profit 时，标记永远不会被写入，幂等链路无法验证。
        #   Phase1 语义：「该 symbol 已调度挂止盈」即视为已处理（一票通行），后续重推一律
        #   跳过；_place_take_profit 内部若真挂失败（风控拒/网关断），由告警人工补挂，
        #   不靠 _tp_placed 之外的重试计数（重试限频是 Phase2 议题）。
        if direction == "BUY" and symbol not in self._tp_placed:
            self._tp_placed.add(symbol)  # 调度点幂等标记（先占位，防 await 期间重入重挂）
            try:
                await self._place_take_profit(symbol, qty, price, order_id)
            except Exception:
                # 止盈挂单失败（被风控挡板拒/网关断线）不抛——人工补挂（告警已记日志）。
                # 注意：此处不回滚 _tp_placed（保留已调度标记，防重推再挂；真失败由人工补）。
                logger.exception("挂止盈失败 symbol=%s（需人工补挂）", symbol)

        # d. 本地持仓账本写入（gap4 · post_close 对账数据源）。
        #    独立 try-except 软降级：账本写入失败不阻断 a 日志/b 通知/c 止盈。
        #    方向 None 不写（保守，对齐 c 连不挂止盈语义——不猜方向误记为买当卖/卖当买，
        #    账本失真比对账漏记更危险）。
        if direction in ("BUY", "SELL"):
            try:
                from trading import position_book
                position_book.apply_fill(order_id, symbol, direction, float(qty), float(price), str(update.get("traded_time", "")))
            except Exception:
                logger.exception("本地账本写入失败 symbol=%s（不影响日志/通知/止盈）", symbol)

    def _order_direction(self, order_id: str) -> Optional[str]:
        """从 ``gw._orders`` 查订单方向（BUY/SELL）。

        物理意图：
            成交回报 ``update`` 只含 order_id 与成交价量，**不含下单时声明的买卖方向**。
            必须回查 ``gw._orders[order_id].order_type`` 拿下单时记录的方向枚举
            （下单瞬间由 broker/qmt.py ``_place_order`` 写入 _orders 字典），才能判定
            本次成交是买单（需挂止盈）还是卖单（无需挂止盈）。

        order_type 枚举（xtconstant 契约，与 broker/qmt.py:724 同源）：
            - ``xtconstant.STOCK_BUY = 23``  → 返 "BUY"
            - ``xtconstant.STOCK_SELL = 24`` → 返 "SELL"
            - 其它/缺失 → 返 None（保守，不误挂止盈）

        Args:
            order_id: 成交回报里的订单 ID（str；gw._orders 的 key 在 broker/qmt.py 内
                      既可能是 seq 也可能是 real order_id，本处按 str(update["order_id"]) 查）。

        Returns:
            "BUY" / "SELL" / None。None 时调用方（_handle_order_update）保守按 "TRADE"
            落日志、跳过挂止盈（不猜方向 → 不误挂反方向单）。

        ⚠️ 测试环境兜底（ImportError）：
            xtconstant 来自 xtquant SDK，CI/单测环境无 xtquant 时 ``from xtquant import
            xtconstant`` 抛 ImportError——此处兜底硬编码 23/24（与 conftest.py 的假
            xtconstant 同值），保证单测可跑。生产环境（miniQMT 通道）xtquant 必装，
            兜底分支不会触达。
        """
        # gw 可能未装配（Task 11 未注入 _gw）——getattr 兜底返 {} 不抛
        orders = getattr(self._gw, "_orders", {}) if self._gw else {}
        rec = orders.get(order_id, {})
        # order_type 用 xtconstant 常量比较（绝不硬编码魔法数字到比较表达式里——
        # 兜底 23/24 只在 ImportError 时启用，生产环境走 xtconstant.STOCK_BUY/SELL 真值）
        try:
            from xtquant import xtconstant  # 与 broker/qmt.py:61 同源导入路径
            STOCK_BUY = xtconstant.STOCK_BUY
            STOCK_SELL = xtconstant.STOCK_SELL
        except ImportError:
            # CI/单测无 xtquant：兜底硬编码（与 tests/conftest.py 假 xtconstant 同值）
            STOCK_BUY, STOCK_SELL = 23, 24
        ot = rec.get("order_type")
        if ot == STOCK_BUY:
            return "BUY"
        if ot == STOCK_SELL:
            return "SELL"
        return None

    async def _place_take_profit(self, symbol: str, filled_qty: float,
                                 fill_price: float, order_id: str) -> None:
        """挂限价止盈卖单（Phase2 分级止盈：tp1 锁利 + tp2 形态目标位）。

        物理意图（plan Task 7 · 对齐缺口 P0-3）：
            买单成交后立刻挂止盈卖单——买单一旦成交即转为持仓，需主动挂限价单等待触发。
            Phase1 只挂单笔全平 tp2（颈线+tp_h_mult×H 形态对称目标位），与回测 simulate_exit
            用 tp1_portion 加权两批止盈的口径背离（回测冠军档套实盘止盈逻辑失真）。
            Phase2 升级为分级：tp1 价位（颈线+tp1_h_mult×H）卖 tp1_portion 比例仓锁利，
            tp2 价位卖剩余仓博形态对称目标位——与 simulate_exit 同口径对齐。

        止盈价来源（与 pre_open / stop_loss 同一张活跃计划，单源一致）：
            ``trading_plan.load_plan(today).orders[i]``（当日 confirmed 计划）：
            - take_profit = tp2（颈线+tp_h_mult×H）
            - tp1 = 颈线 + tp1_h_mult × H（Task 7 落盘）；老 plan 无此字段 → 退回 tp2 单笔
            - tp1_portion：tp1 档分配比例（0~1）

        整手分割红线（A 股卖出约束）：
            tp1_qty = int(filled × portion / 100) × 100（向下取整 100 整手）；
            tp2_qty = filled - tp1_qty（余量含零股，券商接受卖出清仓零股）。
            portion=0 → 全量 tp2；portion=1 → 全量 tp1；filled×portion<100 → tp1_qty=0 全量 tp2。

        Sanity 守卫（防参数坏值）：
            tp1 ≥ tp2 时只挂 tp2（数据异常或 H≤0 致 tp1 算到 tp2 之上，挂 tp1 永远先成交
            反而拖累——正常 tp1_h_mult<tp_h_mult 保证 tp1<tp2，守卫是兜底）。

        数量来源（scope #3 红线同源）：
            ``filled_qty`` 用成交回报里的**实际成交量**（``traded_volume``），**非计划全量**。
            部分成交时若用计划全量挂止盈 → 卖超过实际持仓 = 超卖敞口致命。

        幂等（``_tp_placed``）：
            **标记在调度点 ``_handle_order_update`` 完成**（不在本方法内），先占位防
            ``await`` 期间部分成交重推重入重挂。本方法只负责读计划止盈价 + _submit 挂单 +
            结果观测日志，不写 ``_tp_placed``（单一写入点 = 幂等可测可证）。

        Args:
            symbol:      成交标的（如 "300001.SZ"）。
            filled_qty:  实际成交量（股，来自成交回报 traded_volume，非计划全量）。
            fill_price:  实际成交均价（仅用于日志可观测，不参与挂单价计算）。
            order_id:    触发本次止盈的成交回报 order_id（仅用于日志归因）。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        plan = trading_plan.load_plan(today)
        if not plan:
            logger.warning("挂止盈跳过：无活跃计划 symbol=%s（计划未落盘/已失效）", symbol)
            return
        # 从计划 orders 里查该 symbol 的止盈配置（与 pre_open 挂买单同一张计划同源）
        tp2 = None
        tp1 = None
        tp1_portion = 0.0
        for o in plan.get("orders", []):
            if (o.get("order") or {}).get("symbol") == symbol:
                tp2 = o.get("take_profit")
                tp1 = o.get("tp1")
                tp1_portion = float(o.get("tp1_portion") or 0.0)
                break
        if tp2 is None or tp2 <= 0:
            # 计划缺止盈价（数据瑕疵/手工计划）→ 不挂盲单，告警人工补
            logger.warning("挂止盈跳过：无止盈价配置 symbol=%s（计划缺 take_profit）", symbol)
            return

        # 整手化 filled_qty（A 股卖出红线：int 截断零股，防 broker 按 float 解释成 100x）
        filled_int = int(filled_qty)
        if filled_int <= 0:
            logger.warning("挂止盈跳过：成交量非正 symbol=%s filled_qty=%s", symbol, filled_qty)
            return

        # 挂限价止盈卖单（confirm=True 同 pre_open，引擎是自动批量通道，盘中无人工二次确认）
        from trading.compute.types import OrderRequest

        # 分级分割判定（Task 7）：
        # 1. tp1 缺失 / portion=0 → 全量 tp2（向后兼容 Task 7 前的老 plan）
        # 2. tp1 ≥ tp2 sanity 守卫 → 全量 tp2（防 tp1 永远先成交拖累）
        # 3. 正常分级：tp1_qty = int(filled×portion/100)*100（整手），tp2_qty = 余量
        use_two_legs = (
            tp1 is not None and tp1 > 0
            and tp1_portion > 0.0
            and tp1 < tp2
        )
        if not use_two_legs:
            # 单笔全平 tp2（Phase1 行为，向后兼容）
            result = await _submit(
                OrderRequest(symbol=symbol, qty=filled_int, side="sell", price=tp2),
                confirm=True,
            )
            if result.get("state") not in ("REJECTED", "FAILED"):
                logger.info("【止盈单已挂】%s %s股 @%s（单笔全平 tp2 触发成交价=%s order_id=%s）",
                            symbol, filled_int, tp2, fill_price, order_id)
            else:
                logger.warning("止盈单挂失败 symbol=%s state=%s msg=%s（人工补挂）",
                               symbol, result.get("state"), result.get("message"))
            return

        # 分级止盈（Phase2）：tp1 锁利部分 + tp2 余量
        # tp1_qty 向下取整 100 整手；tp2_qty = 余量（含零股，券商接受卖出清仓）
        tp1_qty = int(filled_int * tp1_portion / 100) * 100
        tp2_qty = filled_int - tp1_qty
        # tp1_qty 整手后为 0（filled×portion<100）→ 退化全量 tp2（不挂零股 tp1）
        if tp1_qty <= 0:
            result = await _submit(
                OrderRequest(symbol=symbol, qty=filled_int, side="sell", price=tp2),
                confirm=True,
            )
            if result.get("state") not in ("REJECTED", "FAILED"):
                logger.info("【止盈单已挂】%s %s股 @%s（tp1整手不足退化全量tp2 触发成交价=%s order_id=%s）",
                            symbol, filled_int, tp2, fill_price, order_id)
            else:
                logger.warning("止盈单挂失败 symbol=%s state=%s msg=%s（人工补挂）",
                               symbol, result.get("state"), result.get("message"))
            return

        # 分级挂两张单（tp1 锁利 + tp2 形态目标位）
        results = []
        # tp1 leg（锁利）
        r1 = await _submit(
            OrderRequest(symbol=symbol, qty=tp1_qty, side="sell", price=tp1),
            confirm=True,
        )
        results.append(("tp1", tp1, tp1_qty, r1))
        # tp2 leg（形态目标位，仅当有余量时挂）
        if tp2_qty > 0:
            r2 = await _submit(
                OrderRequest(symbol=symbol, qty=tp2_qty, side="sell", price=tp2),
                confirm=True,
            )
            results.append(("tp2", tp2, tp2_qty, r2))
        # 观测日志（两腿各自成败独立记录）
        for leg, price, qty, r in results:
            if r.get("state") not in ("REJECTED", "FAILED"):
                logger.info("【止盈单已挂】%s %s腿 %s股 @%s（分级 触发成交价=%s order_id=%s）",
                            symbol, leg, qty, price, fill_price, order_id)
            else:
                logger.warning("止盈单挂失败 symbol=%s leg=%s state=%s msg=%s（人工补挂）",
                               symbol, leg, r.get("state"), r.get("message"))

    # ----- 生命周期 -----
    def start(self) -> None:
        """启动 scheduler（阻塞主线程进入事件循环由 ``__main__`` 负责）。"""
        self.sched.start()
        logger.warning("TradingEngine 已启动（mode=%s）——独立常驻进程运行", _mode())

    def shutdown(self) -> None:
        """优雅停机（wait=False：不等 pending job，进程退出场景）。"""
        self.sched.shutdown(wait=False)
        logger.info("TradingEngine 已停机")
