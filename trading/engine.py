# -*- coding: utf-8 -*-
"""二期自动交易引擎：APScheduler 四触发点编排 + 影子模式分流。

物理意图（四触发点的真实业务节奏 · 术语对齐 T 日盘后扫盘 → T+1 执行）：
  eod_plan  19:00 T 日盘后：扫颈线法信号 → build_orders → DB trade_event(SIGNAL)
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
              + 清动态白名单。
              ⚠️ trailing 盘后演进（Task 9·R-3）已删除（SSoT review P2 · 死计算）：
              C3 删写回 + C2c 切 _stoploss 读 DB SIGNAL.meta 后演进值无消费方。trailing 收紧
              作为独立 live P0 task 重实现（post_close 写 position.current_stop + _stoploss 读最新）。

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
- T1（缝合点 #1）：原模块级活跃引擎单例桥**已删除**。pre_open / post_close
  等 phases 函数改经 ``EnginePorts`` 窄接口显式接收 engine 实例特有依赖（盘前三段闸 gate
  + ``_dynamic_whitelist`` 注入/清空），由 engine cron wrapper / catchup 补跑注入
  ``self._ports``——依赖方向由隐式全局反查变为显式参数透传（为 T6-T8 phases 外迁铺路）。
  ``_submit``（现居 gateway_service，N5 后 engine 零副本）不读实例白名单（A-2 已删
  whitelist 挡板，直透 dry_run），故 ports 不承载它。
- server 路径的 ``submit_order`` **不传** ``whitelist``（默认 None），走旧路径
  ``_whitelist() = get_effective_whitelist()``（``_dynamic_whitelist`` 恒空 = 纯 env）
  ——server 行为与改造前完全一致（向后兼容红线）。
- 因此 engine 与 server 同进程**不再**造成前视污染：server 手动下单路径不会读到 engine
  注入的动态白名单（实例属性隔离双保险）。

⚠️ ``python -m trading``（``trading/__main__.py``）现仅为**开发/调试常驻入口**，不再是
唯一入口；生产路径在 uvicorn lifespan 内起 engine（详见 ``start_all.py``）。

============================================================================
影子模式（AUTO_TRADE_MODE=dry_run，默认）红线
============================================================================
- pre_open / stop_loss_monitor 走 ``gateway_service._submit`` → submit_order 的
  ``dry_run=(_mode()=="dry_run")`` 分流，命中即返 ``{"state":"DRY_RUN"}`` 不真下单。
- 未跑满 TRADE_SHADOW_MIN_DAYS（≥5）禁切 live 的告警由 ``trading/__main__.py``
  启动期处理（Task 10），本引擎内 ``_mode()`` 仅忠实读 env，不重复告警逻辑。
"""
from __future__ import annotations

import logging
import os
from dataclasses import replace as _dc_replace
from typing import Any, Mapping, Optional

# ============================================================================
# W1-B 销账（tech-debt 0815 · Task 10）：T1 拆分期的 re-export 兼容块已删。
# 物理意图：T1 把 critical/data_ctx/eod_plan/phases/order_state 集群拆出本模块后，曾在
# 此处维持 ~40 个 ``from trading.X import Y`` re-export 别名 + 旧 `_` 名双导出垫层（保
# ``from trading.engine import X`` / ``patch("trading.engine.X")`` 公共入口不变形）。W1-A
# 已把全部消费者（phases/order_state/orchestrate/catchup/tools/tests）迁物理真身，本块
# 收敛为【engine 自用】的直 import——engine 不再是任何集群符号的转发中枢。
# 行为零变更红线：自用绑定名与删前逐一同名（_mode/_alert_critical/_load_* 旧名/…），
# ``patch("trading.engine.<自用名>")`` 命中语义不变；仅删除「engine 内零引用、纯为他
# 人转发」的符号（_CriticalHalt/place_take_profit/_cancel_all_open_orders/decide_exit/
# build_orders_from_signals/reconcile_job/qmt_market_data/trading_plan 等）——其消费者
# 已直 import 物理真身，对应 patch 点同步迁至物理模块（详见 task-10-audit）。
# ============================================================================
# 网关服务（W1-B lazy 顶部化 · 模块对象风格）：get_gateway/get_positions 调用点
# 经 ``gateway_service.<attr>`` 属性访问——**禁 from-import 本地绑定**（``patch(
# "trading.gateway_service.X")`` 改的是模块属性，from 绑定在 import 期冻结不跟随），
# 模块对象风格保 monkeypatch 命中（与 _state_store/job_ledger 同范式）。无循环依赖：
# gateway_service 顶层零 engine 反查（broker.base 仅进 compute.types/types 包）。
from trading import calendar, clock, gateway_service
from trading import position_book as _position_book
from trading import state_store as _state_store
# Task 8（C-2 S3 pre_open 三段式 gate）：load_plan/get_ready 独立 import 让
# ``patch("trading.engine.load_plan")`` / ``patch("trading.engine.get_ready")`` 命中
# （_pre_open_gate ①③ 段经 engine 模块全局名解析；其它调用方走各自命名空间，互不污染）。
from trading.trading_plan import load_plan
from trading.state_store import get_ready
from trading.ports import EnginePorts  # T1 缝合点 #1：phases 外迁函数的窄依赖接口（无循环：ports 仅依赖 stdlib + alerting）
from trading.alerting import QuoteBlackoutThrottle  # W1-A/T2：行情黑屏节流状态机（注入 ports.blackout，无循环：alerting 仅依赖 stdlib）
# 集群 A（trading.critical · L1 停调度 + 告警基础设施）：engine 自用直 import（W1-B 删
# re-export 垫层，绑定名不变 → patch("trading.engine._mode"/"_alert_critical"/
# "_critical_guard"/"_trade_cfg") 命中语义不变）。halt/guard_skip_rounds 别名驱动下方
# _halt/_guard_skip_rounds 薄实例 wrapper（「critical 留纯函数 + engine 留薄 wrapper」，
# 避免 critical → engine 耦合）。_CriticalHalt 不在此：engine 内零引用，raise/except 方
# （orchestrate.pipeline / phases / tests）均已直 import trading.critical。
from trading.critical import _alert_critical, _critical_guard, _mode, _trade_cfg
from trading.critical import halt as _halt_logic, guard_skip_rounds as _gskip_fn
# 集群 I（trading.order_state · broker 订单回调三分支 + 状态推进 · T1-Task3 迁出）：
# 下方三个薄实例 wrapper（_handle_order_update/_order_direction/
# _advance_order_state_from_status）委托用（bootstrap set_order_update_callback 绑实例）。
from trading.order_state import (
    advance_order_state_from_status,
    handle_order_update,
    order_direction,
)
# 集群 B（trading.data_ctx · lake 数据加载 helper · T1-Task4 迁出）：_eod 调用点沿用
# 旧 `_` 名——W1-B 由「新名 import + 旧名赋值别名」双导出收敛为 as 别名直绑，绑定名
# 不变 → ``patch("trading.engine._load_*"/"_resolve_*")`` 命中语义不变。plan_data_keys
# 供 TradingEngine._plan_data_keys 薄实例 wrapper 调用（保类方法 patch 点）。
from trading.data_ctx import (
    load_df_upto as _load_df_upto,
    load_integrity_ctx as _load_integrity_ctx,
    load_recent_plan_symbols as _load_recent_plan_symbols,
    load_universe as _load_universe,
    plan_data_keys,
    resolve_cooldown_days as _resolve_cooldown_days,
    resolve_id_window as _resolve_id_window,
)
# 集群 D（trading.eod_plan · 盘后计划生成 · T1-Task5 迁出）：compute 真身以 ``eod_plan``
# 旧名绑定（_eod wrapper 调用点 + ``patch("trading.engine.eod_plan")`` 命中不变；命名
# compute 避免「模块名 = 函数名」歧义）。sanity_check_date_alignment 供启动口径自检薄
# wrapper（_sanity_check_date_alignment）调用。
from trading.eod_plan import compute as eod_plan, sanity_check_date_alignment
# 集群 E（trading.phases.pre_open · 盘前挂单 · T1-Task6 迁出）：engine cron wrapper
# （_pre_open）转调用真身（``patch("trading.engine.pre_open")`` 命中 wrapper 调用点不变）。
# _pre_open_impl 不在此：engine 内零引用，消费者（catchup 补跑语义测试等）已直 import
# phases.pre_open。
from trading.phases.pre_open import pre_open
# 集群 F（trading.phases.stop_loss · 盘中止损/超时巡检 · T1-Task7 迁出）：engine cron
# wrapper（_stoploss）转调用真身（``patch("trading.engine.stop_loss_monitor")`` 命中
# wrapper 调用点不变）。scan/close_expired_positions 及旧 `_` 名不在此：engine 内零引用，
# 消费者（pre_open 超期平仓 / tests）已直 import phases.stop_loss。
from trading.phases.stop_loss import stop_loss_monitor
from trading.stop_loss_context import StopLossContext
# 集群 G（trading.phases.post_close · 盘后对账 + 日内熔断 · T1-Task8 迁出）：engine cron
# wrapper（_post_close）转调用真身。seq_for_real_oid/order_state_to_db 及旧 `_` 名不在
# 此：engine 内零引用，消费者（order_state）已直 import phases.post_close。
from trading.phases.post_close import post_close
# holding_days 交易日口径（_stoploss 构造 monitor_ctx 用）。should_trigger_stop/decide_exit/
# ExitAction/ExitReason、place_take_profit、check_daily_loss_limit、cancel_all_open_orders、
# build_orders_from_signals 等止损/止盈/熔断/撤单/建单真身的消费者在 phases/compute/
# order_state/eod_plan（W1-A/T2 已切直 import），engine 零内部引用不再转发（清单见
# task-10-audit）。
from trading.compute.stop import trading_days_between as _trading_days_between
# C-6 V2：单一时间源口子（clock 已并入上方 ``from trading import`` 行）。三函数分工：
#   clock.today()       = 业务日期 key（load_plan/is_trading_day/熔断基线 date）
#   clock.trading_day() = eod 落盘 key（=next_trading_day(today)，eod 专用，禁混用）
#   clock.now()         = 事件时间戳（submitted_at/written_at/is_intraday_session 时点）
# 触发点入口缓存（_eod/_pre_open/_stoploss/_post_close 入口算一次）防同轮跨午夜漂移。
# _alert_critical 定义在 trading.critical（上方直 import，防 patch 断链说明见其模块）；
# 本模块仅保 logger 供自用。
logger = logging.getLogger(__name__)


# （T1-Task2）_alert_critical / _CriticalHalt / _critical_guard 已迁 trading.critical.py
# （集群 A · L1 停调度 + 告警基础设施），见文件顶部 re-export。下方原模块级定义已删，
# 保 logger 供本模块其余路径用；行为逐行等价（critical 持纯逻辑，engine 经 re-export 复用）。


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


# T1（缝合点 #1）：原模块级活跃引擎单例桥已删除——pre_open / post_close 等
# phases 函数改经 ``EnginePorts``（``TradingEngine._ports``）显式接收实例特有依赖。外部裸调
# （catchup 补跑 / 单测）传 ``ports=self._ports``；未传 ports 时走 None 防御分支（跳过 gate /
# 回退模块级 dynamic_whitelist），与原单例为 None 的防御分支行为逐行等价。

# W1-A/T2（模块级可变状态收口红线）：原 R2 降级告警节流模块级可变状态已迁
# ``trading.alerting.QuoteBlackoutThrottle`` dataclass，经 ``EnginePorts.blackout`` 注入
# stop_loss_monitor（生产主路径走 ``ports.blackout.fire_if_due`` 原子方法）。原两行：
#     _last_quote_blackout_alert_ts: float = 0.0
#     _QUOTE_BLACKOUT_ALERT_INTERVAL_S = 30 * 60
# 已删——依赖方向由隐式 engine 反查变为显式 ports 透传，节流语义逐字等价（30min 窗口 +
# last_ts=0.0 初值）。grep ``_last_quote_blackout_alert_ts`` / ``_QUOTE_BLACKOUT_ALERT_INTERVAL_S``
# 在 trading/ 下应 0 命中（新 home 在 trading/alerting.py 用 last_ts/interval 字段）。

# ② 告警通道节流阈值（W0/D1 Task 7 · spec §3.3.5 ②）：
# filter_universe_by_continuity 在 _eod exp 循环内被调（per-exp window 可能不同），
# 被过滤标的数 ≥ 此阈值 → _alert_critical 推 CRITICAL 钉钉（残数据有声）。
# Why 阈值而非零容忍：单标的偶发漏采（停牌复牌边界）由 data/integrity 内 _log.warning
# 兜底即可，推钉钉只污染运营群致研究员麻木；阈值 ≥5 才告警确保只在 lake 大面积漏采
# （universe 显著收窄、信号锐减）时触达操作员——这是真"残数据"风险，非偶发噪声。
_CONTINUITY_FILTER_ALERT_FLOOR = 5


def _resolve_account_id() -> str:
    """解析当前账户 ID（canonical 委托 trading.account.resolve_account_id）。

    物理意图：engine 落 trade_event/order 需要归属账户。优先读 .env QMT_ACCOUNT_ID（启动期
    _migrate_env_to_account 已落库），缺失（dry_run 无 broker 配置）时用 state_store 默认账户。

    H3/T2 收口（2026-08-12）：实现下沉到 trading/account.resolve_account_id（单一真相源，
    eod_plan/veto/gateway_service 改 import 它，消四处复制）。本函数名保留——W1-A/T2-Task5
    已切断 phases 历史 engine 反查，phases/order_state 改顶部直接 import trading.account 真身；
    engine 侧保 ``_resolve_account_id`` 名让 ``patch("trading.engine._resolve_account_id")`` 兼容
    （Task 8-19 迁 patch 至 trading.account 物理路径后可删）。
    """
    from trading.account import resolve_account_id
    return resolve_account_id()



def get_gateway():
    """取交易网关单例（透传 gateway_service.get_gateway · W1-B lazy 顶部化）。

    Why 透传不重造：网关单例的装配（QMT 唯一，无凭证→None）与懒构造策略已在
    gateway_service.get_gateway 固化，本引擎薄编排不重复，避免双单例漂移。
    本函数独立出来便于测试 monkeypatch（engine.get_gateway）隔离真实网关副作用。

    W1-B 注记：原函数体内 lazy import 改顶部 ``from trading import gateway_service`` +
    调用点属性访问（模块对象风格）——from-import 本地绑定会在 import 期冻结，patch(
    "trading.gateway_service.get_gateway") 不跟随；属性访问在调用时读模块属性，patch
    语义与 lazy import 完全等价（gateway_service 顶层零 engine 反查，无循环依赖）。
    """
    return gateway_service.get_gateway()


# ============================================================================
# N5（debt/new-debt-0816 · Low ②）：模块级 ``_submit`` wrapper 已删除。
# W1-A/T2-Task7 把 phases 的下单入口下沉到 ``trading.gateway_service._submit`` 后，
# 本模块这份副本零生产调用者（phases 顶部 ``from trading.gateway_service import _submit``
# 直取真身，不反向经 engine）——留档只养出「patch("trading.engine._submit") 静默孤儿」
# 一类假锚（test_engine 超期平仓用例与 e2e probabilistic_broker 均曾 patch 此死名）。
# 测试拦截点已迁：phases 调用方模块（trading.phases.pre_open/stop_loss/exit 的 _submit）。
# ============================================================================


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
            30s 巡检必须用 interval。时段约束（09:15-15:00 连续，D2 修订）下放给
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
        # 下放给 ``stop_loss_monitor`` 内 ``calendar.is_intraday_session``
        # （09:15-15:00 连续，D2 修订）——trigger 全天每 30s 触发，非盘中由 monitor 内
        # no-op 兜底。
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
        # T13-B #5 调度：每周全扫 cron（周期 backstop，防日级 pipeline scan 漏网的全历史缺口）。
        # 周六 02:00（周末零实盘 contention）。CronTrigger day_of_week 用名字 'sat' 避数字歧义
        #（engine.py:288 教训：APScheduler 0=周一非周日）。FAIL 只告警+写报告，不自动 repair
        #（全历史补采量大，近期缺口已由日级 pipeline scan 自动 repair）。
        self.sched.add_job(
            self._weekly_scan, CronTrigger.from_crontab(
                os.getenv("ENGINE_WEEKLY_SCAN_CRON", "0 2 * * sat")),
            id="weekly_scan",
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

        # T1（缝合点 #1）：构造 EnginePorts 窄接口，替代原模块级活跃引擎单例桥。
        # 物理意图：phases 外迁（T6-T8）后模块级 pre_open/post_close 无法再反查 engine 实例，
        # 经本 ports 显式注入「实例特有」依赖（gate + 动态白名单注入/清空）。
        # W2 订正（T1 期口径已过时）：state_store/gateway 自 W2-H2 起已入 Ports——但仅服务
        # order_state 回调体（fill/trade_event 落账 + gw._orders/_seq_to_real 实例态反查）；
        # lake 仍走模块级 import，不进 Ports（窄接口红线不放大）。
        # ⚠️ 必须在 _dynamic_whitelist 初始化（上方 set()）之后构造——whitelist_add/clear 绑定它。
        # gate 用延迟 lambda：调用时才解析 self._pre_open_gate，支持测试期 monkeypatch 实例
        # 方法（eng._pre_open_gate = AsyncMock(...)）与未来子类 override；生产路径 eng 永不
        # 重赋该属性，故 lambda 与直接绑 bound method 行为逐行等价。
        self._ports = EnginePorts(
            gate=lambda d, gw: self._pre_open_gate(d, gw),
            whitelist_add=self._dynamic_whitelist.update,   # set.update 原地并集，对齐原 ``|=``
            whitelist_clear=self._dynamic_whitelist.clear,  # set.clear 原地清空，对齐原 ``.clear()``
            # W1-A/T2：行情黑屏 30min 节流告警状态机（原模块级 _last_quote_blackout_alert_ts
            # + _QUOTE_BLACKOUT_ALERT_INTERVAL_S 收口）。stop_loss_monitor 经
            # ports.blackout.fire_if_due 原子读写（单一 Lock 内 check+mark）；默认 last_ts=0.0 + interval=1800.0
            # 等价原模块级初值 + 常量（行为零变更）。显式构造留实例化点便于未来注入测试替身。
            blackout=QuoteBlackoutThrottle(),
        )

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

        物理意图（spec S3 前置 gate 最便宜先做；原 ④ regime 段已按 ADR-16 移除——
        择时判断权归人工，增量拦截由 risk_control 双值接管 · 2026-08-17）：
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

        T1-Task4 外迁注记：逻辑已迁 free function ``trading.data_ctx.plan_data_keys(plan)``
        （经验证原实现不读 self 状态，仅 plan 参数 + 模块级 resolve_active/build_strategy），
        本方法现为**薄实例 wrapper** 调用之。Why 保留实例方法（而非直接改 free function 调用）：
            ``test_engine_pre_open_gate`` 既 ``patch("trading.engine.TradingEngine._plan_data_keys")``
            又 ``eng._plan_data_keys(plan)`` 直调——保留实例方法签名让两类测试命中点不变
            （行为等价红线 + 测试零改动）。详细物理意图 / formed_at 锚点说明见
            ``data_ctx.plan_data_keys`` docstring。
        """
        return plan_data_keys(plan)

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
        # B3：QUANTER_TESTING=1 时跳过 session 锁（pytest 多实例并行不抢生产锁）。
        # Why 只 testing 跳过：测试进程不是真实引擎，acquire 会写 pid 文件/占锁，
        # 干扰同机生产实例的三合一校验（supervisor 会看到「锁被持有但端口无监听」漂移）。
        if _mode() == "live" and os.getenv("QUANTER_TESTING") != "1":
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
        # L3（spec §4.4 · 裁定 L3 · M2 单 SSoT）：启动期把实际 sid 回写 account 行——
        # DB account.session_id 是 actual_sid 唯一真相源（supervisor/ops 经
        # state_store.get_session_id 读）。Why set_session_id 而非 upsert_account：
        # UPSERT 全列 UPDATE 会把上方 _migrate_env_to_account 刚落库的 mode/userdata_path
        # 重置回默认（旧实现的隐性 clobber）；列级 UPDATE 只动 session_id。
        # Why 不阻断：DB 回写失败只是观测缺值，连接本身已成功（json 快照仍在）。
        _actual_sid = getattr(self._gw, "_session_id", None)
        if _actual_sid is not None:
            try:
                _state_store.set_session_id(_resolve_account_id(), int(_actual_sid))
            except Exception:
                logger.exception("L3 回写 account.session_id 失败（不阻断）")
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
    async def _weekly_scan(self) -> None:
        """T13-B #5：每周全市场完整性扫描（周期 backstop，周六 02:00 cron）。

        物理意图：日级 pipeline scan 覆盖近期缺口并自动 repair；全历史缺口由本周扫兜底
        （防日级漏网）。FAIL 只告警 + 写报告 logs/integrity_weekly.json（人工确认后补；
        不自动 repair——全历史补采量大，避免周末撞限频）。
        """
        try:
            from pathlib import Path as _Path
            from data.tools.scan_integrity import scan as _scan_integrity
            _root = _Path(__file__).resolve().parents[1]
            _report = _scan_integrity(lake_dir=str(_root / "data_lake"))
            _n = _report.get("unjustified_gaps", 0)
            if _n > 0:
                import json as _json
                _out = _root / "logs" / "integrity_weekly.json"
                _out.parent.mkdir(parents=True, exist_ok=True)
                _out.write_text(_json.dumps(_report, ensure_ascii=False, indent=2), encoding="utf-8")
                logger.warning("每周全扫发现 %d 段漏采 → %s（人工确认后 repair_gaps --report 补）",
                               _n, _out)
            else:
                logger.info("每周全扫 PASS：无漏采")
        except Exception:
            logger.exception("每周全扫异常（不阻断）")

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
            # T9 主动探针（option 2 防御性实现）：_connected=True 但客户端可能僵死（重启中/
            # 假死，on_disconnected 不触发的盲区）。探针 query_account_status 连续 N 次失败
            # → 判僵死，置 _connected=False 强制下轮走 ④/⑥ 重连（不 no-op 放任废单）。
            # N 由 env T9_PROBE_FAIL_THRESHOLD 配置（默认 3，待模拟盘 CSV 实证微调）。
            if hasattr(gw, "probe_account_status"):
                _threshold = int(os.getenv("T9_PROBE_FAIL_THRESHOLD", "3"))
                probe_ok, probe_detail = await gw.probe_account_status()
                if not probe_ok:
                    _fails = getattr(self, "_probe_fail_count", 0) + 1
                    self._probe_fail_count = _fails
                    if _fails >= _threshold:
                        logger.warning(
                            "T9 探针连续 %d 次失败判客户端僵死（%s）→ 置 _connected=False 走重连",
                            _fails, probe_detail)
                        self._probe_fail_count = 0
                        gw._connected = False  # 强制走出②，下轮 _reconnecting=False 时走 ④/⑥ 重连
                        if _mode() == "live":
                            _alert_critical(
                                f"T9 探针判客户端僵死（连续失败 {_threshold} 次，{probe_detail}），"
                                f"已置 _connected=False 触发 health_guard 重连")
                    else:
                        logger.warning("T9 探针失败 %d/%d（%s）—— 暂不判僵死，下轮再探",
                                       _fails, _threshold, probe_detail)
                    return  # 探针未达稳态：不 no-op（不清 _guard_fail_count）也不重连，等下轮再探
                # 探针成功 → 连接真活着，清探针失败计数
                if getattr(self, "_probe_fail_count", 0):
                    logger.info("T9 探针恢复成功（清前连续失败 %d 次）", self._probe_fail_count)
                    self._probe_fail_count = 0
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
                _caught, _note = await _catchup_pre_open(ports=self._ports)
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
        """L1 统一停调度薄 wrapper（T1-Task2 · 行为零变更）：注入 engine 副作用闭包给 critical.halt。

        物理意图（spec §5 双层保障 · 与原 _halt 逐行等价）：
            sched.shutdown 停「新触发」+ _halted flag 防「in-flight job 继续写」。
            幂等：已 _halted 时直接返回（多路径同时致命不重复 shutdown/alert）。

        T1 缝合点（「critical 留纯函数 + engine 留薄 wrapper」红线）：
            纯顺序契约（幂等检查 → mark_halted → alert → shutdown）在 critical.halt；
            engine 侧仅注入四个副作用闭包（is_halted / mark_halted / shutdown / alert），
            critical 不反向依赖 TradingEngine 类（无 import 级耦合）。

        Why alert=_alert_critical（engine 模块全局名解析）：
            保 ``patch("trading.engine._alert_critical")`` 命中——_alert_critical 在调用时
            经 engine 模块全局名解析（re-export 自 critical），patch 替换该属性即被本 wrapper
            捕获，既有测试断言路径（test_critical_guard / test_engine_alerts 等）不变。
        """
        _halt_logic(
            msg,
            is_halted=lambda: self._halted,
            mark_halted=lambda: setattr(self, "_halted", True),
            shutdown=lambda: self.sched.shutdown(wait=False),
            alert=_alert_critical,
        )

    @staticmethod
    def _guard_skip_rounds(fail_count: int) -> int:
        """失败次数→跳过轮数（指数退避近似，60s/轮）薄 wrapper。

        T1-Task2：纯映射已迁 critical.guard_skip_rounds；此处保 ``TradingEngine._guard_skip_rounds(n)``
        类方法调用路径不变（test_qmt_health_guard.test_guard_skip_rounds_mapping 直接以类名调）。
        """
        return _gskip_fn(fail_count)

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

        T1-Task5：逻辑已迁 trading.eod_plan.sanity_check_date_alignment free function
        （原方法不读 self → free function），本处留薄实例 wrapper 调之，保
        ``eng._sanity_check_date_alignment()``（test_engine_sanity_check /
        test_e2e_trading_flow）+ ``self._sanity_check_date_alignment()``（start()）调用命中。
        """
        return sanity_check_date_alignment(today)

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
        # ② 告警通道节流局部变量（W0/D1 Task 7 · spec §3.3.5 ②）：filter 在下方 exp 循环
        # 内被调，若每个 exp 达阈值都告警 → N 个 exp 推 N 条钉钉（风暴）。此局部变量保
        # 每次 _eod 至多告警一次。Why 局部变量非模块级：_eod 每天 19:00 一次天然不风暴，
        # 模块级可变状态会加重 W1-A 收口负担（参考 _last_quote_blackout_alert_ts 已是负担）。
        _continuity_alerted_this_eod = False
        # 逐实验 × 逐 symbol 扫信号；单 symbol scan_live 异常仅 warn 跳过，不炸整批
        for exp in experiments:
            strategy = build_strategy(exp.strategy_name, cfg_override=exp.params)
            # filter universe（per-experiment window 可能不同，故在 exp 循环内 filter）。
            # 颈线法 id_cfg["window"] 对齐原 scan_live:232 的 df_upto.tail(self.id_cfg["window"])。
            id_window = _resolve_id_window(strategy)
            clean_universe = filter_universe_by_continuity(
                list(df_map.keys()), df_map, id_window, susp, trade_days)
            # ② 告警通道（2026-08-12）：被过滤标的数 ≥ 阈值 → CRITICAL 钉钉（残数据有声）。
            # Why 在 engine 调用方而非 data/integrity：data 层保持纯净（不 import infra.notifier），
            # 告警统一走 _alert_critical（复用 infra.notifier 通道，与 _health_guard / pre_open
            # gate 同链）。data/integrity.py 内 _log.warning 保留作为运维日志兜底（不删）。
            # 节流：局部变量 _continuity_alerted_this_eod 保 _eod 至多告警一次（防 N exp 风暴）。
            dropped = len(df_map) - len(clean_universe)
            if dropped >= _CONTINUITY_FILTER_ALERT_FLOOR and not _continuity_alerted_this_eod:
                _alert_critical(
                    f"完整性 gate 过滤 {dropped}/{len(df_map)} 标的（窗口含未解释漏采），"
                    f"universe 收窄——疑 lake 漏采，查 scan/repair 闭环"
                )
                _continuity_alerted_this_eod = True
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
            # W1-B lazy 顶部化：gateway_service 已顶部 import（模块对象风格，见文件顶部注记），
            # get_positions 经属性访问调用时解析——patch("trading.gateway_service.get_positions")
            # 命中语义与原函数内 lazy import 完全等价。notifier 是纯第三方通道装配，留函数内
            # 局部 import 保持「引擎薄编排」（与 _eod 内 experiment/strategies 同口径）。
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

            positions = await gateway_service.get_positions()

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
        await pre_open(today, ports=self._ports)

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
        # SSoT Phase C · C2c：_stoploss 直接读 DB trade_event(SIGNAL).meta（真相源），
        # 不再依赖 plan_*.json。每 SIGNAL meta 是 per-symbol 计划参数快照（stop_price/
        # take_profit/neckline/atr/cancel_on/tp1 等），由 eod_plan 落盘（engine.py:643）。
        # 致命日期轴：按 substr(trade_id,-10)=today 查（非 timestamp）。
        signals = _state_store.list_signals_with_meta_by_plan_date(today)
        # ── Task 9（U6 执行统一）：构造 monitor_ctx（state+cfg）+ pending_ctx（cancel_on）+ stop_prices（D12 fallback）──
        # 三 map 均从同一张 confirmed 计划 SIGNAL meta 派生（单源一致）：
        #   - stop_prices：{sym: stop_price}（D12 fallback 基准，decide_exit 异常时兜底比价）；
        #   - monitor_ctx：{sym: {state, cfg}}（主路径 decide_exit 输入）；
        #   - pending_ctx：{sym: cancel_on}（D11 pending 期撤单阈值）。
        stop_prices: dict[str, float] = {}
        monitor_ctx: dict[str, dict] = {}
        pending_ctx: dict[str, float] = {}
        # 仅在 confirmed SIGNAL 下抽取（CONFIRMED 是人审闸——研究员未确认就不监控止损，
        # 避免研究员明确否决的计划仍触发卖出，破坏人审语义）。逐 SIGNAL 校验 latest_action
        # ∈ 已确认集合（VETOED/SIGNAL-only 跳过，per-trade 精细化）。
        # **ssot-review P1 fix**：原严格 ==CONFIRMED 在挂单后失效（pre_open 写 ORDERED，
        # event_id > CONFIRMED，latest=ORDERED）→ 止损监控静默失效（live 红线）。
        # 改用 is_trade_confirmed 单点（CONFIRMED + ORDERED/FILLED/CLOSED/TP_FILLED 视作
        # 已确认，与 trading_plan.load_plan:95 语义对齐）。
        _aid_sl = _resolve_account_id()
        # 过滤已确认 SIGNAL（per-trade confirmed gate · is_trade_confirmed 单点）
        confirmed_signals = []
        for sig in signals:
            _tid_sl = _state_store.build_trade_id(_aid_sl, sig["symbol"], today)
            if _state_store.is_trade_confirmed(_tid_sl):
                confirmed_signals.append(sig)
        # entry_dates / avg_prices 来自 position_book（持仓账本，holding_days + entry 基准）
        try:
            entry_dates = _position_book.get_entry_dates()
        except Exception:
            # 账本读失败软降级 → holding_days 全 0（等同 base_stop，不崩）
            logger.warning("_stoploss 读 entry_dates 失败（holding_days 降级 0）", exc_info=True)
            entry_dates = {}
        cfg_trade = _trade_cfg()
        # decide_exit 静态 cfg 基线（env 源 · 整个持有期不变 · resolution 7）：键对齐
        # simulate_exit cfg（backtest.py:177-183）+ decide_exit 契约（execution.py:155-161）。
        # 2026-08-17 单源收敛：此为 **缺 exec_params 键时的 fallback 基线**——新 SIGNAL
        # meta 带 exec_params（实验口径定终身），_decide_cfg_for 逐单覆盖。
        _base_decide_cfg = {
            "stop_atr_mult": cfg_trade["stop_atr_mult"],
            "trailing_grace": cfg_trade.get("grace", 0) or 0,
            "trailing_step": cfg_trade.get("step", 0.0) or 0.0,
            "trailing_floor": cfg_trade.get("floor"),
            "tp1_portion": cfg_trade.get("tp1_portion", 0.5),
            "max_holding": cfg_trade.get("max_holding", 15),
        }

        def _decide_cfg_for(o: dict) -> dict:
            """per-signal decide_cfg：SIGNAL.meta.exec_params（实验快照）> env 基线。

            消灭双源分叉：巡检侧曾全量用 env（trailing 5/0.1/0.5 vs 实验 0、
            max_holding 15 vs 20、tp1_portion 0.5 vs 0.3）。老 SIGNAL（无 exec_params
            键）零变化走 env 基线（向后兼容）。
            """
            ep = o.get("exec_params")
            if isinstance(ep, dict):
                return {
                    "stop_atr_mult": ep.get("stop_atr_mult", _base_decide_cfg["stop_atr_mult"]),
                    "trailing_grace": ep.get("trailing_grace", _base_decide_cfg["trailing_grace"]),
                    "trailing_step": ep.get("trailing_step", _base_decide_cfg["trailing_step"]),
                    "trailing_floor": ep.get("trailing_floor", _base_decide_cfg["trailing_floor"]),
                    "tp1_portion": ep.get("tp1_portion", _base_decide_cfg["tp1_portion"]),
                    "max_holding": int(ep.get("max_holding", _base_decide_cfg["max_holding"])),
                }
            return dict(_base_decide_cfg)

        def _inject_signal_meta(o: dict, *, with_pending: bool = True) -> None:
            """SIGNAL meta → 三 map（stop_prices/monitor_ctx/pending_ctx）装配单源。

            今日计划路径与持仓反查路径共用（防两处构造漂移）。with_pending=False
            用于持仓反查：cancel_on 是入场时的挂单撤单阈值，今日并无该 pending 单，
            塞进 pending_ctx 语义错位（虽然 monitor 查不到可撤单也匹配不上，仍不塞）。
            """
            sym = (o.get("order") or {}).get("symbol") or o.get("symbol")
            sp = o.get("stop_price")
            # 双重防御：symbol 缺失或 stop_price 非数（NaN/None）一律跳过——
            # stop_prices 的每一项都必须是「能拿来比价」的合法 (sym, price) 对。
            if not sym:
                return
            if sp is not None:
                stop_prices[sym] = sp
            cfg = _decide_cfg_for(o)
            max_holding = cfg["max_holding"]
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
                    "cfg": cfg,
                }
            # ── 构造 pending_ctx[sym]（D11 pending 期撤单 · resolution 4）──
            # cancel_on = 颈线 + cancel_thresh_mult×H（build_orders 落盘，对齐 simulate_exit:128）。
            # plan orders 里 cancel_on 字段（Task 9 新增落盘，见 _eod order_dicts）；
            # 老 plan 无此字段 → 不塞 pending_ctx（None=不撤单放飞，向后兼容）。
            if with_pending:
                cancel_on = o.get("cancel_on")
                if cancel_on is not None:
                    pending_ctx[sym] = float(cancel_on)

        for o in confirmed_signals:
            _inject_signal_meta(o)

        # ── 持仓反查（存量止损裸奔修复 · 2026-08-17）──
        # 物理根因：cooldown=8 期内 eod 不重产同标的信号 → 持仓成交次日起不在今日
        # SIGNAL 集里 → monitor_ctx 缺该 symbol → monitor「无止损价」continue → 盘中
        # 个股止损/超时强平对存量持仓完全不生效（只剩预挂 TP 单 + 组合熔断 + pre_open
        # 超期兜底）。补：按当前真实持仓反查各自最新 SIGNAL meta（exec_params 定终身，
        # 今日 SIGNAL 已覆盖的 sym 不重查不覆盖）。
        try:
            _pos_raw_sl = (await gw.query_positions()
                           if (gw is not None and hasattr(gw, "query_positions")) else {})
        except Exception:
            # 反查失败不炸本轮（今日计划路径不受影响）——裸奔风险由下一轮 30s 重试收敛；
            # 持续失败由 gw 健康闸/行情黑屏告警兜底。
            logger.exception("_stoploss 持仓反查 query_positions 失败（本轮仅今日计划覆盖）")
            _pos_raw_sl = {}
        _missing_syms: set[str] = set()
        for _hsym, _hpos in (_pos_raw_sl or {}).items():
            _hvol = _hpos.get("volume") if isinstance(_hpos, dict) else _hpos
            if (not _hsym) or not _hvol or _hvol <= 0:
                continue
            if _hsym in monitor_ctx or _hsym in stop_prices:
                continue   # 今日 SIGNAL 已覆盖（新计划优先）
            _missing_syms.add(_hsym)
        if _missing_syms:
            _before = set(monitor_ctx)
            for sig in _state_store.list_active_holding_signals(
                    _aid_sl, sorted(_missing_syms)):
                _inject_signal_meta(sig, with_pending=False)
            logger.info("_stoploss 持仓反查注入 %d 个存量持仓的止损监控（候选 %d）",
                        len(set(monitor_ctx) - _before), len(_missing_syms))

        # M2 StopLossContext 收口（2026-08-15）：三 map 装箱单参传递——「空 dict → None」
        # 归一移至 monitor 解包处（语义等价，见 stop_loss_monitor 体内 M2 注释）。
        # W1-A/T2：传 ports=self._ports 注入行情黑屏节流状态机（QuoteBlackoutThrottle 经
        # ports.blackout）。stop_loss_monitor 内 ports=None 守卫下跳过 blackout 告警分支，
        # 生产路径总传 self._ports 不受影响。
        await stop_loss_monitor(
            StopLossContext(stop_prices=stop_prices, monitor_ctx=monitor_ctx,
                            pending_ctx=pending_ctx),
            ports=self._ports,
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
        await post_close(today, local_positions=local_positions, ports=self._ports)

    # ----- 成交回报 handler（Task 10 · 修 G5：成交回调链路）-----
    # T1 Task 3 缝合点 #2：_handle_order_update / _order_direction /
    # _advance_order_state_from_status 已迁 trading.order_state.py（集群 I），此处留
    # 薄 wrapper 保 ``eng._handle_order_update`` 等既有调用路径（bootstrap
    # set_order_update_callback 绑实例；止盈 patch 点 W1-B 起在 trading.phases.exit）。
    # W2-H2（回调体 Ports 化）：委托时传 ``self._ports``（副作用依赖 state_store 显式
    # 注入）；gateway 调用时快照对齐——ports 在 __init__ 构造时 self._gw 尚为 None
    # （bootstrap :612 装配网关后才赋值），且 e2e orchestrator / 单测存在直改
    # ``eng._gw`` 的既有模式，故每次回调从 self._gw 快照同步（事件循环单线程无竞态，
    # 兼容全部赋值路径）。逐行原样委托，零行为变更（幂等红线零容忍）。
    async def _handle_order_update(self, update: Mapping[str, Any]) -> None:
        """薄 wrapper：委托 trading.order_state.handle_order_update（T1 Task 3 迁出）。"""
        self._ports.gateway = self._gw  # W2-H2：网关快照对齐（见上方注释块）
        return await handle_order_update(self._ports, update)

    def _order_direction(self, order_id: str) -> Optional[str]:
        """薄 wrapper：委托 trading.order_state.order_direction（T1 Task 3 迁出）。"""
        self._ports.gateway = self._gw  # W2-H2：网关快照对齐（见 _handle_order_update 注释）
        return order_direction(self._ports, order_id)

    def _advance_order_state_from_status(self, update: Mapping[str, Any]) -> None:
        """薄 wrapper：委托 trading.order_state.advance_order_state_from_status（T1 Task 3 迁出）。"""
        self._ports.gateway = self._gw  # W2-H2：网关快照对齐（见 _handle_order_update 注释）
        advance_order_state_from_status(self._ports, update)

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
