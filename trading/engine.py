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
  post_close 15:30 盘后：对账（run_reconcile）+ 日内熔断判定（Task 10·R-2：读 pre_open 快照
              start_equity → check_daily_loss_limit → 触发即 cancel_all + emergency_halt）
              + trailing 盘后演进（Task 9·R-3：_evolve_trailing_stops 按 holding_days 重算次日
              stop 写回 plan）+ 清动态白名单。

============================================================================
⚠️ 不变量（Task5 M2 风险官要求 · 绝对红线 · W1 已重构为进程内隔离）
============================================================================
本引擎**运行在 uvicorn server 进程的 lifespan 内**（C-2 scheduling-orchestration Task 5/W1
重构后的既定架构——engine 与 server 合并进同进程，``presentation/server/main.py`` 的 lifespan
构造 TradingEngine 并起 APScheduler）。历史「必须独立进程、绝不可嵌入 server」的红线
已由 W1 的**实例属性隔离机制**替代：不再依赖进程隔离来防前视污染。

W1 隔离机制（取代旧的进程隔离硬约束）：
- 动态白名单从模块级全局 ``_DYNAMIC`` 改为 engine **实例属性** ``_dynamic_whitelist``
  （注入/清空/拼白名单全部走实例属性，不再 mutate 模块级全局）。
- 模块级 ``_ACTIVE_ENGINE`` 单例（仅 engine 路径构造 TradingEngine 时置位）反查实例，
  ``_submit`` 从该实例取 ``_dynamic_whitelist``，与静态 env 白名单合并后**显式透传**
  给 ``trading_service.submit_order`` 的 ``whitelist`` 参数。
- server 路径的 ``submit_order`` **不传** ``whitelist``（默认 None），``_submit`` 见
  ``_ACTIVE_ENGINE`` 为 None 即走旧路径 ``_whitelist() = get_effective_whitelist()``
  （``_dynamic_whitelist`` 恒空 = 纯 env）——server 行为与改造前完全一致（向后兼容红线）。
- 因此 engine 与 server 同进程**不再**造成前视污染：server 手动下单路径不会读到 engine
  注入的动态白名单（实例属性隔离 + whitelist 参数显式透传双保险）。

⚠️ ``python -m trading``（``trading/__main__.py``）现仅为**开发/调试常驻入口**，不再是
唯一入口；生产路径在 uvicorn lifespan 内起 engine（详见 ``start_all.py``）。

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
import time
from dataclasses import replace as _dc_replace
from datetime import datetime
from typing import Any, Mapping, Optional

from trading import (
    calendar,
    dynamic_whitelist,
    job_ledger,
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
from trading.state_store import get_data_ready, get_ready
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
# Task 10（R-2）：position_book 持仓账本（apply_fill/持仓读口）。
# 注（B5 收口）：daily_equity reader/writer（snapshot_start_equity/get_start_equity）
# 已删——熔断基线唯一读口 = _state_store.snapshot/get_start_equity(account_daily)。
from trading import position_book as _position_book
# state-store-redesign：统一交易状态库（6 表，trade_event/order/fill/position/account_daily）。
# 是 position_book 的超集（真相源）。engine 落 SIGNAL/CONFIRMED 事件、写 order/fill 幂等、读
# stop_price/has_order 走 DB（替代 plan JSON 单一依赖 + _tp_placed 内存）。
from trading import state_store as _state_store
# C-6 V2：单一时间源口子（替代散落 18 处 datetime.now）。三函数分工：
#   clock.today()       = 业务日期 key（load_plan/save_plan/is_trading_day/熔断基线 date）
#   clock.trading_day() = eod 落盘 key（=next_trading_day(today)，eod 专用，禁混用）
#   clock.now()         = 事件时间戳（submitted_at/written_at/is_intraday_session 时点）
# 触发点入口缓存（_eod/_pre_open/_stoploss/_post_close 入口算一次）防同轮跨午夜漂移。
from trading import clock
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


# C-4 U2：L1 致命异常 + 停调度 wrapper。
class _CriticalHalt(Exception):
    """L1 致命异常：交易关键路径失败（DB 写/读失真·网关断线·整批失败·敞口未明）。

    物理意图（spec §3 L1 + review 补强边界）：
        抛出本异常 = 「继续跑会致真金损失或状态真相源失真」，_critical_guard 捕获后调
        _halt() 停所有 job。与单只业务拒单（RuntimeError，L2 聚合 CRITICAL 不停）区分。

    边界判定线（review 补强 · 基础设施 > 单只计数）：
        - DB 写异常（insert_order/update_order_state/insert_trade_event/insert_fill 抛错）= L1
          （哪怕只挂一只，DB 真相源失真优先于「单只」语义，硬抛）；
        - 单只 _submit RuntimeError（业务拒单：涨跌停/资金不足/限频）= L2（不抛本异常）。
    """


def _critical_guard(coro_method):
    """L1 路径 wrapper：_halted 检查 + 捕获 _CriticalHalt → _halt 停调度。

    in-flight 语义（review 补强）：
        - 当前 job：raise _CriticalHalt → 异常向上传播，当前 job 在 raise 处立即中断
          后续写（不会 continue 把半截状态写完）；本 wrapper except 捕获后 _halt + 再 raise
          （APScheduler 顶层吞 job 异常记日志，不影响其他 job）。
        - 其他 job / 下一轮：_halted flag 在本 wrapper 顶 if 兜底——max_instance=1 下，
          被触发或堆积补跑的 job 入口即跳过，不再写。
        即 raise 中断「当前轮」，_halted 防「下一轮/其他 job」，覆盖 in-flight 全部窗口。
    """
    import functools
    @functools.wraps(coro_method)
    async def wrapped(self, *a, **kw):
        if getattr(self, "_halted", False):
            logger.warning("引擎已停调度（_halted），跳过 %s", coro_method.__name__)
            return
        try:
            return await coro_method(self, *a, **kw)
        except _CriticalHalt as e:
            self._halt(f"[{coro_method.__name__}] {e}")
            raise   # 再抛：APScheduler 顶层记 job 异常日志；_halt 已生效
    return wrapped


# ============================================================================
# 环境读取辅助
# ============================================================================

# APScheduler 3.x CronTrigger 工作日语义：day_of_week **0=周一**（非标准 cron 0=周日）。
# 2026-08-03（周一）实证：``"0 18 * * 1-5"`` 周一不触发（下一次是周二），pipeline
# 事件链断链、data_pipeline.log 无当日采集。修复：一律用名字 ``mon-fri``，
# 测试 tests/test_workday_cron.py 钉死语义防回归。.env 里的 ENGINE_*_CRON 覆盖值
# 同样必须用 mon-fri（2026-08-03 已同步修正 .env）。
PIPELINE_CRON_DEFAULT = "0 18 * * mon-fri"    # 盘后事件链（采集→校验→eod→brief）
PRE_OPEN_CRON_DEFAULT = "22 9 * * mon-fri"    # 开盘挂单闸
POST_CLOSE_CRON_DEFAULT = "30 15 * * mon-fri" # 盘后对账/熔断/trailing


# 模块级活跃引擎单例（C-2 scheduling-orchestration W1）：
#   __init__ 末尾置位 self，供模块级 _submit / pre_open / post_close 反查实例的
#   ``_dynamic_whitelist`` 属性（engine 与 server 合并进同进程后的物理隔离机制——
#   注入/清空/拼白名单全部走实例属性，不再 mutate 模块级 _DYNAMIC 全局）。
#   ⚠️ 仅 engine 独立进程（python -m trading）内会构造 TradingEngine（task brief 红线），
#   故该单例在 server 进程内恒为 None——server 路径 submit_order 不传 whitelist，
#   _submit 见 None 即走旧路径（_whitelist() = 纯 env），向后兼容不变。
_ACTIVE_ENGINE: "TradingEngine | None" = None

# R2 降级告警节流：行情源整体失效（xtdata 黑屏）→ live CRITICAL（30min 至多一条）。
_last_quote_blackout_alert_ts: float = 0.0
_QUOTE_BLACKOUT_ALERT_INTERVAL_S = 30 * 60


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
        # 海龟 trailing 动态止损参数（Task 9/10 已落地）：grace/step/floor 三件套由
        # _evolve_trailing_stops 在 post_close 步骤④盘后消费——按 holding_days 重算次日
        # stop_price 写回 plan（holding_days<=grace 用 base_stop 给趋势确认空间，超 grace
        # 每日收紧 step×ATR，floor 卡底）。盘中 stop_loss_monitor 用演进后的静态 stop_prices，
        # 不盘中追踪 ATR high（live-readiness spec §2 决策：R-3 选盘后演进，否决盘中跟踪）。
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
        today:     YYYY-MM-DD（_eod 调用时传 clock.today()）。

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
    # state-store-redesign §3.3：plan 双写——DB 落 trade_event(SIGNAL, meta=计划参数) 作真相源，
    # JSON 保留给人看/veto CLI/钉钉。SIGNAL 幂等（UNIQUE account_id+trade_id+action）：重跑 eod_plan
    # 已存在则跳过（不重复记）。account_id 缺省走默认账户（_migrate_env_to_account 在启动期落真实账户）。
    #
    # W2 顺序约束（不可调）：SIGNAL DB 必须在 confirm_plan 之前写。物理：confirm_plan
    # （W2 改造）会写 DB CONFIRMED，eod_plan auto 路径在 SIGNAL 之后也写 CONFIRMED（veto 保护
    # 复检）。若 confirm_plan 先于 SIGNAL，CONFIRMED 的 event_id < SIGNAL，get_latest_action
    # 返 SIGNAL 掩盖 CONFIRMED（get_latest_action 按 event_id DESC）→ test_eod_plan_auto_confirm_event
    # 回归。故 SIGNAL 先写保证 CONFIRMED 是最新 action（与 pre_open 防线读取语义一致）。
    try:
        account_id = _resolve_account_id()
        # 确保 account 行存在（trade_event/order FK 引用；init_store 只建表不插行）
        if _state_store.get_account(account_id) is None:
            _state_store.upsert_account(account_id, broker="qmt")
        for o in order_dicts:
            sym = (o.get("order") or {}).get("symbol")
            if not sym:
                continue
            trade_id = _state_store.build_trade_id(account_id, sym, date)
            # SIGNAL meta 存计划参数快照（stop_loss/pre_open 改从 DB 读，spec §3.3）
            # SSoT Phase C · C1：meta 补 plan_date/strategy_name/rationale（C2 前置 + 归因重建真相源）。
            # 物理意图（C2 前置语义）：
            #   - plan_date=date（T+1 计划生效日，与 trade_id 同口径）：C2a scan_count 按 plan_date
            #     LIKE 查；C2d experiment report/trigger 按 plan_date 聚合。timestamp=写入日 T ≠ 计划
            #     日 T+1（致命日期轴，若按 timestamp 查会把 T 日盘后写入归到 T 日计划，错位一天）。
            #   - strategy_name="neckline"（当前单策略，未来多策略时由 build_orders 透传）：C2c
            #     review_report/review_service 按 strategy_name 过滤；rebuild_position_attribution 读
            #     真实 strategy_name 回填 position（弥补 B2 重启窗口）。
            #   - rationale=「颈线法@{formed_at}」（人类可读归因，formed_at=T 信号突破日）。
            # 非破坏扩展：{**o, ...} 在原 order_dict（stop_price/take_profit/neckline/formed_at/
            #   experiment_id 等）基础上扩展，不动 o 既有字段——消费方读 meta 不受影响。
            meta_obj = {**o, "plan_date": date, "strategy_name": "neckline",
                        "rationale": f"颈线法@{o.get('formed_at', '')}"}
            _state_store.insert_trade_event(
                account_id, trade_id, sym, "SIGNAL",
                meta=json.dumps(meta_obj, ensure_ascii=False))
            # CONFIRMED 仅在 auto_confirmed 且未被 veto 时写（veto 保护：最新 action=VETOED 不覆盖）
            if auto_confirmed and _state_store.get_latest_action(trade_id) != "VETOED":
                _state_store.insert_trade_event(account_id, trade_id, sym, "CONFIRMED")
    except Exception:
        # DB 写失败不阻断 eod_plan 主流程（plan JSON 已落，DB 软降级，下次 eod 补写）
        logger.exception("eod_plan 落 trade_event(SIGNAL) 失败（不阻断主流程，软降级）")
    if auto_confirmed:
        # 全自动：落盘 + SIGNAL DB 写完后才 confirm_plan（顺序保证 CONFIRMED 是最新 action）。
        # confirm_plan 自身也会写 DB CONFIRMED（W2 补的对齐人审路径），但 eod_plan 上方已
        # 先写一遍（auto 路径专属），confirm_plan 内部 insert_trade_event(CONFIRMED) 幂等跳过。
        trading_plan.confirm_plan(date)
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
    """T 日开盘前入口（C-8 V3 台账包裹）：running → done/skipped/failed。

    物理意图（spec §3.4）：cron（engine._pre_open）与启动补跑（trading.catchup）共用
    本函数，台账在此统一落（begin/finish）——「谁先完成谁生效」防双跑；
    skipped（gate 未过/无计划/未确认）不算完成，补跑窗口内可重试。
    实现 = 薄包裹 + 原逻辑改名 _pre_open_impl（行为零变更）。
    """
    try:
        job_ledger.begin_run("pre_open", date, clock.now().isoformat())
    except Exception:
        logger.exception("job_ledger begin_run 失败（不阻断 pre_open）")
    try:
        result = await _pre_open_impl(date)
    except Exception:
        try:
            job_ledger.finish_run("pre_open", date, "failed", "未预期异常")
        except Exception:
            logger.exception("job_ledger finish_run 失败（不阻断 pre_open）")
        raise
    status = "skipped" if (result.get("skipped") or result.get("reason")) else "done"
    message = str(result.get("skipped") or result.get("reason") or "")
    try:
        job_ledger.finish_run("pre_open", date, status, message)
    except Exception:
        logger.exception("job_ledger finish_run 失败（不阻断 pre_open）")
    return result


async def _pre_open_impl(date: str) -> dict:
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
            # C-4 U5：补传 account_id 激活柜台路径 cancel_order_by_broker_oid_db 回写
            # order.state=CANCELLED（breaker._cancel_via_broker_query 在 account_id 提供时才回写）。
            # Why 必传：不传则撤了昨日单 DB 仍记 SUBMITTED → T+1 对账幽灵单（spec §6.1 判据）。
            # 此为 C-3 审计结论的最小修（无需 purpose='CANCEL' 行，既有回写路径已够）。
            _cancel_res = await _cancel_all_open_orders(gw, account_id=_resolve_account_id())
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

    # ②.5 抓日内熔断基线 + account_daily start 快照（W4 · 08-04 断链根治 · spec §5.2）：
    # 物理意图：post_close 判 -3% 熔断需要 start_equity 基线，开盘前是唯一可靠的
    # 「未受当日交易影响」时点。pre_open 在确认闸 + 撤昨日单后调 gw.query_asset 抓当日
    # 开盘总资产 → 写 **account_daily** 表的 start 字段。
    #
    # W4 断链根治（08-04 发现 2）：原调 ``_position_book.snapshot_start_equity`` 写
    # **daily_equity** 表，而 post_close 的 ``snapshot_close_equity`` 写 **account_daily**
    # 表并读同表 ``start_total_asset`` 算 ``daily_pnl = close - start``——两表断链致
    # account_daily.start_total_asset 恒为 NULL → daily_pnl 恒 NULL（start 落在 daily_equity，
    # post_close 在 account_daily 找不到）。改调 ``_state_store.snapshot_start_equity``
    # 写 account_daily.start，与 post_close 同表 → daily_pnl 闭合。
    #
    # daily_equity 表读口已迁（C-1 收口 · 08-04 Task 9 review 根因）：原熔断读口
    # ``_position_book.get_start_equity``（读 daily_equity 表）在 W4 后恒返 None（daily_equity
    # 再无生产写入方）→ 日内熔断永久失效。post_close 步骤1 已改读
    # ``_state_store.get_start_equity``（与 pre_open 写口同表 account_daily），熔断基线闭合。
    # B5（SSoT Phase B）已删 position_book.snapshot/get_start 函数 + daily_equity DDL，
    # 熔断基线唯一读口 = state_store.get_start_equity(account_daily)。旧库残留 daily_equity
    # 表无害（init 不再 CREATE，不读写）。
    #
    # 边界（红线）：
    # - gw=None / query_asset 返 {}（未连接/锁定/超时）→ 跳过 + WARN，绝不拿 0/None
    #   写基线（否则 post_close check_daily_loss_limit(0, curr) 返 False 反永不熔断）；
    # - query_asset 异常 → 跳过 + 告警（不阻塞挂单主路径）。
    # Why 在撤单后而非前：撤单不影响总资产（仅未成交单状态变化），先后顺序无关；
    # 放后面可与撤单共用同一个 gw 引用，且「撤完昨日 → 抓今日基线」语义更清晰。
    # C-6 V2：用传入 date（入口缓存，_pre_open 已算 clock.today 传 pre_open），不重复 datetime.now。
    today_eq = date
    if gw is not None:
        try:
            asset = await gw.query_asset() if hasattr(gw, "query_asset") else {}
            total = (asset or {}).get("total_asset")
            if total is not None and float(total) > 0:
                # W4：cash 取 asset dict 的 cash 字段（与 post_close snapshot_close_equity
                # 的 close_cash 同口径，gw.query_asset 返 {total_asset, cash, market_value}）。
                cash = (asset or {}).get("cash")
                _state_store.snapshot_start_equity(
                    _resolve_account_id(), today_eq, float(total),
                    float(cash) if cash is not None else None)
                logger.info("pre_open 日内熔断基线已抓取 date=%s start_equity=%s",
                            today_eq, float(total))
            else:
                # query_asset 返空（未连接/锁定/超时）→ 不写基线，让 post_close 跳过熔断
                logger.warning("pre_open 跳过熔断基线快照：query_asset 返空 date=%s", today_eq)
        except Exception:
            logger.exception("pre_open 抓熔断基线异常（不阻塞挂单主路径） date=%s", today_eq)
    else:
        logger.warning("pre_open 跳过熔断基线快照：gw=None date=%s", today_eq)

    # ②.6 Task 8（P0-4 max_holding）：平超期持仓（跌停价释放资金）。
    # 物理意图：回测 max_holding 超时平仓对齐——持仓超 max_holding 日未达止盈即收盘平，
    # 实盘在【pre_open】挂跌停价卖单（保证成交，接受滑点；超时释放资金不等好价位）。
    # Why 在挂新买单前：先释放超期资金占用再挂新单（资金可用度更准）；卖单与买单方向
    # 相反不冲突。qty 必须来自 gw 真实持仓（红线，同 stop_loss_monitor，绝不硬编码）。
    #
    # SSoT Phase B 断点-2（B1 · pre_open 现算，删 expired_positions.json 跨日传递）：
    # 基准日 = clock.pretrade_date(clock.today()) = 上一交易日（T-1 收盘口径），非 today。
    # Why 不用 today：pre_open 在 T 日开盘前跑，T 日未收盘，entry_date 与 T 日做差会把
    # T 日算入 holding_days（多算一日）→ 超期判定整体提前 → 误平窗口内持仓（致命）。
    # Why 现算非读文件：post_close 写盘 + pre_open 读盘是断点-2 前的双写镜像（CSV 形态），
    # 文件覆盖写 + 跨日传递有竞态/崩溃丢失风险，且 holding_days 计算已可通过 position_book
    # 任意时刻现算（无状态，幂等）→ 删文件函数全收口到 pre_open 单点。
    _asof = clock.pretrade_date(clock.today())  # 基准日=上一交易日（断点-2，零漂移）
    _expired = _scan_expired_positions(_asof, _trade_cfg()["max_holding"])
    if _expired:
        await _close_expired_positions(gw, _expired)

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
    # C-6 V2：用传入 date（入口缓存，防同轮跨午夜漂移）。
    today_for_max_wait = date
    cfg_max_wait = int(os.getenv("TRADE_MAX_WAIT", "5"))
    n_submitted = 0
    n_expired = 0
    n_rejected = 0   # C-4 U4：单只业务拒单计数（L2 聚合 CRITICAL 用，防告警风暴）
    account_id = _resolve_account_id()
    # 确保 account 行存在（insert_order/trade_event FK 引用）
    # C-4 U3a：account 行是 insert_order/trade_event 的 FK 源——get_account/upsert_account
    # 失败=后续所有 DB 写 FK 全失效=DB 真故障。原软降级会让下游连环报 FK 错仍继续挂单，
    # 升 L1（review 补强：基础设施 > 单只计数）。
    try:
        if _state_store.get_account(account_id) is None:
            _state_store.upsert_account(account_id, broker="qmt")
    except Exception as e:
        raise _CriticalHalt(
            f"pre_open 确保 account 行失败 account={account_id}（DB 真故障，下游 FK 全失效）") from e
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
        trade_id = _state_store.build_trade_id(account_id, od['symbol'], date)
        try:
            if _state_store.get_latest_action(trade_id) == "VETOED":
                logger.info("pre_open 跳过 vetoed 标的 symbol=%s", od["symbol"])
                continue
            if _state_store.has_order(account_id, date, od["symbol"], "OPEN"):
                logger.info("pre_open 跳过已挂 OPEN（DB 幂等）symbol=%s", od["symbol"])
                continue
        except Exception as e:
            # C-4 U3a：幂等读失败=「不知是否已挂过」→ 继续挂=可能重复挂（双倍成交，真金损失）。
            # 原 soft-degrade 注释「不阻断，可能重复挂」即承认了真金损失风险——升 L1
            # （spec §3 state_store 关键读失败 = L1）。宁可停整批不盲挂。
            raise _CriticalHalt(
                f"pre_open DB 幂等读失败 symbol={od['symbol']}（敞口未明，拒继续挂）") from e
        order_req = OrderRequest(
            symbol=od["symbol"], qty=od["qty"], side=od["side"], price=od["price"],
        )
        # T8：挂单前先 insert_order(OPEN, PENDING)（DB 真相源，幂等 UNIQUE）
        # C-4 U3a：insert_order 是 DB 真相源写入——失败=柜台可能挂了但 DB 没记=对账幽灵单。
        # 原 soft-degrade「不阻断挂单」会让幽灵单在重跑时被当成「未挂」重复挂（双倍成交）。
        # 升 L1（review 补强：单只层面 DB 写异常 > 单只计数，硬抛停调度，绝不带病挂下一只）。
        try:
            _order_id = f"{date}_{od['symbol']}_OPEN_1"
            _state_store.insert_order(
                _order_id, trade_id, account_id, date, od["symbol"], od["side"], "OPEN",
                float(od["qty"]), float(od["price"]), state="PENDING")
        except Exception as e:
            raise _CriticalHalt(
                f"pre_open insert_order(OPEN) 失败 symbol={od['symbol']}（DB 真相源失真）") from e
        try:
            result = await _submit(order_req, confirm=True)
        except Exception as exc:
            # 挡板命中（资金不足/涨跌停/不在白名单等）会 raise RuntimeError
            # （trading_service.submit_order 契约）——必须逐单吞，一只拒单不炸整批。
            logger.warning("pre_open 挂单失败 symbol=%s 原因=%s", od["symbol"], exc)
            n_rejected += 1   # C-4 U4：聚合 L2 CRITICAL 计数（循环末尾汇总一条，防风暴）
            # C-1 final-review fix (I-2)：失败时把残留 PENDING 行标 REJECTED，否则
            # has_order(OPEN) 恒 True → 重跑永久漏挂（live 资金不足/涨跌停挡板致命）。
            try:
                _state_store.update_order_state(_order_id, "REJECTED")
            except Exception:
                logger.exception("pre_open 失败回填 REJECTED 失败 symbol=%s", od["symbol"])
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
                    submitted_at=clock.now().isoformat())
                _state_store.insert_trade_event(
                    account_id, trade_id, od["symbol"], "ORDERED",
                    order_id=_order_id, qty=float(od["qty"]), price=float(od["price"]))
            except Exception as e:
                # C-4 U3a：柜台挂成功了（_submit 返非 REJECTED/FAILED）但 DB 没回 SUBMITTED
                # = 对账以为没挂 → 幽灵单 / 重跑重复挂。升 L1（DB 真相源失真优先于单只）。
                raise _CriticalHalt(
                    f"pre_open 回填 SUBMITTED/ORDERED 失败 symbol={od['symbol']}（DB 真相源失真）") from e
        else:
            logger.warning("pre_open 挂单未成功 symbol=%s state=%s msg=%s",
                           od["symbol"], result.get("state"), result.get("message"))
            n_rejected += 1   # C-4 U4：未成功（REJECTED/FAILED）也计 L2 聚合（业务拒单同义）
            # C-1 final-review fix (I-2)：未成功（REJECTED/FAILED）标死态，has_order 放行重挂。
            try:
                _dead = "REJECTED" if result.get("state") == "REJECTED" else "FAILED"
                _state_store.update_order_state(_order_id, _dead)
            except Exception:
                logger.exception("pre_open 失败回填 %s 失败 symbol=%s", _dead, od["symbol"])

    logger.info("pre_open 完成 date=%s submitted=%d/%d expired=%d mode=%s",
                date, n_submitted, len(plan["orders"]), n_expired, _mode())
    # C-4 U4：部分拒单（L2）聚合一条 CRITICAL——单只研究员要知情，但整批继续不炸。
    # Why 聚合非逐只：防 N 只全拒告警风暴（spec R3）。整批 submitted=0 已有下方 CRITICAL（保留）。
    # Why 限 live：dry_run/测试的拒单非真金风险，防误告警。n_submitted>0 守卫：全拒走下方 submitted=0 通道。
    if n_rejected > 0 and _mode() == "live" and n_submitted > 0:
        _alert_critical(
            f"pre_open 部分挂单被拒 rejected={n_rejected}/{len(plan['orders'])} "
            f"submitted={n_submitted} date={date}（查挡板日志：涨跌停/资金/白名单）")
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
    # C-6 V2：时点判定走 clock.now（单一时间源口子）。
    if not calendar.is_intraday_session(clock.now()):
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
    # C-6 V2：业务日期 key（has_order(STOP)/insert_order(STOP) trade_date）走 clock.today。
    _today = clock.today()

    def _stop_already_placed(sym: str) -> bool:
        """查 DB 是否已挂 STOP 委托（幂等检查）。失败升 L1（不知是否发过=可能重发=双倍卖）。"""
        try:
            return _state_store.has_order(_aid, _today, sym, "STOP")
        except Exception as e:
            # C-4 U3b：幂等读失真——原回退 False（当作没挂）会直接走 _submit 重发卖单 = 双倍卖。
            # 升 L1（spec §3 DB 幂等读失败=L1）：停调度，CRITICAL 唤醒人工核对 DB 真相。
            raise _CriticalHalt(
                f"stop_loss 查 has_order(STOP) 失败 symbol={sym}（幂等读失真，拒继续盲发）") from e

    def _record_stop(sym: str, qty: float, price: float) -> None:
        """发止损单后落 DB order(STOP) + trade_event(STOP_TRIGGERED)。失败升 L1。"""
        try:
            if _state_store.get_account(_aid) is None:
                _state_store.upsert_account(_aid, broker="qmt")
            trade_id = _state_store.build_trade_id(_aid, sym, _today)
            oid = f"{_today}_{sym}_STOP_1"
            _state_store.insert_order(
                oid, trade_id, _aid, _today, sym, "sell", "STOP",
                float(qty), float(price), state="SUBMITTED")
            _state_store.insert_trade_event(
                _aid, trade_id, sym, "STOP_TRIGGERED",
                order_id=oid, qty=float(qty), price=float(price))
        except Exception as e:
            # C-4 U3b：卖单已通过 _submit 发出（柜台已收单）但 DB 没记 = 对账以为没挂 →
            # 下轮 30s 后重发 = 双倍卖（幽灵单）。升 L1（spec §3 DB 写失败=L1）：
            # 立即停调度，CRITICAL 唤醒人工核查 DB 真相 + 撤销可能的重复卖单。
            raise _CriticalHalt(
                f"stop_loss record_stop 落 DB 失败 symbol={sym}（卖单已发，DB 真相源失真）") from e
    # 主路径（monitor_ctx）与 fallback（stop_prices）至少其一非空才有监控意义；
    # pending_ctx 单独判定（pending 期无持仓，positions 空也照巡）。
    has_main_path = monitor_ctx is not None and len(monitor_ctx) > 0
    has_fallback = stop_prices is not None and len(stop_prices) > 0
    has_pending = pending_ctx is not None and len(pending_ctx) > 0
    if not (has_main_path or has_fallback or has_pending):
        return {"checked": 0, "reason": "无止损/撤单配置（monitor_ctx/stop_prices/pending_ctx 均空）"}

    # C-4 U3b：查持仓失败=敞口完全未明——原 return 软降级只是本轮跳过，下轮 30s 继续盲跑。
    # 升 L1（spec §3 查持仓失败=L1）：停调度，CRITICAL 唤醒人工（敞口未明继续跑=盲卖致命）。
    try:
        positions = await gw._fetch_broker_positions()  # {symbol: {volume, ...}}（T7 扩展）
    except Exception as e:
        raise _CriticalHalt("stop_loss_monitor 查持仓失败（敞口未明，拒继续盲跑）") from e

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

    # R2 降级告警（live）：行情源整体失效（xtdata 黑屏）→ 止损链路裸奔，CRITICAL 知会。
    # Why 30min 节流：避免 5min 巡检每轮推一条（M4 告警风暴红线）。
    if _mode() == "live" and relevant_syms:
        _n_valid = sum(
            1 for q in quotes.values()
            if isinstance(q, dict)
            and isinstance(q.get("last_price"), (int, float))
            and q["last_price"] == q["last_price"]  # NaN 判无效
            and q["last_price"] > 0
        )
        if _n_valid == 0:
            global _last_quote_blackout_alert_ts
            _now_mono = time.monotonic()
            if _now_mono - _last_quote_blackout_alert_ts >= _QUOTE_BLACKOUT_ALERT_INTERVAL_S:
                _last_quote_blackout_alert_ts = _now_mono
                _alert_critical(
                    f"stop_loss 行情源整体失效：{len(relevant_syms)} 个标的全无有效 "
                    f"last_price（xtdata 不可用？止损链路裸奔，请人工介入）")

    n_triggered = 0
    n_checked = 0
    n_fallback = 0
    n_pending_cancelled = 0
    n_submit_failed = 0   # C-4 U4：单只止损发卖失败计数（L2 聚合 CRITICAL 用，防风暴）

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
                        # #10：漏挂兜底——DB 无 TP1/TP2 时盘中补挂，否则止盈永远不执行
                        # （拖到止损/超时）。补挂走模块级 place_take_profit（差额幂等）。
                        _tp_ok = False
                        try:
                            _tp_ok = (_state_store.has_order(_aid, _today, sym, "TP1")
                                      or _state_store.has_order(_aid, _today, sym, "TP2"))
                        except Exception:
                            _tp_ok = True  # DB 查失败保守视为已挂（防重复挂超卖）
                        if not _tp_ok:
                            logger.warning(
                                "【TP 漏挂兜底】%s decide_exit=TAKE_PROFIT 但 DB 无 TP1/TP2，盘中补挂",
                                sym)
                            try:
                                await place_take_profit(sym, qty, price, "")
                            except Exception:
                                logger.exception("TP 盘中补挂失败 symbol=%s（需人工补挂）", sym)
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
                            n_submit_failed += 1   # C-4 U4：聚合 L2 CRITICAL 计数（主路径）
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
                n_submit_failed += 1   # C-4 U4：聚合 L2 CRITICAL 计数（fallback 路径）
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

    # C-4 U4：止损发卖失败聚合 L2 CRITICAL——漏止损真金损失，研究员须知情（但整批监控不停）。
    # Why 聚合非逐只：防多标的连板跌停时逐只告警风暴（spec R3）。Why 限 live：dry_run/测试
    # 的发卖失败非真金风险。与 pre_open 部分拒同范式：L2 不停调度，_halted 保持 False。
    if n_submit_failed > 0 and _mode() == "live":
        _alert_critical(
            f"stop_loss 部分卖出失败 submit_failed={n_submit_failed} checked={n_checked}"
            f"（查 gw 挡板/lock_down 日志，漏止损须人工补单）")
    logger.info("stop_loss_monitor 完成 checked=%d triggered=%d fallback=%d pending_cancelled=%d mode=%s",
                n_checked, n_triggered, n_fallback, n_pending_cancelled, _mode())
    return {"checked": n_checked, "stop_triggered": n_triggered,
            "fallback_used": n_fallback, "pending_cancelled": n_pending_cancelled,
            "mode": _mode()}


# ============================================================================
# Task 8（P0-4 max_holding 超时平仓）：pre_open 现算超期 + 跌停价平仓
# ============================================================================
# SSoT Phase B 断点-2（B1 · 2026-08-05）：删 expired_positions.json 跨日传递，
# pre_open 现算超期（无状态、幂等），基准日=clock.pretrade_date(today)=上一交易日。
# 物理意图：原 post_close 写盘 + pre_open 读盘是双写镜像（CSV 形态），文件覆盖写有
# 竞态/崩溃丢失风险；holding_days 已可由 position_book.entry_date 任意时刻现算 →
# 收口到 pre_open 单点，删文件函数全消除。


def _scan_expired_positions(today: str, max_holding: int) -> list[dict]:
    """扫超期持仓（holding_days > max_holding 的 {symbol→entry_date}）。

    物理意图（plan Task 8 · 对齐回测 MAX_HOLDING 超时平仓）：
        回测里成交后 max_holding 日未达任一止盈即收盘卖剩余；实盘对齐——pre_open 现算
        position_book.entry_date 算 holding_days（交易日口径，trading_days_between），
        >max_holding 即标超期，挂跌停价平仓释放资金。

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
          - 第 max_holding+1 日：本函数才标超期，pre_open 现算后跌停价兜底平（处理 monitor
            漏掉的标的：如 monitor 当日崩、断线、持仓查询失败等）。
        若改 `>=`：pre_open 现算与 monitor 同日触发——monitor 市价先平后 pre_open 跌停价
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


async def _close_expired_positions(gw: Any, expired: list[dict]) -> dict:
    """平超期持仓：查 gw 真实持仓 → 跌停价挂卖。

    风控红线（Grill Me · 同 stop_loss_monitor scope #3）：
        - 卖出 qty **必须**来自 gw._fetch_broker_positions 的真实持仓，绝不硬编码；
        - 价格优先跌停价（保证成交，超时释放资金接受滑点），无跌停价退化 last_price，
          都无则跳过（无价不发盲单=卖错价=致命）。

    防重（B1 后唯一手段，原「删文件」已废）：
        DB 幂等（has_order EXPIRED_CLOSE）兜住「同日 pre_open 重入重复挂卖」——pre_open 现算
        是无状态的，崩溃重入会重新扫出同一批超期，唯一防线是 state_store 的 EXPIRED_CLOSE
        UNIQUE 行（与 stop_loss SUBMITTED 防重同源）。

    Args:
        gw:      网关（dry_run 下 None → 无持仓可查，跳过）。
        expired: _scan_expired_positions 返回的超期列表（B1 改 pre_open 现算）。

    Returns:
        {"closed": <成功挂卖数>, "reason"?: ...}
    """
    from trading.compute.types import OrderRequest  # Layer2 阶段6 follow-up #4b：直指 compute.types 真身
    if gw is None:
        # dry_run 无网关无持仓可平
        logger.warning("跳过平超期持仓：gw=None（dry_run 无持仓可平）")
        return {"closed": 0, "reason": "gw=None"}
    try:
        positions = await gw._fetch_broker_positions()
    except Exception:
        # 查持仓失败拒发卖单（敞口未明即操作=盲卖）
        logger.exception("平超期持仓查持仓失败（拒发卖单）")
        return {"closed": 0, "reason": "查持仓异常"}
    # 批量取所有超期标的跌停价（对齐 stop_loss_monitor T3 批量模式，减 GIL/C++ 调用开销）
    try:
        quotes = await qmt_market_data.get_quotes([e["symbol"] for e in expired])
    except Exception:
        quotes = {}
        logger.exception("平超期持仓取行情异常（按无价处理，逐只跳过）")
    n_closed = 0
    today_close = clock.today()
    _aid = _resolve_account_id()
    for e in expired:
        sym = e["symbol"]
        pos = positions.get(sym)
        qty = pos["volume"] if isinstance(pos, dict) else pos  # 兼容老 mock 返 float
        if not qty or qty <= 0:
            continue
        # #7：DB 幂等防重——已挂 EXPIRED_CLOSE（未终态）跳过，兜住“提交后、消费标记前崩溃”
        try:
            if _state_store.has_order(_aid, today_close, sym, "EXPIRED_CLOSE"):
                logger.info("跳过已挂 EXPIRED_CLOSE symbol=%s（DB 幂等）", sym)
                continue
        except Exception:
            logger.exception("has_order(EXPIRED_CLOSE) 查询失败 symbol=%s（保守跳过）", sym)
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
            # #7：落 EXPIRED_CLOSE 幂等行（同日同标的同 purpose UNIQUE），防崩溃后重复挂卖
            try:
                if _state_store.get_account(_aid) is None:
                    _state_store.upsert_account(_aid, broker="qmt")
                _state_store.insert_order(
                    f"{today_close}_{sym}_EXPIRED_CLOSE_1",
                    _state_store.build_trade_id(_aid, sym, today_close), _aid, today_close, sym, "sell",
                    "EXPIRED_CLOSE", float(qty), float(price), state="SUBMITTED")
            except Exception:
                logger.exception("insert_order(EXPIRED_CLOSE) 失败 symbol=%s（告警人工复核）", sym)
    # B1 后无文件消费：DB 幂等（EXPIRED_CLOSE UNIQUE）兜底 pre_open 重入防重。
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
        1) state_store.get_start_equity(account_id, today) → start_equity
           （pre_open 写 account_daily.start_total_asset 的基线；W4 + C-1 已迁同表读口）
        2) gw.query_asset → curr_equity（盘后总资产）
        3) check_daily_loss_limit(start, curr) → True 即 cancel_all_open_orders +
           emergency_halt + ERROR 告警
        缺基线（start=None）→ 跳过 + WARN（不拿 0 触发，防 check(0,X) 永远 False 反永不熔断）。
    """
    result: dict = {"date": date}
    if gw is None:
        gw = get_gateway()

    # ① 对账（W3.4 · broker 权威 · 唯一持仓真相源）：
    # 拷问 3 结论：broker query_stock_positions 是【钱的真实归属】权威；fill/CSV 只解释
    # 「今日变动归因」，不能用它重写 position（否则与柜台漂移）。08-04 事故：post_close
    # 兜底以 CSV 聚合净持仓 diff position_book 并重写 qty，网关恢复后 24 行重复 CSV →
    # 幻影 2400 股 → 止损/止盈基于幻影持仓挂卖单（超卖敞口）。本段唯一以 broker 为准对账。
    # run_reconcile → gw.sync_positions → _fetch_broker_positions → reconcile 纯函数：
    #   - drift（broker vs local）：仅告警，不自动覆盖 position_book（drift 须人工判定，
    #     自动覆盖会与「断线无回报 fill 表空」类假象互相打架，决定权交风控）。
    #   - broker 取数失败/返空：绝【不】覆盖 position_book（返空可能是查询失败而非真空仓，
    #     覆盖会清空真实持仓 → 超卖敞口红线）→ CRITICAL 告警 + 保持既有账本值。
    if gw is not None and local_positions is not None:
        try:
            rec = await reconcile_job.run_reconcile(gw, local_positions, tolerance)
            # drift 判定：not is_ok 综合了 drifted/only_local/only_broker（Task7 契约）
            result["drift"] = not rec.is_ok
        except Exception:
            # broker 取数失败（断线/未连接/query_stock_positions 抛）—— 绝不覆盖 position_book：
            # 返空/异常与「真空仓」不可区分，覆盖=清空真实持仓=超卖敞口红线（W3.4）。
            # 仅标 drift=True + CRITICAL 告警触发人工排查；position_book 既有值原样保留。
            #
            # I-1（Task 8 review fix）：broker 取数失败 = 持仓真相源失效 + 盘后对账无法进行 +
            # drift 永久失明，正是 W3.4 最需叫醒人工的场景（08-04「全天锁死无告警」教训）。
            # 补 live 模式钉钉告警（_alert_critical 不停调度，与 _halt 是两个原语——
            # engine.py:130 docstring 明示「告警失败不阻塞主流程」；C-4 决议 _health_guard
            # 不升 L1 但失败时仍走 _alert_critical，本段同口径，盘后对账软降级不停调度，
            # C-4 既有编排红线不变）。dry_run 不推（避免测试噪音）。
            msg = (
                "post_close 对账异常：broker 取数失败（断线/未连接），"
                "position_book 保持既有值不覆盖（超卖敞口红线，人工复核柜台真实持仓）")
            logger.critical(msg, exc_info=True)
            if _mode() == "live":
                # live 模式 broker 真链路失败 = 实盘持仓真相源失效，必须钉钉叫醒人工。
                # _alert_critical 内部 fire_and_forget 软降级——告警失败不阻塞 post_close
                # 后续段（熔断/trailing/清白名单），与既有 _alert_critical 13 处复用范式一致。
                _alert_critical(msg)
            result["drift"] = True  # 异常视作有偏差（保守，触发人工排查）
    else:
        logger.info("post_close 跳过对账：gw=%s local_positions=%s",
                    "有" if gw is not None else "无",
                    "有" if local_positions is not None else "无")

    # ② aggregate_fills 盘后归因展示（W3.4 · 降级：不重写 position_book，仅产日志）：
    # 物理意图：fill 表/CSV 是「今日成交归因」——解释 position_book 今日为何变动，而非
    # 重写 position 的权威。drift 真相源在 ① broker（钱的归属）；CSV 可能在网关断线期间
    # 漏回报或恢复后重放，用它重写会与柜台漂移（08-04 事故根因）。
    # 故本段【只读 CSV 聚合 + 日志展示】，绝不调 reconcile_qty；drift 由 ① broker 路径判定。
    # gw=None（dry_run）跳过（无真实成交可归因，避免读 CSV 老数据产生误导日志）。
    if gw is not None:
        try:
            from presentation.server.services.trading_service import \
                aggregate_fills_by_symbol as _svc_agg_fills
            # C-6 V2：业务日期 key（当日成交流水口径）走 clock.today。
            today_eq = clock.today()
            net = _svc_agg_fills(today_eq, today_eq)
            local = _position_book.get_local_positions()
            # 归因 drift 展示（只读对比，不重写账本）：CSV 净持仓 vs position_book，
            # 标出哪些 symbol 有出入供研究员复盘「今日成交归因」，但 position_book
            # 维持 broker 权威口径（drift 真相以 ① broker reconcile 为准）。
            attribution: list[tuple[str, float, float]] = []
            for sym, net_qty in net.items():
                if abs(net_qty - local.get(sym, 0.0)) > 0.01:
                    attribution.append((sym, local.get(sym, 0.0), net_qty))
            for sym, local_qty in local.items():
                if sym not in net and abs(local_qty) > 0.01:
                    attribution.append((sym, local_qty, 0.0))
            if attribution:
                result["trades_attribution"] = len(attribution)
                logger.info(
                    "【盘后归因】aggregate_fills(DB fill) vs position_book 出入（仅展示，"
                    "不重写账本；drift 以 broker 权威为准）: %s",
                    ", ".join(f"{s}({lo}→{n})" for s, lo, n in attribution))
        except Exception:
            logger.exception("post_close aggregate_fills 归因展示异常（不阻塞熔断/清白名单）")
    # ③ 日内熔断三步（Task 10 · R-2 · 在 reconcile 之后）：
    # Why 在 reconcile 后：reconcile 查持仓 drift 是另一维度观测，与日内总资产 -3% 熔断
    # 互不依赖；放后面让熔断有最完整的 curr_equity（含盘后 reconcile 拉到的最新持仓估值）。
    # 各步独立 try-except 软降级：单段异常不阻塞清白名单和后续 trailing/max_holding。
    circuit_breaker_triggered = False
    breaker_skipped = False
    try:
        # 步骤 1：读 start_equity 基线（pre_open 写 account_daily.start_total_asset 表）
        # C-1 收口（W4 + 08-04 Task 9 review 根因）：原读 ``_position_book.get_start_equity``
        # （读 **daily_equity** 表），但 W4 已把 pre_open 写口迁到 ``_state_store.snapshot_start_equity``
        # （写 **account_daily** 表）—— daily_equity 表再无生产写入方，读口恒返 None →
        # ``breaker_skipped=True`` → 日内 -3% 熔断永久失效（实盘敞口失控红线）。
        # 改读 ``_state_store.get_start_equity``（与 pre_open 写口同表同口径 account_id），
        # 熔断基线闭合。B5 已删 position_book.snapshot/get_start 函数 + daily_equity DDL，
        # 此处是熔断基线唯一读口。account_id 沿用 ``_resolve_account_id``（与 pre_open W4 写入口径一致，
        # 否则读不到 pre_open 写的基线）。
        # C-6 V2：熔断基线 date（start/close equity 同口径）走 clock.today。
        today_eq = clock.today()
        start_equity = _state_store.get_start_equity(_resolve_account_id(), today_eq)
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
            # C-6 V2：trailing 演进读/写 plan key（load_plan/save_plan 当日口径）走 clock.today。
            today_eq = clock.today()
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

    # ⑤ max_holding 超期标记（Task 8 · P0-4）：SSoT Phase B 断点-2（B1）已迁至 pre_open 现算
    # （基准日=上一交易日=clock.pretrade_date(today)，见 _pre_open ②.6）。原 post_close 写盘 +
    # pre_open 读盘双写镜像已废，max_holding 标记逻辑不再在 post_close 跑——pre_open 任意
    # 时刻现算（无状态幂等），且避免 post_close 熔断优先约束（pre_open T 日跑则昨日已收盘，
    # 不与日内熔断善后冲突）。result["expired_positions"] 字段同步废。
    # Why 熔断优先原约束（B1 前的 if not circuit_breaker_triggered）已无意义：post_close 不
    # 再算超期，pre_open 现算时上一交易日已收盘，不存在「同日熔断善后冲突」场景。

    # ⑥ T11（state-store-redesign §3.2 post_close）：trade_event(CLOSED/TP1_FILLED) +
    # account_daily 收盘快照。DB 真相源收口 trade 生命周期 + 账户盈亏。
    try:
        _aid = _resolve_account_id()
        if _state_store.get_account(_aid) is None:
            _state_store.upsert_account(_aid, broker="qmt")
        # C-6 V2：trade_event(CLOSED/TP_FILLED) + account_daily trade_date key 走 clock.today。
        _today_close = clock.today()
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
            # C-6 V2：account_daily 收盘快照 trade_date key（与 start 口径对齐）走 clock.today。
            _today_eq = clock.today()
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



def _seq_for_real_oid(gw, real_oid: str) -> int | None:
    """_seq_to_real 反查：real→seq（async_response 晚到时按 seq 匹配 DB 行）。"""
    try:
        real_int = int(real_oid)
    except (TypeError, ValueError):
        return None
    seq_map = getattr(gw, "_seq_to_real", None) or {}
    for seq, real in seq_map.items():
        if real == real_int:
            return seq
    return None


def _order_state_to_db(state) -> str:
    """OrderState 枚举/字符串 → order 表 state 列约定（PARTIAL/FILLED/CANCELLED/REJECTED/...）。"""
    name = state.name if hasattr(state, "name") else str(state)
    return {
        "PARTIAL_FILLED": "PARTIAL",
        "FILLED": "FILLED",
        "CANCELLED": "CANCELLED",
        "REJECTED": "REJECTED",
        "PARTIAL_CANCELLED": "PARTIAL_CANCELLED",
    }.get(name, "SUBMITTED")


async def place_take_profit(symbol: str, filled_qty: float, fill_price: float,
                            order_id: str) -> None:
    """挂限价止盈卖单（#4 差额补挂：目标量 − 已挂量，防超卖/防覆盖缺口）。

    Why 模块级：stop_loss_monitor（模块级函数）盘中 TP 漏挂兜底也要调它（#10），
    实例方法无法被模块级函数引用（原 plan E4 的 self 错误根因）。
    """
    today = clock.today()
    plan = trading_plan.load_plan(today)
    if not plan:
        logger.warning("挂止盈跳过：无活跃计划 symbol=%s（计划未落盘/已失效）", symbol)
        return
    tp2 = tp1 = None
    tp1_portion = 0.0
    for o in plan.get("orders", []):
        if (o.get("order") or {}).get("symbol") == symbol:
            tp2 = o.get("take_profit")
            tp1 = o.get("tp1")
            tp1_portion = float(o.get("tp1_portion") or 0.0)
            break
    if tp2 is None or tp2 <= 0:
        logger.warning("挂止盈跳过：无止盈价配置 symbol=%s（计划缺 take_profit）", symbol)
        return
    filled_int = int(filled_qty)
    if filled_int <= 0:
        logger.warning("挂止盈跳过：成交量非正 symbol=%s filled_qty=%s", symbol, filled_qty)
        return

    from trading.compute.types import OrderRequest
    _aid = _resolve_account_id()
    _tid = _state_store.build_trade_id(_aid, symbol, today)

    # 已成交总量：OPEN 行 filled_qty（order 事件累计）优先，入参兜底
    total_filled = float(filled_int)
    if order_id:
        try:
            _open = _state_store.get_order_by_broker_oid(str(order_id))
            if _open is not None and _open.get("filled_qty"):
                total_filled = float(_open["filled_qty"])
        except Exception:
            logger.warning("读 OPEN filled_qty 失败 symbol=%s（用入参兜底）", symbol)

    def _placed(purpose: str) -> float:
        """已挂未终态量（差额基准）。"""
        try:
            return _state_store.get_order_placed_qty(_aid, today, symbol, purpose)
        except Exception:
            logger.exception("get_order_placed_qty(%s) 失败 symbol=%s（保守视为 0 补挂）", purpose, symbol)
            return 0.0

    def _record_tp(purpose: str, qty: int, price: float) -> None:
        """挂止盈单后落 DB order（UPSERT 累加语义）；失败=柜台已发单但 DB 没记 → ERROR 人工复核。"""
        try:
            if _state_store.get_account(_aid) is None:
                _state_store.upsert_account(_aid, broker="qmt")
            ok = _state_store.add_order_qty(
                _aid, today, symbol, purpose, float(qty), float(price))
            if not ok:
                # #4：DB 记失败但本次 _submit 已发出 → 柜台可能多挂。
                logger.error("【止盈幂等冲突】%s %s 落 DB 失败但本次 _submit 已发柜台，"
                             "需人工复核是否多挂超卖", symbol, purpose)
        except Exception:
            logger.exception("insert_order(%s) 失败 symbol=%s", purpose, symbol)
    use_two_legs = (tp1 is not None and tp1 > 0 and tp1_portion > 0.0 and tp1 < tp2)
    if not use_two_legs:
        # 单腿全平 tp2：差额 = 总持仓 − 已挂 TP2
        need2 = int(total_filled) - int(_placed("TP2"))
        if need2 <= 0:
            return
        result = await _submit(
            OrderRequest(symbol=symbol, qty=need2, side="sell", price=tp2), confirm=True)
        if result.get("state") not in ("REJECTED", "FAILED"):
            logger.info("【止盈单已挂】%s %s股 @%s（单笔全平 tp2 差额补挂）", symbol, need2, tp2)
            _record_tp("TP2", need2, tp2)
        else:
            logger.warning("止盈单挂失败 symbol=%s state=%s msg=%s（人工补挂）",
                           symbol, result.get("state"), result.get("message"))
        return

    # 分级两腿：目标量 − 已挂量，各腿独立补挂（防超卖 + 防覆盖缺口）
    tp1_target = int(total_filled * tp1_portion / 100) * 100
    tp2_target = int(total_filled) - tp1_target
    need1 = tp1_target - int(_placed("TP1"))
    need2 = tp2_target - int(_placed("TP2"))
    if need1 > 0:
        r1 = await _submit(
            OrderRequest(symbol=symbol, qty=need1, side="sell", price=tp1), confirm=True)
        if r1.get("state") not in ("REJECTED", "FAILED"):
            _record_tp("TP1", need1, tp1)
        else:
            logger.warning("止盈单挂失败 symbol=%s leg=tp1 state=%s msg=%s（人工补挂）",
                           symbol, r1.get("state"), r1.get("message"))
    if need2 > 0:
        r2 = await _submit(
            OrderRequest(symbol=symbol, qty=need2, side="sell", price=tp2), confirm=True)
        if r2.get("state") not in ("REJECTED", "FAILED"):
            _record_tp("TP2", need2, tp2)
        else:
            logger.warning("止盈单挂失败 symbol=%s leg=tp2 state=%s msg=%s（人工补挂）",
                           symbol, r2.get("state"), r2.get("message"))

# ============================================================================
# TradingEngine：APScheduler 四 cron 装配（独立常驻进程 python -m trading）
# ============================================================================
class TradingEngine:
    """APScheduler 编排容器（cron 触发点装配 + start/shutdown 生命周期）。

    ⚠️ 进程模型（C-2 scheduling-orchestration Task 9 收口）：
        本类实例既可由 ``python -m trading`` 独立进程构造（开发/调试，``trading/__main__``），
        也可由 uvicorn server lifespan 构造（生产，``presentation/server/main.py``）。
        engine 与 server 同进程后的 dynamic_whitelist 物理隔离已由 W1 实例属性化完成
        （``self._dynamic_whitelist``，server 路径 submit_order 不读实例属性 → 两端输入源
        互不污染），原「绝不可同进程」红线已解除。

    cron 触发点（Task4/9 已配 env，缺省值对齐 A 股交易日历 · 术语对齐 T 日盘后扫盘）：
        pipeline_then_eod  18:00 周一-五  盘后事件链：采集→校验→eod→brief（Task9 取代
                          原 19:00 eod 时钟赌博；用 ``await proc.wait()`` 等采集完成，
                          不再靠时差猜测 18:00 增量是否落湖）
        pre_open   09:22 周一-五  T 日开盘前撤昨日 + 挂当日单
        stop_loss  每 30s（IntervalTrigger，Task8：cron 不支持秒级；时段约束在 monitor 兜底）
        post_close 15:30 周一-五  盘后对账 + 清白名单

    每个 job 先过 calendar.is_trading_day 判交易日（节假日整体跳过）。
    """

    def __init__(self) -> None:
        """装配 AsyncIOScheduler + 四 job（不 start）。

        ⚠️ 触发器形态分轨（Task8）：
            pipeline_then_eod / pre_open / post_close：分钟粒度 CronTrigger（标准 5 字段）。
            stop_loss：**IntervalTrigger（秒级）**——cron 最小粒度是分钟，
            30s 巡检必须用 interval。时段约束（9:30-11:30 / 13:00-15:00）下放给
            ``stop_loss_monitor`` 内 ``calendar.is_intraday_session`` 兜底，
            非盘中由 monitor 直接 no-op（不在 trigger 层做时段过滤，避免 interval
            在午休 / 盘后空跑也只是命中 no-op，零副作用）。
        """
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        # C-4 U1：job_defaults 硬化（防 job 堆积重叠 + 休眠补跑风暴）。
        # max_instances=1：每 job 同时只一个实例——pre_open 挂单慢（QMT 限频）跑超 9:22，
        #   下次触发被挡，防重叠双挂；stop_loss 30s 跑超 30s 同理防重叠发卖。
        # misfire_grace_time=300：机器休眠/重启错过触发——5min 内补跑（保盘后 job 不轻易漏），
        #   超 5min 放弃（stop_loss 30s 堆积 10 次只补最近 1 次，防补跑风暴）。
        # coalesce=True：与 misfire 配合，堆积合并成一次（不补跑多次）。
        self.sched = AsyncIOScheduler(job_defaults={
            "max_instances": 1,
            "misfire_grace_time": 300,
            "coalesce": True,
        })
        # C-2 scheduling-orchestration Task 9：eod 改由 ``pipeline_then_eod`` 事件链驱动。
        # 物理意图（取代 19:00 eod 时钟赌博）：原 ``self._eod`` cron（19:00）靠时差猜测
        # 18:00 增量采集是否落湖 + 18:30 检查点② 是否通过——脆弱时序（采集慢/失败时 _eod
        # 读 T-1 数据算 T+1 计划 = 时序 bug）。事件链 ``pipeline_then_eod`` 用确定性的
        # ``await proc.wait()`` 等采集子进程完成 → 按策略声明 check_freshness → 全绿才
        # ``engine._eod()``，把「时钟赌博」换成「事件驱动」。brief 播报也收进事件链尾部。
        # Why 18:00（默认）：盘后 18:00 触发事件链——此时增量采集的 @18:00 sync_all_tushare
        # 刚开始，事件链内部 ``await proc.wait()`` 等其完成（不再靠 19:00 时差赌博）。
        # ENGINE_PIPELINE_CRON env 可覆盖（灰度调整事件链触发时点用）。
        # Why args=[self]：``pipeline_then_eod(engine)`` 需要引擎实例调 ``engine._eod()``，
        # APScheduler ``args`` 透传 self（与原 ``self._eod`` bound method 等价的显式形式）。
        # ``pipeline_then_eod`` 是 async 函数，AsyncIOScheduler 直接 await（与 ``self._eod`` 同）。
        # C-4 U2：pipeline 收编为 ``_pipeline_then_eod`` method（过 _critical_guard 装饰），
        # 替代原外部函数 + ``args=[self]`` 形式——五 job 统一走 L1 停调度 wrapper。
        self.sched.add_job(
            self._pipeline_then_eod, CronTrigger.from_crontab(
                os.getenv("ENGINE_PIPELINE_CRON", PIPELINE_CRON_DEFAULT)),  # 18:00 盘后
            id="pipeline_then_eod",
        )
        # 四 job 注册：id 显式命名便于 get_jobs 自检与外部调试
        self.sched.add_job(
            self._pre_open, CronTrigger.from_crontab(
                os.getenv("ENGINE_PRE_OPEN_CRON", PRE_OPEN_CRON_DEFAULT)),
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
                os.getenv("ENGINE_POST_CLOSE_CRON", POST_CLOSE_CRON_DEFAULT)),
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
        # P0-3（2026-08-04）：上次观测的客户端就绪态（None=首轮），False→True 跃迁时
        # 清零退避立即重连（外部条件恢复 ≠ 失败重试）。
        self._guard_client_ready_prev: Optional[bool] = None
        # W1.2（08-04 静默断线根治）：未就绪连续轮数计数。
        # Why 进程内存且就绪后清零：与 _guard_fail_count 同生命周期（自愈 job 分钟级，
        # 重启从 0 开始无副作用）；就绪后必须清零，否则下次新断线第 1 轮即
        # 历史遗留 + 1 触发错位告警（语义应是「连续 N 轮」而非「累计 N 轮」）。
        # 节流 % 10：每 10 轮推一次钉钉（≈10min），避免 60s 周期刷爆告警通道。
        self._not_ready_rounds: int = 0

        # C-4 U2：停调度 flag（_halt=True 后所有被 _critical_guard 装饰的 job 入口即跳过）。
        # Why 进程内存（不持久化）：致命停调度需人工介入重启，重启后 _halted=False 重新就绪；
        #   持久化反而让重启后仍锁死（与「人工确认恢复」语义冲突）。
        self._halted: bool = False

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
    def _gw_health_gate(self, gw) -> tuple[bool, str]:
        """C-5 V3：网关健康前置 gate（从 _pre_open_gate ② 段抽，共享给 _stoploss/_post_close）。

        物理意图（spec §4.1 · B 共享前置 gate）：
            触发点业务前显式探测网关健康，锁态时返 ``(False, reason)`` 让调用方
            skip + CRITICAL 不跑业务（防静默全失败），与 ``_pre_open_gate`` ② 段
            + ``_health_guard`` 自愈取向一致（不停调度，等 60s 自愈恢复 live）。

        判据（与 _pre_open_gate ② 段逐行等价，DRY 抽离零行为变更）：
            ① ``gw is None`` 或 ``gw._connected=False`` → ``"网关未连接"``；
            ② ``gw.is_client_ready()=False`` → ``"miniQMT 客户端未就绪"``。
        ``is_client_ready`` 是目录存在性探测（W1.1 重定义：userdata 目录在即 True，
        connect 返回码才是客户端可用性唯一权威；mtime 已降级为 ``_client_staleness_diag``
        日志分类素材，绝不硬前置），不触达 xtquant，CI/单测/无 SDK 环境安全调用。

        Args:
            gw: 交易网关实例（``get_gateway()`` 取，可能为 None）。鸭子类型：读
                ``gw._connected`` 与调 ``gw.is_client_ready()``。

        Returns:
            ``(True, "")`` 网关健康；``(False, reason)`` 锁态，reason 简短中文。
        """
        if gw is None or not getattr(gw, "_connected", False):
            return False, "网关未连接"
        if not gw.is_client_ready():
            return False, "miniQMT 客户端未就绪"
        return True, ""

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
              ③ 数据就绪（W5 单口判定 · spec #13 T10）——
                 调 ``get_ready(_data_date, keys)`` 合成 data_ready① 内容校验 AND
                 job_ledger.pipeline② 台账 done。任一源失败 → False + warning 显式
                 暴露差异（消除「台账 done、内容缺、播报 healthy」三张嘴漂移）。

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
        # ② 网关健康（探测，无写副作用）—— C-5 V3 DRY：改调共享 _gw_health_gate
        # （与 _stoploss/_post_close 三入口同口径；行为与原内联逐行等价）。
        gw_ok, gw_reason = self._gw_health_gate(gw)
        if not gw_ok:
            return False, gw_reason
        # ③ 数据就绪（W5 单口判定 · spec #13 消除「三张嘴」漂移）
        # #2 修复（保留）：改查 expected_latest_trade_day(now)——T 日盘后落 data_ready(T)，
        # T+1 日盘前 pre_open 查“最近已收盘交易日”=T 命中。原查 get_data_ready(date=T+1)
        # 永远 None（data_ready 只落 T）→ 整天不挂单。与 _eod next_trading_day 同源口径。
        #
        # W5 改造（spec #13）：原遍历 get_data_ready 只查内容校验①，不查 job_ledger②，
        # 与 catchup（只查②）/ 播报端（mtime+哨兵③）三方各自判定 → 「台账 done、内容缺、
        # 播报 healthy」漂移。改调 get_ready 合成 ①+②，任一源失败 → False + warning 显式
        # 暴露差异。datasets 仍用 _plan_data_keys(plan) 反推（保留策略声明数据集语义）。
        from trading.calendar import expected_latest_trade_day
        _data_date = expected_latest_trade_day(clock.now())
        keys = sorted(self._plan_data_keys(plan)) or None  # None → get_ready 用默认 ["daily"]
        if not get_ready(_data_date, keys):
            # get_ready 内部已 logger.warning 暴露具体哪源漂移（data_ready 内容/job_ledger 台账）；
            # gate reason 给简短中文供台账 skipped.message 记录。
            return False, f"数据未就绪（{_data_date}：内容校验或 pipeline 台账未绿，详见日志）"
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

        QMT session 单实例锁（live 专属）在连网关前 acquire：SERVER_PORT 被覆盖 /
            直跑 server 时端口 8000 天然单例失效，第二个引擎仍可能连同一 session
            （xtquant -1 全天锁死）；锁被占用即拒启动。

        物理意图（保留原 ``__main__`` 注释，行为完全等价 · W3 不改启动语义）：
            - 网关 connect + set_order_update_callback（修 G5：成交回报回流链路就绪）。
            - 异常兜底不抛：连接失败时仍让 cron 起来——触发点内部 get_gateway() 会再次
              惰性取单例做兜底判空（None 时走 dry_run 分支），这里只打 exception 不阻断。
            - position_book.init_db / state_store.init_store / state_store._migrate_env_to_account
              建表 + 从 .env 落 account 行（state-store-redesign T13）。
        """
        # 单实例守护（QMT session 级 · live 专属）：防双引擎抢同一 session。
        # 端口 8000 已拦同端口双起（C-5 V1），本锁补「不同端口双实例」的剩余缺口。
        # Why 只 live：dry_run 无真 session，锁会干扰开发多开/测试。
        if _mode() == "live":
            from trading import single_instance
            _session = os.getenv("QMT_SESSION_ID") or "default"
            _lock = single_instance.acquire(_session)
            if _lock is None:
                _alert_critical(
                    f"检测到另一 TradingEngine 实例持有 QMT session={_session} 锁，"
                    f"拒绝重复连接（防 connect -1 双起，请先停旧实例）")
                raise RuntimeError(
                    f"QMT session={_session} 单实例锁被占用"
                    f"（logs/trading_engine_*.lock），另一引擎已在运行，拒绝启动")
            self._instance_lock = _lock

        gw = get_gateway()
        if gw is None:
            logger.warning("未装配网关（AUTO_TRADE_MODE=dry_run 影子模式，回调链路不生效）")
        else:
            # 回报回调先接好（幂等，不依赖 connect）：health_guard 稍后连接时链路即就绪。
            try:
                gw.set_order_update_callback(self._handle_order_update)  # sync 注入成交回报回调
            except Exception:
                logger.exception("set_order_update_callback 注入异常（继续启动）")
            self._gw = gw  # 供 handler 反查 _orders 判 BUY/SELL side（见 engine._side_from_update）
            # P0-1（2026-08-04 connect -1 根治）：客户端就绪前绝不 connect——
            # xtquant trader.start() 会先于客户端创建 down_queue_win_{sid} 会话文件，
            # 客户端后起挂上后同 sid 恒 -1，只能靠进程级重启/换 sid 恢复。
            if gw.is_client_ready():
                try:
                    await gw.connect()  # async：内部 run_in_executor 包 xtquant C++ 阻塞 connect
                    logger.info("网关已连接 + 成交回调已注册")
                except Exception:
                    logger.exception("网关连接失败（cron 仍启动，触发点内部 get_gateway 兜底）")
            else:
                # W1.2（收口 A）：启动期断线也要有诊断文案（与 _health_guard ④ 同口径）。
                # 旧文案只说「未就绪」不告诉你为什么——操作员看到日志仍要去翻 userdata 找原因。
                # 接入 gw._client_staleness_diag()（T1 四态文案：目录缺失/目录空/无活跃文件/陈旧 N 分钟）
                # 让启动失败时日志自带根因，缩短排障时间。hasattr 兜底防 gw 未升级。
                diag = gw._client_staleness_diag() if hasattr(gw, "_client_staleness_diag") else "无诊断"
                logger.warning(
                    "miniQMT 客户端未就绪（is_client_ready=False，%s），bootstrap 跳过 connect——"
                    "由 health_guard 在客户端就绪后连接（避免先于客户端创建会话文件）", diag)

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
        # SSoT Phase C · C1：启动归因重建（弥补 B2 重启丢失窗口）。
        # 物理意图（spec §5 断点-3 弥补）：B2 把归因落 position 列但**不做重启重建**——重启窗口
        # 内 BUY 成交 + B2 归因未及写就崩的 position 行 strategy IS NULL 裸奔。本补扫从
        # trade_event(SIGNAL).meta 真实 strategy_name 回填（IS NULL 守卫不覆盖 B2 已写）。
        # Why 在 bootstrap：state_store.init_store + _migrate_env_to_account 已就绪（建表+账户
        # 已落），position/trade_event 表可读写；cron 启动前补归因，避免首触发点读到 NULL 归因。
        # Why try/except 不阻断：归因是审计维度（非交易红线），失败不能让 engine 启动崩——
        # 启动是红线（C-5/C-4 决议：归因重建软降级，启动硬约束）。
        try:
            _n = state_store.rebuild_position_attribution(_resolve_account_id())
            if _n:
                logger.info("启动归因重建：从 SIGNAL.meta 回填 %d 个持仓归因（C1 弥补 B2 重启窗口）", _n)
        except Exception:
            logger.exception("启动归因重建失败（不阻断 engine 启动，归因审计维度软降级）")

    @_critical_guard
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
        # #6 修复：风控熔断粘滞——risk_halted 时只告警不重连（熔断应全场停摆，次日人工接管）。
        # 与 on_disconnected 网络断线区分（后者 _risk_halted=False，允许 health_guard 自愈）。
        if getattr(gw, "_risk_halted", False):
            logger.warning("网关处于风控熔断态（risk_halted），health_guard 跳过重连（需人工 clear_risk_halt）")
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
        #    is_client_ready 是目录存在性探测（W1.1 重定义）：False 意味 miniQMT 客户端
        #    userdata 目录缺失/空（进程必然未起）→ connect 必返 -1 或超时，空跑无意义。
        #    W1.2（08-04 静默跳过根治）：旧版此分支静默 return 致网关断线 9 小时无人知
        #    （直到 pre_open 失败才暴露）。现补可见性——WARNING + 诊断文案（来源
        #    gw._client_staleness_diag，四态分类素材）+ 每 10 轮节流推一次钉钉
        #    （复用 _alert_critical 通道，60s/轮 × 10 ≈ 10min 一次，防告警风暴）。
        ready = gw.is_client_ready()
        if not ready:
            self._guard_client_ready_prev = False
            self._not_ready_rounds += 1
            # 诊断文案：T1 提供的四态稳定文案（目录缺失/目录空/无活跃文件/陈旧 N 分钟）。
            # hasattr 兜底防 gw 未升级（T1 未合入的旧网关），降级为「无诊断」不阻断告警。
            diag = gw._client_staleness_diag() if hasattr(gw, "_client_staleness_diag") else "无诊断"
            logger.warning("health_guard 客户端未就绪，跳过 connect（%s，连续 %d 轮）",
                           diag, self._not_ready_rounds)
            # 节流钉钉（I-1 收口 · 断线立刻可见）：首轮即推 + 后续每 10 轮节流推一次。
            # 物理意图：盘中 09:22 pre_open 前断线时，旧版「% 10 == 0」要等到第 10 轮
            # （≈10min 后 = 09:32）才首推，pre_open 已被 gate 静默跳过，错过人审介入窗口。
            # 新口径 _not_ready_rounds==1（刚发现未就绪）立即推一条让操作员在 pre_open
            # 窗口关闭前有机会介入；后续 10/20/30... 轮节流推一次防告警风暴。
            # Why 首推不会引起风暴：风暴是连续推送累积效应（60s/轮 × N 轮），
            # 单次首推仅 1 条，反而能在断线最早期（最值得告警的时刻）触达操作员。
            # 断线恢复又断线时 _not_ready_rounds 在 ④ 就绪分支清零再 +1，每次新断线都首推。
            # _alert_critical 内部 fire_and_forget 软降级——告警失败不阻塞守护主链路
            # （try/except 在 _alert_critical 内兜底，C-4 错误分级决议）。
            if self._not_ready_rounds == 1 or self._not_ready_rounds % 10 == 0:
                # Fix1（用户两轴 review · 告警模式闸）：dry_run 模式无真金风险，
                # 客户端未就绪是环境问题（miniQMT 未起/路径错），推钉钉只会污染运营群
                # 致研究员对真告警麻木。守卫与既有 7 处 _alert_critical 范式一致
                # （pre_open gate/部分拒/漏挂等）。日志/计数照常，只是不推钉钉。
                if _mode() == "live":
                    _alert_critical(
                        f"health_guard 客户端连续未就绪 {self._not_ready_rounds} 轮"
                        f"（≈{self._not_ready_rounds}min），网关无法自愈重连（{diag}）。"
                        f"请人工检查 miniQMT 客户端是否启动/登录")
            return
        # 就绪后清零未就绪计数（W1.2）：防计数漂移——上次断线遗留计数若不清，
        # 下次新断线第 1 轮即历史值 + 1 触发错位告警（语义错位为「累计」非「连续」）。
        self._not_ready_rounds = 0
        # P0-3（2026-08-04）：客户端从不可用→可用是「外部条件恢复」而非失败重试——
        # 清零退避立即重连，避免被历史失败计数拖到 7 轮（≈7min）后才试探。
        if self._guard_client_ready_prev is False:
            self._guard_fail_count = 0
            self._guard_rounds_since_fail = 0
            logger.info("health_guard 检测到 miniQMT 客户端就绪，清零退避立即重连")
        self._guard_client_ready_prev = True
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
            # R1（live 后排期收口）：重连成功后窗口内补挂 pre_open——网关断线/锁死
            # 期间漏挂的订单由 catchup ledger 幂等判定补挂（窗口 [09:22, catchup_until)，
            # 已 done 跳过，过窗 CRITICAL）。补挂异常不阻断守护（下轮重试由 ledger 守卫）。
            try:
                from trading.catchup import _catchup_pre_open
                _caught, _note = await _catchup_pre_open()
                if _caught:
                    logger.warning("health_guard 重连成功 → pre_open 补挂完成（%s）", _note)
                else:
                    logger.info("health_guard 重连成功 → pre_open 无需补挂（%s）", _note)
            except Exception:
                logger.exception("health_guard 重连后 pre_open 补挂异常（不阻断守护）")
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
                # Fix1（用户两轴 review · 告警模式闸）：dry_run 模式无真金风险，
                # connect 重连失败多是开发环境 xtquant/客户端未装，推钉钉纯噪音。
                # live 模式才是真金断线需叫醒人工。守卫与未就绪分支同范式。
                if _mode() == "live":
                    _alert_critical(
                        f"health_guard 重连累计失败 {self._guard_fail_count} 次，"
                        f"网关持续锁死（_reconnect 已耗尽 backoffs 仍未恢复），"
                        f"请人工介入：检查 miniQMT 客户端是否启动 / session 是否被占用 / "
                        f"userdata shm 文件是否过期")

    def _halt(self, msg: str) -> None:
        """L1 统一停调度原语：置 _halted + CRITICAL + sched.shutdown（幂等）。

        物理意图（spec §5 双层保障）：
            sched.shutdown 停「新触发」+ _halted flag 防「in-flight job 继续写」。
            幂等：已 _halted 时直接返回（多路径同时致命不重复 shutdown/alert）。

        Why shutdown(wait=False) 而非 pause()（review 决议）：
            致命场景下「带病跑不如停」——pause 可被误恢复，留口子；shutdown 硬停 + CRITICAL
            唤醒人工，是 live 真金保护取向（spec R4）。
        """
        if self._halted:
            return
        self._halted = True
        _alert_critical(f"致命停调度 {msg}")
        try:
            self.sched.shutdown(wait=False)   # 先例 engine.py shutdown()
        except Exception:
            # shutdown 自身抛（如 scheduler 未 start / 已 shutdown）→ _halted 已置，
            # 被 _critical_guard 装饰的 job 顶检查兜底，不再写。
            logger.exception("sched.shutdown 失败（_halted 已置，job 顶检查兜底）")

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
            today: YYYY-MM-DD；None 时取 clock.today() 当日（启动自检默认当日）。

        Returns:
            True  = 口径正常（next_trading_day 算出次日，落盘 key 与次日读 today 对齐）；
            False = 口径异常（next_trading_day 返 today 自身/空值/抛异常 → 疑似跑旧代码，
                    调用方 start() 须 logger.error 告警，CRITICAL 钉钉接线留 T9）。
        """
        # C-6 V2：启动口径自检的当日基准走 clock.today（单一时间源口子）。
        _today = today or clock.today()
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
    async def _eod(self, *, data_day: str | None = None,
                   plan_date: str | None = None) -> None:
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
        # C-6 V2：单一时间源 + 入口缓存（防同轮跨午夜漂移）。
        # C-8 V2：data_day/plan_date 显式注入（启动补跑传最近已收盘交易日）；缺省 None 时与 C-6 完全等价（_today=clock.today，_td=clock.trading_day）。
        # _today=交易日守卫口径（clock.today），_td=eod 落盘 key 口径（clock.trading_day，
        # =next_trading_day(today)）。命名区分读/写避免 eod/pre_open key 错位
        # （[[eod-date-offbyone-fix]] 病灶：eod 落盘必用 trading_day，pre_open 读 today）。
        _today = data_day or clock.today()
        if not calendar.is_trading_day(_today):
            logger.info("eod_plan 跳过：今日非交易日 %s", _today)
            return
        _td = plan_date or clock.trading_day()
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
            # C-6 V2：df_upto 截止于 T 日 today（入口缓存 _today，T 日盘后扫到 T）。
            df_upto = _load_df_upto(lake, sym, _today)
            if df_upto is not None and len(df_upto) >= 60:
                # 历史不足（<60 行）不进 df_map（与原 _eod 内联 <60 跳过同口径）
                df_map[sym] = df_upto

        # 加载停牌区间 + 近 2 年 trade_days（逻辑从 scan_live 原 _ensure_integrity_cache 搬，
        # fail-open 同口径：加载失败返 ({}, set()) 让 filter 放行——trade_days 空集 →
        # check_window_continuity 的 expected 恒空 → ok=True 全放行，退回原行为）。
        susp, trade_days = _load_integrity_ctx(_today)

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
                    # C-6 V2：scan_live 截止日（T 日 today，入口缓存）传 _today。
                    for s in strategy.scan_live(sym, df_upto, _today):
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
            # C-6 V2：cooldown 回溯基准（T 日 today，入口缓存）传 _today。
            recent_syms = _load_recent_plan_symbols(days_back=cooldown + 2, today=_today)
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
        # C-6 V2：_td = clock.trading_day() = next_trading_day(_today)，与原逻辑等价但入口
        # 缓存一次（防同轮跨午夜漂移，且命名区分读/写口径——eod 必用 trading_day）。
        await eod_plan(
            _td, signals, atr_map,
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

    @_critical_guard
    async def _pre_open(self) -> None:
        # C-6 V2：单一时间源 + 入口缓存（clock.today，防同轮跨午夜漂移）。
        # 入口算一次 today 传下游 pre_open（pre_open 内 today_eq/today_for_max_wait 用 date 参数）。
        today = clock.today()
        if not calendar.is_trading_day(today):
            logger.info("pre_open 跳过：今日非交易日 %s", today)
            return
        await pre_open(today)

    @_critical_guard
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
        # C-5 V4 B：网关健康前置 gate（@_critical_guard 后、交易日守卫前）。
        # 物理意图（spec §4.3）：gw 锁态时 skip+CRITICAL 不跑业务（不查 plan、不调
        # stop_loss_monitor），与 _pre_open_gate 网关锁态 skip 同口径；与 _health_guard
        # 不升 L1 自愈取向一致（C-4 决议）—— 等待 60s 自愈恢复 live，而非 _halt 停调度。
        gw = get_gateway()
        ok, reason = self._gw_health_gate(gw)
        if not ok:
            # Fix1（用户两轴 review · 告警模式闸）：dry_run 模式 gw 锁态无真金风险，
            # 推钉钉纯噪音（dry_run 网关常未装/未起）。守卫与既有 _alert_critical 范式一致。
            if _mode() == "live":
                _alert_critical(f"stop_loss 跳过：{reason}（gw 锁态，等 _health_guard 自愈）")
            return
        # C-6 V2：单一时间源 + 入口缓存（clock.today，防同轮跨午夜漂移）。
        today = clock.today()
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

    @_critical_guard
    async def _post_close(self) -> None:
        # C-5 V4 B：网关健康前置 gate（与 _stoploss 同口径，spec §4.3）。
        # gw 锁态时 skip+CRITICAL 不跑对账业务（防基于陈旧/缺失快照误判 drift），
        # 等 _health_guard 自愈；不停调度（与 _pre_open_gate + _health_guard 一致）。
        gw = get_gateway()
        ok, reason = self._gw_health_gate(gw)
        if not ok:
            # Fix1（用户两轴 review · 告警模式闸）：dry_run 模式 gw 锁态无真金风险，
            # 推钉钉纯噪音（dry_run 网关常未装/未起）。守卫与既有 _alert_critical 范式一致。
            if _mode() == "live":
                _alert_critical(f"post_close 跳过：{reason}（gw 锁态，等 _health_guard 自愈）")
            return
        # C-6 V2：单一时间源 + 入口缓存（clock.today，防同轮跨午夜漂移）。
        today = clock.today()
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
        if kind == "async_response":
            # #5 修复：seq→real 映射回填 DB order.broker_oid（撤单/对账唯一可靠锚点）。
            # 原实现 kind!='trade' 直接 return 丢弃本事件 → broker_oid 恒 str(seq) →
            # cancel_order_by_broker_oid_db 永匹配不到行（幽灵单）+ post_close TP_FILLED 恒空。
            seq_str = str(update.get("seq", ""))
            real = str(update.get("order_id", ""))
            if seq_str and real and real != seq_str:
                try:
                    n = _state_store.update_order_state_by_broker_oid(
                        seq_str, new_broker_oid=real)
                    if n == 0:
                        logger.warning(
                            "async_response 未命中 DB 行 seq=%s real=%s（可能 pre_open 未落库）",
                            seq_str, real)
                except Exception:
                    # 回填失败 = 撤单/对账锚点失效（可补偿：下次 order/trade 事件按 seq 反查）
                    logger.exception(
                        "async_response 回填 broker_oid 失败 seq=%s real=%s（CRITICAL：撤单锚点失效）",
                        seq_str, real)
            return
        if kind == "order":
            # #5 第二刀：柜台委托状态推送（含累计 traded_volume）→ 推进 DB order state。
            # 中间态（SUBMITTED）更新为同值 no-op；终态/部分态精确落库。
            self._advance_order_state_from_status(update)
            return
        if kind != "trade":
            return  # 仅处理成交回报（order/order_error 由风控层负责，不在本 handler 范围）
        symbol = update.get("stock_code", "")
        qty = update.get("traded_volume", 0)
        price = update.get("traded_price", 0.0)
        order_id = str(update.get("order_id", ""))
        if not symbol or qty <= 0:
            # 脏数据/撤单回报（traded_volume=0）不应落账或挂止盈，直接跳过
            return

        # 判定方向（BUY/SELL/None）——账本写入与挂止盈决策都依赖
        direction = self._order_direction(order_id)
        if direction is None:
            # #1：方向未知 = 审计黑洞（不挂止盈 + 不落账），必须叫醒人工对账，禁止静默。
            # Fix1（用户两轴 review · 告警模式闸）：dry_run 模式理论上无真实成交回报
            # （无真单），方向未知只会在脏 mock/测试数据中出现，推钉钉纯噪音。
            # live 模式才是真审计黑洞需叫醒人工。守卫与既有 _alert_critical 范式一致。
            if _mode() == "live":
                _alert_critical(
                    f"成交回报方向未知 order_id={order_id} symbol={symbol} qty={qty} "
                    f"（DB 无 side、内存无 order_type，需人工对账补账）")

        # C-6 V2：TP1 幂等 key（trade_date 口径）与账本 account/trade_id 同源计算一次。
        today_tp = clock.today()
        _account_id = _resolve_account_id()
        _trade_id = _state_store.build_trade_id(_account_id, symbol, today_tp)

        # spec §A1：direction=None 旁路补 trade_event(DIRECTION_UNKNOWN) 审计（Fix 3b，
        # 用户 final review 抓出）。原旁路只有 _alert_critical（仅 live 模式推钉钉），
        # dry_run 模式下方向未知回报在事件流里完全无痕迹 → 事后复盘无法对账（审计黑洞）。
        # trade_event 审计不受 _mode 守卫限制（任何模式都该留痕），与 _alert_critical
        # （live 才推钉钉的告警通道）解耦。UNIQUE(account_id, trade_id, action) 天然幂等，
        # 同 (order_id, traded_time) 重放不重复落行（与 fill 表去重同口径）。
        if direction is None:
            try:
                # 确保 account 行存在（trade_event FK 引用 account，与下方 BUY/SELL
                # 分支同范式——dry_run 影子期可能未预置 default 账户，缺失则 FK 失败）
                if _state_store.get_account(_account_id) is None:
                    _state_store.upsert_account(_account_id, broker="qmt")
                _state_store.insert_trade_event(
                    _account_id, _trade_id, symbol, "DIRECTION_UNKNOWN",
                    order_id=order_id, qty=float(qty) if qty else None,
                    price=float(price) if price else None,
                    meta=f"reason=direction_unknown|update={update.get('traded_time')}")
            except Exception:
                # 审计旁路软降级：不阻断 handler 主路径（与 _alert_critical 同范式，
                # DB 写失败不抛——审计缺失由日志告警供人工补对账）
                logger.exception(
                    "direction=None trade_event 审计失败 symbol=%s order_id=%s",
                    symbol, order_id)

        # ── d. 成交账本写入（真相源，最先做——先落账再挂止盈/落日志，防 crash 窗口账账不符）──
        # state-store-redesign §4.2 + W3.1（gateway-ssot-hardening）：
        #   state_store.insert_fill 是成交回报的**唯一幂等真相源**（UNIQUE(order_id, traded_time)）。
        #   CSV 审计镜像（record_live_trade kind=fill）+ 钉钉通知（notify_trade_event）+
        #   position 累加（apply_fill_to_position）+ FILLED 事件（insert_trade_event）
        #   必须与 insert_fill **同一判定点**——首次写入（inserted=True）才执行，重放（False）
        #   全部跳过。这是 08-04 事故（同笔成交重放 24 次致简报「买 24 笔」+ 钉钉轰炸）的根因修复：
        #   原实现把 CSV/钉钉放在 insert_fill 幂等判定**之外**无条件调，与真相源不同判定点 → 镜像失真。
        _fill_inserted = False  # 是否首次成功落 fill（重放=False 时跳过所有镜像写入）
        if direction in ("BUY", "SELL"):
            try:
                # 确保 account 行存在（fill/trade_event FK 引用 account）
                if _state_store.get_account(_account_id) is None:
                    _state_store.upsert_account(_account_id, broker="qmt")
                traded_time = str(update.get("traded_time", ""))
                _fill_inserted = _state_store.insert_fill(
                    order_id, _account_id, traded_time, symbol, direction,
                    float(qty), float(price), strategy="neckline")
                if _fill_inserted:
                    # insert_fill 首次入账才更新 position（避免重推重复累加）
                    _state_store.apply_fill_to_position(
                        _account_id, symbol, direction, float(qty), float(price), traded_time)
                    # FILLED 事件（W3.1：与真相源同判定点——首次 fill 才记 FILLED，
                    # 重放不再追加事件行，保证事件流与 fill 表 1:1 对齐）
                    _state_store.insert_trade_event(
                        _account_id, _trade_id, symbol, "FILLED",
                        order_id=order_id, qty=float(qty), price=float(price))
                    # SSoT Phase B · B2b：BUY 成交写持仓归因（接线 engine 成交路径）。
                    # 物理意图：原 record_position_attribution 全仓无生产调用方，归因散在
                    # trading_service 内存字典重启即丢。B2 在 apply_fill_to_position 后接线，
                    # 把 strategy/entry_rationale 落 position 表（与 qty/avg_price 同行）。
                    # 重启后归因随持仓行存活——「重启后归因不丢」验收数据来源。
                    # SELL 不调 clear：apply_fill_to_position 归零删 position 行（state_store.py
                    # DELETE WHERE qty=0），归因随行消失——clear 会 UPDATE 0 行（空操作）。
                    # 断点-3 Resolution：position 行删除即归因消失（非 clear 调用）。
                    # 风控红线：try/except 不阻断成交主路径（成交是交易红线，归因是审计，
                    # 失败可补偿——与上方 fill/position 异常升 L1 不同，归因异常软降级）。
                    if direction == "BUY":
                        try:
                            from presentation.server.services.trading_service import \
                                record_position_attribution
                            record_position_attribution(
                                symbol, "neckline", f"成交建仓@{traded_time}")
                        except Exception:
                            logger.exception(
                                "归因登记失败 symbol=%s traded_time=%s（不阻断成交主路径）",
                                symbol, traded_time)
                else:
                    # 重放（insert_fill 命中 UNIQUE 返 False）：CSV/钉钉/position 全部跳过。
                    # 物理意图：on_stock_trade 在部分成交/柜台重推时会重放同一 (order_id, traded_time)，
                    # 真相源已挡住重复入库，镜像（CSV/钉钉）必须同判定点同步挡住，否则审计旁路与
                    # 真相源漂移（08-04 事故 1 笔成交被记 24 次）。
                    logger.info(
                        "成交回报重复，跳过 CSV/钉钉/position（order_id=%s traded_time=%s）",
                        order_id, traded_time)
            except Exception as e:
                # #5/A5：C-4 分级——敞口真相失真 = L1 停调度（宁可停不可带病跑）。
                # 原软降级会让 fill/position 静默缺失，对账只能事后发现。
                logger.exception("成交回报落账失败 symbol=%s order_id=%s", symbol, order_id)
                raise _CriticalHalt(
                    f"成交回报落账失败 symbol={symbol} order_id={order_id}"
                    f"（fill/position 真相源失真）") from e

        # ── c. 买单成交 + 未挂止盈 → 挂限价止盈卖单（DB 幂等防重挂）──
        # 卖单成交（direction=="SELL"）无需挂止盈（卖出即离场，无持仓可止盈）。
        # 方向未知（None）保守不挂——宁可漏挂止盈让人工补，也不误把卖单当买单挂反方向单。
        # 注：TP 挂单的幂等独立于 fill（has_order(TP1) DB 查询），与 _fill_inserted 不耦合
        # （fill 重放时 TP 可能因 has_order 已 True 而跳过，但两套幂等各管各的真相源）。
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

        # ── a/b. 成交日志（CSV）+ 钉钉通知（W3.1：与 fill 真相源同判定点）──
        # 方向已知（BUY/SELL）：仅在 _fill_inserted=True（首次落账）时写 CSV + 推钉钉。
        #   重放（_fill_inserted=False）→ 完全跳过，保证 CSV/钉钉与 fill 表 1:1（08-04 事故根因）。
        # 方向未知（None）：W3 完整收口（用户两轴 review，spec §3.3.1「同一判定点」）——
        #   **不再写 CSV / 不再推钉钉**。Why：原 direction=None 旁路无条件写 CSV/推钉钉
        #   （"TRADE" 中性标签），与 insert_fill 不同判定点 —— 同一条「方向未知回报」被重放
        #   N 次会重复落 CSV/推钉钉，污染审计镜像与 IM 通知（重放不幂等）。W3 完整收口选 C
        #   （最干净 + 符合 spec）：direction=None 时 CSV/钉钉也不写，与 fill 表「direction
        #   不在 (BUY,SELL) 时 insert_fill 不被调（无 fill 表行）」同判定点（都不写）；
        #   告警由上方 _alert_critical 承担（人工对账线索），CSV 旁证在重放时反而污染真相
        #   源判定。direction is None 时不进入下方 if 块（CSV/钉钉双跳过）。
        if _fill_inserted:
            # SSoT Phase A · Task A1：原 record_live_trade CSV 审计块已删除（审计平移 trade_event，
            # fill 表本身已是真相源 + 上面 insert_trade_event FILLED 已记事件流，CSV 镜像冗余）。
            # 重放幂等性由 fill 表 UNIQUE(order_id, traded_time) + _fill_inserted 守卫共同保证，
            # 不再依赖 CSV 旁证。NotificationManager 钉钉通知保留（与 fill 表同判定点，首次才推）。
            try:
                from infra.notifier import NotificationManager, fire_and_forget
                fire_and_forget(NotificationManager.get_default().notify_trade_event(
                    symbol, direction or "TRADE", float(qty), float(price),
                ))
            except Exception:
                logger.exception("成交通知发送失败 symbol=%s", symbol)
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
        # #1 修复：方向反查 DB 优先（state_store.order.side，pre_open 已落库），
        # 内存 gw._orders.order_type 仅兜底（_sync_orders_if_stale 走 query_orders 时才有 order_type）。
        # 竞态兜底：DB 按 real 查 miss 时经 _seq_to_real 反查 seq 再查一次（async_response 晚到）。
        _row = None
        try:
            _row = _state_store.get_order_by_broker_oid(order_id)
            if _row is None:
                _seq = _seq_for_real_oid(self._gw, order_id)
                if _seq is not None:
                    _row = _state_store.get_order_by_broker_oid(str(_seq))
        except Exception:
            logger.exception("get_order_by_broker_oid 失败 order_id=%s（回退内存）", order_id)
        if _row is not None:
            side = str(_row.get("side") or "").lower()
            if side == "buy":
                return "BUY"
            if side == "sell":
                return "SELL"
            # DB 有行但 side 异常 → 继续走内存兜底，不轻易返 None
        orders = getattr(self._gw, "_orders", {}) if self._gw else {}
        rec = orders.get(order_id, {})
        try:
            from xtquant import xtconstant  # 与 broker/qmt.py:61 同源导入路径
            STOCK_BUY = xtconstant.STOCK_BUY
            STOCK_SELL = xtconstant.STOCK_SELL
        except ImportError:
            STOCK_BUY, STOCK_SELL = 23, 24  # CI/单测无 xtquant 兜底（与 conftest 同值）
        ot = rec.get("order_type")
        if ot == STOCK_BUY:
            return "BUY"
        if ot == STOCK_SELL:
            return "SELL"
        return None

    def _advance_order_state_from_status(self, update: Mapping[str, Any]) -> None:
        """kind=order：按柜台状态推进 DB order.state/filled_*（#5 第二刀）。

        Why 用 order 事件而非 trade 事件：order_status 55/56 区分 PARTIAL/FILLED，
        traded_volume 是累计成交（trade 是本笔增量），状态推进必须用累计量。
        竞态（async_response 晚到）：按 real 查 miss 时经 _seq_to_real 反查 seq 再匹配。
        """
        lookup = str(update.get("order_id", ""))
        if not lookup:
            return
        row = None
        try:
            row = _state_store.get_order_by_broker_oid(lookup)
            if row is None:
                seq = _seq_for_real_oid(self._gw, lookup)
                if seq is not None:
                    row = _state_store.get_order_by_broker_oid(str(seq))
        except Exception:
            logger.exception("get_order_by_broker_oid 失败 lookup=%s", lookup)
            return
        if row is None:
            logger.warning("order 事件未命中 DB 行 lookup=%s（可能 server 手动单/未落库）", lookup)
            return
        traded_volume = update.get("traded_volume")
        traded_price = update.get("traded_price")
        try:
            n = _state_store.update_order_state_by_broker_oid(
                row["broker_oid"] or lookup,
                state=_order_state_to_db(update.get("state")),
                filled_qty=float(traded_volume) if traded_volume is not None else None,
                filled_price=float(traded_price) if traded_price is not None else None,
            )
            if n == 0:
                logger.warning("order 状态推进未命中 broker_oid=%s（下个事件补推进）", row.get("broker_oid"))
        except Exception:
            logger.exception("order 状态推进失败 lookup=%s（软降级，下个事件补推进）", lookup)

    async def _place_take_profit(self, symbol: str, filled_qty: float,
                                 fill_price: float, order_id: str) -> None:
        """薄包装：成交回报链路调模块级 place_take_profit（#4 差额补挂）。"""
        return await place_take_profit(symbol, filled_qty, fill_price, order_id)
    @_critical_guard
    async def _pipeline_then_eod(self) -> None:
        """C-4 U2：pipeline_then_eod 收编为 method（过 _critical_guard）。

        Why 包装：pipeline_then_eod 是 orchestrate/pipeline.py 的外部函数（编排层，
        不该塞进 engine），但需要与其他四 job 同享 L1 停调度语义。包一层 method 让
        五 job 统一被 guard 装饰（满足验收标准 2），pipeline 内 raise _CriticalHalt
        经本 wrapper 捕获 → _halt。
        """
        from trading.orchestrate.pipeline import pipeline_then_eod
        await pipeline_then_eod(self)

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
            # Fix1（用户两轴 review · 告警模式闸）：dry_run 模式跑旧代码风险低（无真单），
            # 推钉钉纯噪音（开发/测试常跑旧代码做回归）；live 模式才是真隐患需叫醒人工。
            # 守卫与既有 _alert_critical 范式一致。logger.error 仍打（运维可见）。
            if _mode() == "live":
                _alert_critical(
                    "口径自检失败：next_trading_day 未算出次日（疑似跑旧代码），"
                    "已降级启动，请立即重启 engine 加载新代码并核查 next_trading_day 口径")
        self.sched.start()
        logger.warning("TradingEngine 已启动（mode=%s）——独立常驻进程运行", _mode())

    def shutdown(self) -> None:
        """优雅停机（wait=False：不等 pending job，进程退出场景）。"""
        try:
            self.sched.shutdown(wait=False)
        except Exception:
            # scheduler 未 start / 已 shutdown → 幂等忽略（锁释放不依赖调度器状态）。
            logger.debug("sched 未启动或已停机，跳过（幂等）", exc_info=True)
        _lock = getattr(self, "_instance_lock", None)
        if _lock is not None:
            _lock.release()
            self._instance_lock = None
        logger.info("TradingEngine 已停机")
