# -*- coding: utf-8 -*-
"""L1 致命停调度 + 告警基础设施（engine 模块化拆分 T1 · 集群 A · 最独立）。

物理意图（spec §3 L1 + C-4 错误分级 + M4 静默漏单消灭）：
    本模块是 engine 致命事件链的「纯基础设施层」，**零下游交易耦合**：
        _alert_critical   致命事件钉钉 CRITICAL（fire_and_forget 不阻塞主流程）
        _CriticalHalt     L1 致命异常（DB 写/读失真·网关断线·整批失败·敞口未明）
        _critical_guard   L1 路径 wrapper（_halted 检查 + 捕获 _CriticalHalt → _halt 停调度）
        halt              _halt 的 free-function 化（幂等 + 顺序契约，副作用经回调注入）
        guard_skip_rounds _guard_skip_rounds 的纯函数化（失败次数→跳过轮数退避映射）
        _mode / _trade_cfg  交易模式 / 参数 env 读口（集群 A 基础设施）

T1 拆分红线（缝合点设计 · spec 集群 A 「最独立」判定依据）：
    ``halt`` 不反向依赖 ``TradingEngine`` 类——engine 侧 ``_halt`` 薄实例 wrapper 注入
    「is_halted / mark_halted / shutdown / alert」四个副作用闭包，critical 仅持纯顺序契约；
    ``_critical_guard`` 捕获 _CriticalHalt 后调 ``self._halt(...)``（鸭子类型，engine wrapper
    实装），critical 与 engine 无 import 级耦合（避免循环依赖）。

公共 API 兼容（spec 公共 API 不变形红线）：
    engine.py 顶部 ``from trading.critical import (...)`` re-export 全部符号——既保
    ``from trading.engine import _CriticalHalt`` 等既有 import 路径不断，又保
    ``patch("trading.engine._alert_critical")`` / ``patch("trading.engine._mode", ...)`` 等
    monkeypatch 命中（engine 内部调用点经模块全局名解析，re-export 后仍是 engine 模块属性；
    _halt 薄 wrapper 透传 ``alert=_alert_critical`` 同理）。
"""
from __future__ import annotations

import logging
import os
from typing import Callable

logger = logging.getLogger(__name__)


# ============================================================================
# Task 9（M4 静默漏单消灭）：致命事件钉钉 CRITICAL 告警（复用 infra.notifier，
# broker/qmt.py _reconnect 已在用同一套）。lazy import 避免顶层 import 副作用扩散到
# 仅用纯函数的测试场景——_alert_critical 内部 import 保持引用局部化。
# ============================================================================
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

    缝合点（T1 集群 A）：``self._halt(...)`` 鸭子类型——engine 侧薄 wrapper 实装
        （critical 不 import TradingEngine，无循环依赖）。
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
# L1 停调度 + 退避纯函数（原 engine 实例方法 _halt / _guard_skip_rounds 的 free-function 化）
# T1 红线：「critical 留纯函数 + engine 留薄 wrapper」，避免 critical → engine 耦合。
# ============================================================================

def halt(msg: str, *, is_halted: Callable[[], bool], mark_halted: Callable[[], None],
         shutdown: Callable[[], None], alert: Callable[[str], None]) -> None:
    """L1 统一停调度纯逻辑（幂等 + 顺序契约）；副作用经回调注入，critical 不依赖 TradingEngine。

    物理意图（spec §5 双层保障）：
        sched.shutdown 停「新触发」+ _halted flag 防「in-flight job 继续写」。
        幂等：已 _halted 时直接返回（多路径同时致命不重复 shutdown/alert）。

    顺序契约（对齐原 engine._halt 逐行语义，行为零变更）：
        ① 幂等检查（is_halted()=True 即返）→ ② mark_halted() 置 _halted=True
        → ③ alert(CRITICAL) → ④ shutdown(wait=False)。
        _halted 先置真，即便 alert/shutdown 抛，被 _critical_guard 装饰的 job 顶 _halted
        检查仍兜底防 in-flight 继续写（spec §5 双层保障的「flag 先于副作用」红线）。

    Why shutdown(wait=False) 而非 pause()（review 决议）：
        致命场景下「带病跑不如停」——pause 可被误恢复，留口子；shutdown 硬停 + CRITICAL
        唤醒人工，是 live 真金保护取向（spec R4）。

    Args:
        msg: 致命原因正文（alert 前缀「致命停调度 」由本函数拼，对齐原 engine._halt 口径）。
        is_halted: 读 _halted 当前值（幂等检查用；engine wrapper 注入 ``lambda: self._halted``）。
        mark_halted: 置 _halted=True（防 in-flight job 继续写；engine wrapper 注入
            ``lambda: setattr(self, "_halted", True)``）。
        shutdown: 调 ``sched.shutdown(wait=False)``（停新触发；engine wrapper 注入闭包）。
        alert: 致命告警通道（engine wrapper 注入 engine 模块全局 ``_alert_critical`` →
            ``patch("trading.engine._alert_critical")`` 命中，保既有测试断言路径）。
    """
    if is_halted():
        return
    mark_halted()
    alert(f"致命停调度 {msg}")
    try:
        shutdown()
    except Exception:
        # shutdown 自身抛（如 scheduler 未 start / 已 shutdown）→ _halted 已置，
        # 被 _critical_guard 装饰的 job 顶检查兜底，不再写。
        logger.exception("sched.shutdown 失败（_halted 已置，job 顶检查兜底）")


def guard_skip_rounds(fail_count: int) -> int:
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


# ============================================================================
# 交易模式 / 参数 env 读口（集群 A 基础设施 · engine re-export）
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
        # pending 期撤单阈值（Task 9 · D11 · 对齐 simulate_exit:128 + NecklineConfig EXEC）：
        # cancel_on = 颈线 + cancel_thresh_mult×H。I-3 修正：默认 1.0（对齐回测 EXEC_DEFAULTS
        # backtest.py:49 + NecklineConfig schema.py:45，二者均为 1.0；原 Task 9 误设 0.75
        # 致实盘 env 缺省与回测分叉，违背 spec D9/「回测实盘一套逻辑」宗旨）。
        # 物理意图：挂单等待期 high≥此价 → 涨幅已兑现撤买单（过滤「猛突破后回踩」陷阱）。
        # env 缺省 → 对齐回测基准 1.0；显式 env 可覆盖（灰度调参用）。
        "cancel_thresh_mult": float(os.getenv("TRADE_CANCEL_THRESH_MULT", "1.0")),
        # 海龟 trailing 动态止损参数（grace/step/floor）：
        # ⚠️ 原 _evolve_trailing_stops 消费方已删除（SSoT review P2 · 死计算）——C3 删写回 +
        #   C2c 切 _stoploss 读 DB SIGNAL.meta 后演进值无消费方。env 三件套保留供 follow-up
        #   「trailing 收紧作独立 live P0 task」（post_close 写 position.current_stop +
        #   _stoploss 读最新）重实现时复用，compute_stop_price 函数（trading.compute.stop）
        #   本身仍在库内可用。spec §5.3 红线（holding_days<=grace 用 base_stop 给趋势确认空间，
        #   超 grace 每日收紧 step×ATR，floor 卡底；盘中不调整 stop，盘后演进一步/日）不变。
        "grace": int(os.getenv("TRADE_STOPLOSS_GRACE_DAYS", "5")),
        "step": float(os.getenv("TRADE_STOPLOSS_STEP_ATR", "0.1")),
        "floor": float(os.getenv("TRADE_STOPLOSS_FLOOR", "0.5")),
        # max_holding（Task 8 · P0-4 超时平仓）：成交后超时持仓周期（交易日），对齐回测
        # strategies/neckline/backtest.py MAX_HOLDING=15。post_close 扫超期 → 次日 pre_open
        # 跌停价平仓释放资金（对齐回测「成交后 max_holding 日未达止盈收盘卖剩余」语义）。
        "max_holding": int(os.getenv("TRADE_MAX_HOLDING", "15")),
    }
