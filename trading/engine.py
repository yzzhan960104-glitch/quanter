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
# Task 8（C-2 S3 pre_open 三段式 gate）：显式 re-export 让 patch 目标稳定。
# 物理意图：``pre_open`` 模块级函数与 ``TradingEngine._pre_open_gate`` 都直接调
# ``load_plan`` / ``get_data_ready``，独立 import 让测试可 ``patch("trading.engine.load_plan")``
# / ``patch("trading.engine.get_data_ready")`` 命中（trading_plan.get_data_ready 等其它
# 调用方仍走各自的命名空间引用，互不污染）。
from trading.trading_plan import load_plan
from trading.state_store import get_data_ready
from trading.io.breaker import cancel_all_open_orders as _cancel_all_open_orders
# Layer2 阶段6 follow-up #4a：signal_runner 垫片已删，直指真身 trading.compute.plan
from trading.compute.plan import build_orders_from_signals
from trading.compute.stop import should_trigger_stop, trading_days_between as _trading_days_between, compute_stop_price as _compute_stop_price
# Task 9（U6 实盘执行统一）：decide_exit 执行单源纯函数（Task 4 · simulate_exit 已切，实盘 stop_loss_monitor 切）
# strangler 等价红线：decide_exit 已证等价 simulate_exit（Task 5 golden 守护），实盘切用后
# 止损判定行为等价 should_trigger_stop（price≤stop → CLOSE/STOP_LOSS）；D12 fallback 保底。
from strategies.neckline.execution import decide_exit, ExitAction, ExitReason
# Task 10（R-2 日内熔断）：check_daily_loss_limit 纯判定 functional core
from trading.compute.breaker import check_daily_loss_limit as _check_daily_loss_limit
# Task 10（R-2）：position_book 的 daily_equity reader/writer（snapshot/get_start）
from trading import position_book as _position_book
# state-store-redesign：统一交易状态库（6 表，trade_event/order/fill/position/account_daily）。
# 是 position_book 的超集（真相源）。engine 落 SIGNAL/CONFIRMED 事件、写 order/fill 幂等、读
# stop_price/has_order 走 DB（替代 plan JSON 单一依赖 + _tp_placed 内存）。
from trading import state_store as _state_store
# Task 9（M4 静默漏单消灭）：致命事件钉钉 CRITICAL 告警（复用 infra.notifier，
# broker/qmt.py _reconnect 已在用同一套）。lazy import 避免顶层 import 副作用扩散到
# 仅用纯函数的测试场景——_alert_critical 内部 import 保持引用局部化。
logger = logging.getLogger(__name__)


def _alert_critical(msg: str) -> None:
    """Task 9（M4）：致命事件钉钉 CRITICAL（fire_and_forget 不阻塞主流程）。

    物理意图（spec M4 · [[qmt-connect-1-rootcase]] 教训）：
        引擎致命事件（pre_open 漏挂 / 口径自检失败 / health_guard 重连耗尽）若只写日志，
        钉钉不推 → 用户事后才发现漏单（全天锁死无告警 = 实盘废单日）。本函数是这些事件
        点的统一收口：复用 infra.notifier 推 CRITICAL 级钉钉（与 broker _reconnect 同通道）。

    Why level=CRITICAL：notify_risk_event 按 level 加前缀（🚨），CRITICAL 限致命事件
        避免告警风暴（撤单超时/WARN 级不升级）。三事件点均已用业务语义过滤过：

    Why fire_and_forget：告警在 daemon 线程跑（见 infra.notifier.fire_and_forget docstring），
        跨线程触发异步告警的最简显式做法，不阻塞 pre_open/start/health_guard 主路径——
        告警系统绝不能成为交易主链路的单点故障源（告警失败由 except 兜底记日志）。

    Args:
        msg: 告警正文（不含 level 前缀，notify_risk_event 自动加 🚨）。
    """
    try:
        from infra.notifier import NotificationManager, fire_and_forget
        fire_and_forget(NotificationManager.get_default().notify_risk_event(msg, "CRITICAL"))
    except Exception:
        # 告警发送失败不阻塞主流程（fire_and_forget 内部 asyncio.run 异常已被它自己 except，
        # 此处仅兜底 import/get_default 等同步异常）。告警是「尽最大努力」的观测通道，
        # 不能拖垮 pre_open/start/health_guard 主路径。
        logger.exception("CRITICAL 告警发送失败（不阻塞主流程）：%s", msg)


# ============================================================================
# 环境读取辅助
# ============================================================================

# 模块级活跃引擎单例（C-2 scheduling-orchestration W1）：
#   __init__ 末尾置位 self，供模块级 _submit / pre_open / post_close 反查实例的
#   ``_dynamic_whitelist`` 属性（engine 与 server 合并进同进程后的物理隔离机制——
#   注入/清空/拼白名单全部走实例属性，不再 mutate 模块级 _DYNAMIC 全局）。
#   ⚠️ 仅 engine 独立进程（python -m trading）内会构造 TradingEngine（task brief 红线），
#   故该单例在 server 进程内恒为 None——server 路径 submit_order 不传 whitelist，
#   _submit 见 None 即走旧路径（_whitelist() = 纯 env），向后兼容不变。
_ACTIVE_ENGINE: "TradingEngine | None" = None


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
        # pending 期撤单阈值（Task 9 · D11 · 对齐 simulate_exit:128 + NecklineConfig EXEC）：
        # cancel_on = 颈线 + cancel_thresh_mult×H。I-3 修正：默认 1.0（对齐回测 EXEC_DEFAULTS
        # backtest.py:49 + NecklineConfig schema.py:45，二者均为 1.0；原 Task 9 误设 0.75
        # 致实盘 env 缺省与回测分叉，违背 spec D9/「回测实盘一套逻辑」宗旨）。
        # 物理意图：挂单等待期 high≥此价 → 涨幅已兑现撤买单（过滤「猛突破后回踩」陷阱）。
        # env 缺省 → 对齐回测基准 1.0；显式 env 可覆盖（灰度调参用）。
        "cancel_thresh_mult": float(os.getenv("TRADE_CANCEL_THRESH_MULT", "1.0")),
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


# ============================================================================
# Task 7 U5 gate 下沉：完整性 gate 上下文加载 + 策略窗口解析（_eod 辅助）
# ============================================================================
def _load_integrity_ctx(today: str):
    """加载完整性 gate 上下文：停牌区间 + 近 2 年 trade_days（fail-open）。

    物理意图（Task 7 U5 · 300214.SZ 漏采教训）：完整性 gate 从 scan_live 上提到 _eod
    后，_eod 需在 filter_universe_by_continuity 前加载 susp/trade_days。逻辑从原 scan_live
    内联的 ``_ensure_integrity_cache`` 搬出（模块级 cache 删除——_eod 每次盘后只调一次，
    无需跨调用缓存；若重复调用可再引入缓存）。

    fail-open 红线（与原 _ensure_integrity_cache:80-83 同口径）：
        加载失败（无文件/无 token/网络异常）→ 返 ({}, set()) 让 filter 放行。
        trade_days 空集 → check_window_continuity 的 expected 恒空 → missing 恒空 →
        ok=True 全放行，退回原行为（gate 是新增防护，失效时不阻断识别）。

    Returns:
        (susp_intervals, trade_days_set)：dict[str, set[str]] + set[str]。
    """
    try:
        from datetime import datetime as _dt, timedelta as _td
        from pathlib import Path as _Path
        import pandas as _pd
        from data.integrity import load_suspend_intervals, fetch_trade_days
        start = (_dt.strptime(today, "%Y-%m-%d") - _td(days=730)).strftime("%Y-%m-%d")
        trade_days = fetch_trade_days(start, today)
        susp_path = _Path("data_lake/suspend_d.parquet")
        if susp_path.exists():
            susp_df = _pd.read_parquet(susp_path)
            susp = load_suspend_intervals(susp_df, trade_days)
        else:
            logger.warning("suspend_d.parquet 缺失，完整性 gate 无停牌 ground-truth（全判漏采）")
            susp = {}
        return susp, trade_days
    except Exception as e:
        logger.warning("完整性 gate 上下文加载失败（fail-open 放行）：%s", e)
        return {}, set()


def _resolve_id_window(strategy) -> int:
    """从策略实例解析识别窗口（颈线法 id_cfg["window"]）。

    Why 不硬编码：颈线法的 window 经 NecklineConfig 默认值 + cfg_override 覆盖后落在
    strategy.id_cfg["window"]（与原 scan_live:232 的 self.id_cfg["window"] 同源）。本函数
    安全兜底：策略无 id_cfg 属性或缺 window 键时返 DEFAULTS.window（60）。
    """
    try:
        w = getattr(strategy, "id_cfg", {}).get("window")
        if w and int(w) > 0:
            return int(w)
    except Exception:
        pass
    # 兜底：颈线法 DEFAULTS.window=60（与 method_v0.DEFAULTS 同口径）
    return 60


def _resolve_account_id() -> str:
    """解析当前账户 ID（state_store 写事件/委托时的 account_id）。

    物理意图：engine 落 trade_event/order 需要归属账户。优先读 .env QMT_ACCOUNT_ID（启动期
    _migrate_env_to_account 已落库），缺失（dry_run 无 broker 配置）时用 state_store 默认账户。
    保证 dry_run 测试/影子期也有稳定 account_id（不依赖真实 QMT 凭证）。
    """
    aid = os.getenv("QMT_ACCOUNT_ID")
    return aid if aid else _state_store._DEFAULT_ACCOUNT_ID



def get_gateway():
    """惰性取交易网关单例（透传 trading_service.get_gateway）。

    Why 透传不重造：网关单例的装配（QMT 唯一，无凭证→None）与懒构造策略已在
    trading_service.get_gateway 固化，本引擎薄编排不重复，避免双单例漂移。
    本函数独立出来便于测试 monkeypatch（engine.get_gateway）隔离真实网关副作用。
    """
    from presentation.server.services.trading_service import get_gateway as _svc_get_gw
    return _svc_get_gw()


async def _submit(order, *, confirm: bool = True, whitelist: set | None = None) -> dict:
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

    whitelist 物理隔离（C-2 scheduling-orchestration W1）：
        调用方（pre_open/stop_loss_monitor）均不显式传 whitelist（默认 None），
        由本函数从模块级 ``_ACTIVE_ENGINE`` 单例取 engine 实例的 ``_dynamic_whitelist``
        并拼静态 env（``self._dynamic_whitelist | static_env_whitelist()``），显式透传
        给 svc_submit。这样 engine 自动下单通道的白名单与模块级 ``_DYNAMIC`` 全局物理解耦——
        engine 与 server 合并进同进程后，server 路径的 submit_order（不传 whitelist）仍走
        ``_whitelist() = get_effective_whitelist()``（_DYNAMIC 恒空 = 纯 env），向后兼容不变。
        ``_ACTIVE_ENGINE is None`` 仅在未构造 TradingEngine 时发生（理论上不会，__init__ 必置），
        此防御性分支回退到旧路径（读 get_effective_whitelist），保证 ``python -m trading``
        单进程语义不变。
    """
    from presentation.server.services.trading_service import submit_order as svc_submit
    from trading.dynamic_whitelist import get_effective_whitelist, static_env_whitelist
    if whitelist is None:
        if _ACTIVE_ENGINE is not None:
            whitelist = _ACTIVE_ENGINE._dynamic_whitelist | static_env_whitelist()
        else:
            # 防御性回退：未构造 TradingEngine（理论不会，__init__ 必置 _ACTIVE_ENGINE）
            whitelist = get_effective_whitelist()
    return await svc_submit(order, dry_run=(_mode() == "dry_run"), confirm=confirm,
                            whitelist=whitelist)


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
            # Task 9（D11）：cancel_thresh_mult 透传 build_orders 算 pending 期撤单阈值
            # （颈线+cancel_thresh_mult×H，对齐 simulate_exit:128-129）。env 缺省 0.75。
            "cancel_thresh_mult": cfg.get("cancel_thresh_mult"),
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
            # Task 9（D11）：cancel_on pending 期撤单阈值落盘（颈线+cancel_thresh_mult×H）。
            # Why 落盘：_stoploss pending_ctx 盘中读 plan.cancel_on 监控 high≥此价撤买单
            # （对齐 simulate_exit:130 skip_target_met）。None（cfg 未配 cancel_thresh_mult）
            # → JSON null，_stoploss 检测 falsy 不塞 pending_ctx（不撤单放飞，向后兼容）。
            "cancel_on": round(o.cancel_on, 2) if o.cancel_on is not None else None,
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
    # state-store-redesign §3.3：plan 双写——DB 落 trade_event(SIGNAL, meta=计划参数) 作真相源，
    # JSON 保留给人看/veto CLI/钉钉。SIGNAL 幂等（UNIQUE account_id+trade_id+action）：重跑 eod_plan
    # 已存在则跳过（不重复记）。account_id 缺省走默认账户（_migrate_env_to_account 在启动期落真实账户）。
    try:
        account_id = _resolve_account_id()
        # 确保 account 行存在（trade_event/order FK 引用；init_store 只建表不插行）
        if _state_store.get_account(account_id) is None:
            _state_store.upsert_account(account_id, broker="qmt")
        for o in order_dicts:
            sym = (o.get("order") or {}).get("symbol")
            if not sym:
                continue
            trade_id = f"{account_id}_{sym}_{date}"
            # SIGNAL meta 存计划参数快照（stop_loss/pre_open 改从 DB 读，spec §3.3）
            _state_store.insert_trade_event(
                account_id, trade_id, sym, "SIGNAL", meta=json.dumps(o, ensure_ascii=False))
            # CONFIRMED 仅在 auto_confirmed 且未被 veto 时写（veto 保护：最新 action=VETOED 不覆盖）
            if auto_confirmed and _state_store.get_latest_action(trade_id) != "VETOED":
                _state_store.insert_trade_event(account_id, trade_id, sym, "CONFIRMED")
    except Exception:
        # DB 写失败不阻断 eod_plan 主流程（plan JSON 已落，DB 软降级，下次 eod 补写）
        logger.exception("eod_plan 落 trade_event(SIGNAL) 失败（不阻断主流程，软降级）")
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
    # S3（Task 8 · C-2）：三段式前置 gate（通过 _ACTIVE_ENGINE 单例调用实例方法）。
    # 物理意图：plan-confirmed → gateway-health → data-ready 三段全绿才放行下游（撤昨日单 /
    # 抓熔断基线 / 挂新单）。任一未绿即早返 skip payload，绝不触达网关写操作。顺序「先便宜
    # 后贵」（JSON < 探测 < DB 查询）。gate 失败在 live 模式下推 CRITICAL 钉钉（复用
    # _alert_critical 统一收口，与 M4 静默漏单告警同通道）。
    # Why 经 _ACTIVE_ENGINE 单例：本函数是模块级函数，gate 是 TradingEngine 实例方法
    # （需 ``self._plan_data_keys`` 反查策略数据集），故经模块级单例桥接（Task 4 范式）。
    # _ACTIVE_ENGINE is None 仅在未构造 TradingEngine 时发生（理论不会，__init__ 必置），
    # 此防御性分支跳过 gate 直接走原 plan["confirmed"] 检查（向后兼容，不破坏旧行为）。
    if _ACTIVE_ENGINE is not None:
        gate_ok, gate_reason = await _ACTIVE_ENGINE._pre_open_gate(date, get_gateway())
        if not gate_ok:
            msg = f"pre_open gate 未通过：{gate_reason}，跳过挂单"
            logger.warning(msg)
            if _mode() == "live":
                # live 模式 gate 拦截 = 当日废单日风险（网关锁死 / 数据未就绪 / 计划未确认），
                # 仅 logger.warning 不足以叫醒用户（spec M4 教训），推 CRITICAL 钉钉。
                _alert_critical(msg)
            # 返回 shape 与 pre_open 其它返回对齐（success: {"submitted","mode"}；
            # skip: {"submitted","reason"}）——保留 skipped（携带 gate reason，比 reason 更
            # 富信息）+ 补 submitted/mode 让任何读 result["submitted"] 的调用方不 KeyError。
            return {"submitted": 0, "mode": _mode(), "skipped": gate_reason}

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
            # M2（T3）：cancel_all_open_orders 返回 {cancelled, unconfirmed}。
            # cancelled=成功发起撤单数；unconfirmed=发起后 _confirm_cancelled 超时
            # 未到终态的笔数（主推延迟/柜台未响应）。unconfirmed>0 仅告警不阻塞挂单——
            # 撤单已发，本地状态终会被 on_cancel_error/on_stock_order 对账修正，但必须
            # 显式暴露此口径让运维知晓，杜绝「本地以为撤了、柜台其实没撤」的状态悬空。
            _cancel_res = await _cancel_all_open_orders(gw)
            n_cancelled = _cancel_res["cancelled"]
            n_unconfirmed = _cancel_res["unconfirmed"]
            logger.info(
                "pre_open 撤昨日未成交单 发起 %s 笔（未确认 %s 笔）",
                n_cancelled, n_unconfirmed,
            )
            if n_unconfirmed > 0:
                logger.warning(
                    "pre_open 有 %s 笔撤单未确认终态（主推延迟或柜台未响应，需人工复核）",
                    n_unconfirmed,
                )
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

    # ③ 注入动态白名单（Task5 → C-2 W1 改实例属性）：仅 engine 通道生效。
    # 改造后注入到 engine 实例 self._dynamic_whitelist（通过 _ACTIVE_ENGINE 单例反查），
    # 而非模块级 _DYNAMIC 全局——engine 与 server 合并进同进程后，实例属性化是两端
    # 白名单物理隔离的唯一手段（server 路径不读实例属性）。_ACTIVE_ENGINE 理论非空
    # （pre_open 由 TradingEngine 装配的 job 触发），None 守卫为防御性兜底（回退旧路径）。
    symbols = {o["order"]["symbol"] for o in plan["orders"]}
    if _ACTIVE_ENGINE is not None:
        _ACTIVE_ENGINE._dynamic_whitelist |= symbols
    else:  # pragma: no cover - 防御性回退：未构造 TradingEngine（理论不会）
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
    account_id = _resolve_account_id()
    # 确保 account 行存在（insert_order/trade_event FK 引用）
    try:
        if _state_store.get_account(account_id) is None:
            _state_store.upsert_account(account_id, broker="qmt")
    except Exception:
        logger.exception("pre_open 确保 account 行失败（不阻断挂单，DB 写入将软降级）")
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
        # T8（state-store-redesign §4.1）DB 幂等挂单：
        # ① veto 保护：trade_event 最新 action=VETOED → 跳过（研究员否决不挂）
        # ② has_order(OPEN)：同日同标的已挂过 OPEN → 跳过（pre_open 重跑/崩溃重启不重复挂）
        trade_id = f"{account_id}_{od['symbol']}_{date}"
        try:
            if _state_store.get_latest_action(trade_id) == "VETOED":
                logger.info("pre_open 跳过 vetoed 标的 symbol=%s", od["symbol"])
                continue
            if _state_store.has_order(account_id, date, od["symbol"], "OPEN"):
                logger.info("pre_open 跳过已挂 OPEN（DB 幂等）symbol=%s", od["symbol"])
                continue
        except Exception:
            # DB 查询失败不阻断挂单（主路径是真实挂单，DB 是对账层，软降级）
            logger.exception("pre_open DB 幂等检查失败 symbol=%s（不阻断，可能重复挂）", od["symbol"])
        order_req = OrderRequest(
            symbol=od["symbol"], qty=od["qty"], side=od["side"], price=od["price"],
        )
        # T8：挂单前先 insert_order(OPEN, PENDING)（DB 真相源，幂等 UNIQUE）
        try:
            _order_id = f"{date}_{od['symbol']}_OPEN_1"
            _state_store.insert_order(
                _order_id, trade_id, account_id, date, od["symbol"], od["side"], "OPEN",
                float(od["qty"]), float(od["price"]), state="PENDING")
        except Exception:
            logger.exception("pre_open insert_order(OPEN) 失败 symbol=%s（不阻断挂单）", od["symbol"])
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
            # T8：挂单成功 → 回填 order.state=SUBMITTED + broker_oid（seq）+ trade_event(ORDERED)
            try:
                broker_oid = str(result.get("order_id") or "")
                _state_store.update_order_state(
                    _order_id, "SUBMITTED",
                    broker_oid=broker_oid or None,
                    submitted_at=datetime.now().isoformat())
                _state_store.insert_trade_event(
                    account_id, trade_id, od["symbol"], "ORDERED",
                    order_id=_order_id, qty=float(od["qty"]), price=float(od["price"]))
            except Exception:
                logger.exception("pre_open 回填 order SUBMITTED/ORDERED 失败 symbol=%s", od["symbol"])
        else:
            logger.warning("pre_open 挂单未成功 symbol=%s state=%s msg=%s",
                           od["symbol"], result.get("state"), result.get("message"))

    logger.info("pre_open 完成 date=%s submitted=%d/%d expired=%d mode=%s",
                date, n_submitted, len(plan["orders"]), n_expired, _mode())
    # Task 9（M4 静默漏单消灭）：live 模式 submitted=0 且有计划单 → 钉钉 CRITICAL。
    # 物理意图：live 下「全部挂单失败」= 当日废单日（网关锁死 / 涨跌停挡板 / 资金不足），
    # 仅 logger.warning 不足以叫醒用户（[[qmt-connect-1-rootcause]] 全天锁死无告警教训）。
    # Why 限 live：dry_run submitted=0 多半是 DRY_RUN 状态误判或测试 mock，非真漏单风险；
    # Why 限 len(orders)>0：无计划单（0/0）是「当日无信号」正常态，不该误告警。
    if n_submitted == 0 and _mode() == "live" and len(plan["orders"]) > 0:
        _alert_critical(
            f"pre_open 漏挂 submitted=0/{len(plan['orders'])} date={date}"
            f"（网关锁死? 网关拒绝所有单? 人工核查 gw 状态与挡板日志）")
    return {"submitted": n_submitted, "mode": _mode()}


# ============================================================================
# 触发点 3：stop_loss_monitor —— 盘中持仓巡检切 decide_exit + pending cancel_on
# ============================================================================
async def stop_loss_monitor(
    stop_prices: Optional[Mapping[str, float]] = None,
    *,
    gw: Any = None,
    monitor_ctx: Optional[Mapping[str, Mapping[str, Any]]] = None,
    pending_ctx: Optional[Mapping[str, float]] = None,
) -> dict:
    """盘中止损/超时巡检 + pending 期撤单（Task 9 · U6 实盘执行统一 · 最高风险）。

    物理定位（spec §2 目标 3 + §4.6 + D10/D11/D12）：
        【主路径】monitor_ctx 注入 → 每标的构造 state+bar → decide_exit（Task 4 执行单源
        纯函数，simulate_exit 已切 Task 5）→ 按 action/reason/portion 发单/跳过。让实盘
        与回测共用执行判定（strangler 等价：止损判定行为等价 should_trigger_stop，
        decide_exit 已证等价 simulate_exit）。
        【D12 fallback】decide_exit 抛异常 → 降级 should_trigger_stop(price, sp)（原 :684
        逻辑），盘中不裸奔（真金损失红线）。fallback 有告警计数。
        【D11 pending cancel_on】pending_ctx 注入 + gw.query_orders → pending 买单 high≥
        cancel_on 撤单（对齐 simulate_exit:130 skip_target_met，当前实盘缺这环）。

    ⚠️ live 安全红线（scope #3）：卖出 qty **必须**来自 gw._fetch_broker_positions() 的真实
    持仓，**绝不硬编码**——硬编码 100 会导致实盘卖错数量（致命）。

    ⚠️ R7 bar 防御（resolution 3/5 · 防漏判误判）：bar.high/low 用 xtdata 当日累积快照
    （``get_quotes`` 返 tick 的 high/low 字段，get_full_tick 已是开盘至当前的累积最高/最低），
    **非单 tick last_price**——单 tick 会漏盘中摸高（漏止盈）/探底（漏止损）致 decide_exit
    误判。close=last_price。若 xtdata 当日累积不可得（quote 无 high/low）→ 回退 last_price
    作 high/low/close（fail-safe，日志告警，最保守）。

    ⚠️ 现价依赖（C1 fix + T3 批量）：现价统一从 ``qmt_market_data.get_quotes`` 批量取
    ``last_price`` + 当日累积 ``high``/``low``。该接口底层 ``xtdata.get_full_tick``，仅在
    miniQMT 通道可用时返回有效快照。**止损/止盈/撤单链路依赖 xtdata 行情源，无 xtdata 时
    live 前必须另接行情源**（live 前必修 follow-up，切勿在无 xtdata 行情源环境切 live）。

    Args:
        stop_prices: {symbol: stop_price}（D12 fallback 来源 + 向后兼容旧契约）。主路径
                     monitor_ctx 注入后此参数仅作 decide_exit 异常时的兜底比价基准；
                     monitor_ctx 为 None 时退回纯 should_trigger_stop 旧路径（向后兼容）。
        gw:          网关实例（测试注入）；None 时内部 get_gateway()。
        monitor_ctx: {symbol: {"state": dict, "cfg": dict}}（主路径）。state/cfg 字段对齐
                     decide_exit 契约（execution.py:131-201）+ simulate_exit cfg
                     （backtest.py:177-183）。None 时纯走 stop_prices 旧路径。
        pending_ctx: {symbol: cancel_on}（D11 pending 撤单）。None 时跳过 pending 巡检。

    Returns:
        盘中：{"checked":N, "stop_triggered":M, "fallback_used":K, "pending_cancelled":P,
               "mode":...}
        非盘中：{"checked":0, "reason":"非盘中时段..."}
        无 gw：{"checked":0, "reason":"...网关..."}
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

    # T10（state-store-redesign §4.3）止损 DB 幂等辅助闭包：
    # _stop_already_placed：查 has_order(STOP)，已挂未终态 → 跳过（不重复发卖）
    # _record_stop：发止损单后落 DB order(STOP) + trade_event(STOP_TRIGGERED)
    _aid = _resolve_account_id()
    _today = datetime.now().strftime("%Y-%m-%d")

    def _stop_already_placed(sym: str) -> bool:
        """查 DB 是否已挂 STOP 委托（幂等检查）。"""
        try:
            return _state_store.has_order(_aid, _today, sym, "STOP")
        except Exception:
            logger.exception("查 DB has_order(STOP) 失败 symbol=%s（回退非幂等，可能重发）", sym)
            return False

    def _record_stop(sym: str, qty: float, price: float) -> None:
        """发止损单后落 DB order(STOP) + trade_event(STOP_TRIGGERED)。失败仅 log。"""
        try:
            if _state_store.get_account(_aid) is None:
                _state_store.upsert_account(_aid, broker="qmt")
            trade_id = f"{_aid}_{sym}_{_today}"
            oid = f"{_today}_{sym}_STOP_1"
            _state_store.insert_order(
                oid, trade_id, _aid, _today, sym, "sell", "STOP",
                float(qty), float(price), state="SUBMITTED")
            _state_store.insert_trade_event(
                _aid, trade_id, sym, "STOP_TRIGGERED",
                order_id=oid, qty=float(qty), price=float(price))
        except Exception:
            logger.exception("record_stop 落 DB 失败 symbol=%s（不阻断卖出）", sym)
    # 主路径（monitor_ctx）与 fallback（stop_prices）至少其一非空才有监控意义；
    # pending_ctx 单独判定（pending 期无持仓，positions 空也照巡）。
    has_main_path = monitor_ctx is not None and len(monitor_ctx) > 0
    has_fallback = stop_prices is not None and len(stop_prices) > 0
    has_pending = pending_ctx is not None and len(pending_ctx) > 0
    if not (has_main_path or has_fallback or has_pending):
        return {"checked": 0, "reason": "无止损/撤单配置（monitor_ctx/stop_prices/pending_ctx 均空）"}

    try:
        positions = await gw._fetch_broker_positions()  # {symbol: {volume, ...}}（T7 扩展）
    except Exception:
        # 持仓查询失败绝不下卖出单（敞口未明即操作 = 盲卖，违反风控）
        logger.exception("stop_loss_monitor 查持仓失败（拒发任何卖出单）")
        return {"checked": 0, "reason": "查持仓异常，拒发卖出单"}

    # ③ 批量取所有相关标的现价 + 当日累积 high/low（T3 优化 + R7 bar 防御）。
    #   相关标的 = 持仓 ∪ monitor_ctx keys ∪ pending_ctx keys（撤单期标的可能未成交无持仓）。
    #   Why 批量：N 只原 N 次 get_full_tick → 1 次（原生 list 入参），减 GIL/C++ 调用开销。
    #   ⚠️ xtdata 通道（miniQMT）返快照含当日累积 high/low；无 xtdata 时 live 前需另接行情源。
    from trading.compute.types import OrderRequest  # Layer2 阶段6 follow-up #4b：直指 compute.types 真身
    relevant_syms = set(positions.keys())
    if has_main_path:
        relevant_syms |= set(monitor_ctx.keys())
    if has_pending:
        relevant_syms |= set(pending_ctx.keys())
    quotes = await qmt_market_data.get_quotes(list(relevant_syms)) if relevant_syms else {}
    n_triggered = 0
    n_checked = 0
    n_fallback = 0
    n_pending_cancelled = 0

    # ── ④ holding 期巡检：每持仓标的构造 state+bar → decide_exit → 按分发（D12 fallback）──
    # T7：positions 现为 {sym: {volume, avg_price, ...}}（dict-of-dict），qty 取 volume 子键。
    for sym, pos in positions.items():
        qty = pos["volume"] if isinstance(pos, dict) else pos  # 兼容老 mock 返 float
        if qty <= 0:
            continue
        quote = quotes.get(sym)
        price = quote.get("last_price") if quote else None
        if price is None or price != price:  # NaN check（price != price ⟺ isNaN）
            # 现价缺失/NaN 绝不下卖出单：无价不能判断跌破（盲单 = 卖错价 = 致命）
            logger.warning("stop_loss_monitor 跳过 %s：现价缺失（quote=%s），无法判定跌破", sym, quote)
            continue
        n_checked += 1

        # R7 bar 防御（resolution 3/5）：high/low 优先 xtdata 当日累积（quote.high/low），
        # 缺失时回退 last_price（fail-safe，盘中穿止损后反弹的单 tick 会漏判，告警人工）。
        q_high = quote.get("high") if quote else None
        q_low = quote.get("low") if quote else None
        if q_high is None or q_low is None or q_high != q_high or q_low != q_low:
            logger.warning(
                "stop_loss_monitor %s 当日累积 high/low 缺失（quote=%s），回退 last_price 作 bar"
                "（R7 防御降级：单 tick 可能漏判盘中摸高/探底，告警人工复核）", sym, quote)
            bar = {"high": price, "low": price, "close": price}
        else:
            bar = {"high": float(q_high), "low": float(q_low), "close": float(price)}

        # ── 主路径：monitor_ctx 注入 → decide_exit（执行单源）──
        ctx = monitor_ctx.get(sym) if has_main_path else None
        if ctx is not None and isinstance(ctx, Mapping):
            try:
                state = dict(ctx.get("state") or {})
                cfg = dict(ctx.get("cfg") or {})
                # 兜底补 phase（_stoploss 已设，防御直接调 monitor_ctx 的测试/灰度场景）
                state.setdefault("phase", "holding")
                dec = decide_exit(state, bar, cfg)
            except Exception:
                # D12 fallback 红线（resolution 2 · 盘中不裸奔）：decide_exit 抛任何异常
                # （state 缺键 / compute_stop_price 除零 / bar NaN 等）→ 降级 should_trigger_stop，
                # 绝不让止损监控整体崩溃（持仓裸奔 = 真金损失）。fallback 有告警计数。
                n_fallback += 1
                logger.exception(
                    "【D12 降级】%s decide_exit 异常，降级 should_trigger_stop 兜底（盘中不裸奔）",
                    sym, exc_info=True)
                dec = None
            else:
                # ── 按 decide_exit action 分发（等价 simulate_exit:208-254 的分发循环单根）──
                # 分发三路径（I-1 修正 · spec D10 物理边界 · reviewer 方案 A）：
                #   ① STOP_LOSS/TIMEOUT（CLOSE/STOP_LOSS | CLOSE/TIMEOUT）→ monitor 发市价卖单
                #      （止损/超期是 monitor 职责，无预挂单，必须 monitor 主动平）。
                #   ② TAKE_PROFIT（CLOSE/TAKE_PROFIT，含 portion<1 tp1 与 portion=1.0 tp2）
                #      → monitor **不发单跳过**（continue）：实盘止盈由 _place_take_profit
                #      （engine.py:1899）预挂 tp1+tp2 限价单撮合（D10 物理边界，spec §4.6）。
                #      Why skip：decide_exit 是无状态纯函数，monitor_ctx.state.lot1_open/
                #      lot2_open 默认 True 不翻转（限价单成交无回报改 state），tp1 限价单
                #      成交后下次巡检 decide_exit priority 3 仍返 CLOSE/TAKE_PROFIT → monitor
                #      再发一笔 tp1 市价部分卖单 = 与已成交限价单重复（滑点差异 + broker 拒单
                #      风险）。skip 消除重复窗口，TP 完全交预挂限价单——符合 D10。
                #   ③ HOLD → 跳过（decide_exit 已判无需离场）。
                if dec.action is ExitAction.CLOSE:
                    if dec.reason is ExitReason.TAKE_PROFIT:
                        # I-1：TP 分支交 _place_take_profit 预挂限价单，monitor 不发市价单
                        # （D10 物理边界：实盘止盈=限价单预挂撮合，非市价追平）。日志观测用。
                        logger.info(
                            "【TP 跳过】%s decide_exit %s/%s portion=%.2f —— TP 由 _place_take_profit"
                            " 预挂限价单撮合，monitor 跳过不发市价单（D10 物理边界，防与预挂重复）",
                            sym, dec.action.name, dec.reason.name, dec.portion)
                        continue   # TP 交预挂，不走 fallback
                    # STOP_LOSS | TIMEOUT（CLOSE，portion=1.0 全平）→ 发市价卖出单
                    # （止损/超期是 monitor 职责，对齐 Task 9 前的 _submit 路径）。
                    sell_qty = int(qty) if dec.portion >= 1.0 else int(qty * dec.portion / 100) * 100
                    if sell_qty > 0:
                        # T10（state-store-redesign §4.3）DB 幂等：已有 STOP 委托未终态 → 跳过
                        if _stop_already_placed(sym):
                            logger.info("stop_loss 跳过已挂 STOP（DB 幂等）symbol=%s", sym)
                            continue
                        try:
                            result = await _submit(
                                OrderRequest(symbol=sym, qty=sell_qty, side="sell", price=price),
                                confirm=True,
                            )
                        except Exception as exc:
                            logger.warning(
                                "stop_loss_monitor 卖出失败 symbol=%s qty=%s 原因=%s",
                                sym, sell_qty, exc)
                            result = {"state": "FAILED"}
                        if result.get("state") not in ("REJECTED", "FAILED"):
                            n_triggered += 1
                            _record_stop(sym, sell_qty, price)
                            logger.warning(
                                "【止损/超时触发】%s 卖出 %s 股 @%s（decide_exit %s/%s portion=%.2f mode=%s）",
                                sym, sell_qty, price, dec.action.name, dec.reason.name,
                                dec.portion, _mode())
                    continue   # decide_exit 已决策（CLOSE），不走 fallback
                # HOLD → 跳过不发单（decide_exit 已判无需离场）
                if dec.action is ExitAction.HOLD:
                    continue
                # CANCEL（pending 期撤单，holding 期不应触达，防御日志）
                logger.warning(
                    "stop_loss_monitor %s decide_exit 返异常 action=%s（holding 期不应 CANCEL）",
                    sym, dec.action.name)

            # dec is None（decide_exit 异常）或 CLOSE 分支未 continue → 走 D12 fallback
            # （下方 should_trigger_stop 逻辑，sp 从 stop_prices 或 state.stop 取）

        # ── fallback 路径：should_trigger_stop（D12 兜底 + monitor_ctx 未注入的旧契约）──
        # sp 来源优先：monitor_ctx.state.stop（_stoploss 从 plan 注入的当日固定止损价）>
        # stop_prices[sym]（旧契约）。两者都无则跳过（无止损价不盲卖）。
        sp = None
        if ctx is not None and isinstance(ctx, Mapping):
            sp = (ctx.get("state") or {}).get("stop")
        if sp is None and has_fallback:
            sp = stop_prices.get(sym)
        if sp is None:
            continue   # 无止损价配置，跳过（保守不盲卖）
        if should_trigger_stop(price, sp):
            # T10（state-store-redesign §4.3）DB 幂等：已有 STOP 委托未终态 → 跳过
            if _stop_already_placed(sym):
                logger.info("stop_loss 跳过已挂 STOP（DB 幂等）symbol=%s", sym)
                continue
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
                _record_stop(sym, qty, price)
                logger.warning(
                    "【止损触发】%s 卖出 %s 股 @%s（止损价 %s，fallback should_trigger_stop mode=%s）",
                    sym, qty, price, sp, _mode())

    # ── ⑤ pending 期 cancel_on 巡检（D11 · resolution 4 · 当前实盘缺这环）──
    # 物理意图：挂单等待期（pre_open 挂买单未成交），盘中 high≥cancel_on → 涨幅已兑现，
    # 回踩是退潮，撤买单（对齐 simulate_exit:130 skip_target_met）。cancel_on 从 pending_ctx。
    # 查 gw 今日可撤买单（cancelable_only=True）→ 匹配 symbol → high≥cancel_on → cancel_order。
    if has_pending and gw is not None:
        try:
            cancelable = await gw.query_orders(cancelable_only=True)
        except Exception:
            cancelable = []
            logger.warning("stop_loss_monitor 查可撤单异常（跳过 pending cancel_on 巡检）",
                           exc_info=True)
        # 按 symbol 索引可撤买单（order_type=STOCK_BUY，xtconstant 契约，与 broker/qmt.py:724
        # 及 engine._order_direction 同源）。lazy import xtconstant（CI/单测无 xtquant 兜底 23）。
        try:
            from xtquant import xtconstant
            _STOCK_BUY = xtconstant.STOCK_BUY
        except ImportError:
            _STOCK_BUY = 23   # 与 engine.py:1655 兜底同值（tests/conftest.py 假 xtconstant）
        pending_by_sym: dict[str, list[str]] = {}
        for o in cancelable or []:
            osym = o.get("stock_code")
            # 防御性只撤买单（order_type==STOCK_BUY）；卖单/未知方向不撤（撤错=漏买机会成本）
            if osym and o.get("order_type") == _STOCK_BUY:
                pending_by_sym.setdefault(osym, []).append(o.get("order_id"))
        for sym, cancel_on in (pending_ctx or {}).items():
            if cancel_on is None:
                continue
            quote = quotes.get(sym)
            day_high = quote.get("high") if quote else None
            if day_high is None or day_high != day_high:
                # 当日累积 high 缺失无法判摸高 → 跳过（不盲撤，撤错单=漏买机会成本）
                continue
            if float(day_high) < float(cancel_on):
                continue   # 未触 cancel_on，继续等回踩
            # high ≥ cancel_on → 撤该 sym 所有 pending 买单
            for oid in pending_by_sym.get(sym, []):
                try:
                    await gw.cancel_order(oid)
                    # M2（T3）：撤单「发起」成功后，确认是否真到终态。
                    # QMT 主推延迟 1-2s（[[qmt-live-smoke-findings]]），不确认则
                    # 状态悬空（本地计 pending_cancelled 但柜台没撤 → 漏买变漏卖）。
                    # True=到终态（CANCELLED 撤成 或 FILLED 撤晚已成）才计成功；
                    # False=超时未确认 → 不计 pending_cancelled + WARNING 人工复核。
                    # 鸭子类型（与 breaker.py 同风格）：getattr 取方法引用，None 则跳过
                    # 确认。严禁 hasattr + 直 await：MagicMock 自动属性会让 hasattr 恒 True
                    # 但裸 MagicMock 不可 await → TypeError。getattr 默认 None 规避此陷阱。
                    _confirm = getattr(gw, "_confirm_cancelled", None)
                    _ok = await _confirm(str(oid), timeout=5.0, interval=0.5) if _confirm else True
                    if _ok:
                        n_pending_cancelled += 1
                        logger.warning(
                            "【pending 撤单】%s order_id=%s（high=%s ≥ cancel_on=%s，涨幅兑现撤买单）",
                            sym, oid, day_high, cancel_on)
                    else:
                        logger.warning(
                            "pending 撤单未确认终态 %s order_id=%s（主推延迟或柜台未响应，不计成功，需人工复核）",
                            sym, oid)
                except Exception as exc:
                    logger.warning("pending 撤单失败 symbol=%s order_id=%s 原因=%s",
                                   sym, oid, exc)

    logger.info("stop_loss_monitor 完成 checked=%d triggered=%d fallback=%d pending_cancelled=%d mode=%s",
                n_checked, n_triggered, n_fallback, n_pending_cancelled, _mode())
    return {"checked": n_checked, "stop_triggered": n_triggered,
            "fallback_used": n_fallback, "pending_cancelled": n_pending_cancelled,
            "mode": _mode()}


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

    I-4 口径澄清（与 monitor is_last 的优先级语义，**不改运算符**）：
        本函数用 `holding_days > max_holding`（严格大于，第 max_holding+1 日才标），
        而 monitor is_last 用 `holding_days >= max_holding`（第 max_holding 日即市价强平）。
        Why `>` 非 `>=`（兜底设计，防同日双卖）：
          - 第 max_holding 日：monitor is_last=True 先触发，市价优先平仓（对齐回测 is_last
            语义，市价成交）；
          - 第 max_holding+1 日：本函数才标超期，post_close 写 expired_positions → 次日
            pre_open 跌停价兜底平（处理 monitor 漏掉的标的：如 monitor 当日崩、断线、
            持仓查询失败等）。
        若改 `>=`：post_close 与 monitor 同日触发——monitor 市价先平后 post_close 跌停价
        再挂卖 = 卖空风险（持仓已平再挂卖单）。故 `>` 是兜底设计，与 monitor `>=` 错位
        一日，互不冲突。
    """
    expired: list[dict] = []
    for sym, entry_date in _position_book.get_entry_dates().items():
        holding_days = _trading_days_between(entry_date, today)
        # I-4：`>` 严格大于（第 max_holding+1 日才标超期）——兜底 monitor 漏掉的标的，
        # 不与 monitor is_last `>=`（第 max_holding 日市价强平）同日冲突（防卖空）。详见上方 docstring。
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

    # ② query_trades 盘后兜底纠正（Task 11 · R-1 · reconcile 之后、熔断之前）：
    # 物理意图：apply_fill 可能因 db lock/异常漏记（_handle_order_update 软降级），position_book
    # 少记；record_live_trade 写 CSV 是独立 try-except，漏笔概率低于 apply_fill。用 CSV 流水聚合
    # vs position_book，drift 以 CSV 为准重写 qty + 告警。分工：①reconcile 查持仓 drift，本步查
    # 成交流水漏笔。gw=None（dry_run）跳过（无真实成交可对，避免误读 CSV 老数据重写账本）。
    if gw is not None:
        try:
            from presentation.server.services.trading_service import query_trades as _svc_query_trades
            today_eq = datetime.now().strftime("%Y-%m-%d")
            trades = (_svc_query_trades(today_eq, today_eq, limit=1000) or {}).get("trades", [])
            # 聚合净持仓（BUY 加 SELL 减）——CSV 流水的权威净持仓口径
            net: dict[str, float] = {}
            for t in trades:
                sym = t.get("symbol")
                direction = (t.get("direction") or "").upper()
                shares = t.get("shares")
                if not sym or direction not in ("BUY", "SELL") or shares is None:
                    continue
                net[sym] = net.get(sym, 0.0) + (float(shares) if direction == "BUY" else -float(shares))
            local = _position_book.get_local_positions()
            drifts: list[tuple[str, float, float]] = []
            # CSV 有：账本少记 → 以 CSV 为准重写
            for sym, net_qty in net.items():
                if abs(net_qty - local.get(sym, 0.0)) > 0.01:
                    _position_book.reconcile_qty(sym, net_qty)
                    drifts.append((sym, local.get(sym, 0.0), net_qty))
            # CSV 无但账本有：账本多记（疑似外部单/且回报）→ 归零（保守以 CSV 为准）
            for sym, local_qty in local.items():
                if sym not in net and abs(local_qty) > 0.01:
                    _position_book.reconcile_qty(sym, 0.0)
                    drifts.append((sym, local_qty, 0.0))
            if drifts:
                result["trades_reconciled"] = len(drifts)
                msg = "【盘后兜底】query_trades vs position_book drift " + ", ".join(
                    f"{s}({lo}→{n})" for s, lo, n in drifts)
                logger.warning(msg)
                try:
                    from infra.notifier import NotificationManager, fire_and_forget
                    fire_and_forget(NotificationManager.get_default().notify_risk_event(msg, "WARN"))
                except Exception:
                    logger.exception("盘后兜底告警推送失败（不阻塞）")
        except Exception:
            logger.exception("post_close query_trades 兜底异常（不阻塞熔断/清白名单）")

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
                            # M2（T3 fix I-1）：熔断撤单必须消费 unconfirmed 口径。
                            # 熔断是实盘最致命路径（敞口已超阈值），撤单若有未确认
                            # 终态的单，必须 critical 告警——既不与上方 :1248 的
                            # 「触发」告警重复（那一条是宣告触发，此条是追加撤单质量
                            # 口径），也绝不允许静默（与 pre_open:530 同口径双标）。
                            _cb_res = await _cancel_all_open_orders(gw)
                            if _cb_res["unconfirmed"] > 0:
                                logger.critical(
                                    "【日内熔断】撤单有 %s 笔未确认终态（发起 %s 笔）"
                                    "——敞口可能残留，必须人工复核柜台真实持仓",
                                    _cb_res["unconfirmed"], _cb_res["cancelled"],
                                )
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

    # ⑥ T11（state-store-redesign §3.2 post_close）：trade_event(CLOSED/TP1_FILLED) +
    # account_daily 收盘快照。DB 真相源收口 trade 生命周期 + 账户盈亏。
    try:
        _aid = _resolve_account_id()
        if _state_store.get_account(_aid) is None:
            _state_store.upsert_account(_aid, broker="qmt")
        _today_close = datetime.now().strftime("%Y-%m-%d")
        # 活跃 trade 盘后收口：position 归零的标 CLOSED（卖出平仓），有 TP1/TP2 FILLED 的标 TP1_FILLED
        for t in _state_store.get_active_trades(_aid):
            sym = t["symbol"]
            tid = t["trade_id"]
            pos = _state_store.get_position(_aid, sym)
            if pos is None:
                # 持仓归零 → trade 生命周期结束，标 CLOSED（realized_pnl 可后续从 fill 算，此处先标事件）
                _state_store.insert_trade_event(_aid, tid, sym, "CLOSED")
                logger.info("post_close 标 CLOSED（持仓归零）trade=%s symbol=%s", tid, sym)
        # TP1/TP2 委托 FILLED → trade_event(TP1_FILLED/TP2_FILLED, realized_pnl)
        # 查当日 FILLED 的 TP 类委托（止盈成交）
        import sqlite3 as _sqlite3
        with _state_store._connect(_state_store._DEFAULT_DB) as _con:
            _tp_filled = _con.execute(
                "SELECT trade_id, symbol, purpose, filled_qty, filled_price, qty, price"
                " FROM \"order\" WHERE account_id=? AND trade_date=? AND state='FILLED'"
                " AND purpose IN ('TP1','TP2')", (_aid, _today_close)).fetchall()
        for row in _tp_filled:
            _purpose = row["purpose"]
            _action = "TP1_FILLED" if _purpose == "TP1" else "TP2_FILLED"
            # realized_pnl = filled_qty × (filled_price - 开仓均价)；无开仓均价时 None（不猜）
            _pos_avg = None
            _tpos = _state_store.get_position(_aid, row["symbol"])
            if _tpos is not None and _tpos.get("avg_price") is not None:
                _pos_avg = float(_tpos["avg_price"])
            _pnl = (float(row["filled_qty"]) * (float(row["filled_price"]) - _pos_avg)) if _pos_avg else None
            _state_store.insert_trade_event(
                _aid, row["trade_id"], row["symbol"], _action,
                qty=float(row["filled_qty"]), price=float(row["filled_price"]),
                realized_pnl=_pnl)
    except Exception:
        logger.exception("post_close 落 trade_event(CLOSED/TP_FILLED) 失败（不阻断主流程）")

    # account_daily 收盘快照（daily_pnl = close - start）
    if gw is not None and hasattr(gw, "query_asset"):
        try:
            _aid_eq = _resolve_account_id()
            _today_eq = datetime.now().strftime("%Y-%m-%d")
            asset = await gw.query_asset()
            total = (asset or {}).get("total_asset")
            if total is not None and float(total) > 0:
                _state_store.snapshot_close_equity(
                    _aid_eq, _today_eq, float(total),
                    close_cash=(asset or {}).get("cash"),
                    close_market_value=(asset or {}).get("market_value"))
                logger.info("post_close account_daily 收盘快照 date=%s close=%s", _today_eq, total)
        except Exception:
            logger.exception("post_close snapshot_close_equity 失败（不阻断主流程）")

    # 清动态白名单（Task5 → C-2 W1 改实例属性）：保证下一交易日从干净状态开始。
    # 改造后清 engine 实例 self._dynamic_whitelist（通过 _ACTIVE_ENGINE 单例反查），
    # 而非模块级 _DYNAMIC 全局——与 pre_open 注入对称（实例属性化两端隔离）。
    try:
        if _ACTIVE_ENGINE is not None:
            _ACTIVE_ENGINE._dynamic_whitelist.clear()
        else:  # pragma: no cover - 防御性回退：未构造 TradingEngine（理论不会）
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
        # M1 健康守护 job（Task 8）：interval 60s，统一网关自愈入口。
        # Why interval：与 _stoploss 同机制（cron 最小粒度分钟，秒级周期必须 IntervalTrigger）；
        #   时段约束下放给 _health_guard 内 is_client_ready/_connected 判据——非盘中或已连
        #   时直接 no-op，全天跑零副作用。
        # Why 60s：connect 是重操作（start/connect/subscribe C++ 阻塞链），60s 周期既能在
        #   断线/启动失败后分钟级恢复 live，又不至于刷柜台（QMT connect 返 -1 session 占用
        #   会触发自扰死循环，退避表 _guard_skip_rounds 抑制）。
        # 守护逻辑见 _health_guard（就绪探测 + 互斥让出 + 退避），此处仅注册。
        self.sched.add_job(
            self._health_guard,
            IntervalTrigger(
                seconds=int(os.getenv("ENGINE_HEALTH_GUARD_INTERVAL_SECONDS", "60"))),
            id="_health_guard",
        )

        # 成交回调链路状态：
        #   _gw：交易网关引用（_order_direction 查 gw._orders[order_id].order_type 判买卖方向）。
        #        Task 11 在 gw.connect 注册 _on_order_update 回调时同步注入，本 task 仅声明槽位。
        #
        # ⚠️ 止盈幂等已迁移到 state_store.has_order(TP1) DB 查询（state-store-redesign T12）：
        #   原 _tp_placed 内存集合已废弃（重启清空→重连重推→重复挂止盈超卖 P0-1 根因）。
        #   _handle_order_update 查 DB order 表 has_order(TP1) 判定是否已挂止盈（跨重启持久）。
        self._gw: Any = None

        # M1 健康守护 job 退避状态（Task 8）：
        #   _guard_fail_count：connect 连续失败次数——失败越多退避越长（防刷柜台）。
        #   _guard_rounds_since_fail：上次失败后已过的守护轮数——达 skip 阈值才再试。
        # Why 进程内存（不持久化）：守护 job 是分钟级自愈，进程重启后从 0 开始退避无副作用
        #   （重启本身已是一次「连接重置」，无需继承历史失败计数）；持久化反增复杂度（YAGNI）。
        self._guard_fail_count: int = 0
        self._guard_rounds_since_fail: int = 0

        # 动态白名单实例属性（C-2 scheduling-orchestration W1）：
        #   当日颈线法计划标的的临时注入集合（pre_open 注入，post_close 清空）。
        # Why 实例属性而非模块级 _DYNAMIC 全局：engine 与 server 合并进同进程后，
        #   模块级全局会被 engine 注入污染 server 手动下单路径（破坏向后兼容红线）。
        #   实例属性 + submit_order(whitelist=...) 显式参数透传实现物理隔离——
        #   engine 通道读实例属性 + 静态 env，server 通道读 get_effective_whitelist()
        #   （_DYNAMIC 恒空 = 纯 env），两端输入源互不污染。
        self._dynamic_whitelist: set[str] = set()

        # 模块级活跃引擎单例置位（供模块级 _submit / pre_open / post_close 反查实例）。
        global _ACTIVE_ENGINE
        _ACTIVE_ENGINE = self

    # ---------------------------------------------------------------------
    # Task 8（C-2 S3）：pre_open 三段式前置 gate
    # ---------------------------------------------------------------------
    async def _pre_open_gate(self, date: str, gw) -> tuple[bool, str]:
        """S3：pre_open 三段式前置 gate。全绿返 ``(True, "")``，任一未绿即返。

        物理意图（spec S3 · 三段式前置 gate，最便宜先做）：
            模块级 ``pre_open(date)`` 入口最先调用本方法，**任一未绿即早返**，绝不触达
            网关写操作（撤昨日单 / 抓熔断基线 / 挂新单）。顺序「先便宜后贵」：

              ① 计划确认（读本地 JSON，最便宜）——
                 ``load_plan`` 返 None → ``"无计划"``；
                 ``plan["confirmed"]`` 假 → ``"计划未确认（人审闸）"``。
              ② 网关健康（探测，无写副作用）——
                 ``gw is None`` 或 ``gw._connected=False`` → ``"网关未连接"``；
                 ``gw.is_client_ready()=False`` → ``"miniQMT 客户端未就绪"``。
              ③ 数据就绪（DB 查询；防御性双检）——
                 遍历 ``self._plan_data_keys(plan)``，``get_data_ready(date, k)`` 返 None
                 或 ``ok!=1`` → ``f"数据 {k} 未就绪（{message}）"``。

        Args:
            date: T 日（YYYY-MM-DD，与 ``load_plan`` 读取口径一致）。
            gw:   交易网关实例（``get_gateway()`` 取，可能为 None）。鸭子类型：读
                  ``gw._connected`` 与调 ``gw.is_client_ready()``（broker/qmt.py:311 契约）。

        Returns:
            ``(True, "")`` 三段全绿；``(False, reason)`` 任一未绿，reason 为简短中文。
        """
        # ① 计划确认（读本地 JSON，最便宜）
        plan = load_plan(date)
        if not plan:
            return False, "无计划"
        if not plan.get("confirmed"):
            return False, "计划未确认（人审闸）"
        # ② 网关健康（探测，无写副作用）
        if gw is None or not getattr(gw, "_connected", False):
            return False, "网关未连接"
        if not gw.is_client_ready():
            return False, "miniQMT 客户端未就绪"
        # ③ 数据就绪（DB 查询；防御性双检）
        for k in self._plan_data_keys(plan):
            ready = get_data_ready(date, k)
            if ready is None or not ready.get("ok"):
                msg = ready["message"] if ready else "未采集"
                return False, f"数据 {k} 未就绪（{msg}）"
        return True, ""

    def _plan_data_keys(self, plan: dict) -> set[str]:
        """从 plan 反推策略声明的数据集 key 并集（③ 数据就绪段防御性双检用）。

        物理意图（spec S3 · ③ 数据就绪段的「查哪些数据集」来源）：
            plan orders 携带 ``experiment_id``，经 ``resolve_active`` 反查
            ``strategy_name`` → ``build_strategy(name, params).required_data_keys``
            （Task 2 策略接口声明的依赖数据集），取并集。解析失败（无实验 / DB 锁 /
            策略未注册）→ 返 ``{"daily"}``（保守默认，③ 本就是防御性双检，回退默认
            不会误放行未就绪数据：daily 未就绪时 gate 仍会拦）。

        Why resolve_active 而非读 plan orders 内联策略名：plan orders 只存
            ``experiment_id``（归因字段），不存 ``strategy_name``——必须经 resolver
            反查才能拿到策略名 → build_strategy。Why 不缓存：pre_open 单进程每日仅
            一次调用，零缓存一致性成本。

        Args:
            plan: ``load_plan`` 返回的 dict（含 ``orders`` 列表，每项 ``experiment_id``）。

        Returns:
            数据集 key 并集（如 ``{"daily"}`` 或 ``{"daily", "moneyflow"}``）；
            解析失败/空 orders → ``{"daily"}``。
        """
        keys: set[str] = set()
        try:
            from experiment.resolver import resolve_active
            from strategies.registry import build_strategy
            # ActiveExperiment 字段名是 experiment_id（非 .id，见 experiment/models.py:55）
            exp_map = {e.experiment_id: e for e in resolve_active()}
            for o in plan.get("orders", []):
                exp = exp_map.get(o.get("experiment_id"))
                if exp is not None:
                    strat = build_strategy(exp.strategy_name, exp.params)
                    keys |= set(strat.required_data_keys)
        except Exception:
            logger.exception("_plan_data_keys 解析失败，回退默认 {daily}")
        return keys or {"daily"}

    async def bootstrap(self) -> None:
        """W3：I/O 初始化收口（原 ``__main__._run_forever`` 的 7 步）。

        三段分离（构造 → bootstrap → start）：
            - ``__init__``：构造 AsyncIOScheduler + 注册四 cron job（零 I/O，可安全在
              server lifespan 内构造）。
            - ``bootstrap``（本方法）：连接网关 + 注册成交回报回调 + position_book/state_store
              建表迁移（I/O init，必须在 start() 之前）。
            - ``start``：启 scheduler（调度启动；cron 一旦启动，触发点可能读写 DB/网关）。

        Why 必须在 start() 之前：cron 一旦启动，下一个触发点（如 stop_loss_monitor /
            _handle_order_update）可能读 position_book/state_store / 调 gw 回调链路，
            建表与回调注册必须先就绪，否则首触发点会崩。

        Why 从 ``__main__._run_forever`` 提取：让独立 ``python -m trading`` 与 uvicorn
            server lifespan 复用同一段 I/O 初始化（W3 收口），避免两处复制漂移。

        物理意图（保留原 ``__main__`` 注释，行为完全等价 · W3 不改启动语义）：
            - 网关 connect + set_order_update_callback（修 G5：成交回报回流链路就绪）。
            - 异常兜底不抛：连接失败时仍让 cron 起来——触发点内部 get_gateway() 会再次
              惰性取单例做兜底判空（None 时走 dry_run 分支），这里只打 exception 不阻断。
            - position_book.init_db / state_store.init_store / state_store._migrate_env_to_account
              建表 + 从 .env 落 account 行（state-store-redesign T13）。
        """
        gw = get_gateway()
        if gw is not None:
            try:
                await gw.connect()  # async：内部 run_in_executor 包 xtquant C++ 阻塞 connect
                gw.set_order_update_callback(self._handle_order_update)  # sync 注入成交回报回调
                self._gw = gw  # 供 handler 反查 _orders 判 BUY/SELL side（见 engine._side_from_update）
                logger.info("网关已连接 + 成交回调已注册")
            except Exception:
                logger.exception("网关连接失败（cron 仍启动，触发点内部 get_gateway 兜底）")
        else:
            logger.warning("未装配网关（AUTO_TRADE_MODE=dry_run 影子模式，回调链路不生效）")

        # 初始化本地持仓账本（gap4 · 幂等建表，对齐 experiment/store.init_db 范式）。
        # 必须在 start() 之前：cron 一旦启动，_handle_order_update/_post_close 就可能
        # 读写账本，建表必须先就绪。
        # state_store 统一交易状态库（state-store-redesign T13）：
        # init_store 建 6 张表（account/trade_event/order/fill/position/account_daily）+
        # _migrate_env_to_account 从 .env 读 QMT_* 配置写入 account 表（多账户扩展基础）。
        # 必须在 start() 之前：eod_plan/pre_open/_handle_order_update 全查 state_store。
        # 函数局部 import（W3）：patch 源模块路径（trading.position_book.init_db 等）可覆盖。
        from trading import position_book, state_store
        position_book.init_db()
        state_store.init_store()
        state_store._migrate_env_to_account()

    async def _health_guard(self) -> None:
        """M1：网关健康守护——未连接时探测客户端就绪→重连，恢复 live（Task 8）。

        物理定位（spec M1 · 统一网关自愈入口）：
            apscheduler interval job 每 60s 跑一次（``__init__`` 内 add_job 注册，
            id="_health_guard"）。覆盖两条断线恢复路径的统一收口：
              ① 启动时 connect 失败（客户端未起 / session 占用 / 环境类故障）；
              ② 盘中断线（on_disconnected→_reconnect 耗尽 backoffs 仍未恢复）。
            无此守护 job 时，任一路径失败都会让网关全天锁死 _lock_down=True，
            pre_open/stop_loss 全线拒单（行情/下单/对账均不可用）→ 实盘当天废单。

        逻辑（顺序不可调，前置条件逐级过滤）：
          ① gw is None（网关未装配，如 dry_run / 未配凭证）→ no-op；
          ② 已连接 _connected=True → 清失败计数 + no-op（不捣乱活跃连接）；
          ③ _reconnecting=True → 让出（on_disconnected 路径正在重连，避免并发抢连
             同一 sid 触发 QMT -1 自扰）；
          ④ is_client_ready=False（miniQMT 客户端未起 / userdata shm 文件 mtime 过期）
             → 跳过（connect 必失败，空跑只会刷柜台日志 + 撞限流）；
          ⑤ 退避：连续失败越多跳过越多轮次（等效指数退避，不改 apscheduler 调度）；
          ⑥ 调 connect()——成功清计数恢复 live，失败累加计数等下轮退避。

        ⚠️ 与 _reconnect 的边界（两条重连路径的分工）：
            _reconnect（broker/qmt.py）：on_disconnected 触发的即时重连，带固定
                backoff 序列（_RECONNECT_BACKOFFS），失败 N 次后判「真断线」留 lock_down。
            _health_guard（本方法）：常驻周期守护，在 _reconnect 耗尽后的长时间窗口
                持续探测客户端就绪→重连（客户端可能在 _reconnect 耗尽后才重启完成）。
            互斥靠 gw._reconnecting——_reconnect 持锁时本方法 ③ 让出。
        """
        gw = get_gateway()
        if gw is None:
            # 网关未装配（dry_run / 未配 QMT 凭证）→ 无可守护对象，直接返回
            return
        # ② 已连接 → 清失败计数 + no-op（活跃连接不能被周期 job 重连打断：
        #    重连会断开活跃 session 重建，导致回报回调丢失 / 主推重新订阅抖动）
        if getattr(gw, "_connected", False):
            self._guard_fail_count = 0
            self._guard_rounds_since_fail = 0
            return
        # ③ 互斥让出：on_disconnected→_reconnect 正在进行（_reconnecting=True），
        #    并发重连会同时 start/connect 同一 sid → QMT 返回 -1（session 占用）→ 自扰死循环。
        if getattr(gw, "_reconnecting", False):
            return
        # ④ 客户端未就绪 → 不空跑 connect（防刷柜台）。
        #    is_client_ready 是纯文件 mtime 探测（T6）：False 意味 miniQMT 客户端进程
        #    未起 / userdata 共享内存文件老旧 → connect 必返 -1 或超时，空跑无意义。
        if not gw.is_client_ready():
            return
        # ⑤ 退避：失败次数→应跳过轮数（等效指数退避，不改 apscheduler 60s 调度）。
        #   skip=0 立即试，skip>0 时累加 _guard_rounds_since_fail 直到达阈值再试一次。
        skip = self._guard_skip_rounds(self._guard_fail_count)
        if self._guard_rounds_since_fail < skip:
            self._guard_rounds_since_fail += 1
            return
        # ⑥ 调 connect——成功清计数恢复 live，失败累加计数（下轮按新 fail_count 退避）
        try:
            await gw.connect()
            prev_fails = self._guard_fail_count
            self._guard_fail_count = 0
            self._guard_rounds_since_fail = 0
            logger.warning(
                "【health_guard 重连成功】网关恢复 live（前累计失败 %s 次），"
                "pre_open/stop_loss 链路恢复可用", prev_fails)
        except Exception as exc:
            self._guard_fail_count += 1
            self._guard_rounds_since_fail = 0  # 失败后从 0 重新累计等待轮数
            next_skip = self._guard_skip_rounds(self._guard_fail_count)
            logger.warning(
                "health_guard 重连失败（第 %s 次，下次退避 %s 轮 ≈%ss）：%s",
                self._guard_fail_count, next_skip, next_skip * 60, exc)
            # Task 9（M4 静默漏单消灭）：连续失败超阈值 → 钉钉 CRITICAL（网关持续锁死需人工介入）。
            # Why % 10 节流：每 10 次失败推一条（避免高频告警风暴）；connect -1 的即时告警
            # 已在 broker/qmt.py _reconnect（T7）处理，本守护是长周期兜底，不重复推。
            # 物理阈值：fail_count=10 意味 connect 已连续失败 10 轮（含退避至少数十分钟），
            # 自愈无望，必须人工重启 miniQMT 客户端 / 排查 session 占用。
            if self._guard_fail_count % 10 == 0 and self._guard_fail_count > 0:
                _alert_critical(
                    f"health_guard 重连累计失败 {self._guard_fail_count} 次，"
                    f"网关持续锁死（_reconnect 已耗尽 backoffs 仍未恢复），"
                    f"请人工介入：检查 miniQMT 客户端是否启动 / session 是否被占用 / "
                    f"userdata shm 文件是否过期")

    @staticmethod
    def _guard_skip_rounds(fail_count: int) -> int:
        """失败次数→跳过轮数（指数退避近似，60s/轮）。

        映射：0→0, 1→0, 2→1, 3→3, ≥4→7。
        物理意图：connect 连续失败（柜台持续不可用）时空跑无意义，按失败次数拉长间隔
        （60→120→240→480s），等效指数退避但不引入 apscheduler reschedule 复杂度
        （只在本方法内累加计数）。
        上限 7 轮 ≈ 8min：再长则恢复延迟过大（盘中断线 8min 不恢复 = 实盘敞口失控）。
        """
        if fail_count < 2:
            return 0
        if fail_count == 2:
            return 1
        if fail_count == 3:
            return 3
        return 7  # 上限≈8min（60s×8 含本轮）

    def _sanity_check_date_alignment(self, today: str | None = None) -> bool:
        """M3：启动口径自检——确认 _eod 落盘用 next_trading_day、_pre_open 读 today。

        Why（[[eod-date-offbyone-fix]] 主动防线）：
            代码口径已修（_eod 传 next_trading_day(today) 落盘、_pre_open 次日读 today），
            但若进程跑的是未重启的旧代码（口径退回 today 落盘 → 次日读 T+1 永远差一天），
            会直接导致「标的错位 + 永不挂单」——这是已知的静默致命 bug，进程级口径不可见。
            本自检在 start() 注册 cron 前主动验证 ``calendar.next_trading_day(today) != today``
            （即确实算出了次日而非原样返回），否则视为口径坏，让调用方降级（dry_run + 告警）。

        Args:
            today: YYYY-MM-DD；None 时取 datetime.now() 当日（启动自检默认当日）。

        Returns:
            True  = 口径正常（next_trading_day 算出次日，落盘 key 与次日读 today 对齐）；
            False = 口径异常（next_trading_day 返 today 自身/空值/抛异常 → 疑似跑旧代码，
                    调用方 start() 须 logger.error 告警，CRITICAL 钉钉接线留 T9）。
        """
        _today = today or datetime.now().strftime("%Y-%m-%d")
        try:
            nxt = calendar.next_trading_day(_today)
        except Exception as exc:
            # next_trading_day 抛异常（日历源故障/网络异常）同样判口径坏（保守拒进 live）
            logger.exception("【口径自检失败】next_trading_day(%s) 抛异常，判口径坏：%s", _today, exc)
            return False
        if not nxt or nxt == _today:
            # 旧 bug 口径：next_trading_day 原样返回 today（或空值）→ 落盘 key=今日，
            # 次日 pre_open 读 today 永远差一天 → 标的错位 + 永不挂单（静默致命）。
            logger.error(
                "【口径自检失败】next_trading_day(%s)=%s 未算出次日（疑似跑旧代码口径），"
                "拒绝进 live（降级 dry_run，CRITICAL 钉钉告警待 T9 接入）", _today, nxt)
            return False
        logger.info("口径自检通过：eod 落盘 key=%s，pre_open 次日读 today 与之对齐", nxt)
        return True

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
        # Task 7 U5：完整性 gate 上提到 _eod（filter 复用 data.integrity 单源）
        from data.integrity import filter_universe_by_continuity

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

        # ── Task 7 U5 gate 下沉：完整性 gate 从 scan_live 上提到 _eod（universe 级 pre-filter）─
        # 物理意图（300214.SZ 漏采教训 · memory data-lake-integrity-gap）：lake 缺停牌复牌段
        # 时残缺数据误判颈线突破产误信号。原 gate 内联在 scan_live（per-symbol 自验窗口），
        # 现上提到 data/integrity.filter_universe_by_continuity——策略层 scan_live 假设已过滤，
        # 回测/实盘共用同一 filter（数据校验单源）。逻辑零改动于原 scan_live:228-235 的
        # per-symbol gate（同一 check_window_continuity，只从 per-symbol 上提到 universe 级）。
        #
        # 一次性加载全 universe 的 df_upto（df_map：sym → df_upto），后续 scan_live 复用——
        # df_upto 复用红线（与 Task 7b lake 复用同源）：避免 filter 与 scan_live 各 _load_df_upto
        # 一次（double load）。filter 在 df_map 上跑（per-experiment window 可能不同，故 df_map
        # 在 exp 循环外构建一次）。
        df_map: dict = {}
        for sym in universe:
            df_upto = _load_df_upto(lake, sym, today)
            if df_upto is not None and len(df_upto) >= 60:
                # 历史不足（<60 行）不进 df_map（与原 _eod 内联 <60 跳过同口径）
                df_map[sym] = df_upto

        # 加载停牌区间 + 近 2 年 trade_days（逻辑从 scan_live 原 _ensure_integrity_cache 搬，
        # fail-open 同口径：加载失败返 ({}, set()) 让 filter 放行——trade_days 空集 →
        # check_window_continuity 的 expected 恒空 → ok=True 全放行，退回原行为）。
        susp, trade_days = _load_integrity_ctx(today)

        signals: list = []
        atr_map: dict = {}
        # 逐实验 × 逐 symbol 扫信号；单 symbol scan_live 异常仅 warn 跳过，不炸整批
        for exp in experiments:
            strategy = build_strategy(exp.strategy_name, cfg_override=exp.params)
            # filter universe（per-experiment window 可能不同，故在 exp 循环内 filter）。
            # 颈线法 id_cfg["window"] 对齐原 scan_live:232 的 df_upto.tail(self.id_cfg["window"])。
            id_window = _resolve_id_window(strategy)
            clean_universe = filter_universe_by_continuity(
                list(df_map.keys()), df_map, id_window, susp, trade_days)
            for sym in clean_universe:
                df_upto = df_map[sym]   # df_upto 复用（不重复 _load_df_upto）
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
        # ── Task 9（U6 执行统一）：构造 monitor_ctx（state+cfg）+ pending_ctx（cancel_on）+ stop_prices（D12 fallback）──
        # 三 map 均从同一张 confirmed 计划 orders 派生（单源一致）：
        #   - stop_prices：{sym: stop_price}（D12 fallback 基准，decide_exit 异常时兜底比价）；
        #   - monitor_ctx：{sym: {state, cfg}}（主路径 decide_exit 输入）；
        #   - pending_ctx：{sym: cancel_on}（D11 pending 期撤单阈值）。
        stop_prices: dict[str, float] = {}
        monitor_ctx: dict[str, dict] = {}
        pending_ctx: dict[str, float] = {}
        # 仅在 confirmed 计划下抽取（confirmed=False 是人审闸——研究员未确认就不监控止损，
        # 避免研究员明确否决的计划仍触发卖出，破坏人审语义）。
        if plan and plan.get("confirmed"):
            # entry_dates / avg_prices 来自 position_book（持仓账本，holding_days + entry 基准）
            try:
                entry_dates = _position_book.get_entry_dates()
            except Exception:
                # 账本读失败软降级 → holding_days 全 0（等同 base_stop，不崩）
                logger.warning("_stoploss 读 entry_dates 失败（holding_days 降级 0）", exc_info=True)
                entry_dates = {}
            cfg_trade = _trade_cfg()
            # decide_exit 静态 cfg（整个持有期不变 · resolution 7）：键对齐 simulate_exit cfg
            # （backtest.py:177-183）+ decide_exit 契约（execution.py:155-161）。
            decide_cfg = {
                "stop_atr_mult": cfg_trade["stop_atr_mult"],
                "trailing_grace": cfg_trade.get("grace", 0) or 0,
                "trailing_step": cfg_trade.get("step", 0.0) or 0.0,
                "trailing_floor": cfg_trade.get("floor"),
                "tp1_portion": cfg_trade.get("tp1_portion", 0.5),
                "max_holding": cfg_trade.get("max_holding", 15),
            }
            max_holding = decide_cfg["max_holding"]
            for o in plan.get("orders", []):
                sym = (o.get("order") or {}).get("symbol")
                sp = o.get("stop_price")
                # 双重防御：symbol 缺失或 stop_price 非数（NaN/None）一律跳过——
                # stop_prices 的每一项都必须是「能拿来比价」的合法 (sym, price) 对。
                if not sym:
                    continue
                if sp is not None:
                    stop_prices[sym] = sp

                # ── 构造 monitor_ctx[sym]（主路径 decide_exit · resolution 3）──
                # state 字段对齐 decide_exit 契约（execution.py:142-153）：
                #   phase=holding（实盘 monitor 是 holding 期巡检）；entry/stop/tp1/tp2/neckline/
                #   atr from plan orders；holding_days from position_book.entry_date；is_last =
                #   (holding_days >= max_holding)（resolution 6：超期即当末根，不判浮盈 threshold）；
                #   lot1_open/lot2_open 默认 True（实盘单仓，_place_take_profit 限价单成交翻 lot，
                #   monitor 不维护 lot 翻转——对齐 simulate_exit 的单根无状态语义）。
                neckline = o.get("neckline")
                atr = o.get("atr")
                tp1 = o.get("tp1")
                tp2 = o.get("take_profit")
                # 缺 neckline/atr/tp2 的 order（老 plan 无这些字段）→ 无法构造 decide_exit
                # state（compute_stop_price 要 neckline+atr，holding 期 tp2 必填）→ 只塞
                # stop_prices 走 fallback，不塞 monitor_ctx（保守，不拿脏 state 喂 decide_exit）。
                if (neckline is not None and atr is not None and tp2 is not None
                        and tp1 is not None):
                    entry_date = entry_dates.get(sym)
                    # holding_days 交易日口径（trading_days_between，与 _scan_expired_positions 同源）；
                    # entry_date 缺失（未成交 / 老数据）→ holding_days=0（等同 base_stop，向后兼容）。
                    holding_days = _trading_days_between(entry_date, today) if entry_date else 0
                    # I-4：is_last 用 `>=`（第 max_holding 日即当末根 → decide_exit TIMEOUT
                    # 市价优先强平），对齐回测 simulate_exit is_last 语义。与 _scan_expired_positions
                    # 的 `>`（第 max_holding+1 日跌停价兜底）错位一日——monitor 先市价平，
                    # post_close 次日兜底仅处理 monitor 漏掉的标的（防同日双卖卖空）。
                    is_last = holding_days >= max_holding
                    monitor_ctx[sym] = {
                        "state": {
                            "phase": "holding",
                            "entry": None,     # 实盘 entry 不入 state（decide_exit holding 分支不读 entry，
                                               # pnl 在 simulate_exit 算；monitor 只决策发不发单）
                            "stop": float(sp) if sp is not None else None,  # D12 fallback 观测用
                            "tp1": float(tp1), "tp2": float(tp2),
                            "neckline": float(neckline), "atr": float(atr),
                            "holding_days": holding_days, "is_last": is_last,
                            "lot1_open": True, "lot2_open": True,
                        },
                        "cfg": dict(decide_cfg),
                    }

                # ── 构造 pending_ctx[sym]（D11 pending 期撤单 · resolution 4）──
                # cancel_on = 颈线 + cancel_thresh_mult×H（build_orders 落盘，对齐 simulate_exit:128）。
                # plan orders 里 cancel_on 字段（Task 9 新增落盘，见 _eod order_dicts）；
                # 老 plan 无此字段 → 不塞 pending_ctx（None=不撤单放飞，向后兼容）。
                cancel_on = o.get("cancel_on")
                if cancel_on is not None:
                    pending_ctx[sym] = float(cancel_on)

        # 空时显式转 None：与 stop_loss_monitor 的「xxx is None or empty → no-op」契约对齐。
        await stop_loss_monitor(
            stop_prices=stop_prices or None,
            monitor_ctx=monitor_ctx or None,
            pending_ctx=pending_ctx or None,
        )

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

        幂等红线（state_store.has_order(TP1)）：
            on_stock_trade 在部分成交 / 柜台重推时会多次推送同一 order_id 的 trade 回报。
            若每次都重挂止盈卖单 → 同笔持仓挂 N 张卖单 → 超卖敞口致命。故查 DB order 表
            has_order(TP1)，已挂即跳过（跨重启持久，state-store-redesign T12 替代原 _tp_placed 内存）。

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

        # c. 买单成交 + 未挂止盈 → 挂限价止盈卖单（DB 幂等防重挂）
        #    卖单成交（direction=="SELL"）无需挂止盈（卖出即离场，无持仓可止盈）。
        #    方向未知（None）保守不挂——宁可漏挂止盈让人工补，也不误把卖单当买单挂反方向单。
        #
        # 幂等闸（state-store-redesign §4.2，P0-1 止盈超卖根因修复）：
        #   DB has_order(TP1)：查 state_store.order 表是否已挂 TP1（跨重启持久）。
        #   T12 已废弃 _tp_placed 内存态（重启清空→重连重推→重复挂止盈超卖），DB 为唯一真相源。
        #   _place_take_profit 内 _record_tp 落 insert_order(TP1/TP2)（UNIQUE 幂等），双重保护。
        today_tp = datetime.now().strftime("%Y-%m-%d")
        _account_id = _resolve_account_id()
        _trade_id = f"{_account_id}_{symbol}_{today_tp}"
        _tp_already = False
        try:
            _tp_already = _state_store.has_order(_account_id, today_tp, symbol, "TP1")
        except Exception:
            logger.exception("查 DB has_order(TP1) 失败 symbol=%s（保守跳过，防重复挂）", symbol)
            _tp_already = True  # DB 查询失败保守视为已挂（宁可漏挂人工补，不超卖）
        if direction == "BUY" and not _tp_already:
            try:
                await self._place_take_profit(symbol, qty, price, order_id)
            except Exception:
                # 止盈挂单失败（被风控挡板拒/网关断线）不抛——人工补挂（告警已记日志）。
                logger.exception("挂止盈失败 symbol=%s（需人工补挂）", symbol)

        # d. 成交账本写入（gap4 · post_close 对账数据源）。
        #    独立 try-except 软降级：账本写入失败不阻断 a 日志/b 通知/c 止盈。
        #    方向 None 不写（保守，对齐 c 连不挂止盈语义——不猜方向误记为买当卖/卖当买，
        #    账本失真比对账漏记更危险）。
        #    state-store-redesign §4.2：state_store 是真相源——insert_fill（增量幂等）+
        #    apply_fill_to_position（加权 avg）+ insert_trade_event(FILLED)。
        #    不再调 position_book.apply_fill（避免与 state_store 双写 fill 表致 insert_fill
        #    恒返 False、position 用错 account_id）。position_book 读函数（get_local_positions
        #    等）仍读同一张 position 表，向后兼容。
        if direction in ("BUY", "SELL"):
            try:
                # 确保 account 行存在（fill/trade_event FK 引用 account）
                if _state_store.get_account(_account_id) is None:
                    _state_store.upsert_account(_account_id, broker="qmt")
                traded_time = str(update.get("traded_time", ""))
                if _state_store.insert_fill(
                        order_id, _account_id, traded_time, symbol, direction,
                        float(qty), float(price)):
                    # insert_fill 首次入账才更新 position（避免重推重复累加）
                    _state_store.apply_fill_to_position(
                        _account_id, symbol, direction, float(qty), float(price), traded_time)
                # FILLED 事件（幂等：同 trade 同 action 跳过）
                _state_store.insert_trade_event(
                    _account_id, _trade_id, symbol, "FILLED",
                    order_id=order_id, qty=float(qty), price=float(price))
            except Exception:
                logger.exception("state_store fill/position/FILLED 写入失败 symbol=%s（软降级）", symbol)

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

        幂等（state_store DB 双闸）：
            **has_order(TP1) 在调度点 ``_handle_order_update`` 完成**（查 DB 是否已挂 TP1），
            本方法挂单成功后 ``_record_tp`` 落 insert_order(TP1/TP2)（UNIQUE 幂等，单一写入点）。
            双闸防 await 期间部分成交重推重入重挂：调度点查 + 写入点 UNIQUE 冲突兜底。

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

        # T9（state-store-redesign §4.2）：止盈挂单落 DB（has_order(TP1/TP2) 幂等真相源）。
        # insert_order UNIQUE(account_id, trade_date, symbol, purpose)：重推/重启重挂 → 跳过。
        # 物理意图：替代 _tp_placed 内存（跨重启持久，防 P0-1 止盈超卖）。
        _aid = _resolve_account_id()
        _tid = f"{_aid}_{symbol}_{today}"

        def _record_tp(purpose: str, qty: int, price: float) -> None:
            """挂止盈单后落 DB order（幂等，重挂跳过）。失败仅 log 不阻断挂单。"""
            try:
                # 确保 account 行存在（insert_order FK 引用 account）
                if _state_store.get_account(_aid) is None:
                    _state_store.upsert_account(_aid, broker="qmt")
                oid = f"{today}_{symbol}_{purpose}_1"
                _state_store.insert_order(
                    oid, _tid, _aid, today, symbol, "sell", purpose,
                    float(qty), float(price), state="SUBMITTED")
            except Exception:
                logger.exception("insert_order(%s) 失败 symbol=%s（不阻断挂单）", purpose, symbol)

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
                _record_tp("TP2", filled_int, tp2)
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
                _record_tp("TP2", filled_int, tp2)
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
        if r1.get("state") not in ("REJECTED", "FAILED"):
            _record_tp("TP1", tp1_qty, tp1)
        # tp2 leg（形态目标位，仅当有余量时挂）
        if tp2_qty > 0:
            r2 = await _submit(
                OrderRequest(symbol=symbol, qty=tp2_qty, side="sell", price=tp2),
                confirm=True,
            )
            results.append(("tp2", tp2, tp2_qty, r2))
            if r2.get("state") not in ("REJECTED", "FAILED"):
                _record_tp("TP2", tp2_qty, tp2)
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
        """启动 scheduler（阻塞主线程进入事件循环由 ``__main__`` 负责）。

        M3 口径自检（Task 5 · 注册 cron 前）：
            调 ``_sanity_check_date_alignment`` 主动校验 next_trading_day 口径正常。
            - 通过 → 继续 register cron（正常进调度）；
            - 失败 → ``logger.error`` 告警（CRITICAL 钉钉接线是 T9 的事，本任务不调
              ``_alert_critical`` 避免前置依赖 T9），但仍让 cron 起（降级语义由 T9/M4
              强化告警定，本任务先打 error 不阻断调度——避免 T9 未合入时启动直接卡死）。
        """
        if not self._sanity_check_date_alignment():
            # 口径坏：疑似跑旧代码（next_trading_day 退回返 today 自身）。
            # 本任务只 logger.error 告警（_sanity_check 内已打），不阻断 cron 调度；
            # live 模式下若真跑旧代码会持续标的错位，由 T9 CRITICAL 钉钉 + 人工介入兜底。
            logger.error(
                "【M3 口径自检未过】TradingEngine 以降级模式启动（mode=%s）——"
                "疑似 next_trading_day 口径坏，进 live 前必须人工核查代码版本/进程重启状态",
                _mode())
            # Task 9（M4）：口径自检失败补钉钉 CRITICAL（原 T5 仅 logger.error，钉钉不推）。
            # ⚠️ 不改 T5「仅告警不阻断」行为（cron 照起，硬阻断升级是 R4 follow-up）。
            # 降级运行是真隐患（旧代码口径会让标的错位 + 永不挂单），必须叫醒人工。
            _alert_critical(
                "口径自检失败：next_trading_day 未算出次日（疑似跑旧代码），"
                "已降级启动，请立即重启 engine 加载新代码并核查 next_trading_day 口径")
        self.sched.start()
        logger.warning("TradingEngine 已启动（mode=%s）——独立常驻进程运行", _mode())

    def shutdown(self) -> None:
        """优雅停机（wait=False：不等 pending job，进程退出场景）。"""
        self.sched.shutdown(wait=False)
        logger.info("TradingEngine 已停机")
