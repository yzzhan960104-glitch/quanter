# -*- coding: utf-8 -*-
"""trading.phases.stop_loss — 盘中止损/超时巡检 + pending 撤单 + 超期平仓（集群 F · 最高风险）。

物理定位（T1 模块化拆分 Task 7 · 集群 F）：
    本模块承载盘中风控域的三个符号（原 ``trading/engine.py`` 模块级函数，逐字搬移 · 行为零变更）：
    - ``stop_loss_monitor(...)``（~370 行）：盘中止损/超时巡检主循环（spec §2 目标 3 + §4.6 +
      D10/D11/D12）。含两个内嵌闭包 ``_stop_already_placed``（DB 幂等查 STOP）/ ``_record_stop``
      （DB 落 order(STOP)+trade_event(STOP_TRIGGERED)），闭包随本体一并迁入（整体移动）。
      主路径 monitor_ctx → decide_exit（执行单源）→ 按 action/reason/portion 分发（STOP_LOSS/
      TIMEOUT 发市价卖 / TAKE_PROFIT 交预挂限价单 skip / HOLD 跳过）；D12 fallback decide_exit
      异常 → 降级 should_trigger_stop（盘中不裸奔红线）；D11 pending cancel_on 巡检撤买单。
    - ``scan_expired_positions(today, max_holding)``（原 ``_scan_expired_positions``）：扫超期持仓
      （holding_days > max_holding，I-4 严格 ``>`` 与 monitor is_last ``>=`` 错位一日防同日双卖）。
    - ``close_expired_positions(gw, expired)``（原 ``_close_expired_positions``）：跌停价平超期持仓
      （DB 幂等 EXPIRED_CLOSE 防同日 pre_open 重入重复挂卖）。

    ``TradingEngine._stoploss`` 实例 wrapper（IntervalTrigger 绑定 + ``@_critical_guard`` + 网关健康
    闸 + 交易日守卫 + 从 DB SIGNAL.meta 构造 monitor_ctx/pending_ctx/stop_prices）**留 engine**
    （spec §2 深剖：wrapper 形态是解耦伏笔 · job_ledger + critical_guard 留 engine），内部改调
    ``await stop_loss_monitor(...)``（经 engine re-export 命中 ``patch("trading.engine.stop_loss_monitor")``）。

迁出纪律（strangler 红线①）：
    海龟移动止损算法（grace/step/floor + expire 平仓逻辑）与 decide_exit 分发链路【逐行原样】，
    只搬位置（trading/engine.py → trading/phases/stop_loss.py）。盘中风控核心零逻辑改动：
      - decide_exit 四分支（STOP_LOSS/TIMEOUT 发市价卖 / TAKE_PROFIT skip 交预挂 / HOLD 跳过）不变；
      - D12 fallback（decide_exit 异常降级 should_trigger_stop）+ D11 pending cancel_on 不变；
      - R7 bar 防御（high/low 优先 xtdata 当日累积，缺则回退 last_price）不变；
      - R2 行情黑屏 30min 节流告警（``_last_quote_blackout_alert_ts``）不变；
      - trade_event STOP/EXIT 落 DB 口（``_record_stop`` 闭包）+ 限频 gw.query_stock_positions/
        get_quotes 批量调用不变；
      - DB 幂等（STOP / EXPIRED_CLOSE UNIQUE）+ L1/L2 错误分级（_CriticalHalt 停调度 vs L2 聚合
        CRITICAL）不变。

================================================================================
模块级符号 patch 路径设计（brief 关键上下文 §3 · 保行为等价 + 测试全绿）
================================================================================
盘中止损路径是测试 patch 最密集 + 最高风险的域（decide_exit 四分支 / D12 fallback / pending
cancel_on / DB 幂等 L1 / 行情黑屏节流 / e2e probabilistic_broker / engine alerts 等）。逐符号
归类如下（与 pre_open.py / order_state.py 同口径）：

①经函数内 lazy ``import trading.engine as _eng_mod`` 反查（保 patch 命中 + 避循环 import）：
    **W1-A/T2-Task4 进展**：无状态符号（_state_store / _mode / _alert_critical / calendar /
    qmt_market_data / _trading_days_between / decide_exit）已切断 _eng_mod 反查 → 顶部直接 import
    物理叶子（见 ②段）。①段以下叙述保留历史（这些符号已迁走）；现行反查仅余 get_gateway /
    _submit / place_take_profit（Task 6/7 切断）。
    **W1-A/T2-Task5 进展**：_resolve_account_id 已切断 _eng_mod 反查 → 顶部直接 import
    trading.account SSoT 真身（account 是叶子模块无环 · patch engine._resolve_account_id
    失效 → Task 8-19 迁 monkeypatch account.resolve_account_id 或 setenv QMT_ACCOUNT_ID）。
    以下符号均在 stop_loss 相关测试中经 ``patch("trading.engine._xxx")`` /
    ``monkeypatch.setattr(engine, "_xxx", ...)`` / ``patch("trading.engine.xxx")`` 注入 mock，且
    engine 顶部 re-export 本模块会触发循环——故必须函数体内 lazy 引用，绝不能顶部
    ``from trading.engine import _xxx``：
    - ``get_gateway`` / ``_submit`` / ``place_take_profit`` / ``decide_exit``：engine 模块级（含
      re-export）函数，测试 ``patch("trading.engine.get_gateway"/"_submit"/"place_take_profit")``
      （test_stop_loss_monitor_decide_exit._run_monitor / test_stop_loss_l1_halt）+
      ``monkeypatch.setattr(engine, "decide_exit"/"_submit")``（test_stop_loss_l1_halt 驱动 L1 分支）。
    - ``_state_store``：engine 经 ``from trading import state_store as _state_store`` 引入，测试既
      ``patch("trading.engine._state_store")``（整体 MagicMock · test_stop_loss_l1_halt 三 Case 验
      has_order/insert_order L1 / test_engine_stoploss_inject）又 ``monkeypatch`` 属性级。走
      ``_state_store`` 在 call-time 解析：整体 patch 拿 mock，无 patch 拿真模块（autouse
      fixture 隔离 _DEFAULT_DB）。
    - ``_mode`` / ``_alert_critical``：critical 定义、engine re-export，但测试 patch 的是
      ``trading.engine._mode`` / ``trading.engine._alert_critical``（test_stop_loss_monitor_decide_exit
      行情黑屏 4 测 / test_stoploss_post_close_gate）——若本模块顶部 ``from trading.critical import
      _mode`` 则拿到 critical 引用，patch engine._mode 不命中，破坏 live 告警断言。故走 _eng_mod。
    - ``_resolve_account_id``：engine 模块级函数（account_id 解析），测试经整体 patch
      ``trading.engine._state_store`` 驱动——走 ``engine._resolve_account_id()`` 在 call-time
      解析（与 pre_open.py / order_state.py 同范式，待 Task 9 收口）。**W1-A/T2-Task5 已切顶部
      直接 import trading.account.resolve_account_id 真身**（原走 engine 反查，现 patch
      engine._resolve_account_id 失效 → Task 8-19 迁 patch 物理路径）。
    - ``calendar`` / ``qmt_market_data``：测试既 ``patch("trading.engine.calendar")`` /
      ``patch("trading.engine.qmt_market_data")``（整体 MagicMock · test_stop_loss_monitor_decide_exit
      _run_monitor / test_engine_stoploss_inject）又 ``monkeypatch.setattr(engine.calendar, ...)`` /
      ``monkeypatch.setattr(engine.qmt_market_data, "get_quotes", ...)``（属性级 · test_stop_loss_l1_halt）。
      走 ``calendar`` / ``qmt_market_data``：整体 patch 时拿 mock，属性级 patch 时
      拿真模块对象 → ``.is_intraday_session`` / ``.get_quotes`` patch 同样命中。
    - ``_trading_days_between``：engine 模块级别名（``from trading.compute.stop import trading_days_between
      as _trading_days_between``），test_engine.py test_scan_expired_* 经
      ``monkeypatch.setattr(engine, "_trading_days_between", lambda s,e: N)`` 确定性驱动 scan_expired —
      走 ``_trading_days_between`` 保命中（与 pre_open.py 同结论）。
    - ``_last_quote_blackout_alert_ts`` / ``_QUOTE_BLACKOUT_ALERT_INTERVAL_S``：**W1-A/T2 已收口**
      ——原 engine 模块级节流状态迁 ``trading.alerting.QuoteBlackoutThrottle`` dataclass，经
      ``ports.blackout`` 注入本函数（``ports.blackout.should_alert(now)`` / ``mark(now)`` 替代
      ``_eng_mod._last_quote_blackout_alert_ts`` 反查读写）。测试改经构造 ``QuoteBlackoutThrottle
      (last_ts=0.0)`` 注入 ports 重置节流（原 ``monkeypatch.setattr("trading.engine._last_quote_
      blackout_alert_ts", 0.0)`` 已删，详见 test_stop_loss_monitor_decide_exit._make_ports_with_
      fresh_blackout）。本项不再属「engine 模块级符号反查」清单（仅历史注释保留）。
      现行反查项仅余：``get_gateway`` / ``_submit`` / ``place_take_profit``（Task 6/7 切断）。
      无状态符号（_state_store / _mode /
      _alert_critical / calendar / qmt_market_data / _trading_days_between / decide_exit）
      W1-A/T2-Task4 已切断，改顶部直接 import 物理叶子。
      _resolve_account_id W1-A/T2-Task5 已切断 → 顶部直接 import trading.account 真身。

②顶部直接 import（不涉及 engine patch，无循环风险）：
    - ``clock``（``from trading import clock``）：单一时间源（测试经 ``monkeypatch clock.today/now``
      在共享模块对象上 patch → 命中；无 ``patch("trading.engine.clock")`` 整体 mock 驱动 stop_loss）。
    - ``position_book``（``from trading import position_book as _position_book``）：持仓账本（测试经
      ``monkeypatch.setattr(position_book, "get_entry_dates", ...)`` / ``"_DEFAULT_DB"`` 在共享模块对象
      上 patch → 顶部 import 同对象命中；无 ``patch("trading.engine._position_book")`` 整体 mock）。
    - ``_CriticalHalt``（``from trading.critical import _CriticalHalt``）：L1 致命停调度异常，按类身份
      catch 不被 patch。
    - ``should_trigger_stop``（``from trading.compute.stop import should_trigger_stop``）：D12 fallback
      纯判定函数，stop_loss 路径无 ``patch("trading.engine.should_trigger_stop")``（test_stop_loss.py
      直测 compute.stop 真身，不经 engine）。
    - ``ExitAction`` / ``ExitReason``（``from strategies.neckline.execution import ExitAction, ExitReason``）：
      枚举单例，按身份 ``is`` 比较（与 engine re-export 同源同类，mock decide_exit 返的 fake_dec.action
      = engine.ExitAction.CLOSE 与本模块 ExitAction.CLOSE 同对象 → ``is`` 成立）。
    - ``OrderRequest``：函数内 local import（原 engine 同位置，``from trading.compute.types``）。

logger 名硬编码 ``trading.engine``（与 pre_open.py / order_state.py / eod_plan.py 同口径）：
    stop_loss_monitor 原是 engine 模块级函数，日志/异常打到 trading.engine logger。迁出后保 logger
    名不变 = 观测面等价（运维按 trading.engine 过滤盘中止损/超期平仓日志不断 + test_stop_loss_*
    caplog 断言命中）。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Mapping, Optional

# 项目级单例（共享模块对象属性 patch 命中）：
# clock=单一时间源（clock.now/today）/ position_book=持仓账本（get_entry_dates/_DEFAULT_DB）。
# W1-A/T2-Task4：calendar / qmt_market_data / state_store 从 _eng_mod 反查切断 → 顶部直接 import
# 物理叶子（底层无环 · 整体 patch engine.X 将失效 → Task 8-19 迁 patch 物理路径）。
from trading import clock
from trading import calendar
from trading import position_book as _position_book
from trading import qmt_market_data
from trading import state_store as _state_store
# W1-A/T2-Task5：_resolve_account_id 从 _eng_mod 反查切断 → 顶部直接 import trading.account
# SSoT 真身（account 是叶子模块无环 · patch engine._resolve_account_id 失效 → Task 8-19 迁）。
from trading.account import resolve_account_id as _resolve_account_id
# _CriticalHalt（L1 致命停调度异常，按类身份 catch 不被 patch）。
# W1-A/T2-Task4：_mode / _alert_critical 从 _eng_mod 反查切断 → 顶部直接 import critical 真身
# （critical 是 SSoT 基础设施域叶子，不反向 import 本文件 · patch engine._mode 失效 → Task 8-19 迁）。
from trading.critical import _CriticalHalt, _mode, _alert_critical
# W1-A/T2：EnginePorts 经 ports.blackout 注入 QuoteBlackoutThrottle（行情黑屏节流状态）。
# 窄依赖接口纯 stdlib + alerting 依赖（无循环），与 pre_open.py 同口径顶层 import。
from trading.ports import EnginePorts
# should_trigger_stop（D12 fallback 纯判定 · compute 单源 · stop_loss 路径无 engine patch）。
# W1-A/T2-Task4：_trading_days_between 从 _eng_mod 反查切断 → 同 compute.stop 顶部直接 import
# （test_scan_expired_* 的 monkeypatch(engine, "_trading_days_between") 失效 → Task 8-19 迁）。
from trading.compute.stop import should_trigger_stop, trading_days_between as _trading_days_between
# ExitAction / ExitReason（执行单源枚举 · 按身份 is 比较 · 与 engine re-export 同源同类）。
# W1-A/T2-Task4：decide_exit 从 _eng_mod 反查切断 → 同 execution 顶部直接 import（无新环 ·
# monkeypatch(engine, "decide_exit") 失效 → Task 8-19 迁 patch execution.decide_exit）。
from strategies.neckline.execution import ExitAction, ExitReason, decide_exit

# logger 名硬编码 trading.engine（而非 __name__=trading.phases.stop_loss）：stop_loss_monitor 原是
# engine 模块级函数，日志打到 trading.engine logger。迁出后保 logger 名不变 = 观测面等价（运维按
# trading.engine 过滤盘中止损日志不断 + test_stop_loss_* / 行情黑屏告警 caplog 断言命中）。
logger = logging.getLogger("trading.engine")


# ============================================================================
# 触发点 3：stop_loss_monitor —— 盘中：止损/超时巡检 + pending 期撤单（每 30s）
# ============================================================================
async def stop_loss_monitor(
    stop_prices: Optional[Mapping[str, float]] = None,
    *,
    gw: Any = None,
    monitor_ctx: Optional[Mapping[str, Mapping[str, Any]]] = None,
    pending_ctx: Optional[Mapping[str, float]] = None,
    ports: Optional["EnginePorts"] = None,
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
        ports: W1-A/T2 注入的 EnginePorts 窄接口（仅消费 ``ports.blackout``——行情黑屏
               30min 节流状态机，生产主路径走 ``ports.blackout.fire_if_due(now)`` 原子方法：
               单一 Lock 内 check+mark，杜绝 catchup 重叠 / daemon 并发双发告警）。生产路径
               ``_stoploss`` wrapper 总传 ``self._ports``（含 blackout），与原 engine 模块级
               ``_last_quote_blackout_alert_ts`` 反查语义等价（行为零变更）。None 时（测试
               裸调 / 未装配）等价 blackout 跳过——仅非生产裸调（生产 _stoploss 总传
               self._ports），不阻断监控主链路。blackout 仅本函数用，``scan_expired_positions``
               / ``close_expired_positions`` 不加 ports 参数。

    Returns:
        盘中：{"checked":N, "stop_triggered":M, "fallback_used":K, "pending_cancelled":P,
               "mode":...}
        非盘中：{"checked":0, "reason":"非盘中时段..."}
        无 gw：{"checked":0, "reason":"...网关..."}
    """
    # T1-Task7：经 _eng_mod 引用调 engine 模块级符号（保 patch("trading.engine.xxx") /
    # monkeypatch.setattr(engine, "xxx") 命中——test_stop_loss_monitor_decide_exit._run_monitor
    # 经 patch("trading.engine.calendar"/"qmt_market_data"/"_submit"/"get_gateway") 驱动主路径；
    # test_stop_loss_l1_halt 经 monkeypatch.setattr(engine, "decide_exit"/"_submit") 驱动 L1 分支；
    # 行情黑屏 4 测经 patch("trading.engine._alert_critical") 验 _alert_critical 命中
    # （W1-A/T2：节流状态已迁 ports.blackout，不再 monkeypatch engine 模块级）+ 避循环 import
    # （engine 顶部 re-export 本模块）。详见本文件模块 docstring「模块级符号 patch 路径设计」。
    import trading.engine as _eng_mod
    # ① 盘中时段判定（Task1）
    # C-6 V2：时点判定走 clock.now（单一时间源口子）。
    if not calendar.is_intraday_session(clock.now()):
        return {"checked": 0, "reason": "非盘中时段（09:15-15:00 连续，D2 修订后），跳过止损监控"}

    # ② 取网关与持仓
    if gw is None:
        gw = _eng_mod.get_gateway()
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
    # Why 30min 节流：避免 IntervalTrigger 30s 巡检每轮推一条（M4 告警风暴红线）。
    # W1-A/T2（模块级可变状态收口）：原节流状态 _last_quote_blackout_alert_ts / 间隔常量
    # _QUOTE_BLACKOUT_ALERT_INTERVAL_S 是 engine 模块级可变 global，经 _eng_mod 反查读写——
    # 违反「模块级可变状态收口」红线。现收敛为 trading.alerting.QuoteBlackoutThrottle dataclass
    # 实例，经 ports.blackout 注入，生产主路径走 ``fire_if_due(now)`` 原子方法（单一 Lock 内
    # check+mark——I-1 fix：原 should_alert+mark 两步 check-then-act 非原子，catchup 重叠 /
    # daemon 并发下可双发；fire_if_due 收互斥区杜绝双发，单线程下与两步逐字等价）。
    # 行为等价：30min 窗口（interval=1800.0）+ last_ts=0.0 初值 + ``now - last_ts >= interval``
    # 边界等价触发，逐字对齐原 ``_now_mono - _last >= _INTERVAL`` 语义。
    # ports=None 守卫：ports=None 等价 blackout 跳过——仅非生产裸调（生产 _stoploss 总传
    # self._ports，e2e orchestrator 显式传 eng._ports 保 blackout 语义完整）；不阻断主链路。
    # _alert_critical 仍经 _eng_mod（Task 4 才切断），本 Task 只迁 blackout 状态。
    if (ports is not None and _mode() == "live" and relevant_syms):
        _n_valid = sum(
            1 for q in quotes.values()
            if isinstance(q, dict)
            and isinstance(q.get("last_price"), (int, float))
            and q["last_price"] == q["last_price"]  # NaN 判无效
            and q["last_price"] > 0
        )
        if _n_valid == 0:
            _now_mono = time.monotonic()
            # fire_if_due：单一 Lock 内 check+mark 原子返回 True/False——杜绝并发双发（I-1）。
            if ports.blackout.fire_if_due(_now_mono):
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
                                await _eng_mod.place_take_profit(sym, qty, price, "")
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
                            result = await _eng_mod._submit(
                                OrderRequest(symbol=sym, qty=sell_qty, side="sell", price=price),
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
                result = await _eng_mod._submit(
                    OrderRequest(symbol=sym, qty=qty, side="sell", price=price),
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
# T1-Task7：原 ``_scan_expired_positions`` 去 `_` 前缀 → ``scan_expired_positions``（engine
# re-export 双名保 ``patch("trading.engine._scan_expired_positions")`` + pre_open ``_eng_mod.
# _scan_expired_positions`` 命中）。逻辑逐字原样。


def scan_expired_positions(today: str, max_holding: int) -> list[dict]:
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
    # T1-Task7：_trading_days_between 经 _eng_mod 反查（test_engine.py test_scan_expired_* 经
    # monkeypatch.setattr(engine, "_trading_days_between", lambda s,e: N) 确定性驱动本函数验
    # I-4 `>` 边界——直 import 拿 compute.stop 真身则 patch engine 失效）。_position_book 顶部
    # import（测试 monkeypatch position_book.get_entry_dates 属性级 patch · 共享模块对象命中）。
    import trading.engine as _eng_mod
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


async def close_expired_positions(gw: Any, expired: list[dict]) -> dict:
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
        expired: scan_expired_positions 返回的超期列表（B1 改 pre_open 现算）。

    Returns:
        {"closed": <成功挂卖数>, "reason"?: ...}
    """
    # T1-Task7：经 _eng_mod 反查 engine 模块级符号（_resolve_account_id/_state_store/_submit/
    # _mode/qmt_market_data · 保 patch("trading.engine.xxx") 命中 + 避循环 import）。clock 顶部
    # import（单一时间源 · 无整体 patch）。详见本文件模块 docstring。
    import trading.engine as _eng_mod
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
            result = await _eng_mod._submit(
                OrderRequest(symbol=sym, qty=qty, side="sell", price=price),
            )
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
