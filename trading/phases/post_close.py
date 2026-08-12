# -*- coding: utf-8 -*-
"""trading.phases.post_close — T 日盘后对账 + 日内熔断 + 清白名单（集群 G · 盘后收口）。

物理定位（T1 模块化拆分 Task 8 · 集群 G）：
    本模块承载盘后收口域的三个符号（原 ``trading/engine.py`` 模块级函数，逐字搬移 · 行为零变更）：
    - ``post_close(date, *, gw=None, local_positions=None, tolerance=0.0, ports=None)``（~290 行）：
      盘后编排主入口（spec §6 数据流 · plan §6.6 R-2 日内熔断）。六段独立 try-except 软降级：
        ① reconcile 持仓对账（broker 权威 · run_reconcile · W3.4 红线：broker 取数失败/返空绝不
           覆盖 position_book = 超卖敞口红线）；
        ② aggregate_fills 盘后归因展示（W3.4 降级：只读 CSV 对比日志，不重写 position_book）；
        ③ 日内熔断三步（Task 10 · R-2 · spec §5.2）：读 start_equity 基线 → query_asset 拉盘后
           总资产 → check_daily_loss_limit → 触发即 cancel_all_open_orders + emergency_halt；
        ④ trailing 盘后演进（Task 9 · R-3）：已删除（SSoT review P2 · 死计算，无消费方）；
        ⑤ max_holding 超期标记（Task 8）：已迁 pre_open 现算（B1 断点-2，post_close 不再跑）；
        ⑥ T11 trade_event(CLOSED/TP_FILLED) + account_daily 收盘快照 + 清动态白名单
           （Task1 经 ports.whitelist_clear）。
    - ``seq_for_real_oid(gw, real_oid)``（原 ``_seq_for_real_oid``）：real→seq 反查（async_response
      晚到时按 seq 匹配 DB 行）。纯 helper，无模块级依赖。
    - ``order_state_to_db(state)``（原 ``_order_state_to_db``）：OrderState 枚举/字符串 → order 表
      state 列约定（PARTIAL/FILLED/CANCELLED/REJECTED/...）。纯映射 helper，无模块级依赖。

    ``TradingEngine._post_close`` 实例 wrapper（CronTrigger 绑定 + ``@_critical_guard`` + 网关健康
    闸 + 交易日守卫 + 读 position_book 注入 local_positions）**留 engine**（spec §2 深剖：wrapper
    形态是解耦伏笔 · job_ledger + critical_guard 留 engine），内部改调
    ``await post_close(today, local_positions=local_positions, ports=self._ports)``（经 engine
    re-export 命中 ``monkeypatch.setattr(engine, "post_close", _fake)`` · test_post_close_reads_position_book /
    test_post_close_empty_book_passes_empty_dict）。

迁出纪律（strangler 红线①）：
    盘后对账 + 熔断链路【逐行原样】，只搬位置（trading/engine.py → trading/phases/post_close.py）。
    盘后收口核心零逻辑改动：
      - 熔断 -3% 基线 read 口 = ``state_store.get_start_equity(account_id, today)``（account_daily
        表 start 字段 · W4 断链根治后唯一读口 · 旧 daily_equity 表已退役 B5）；
      - 熔断判定 + 持仓快照 + TP_FILLED 对账 + 清白名单（经 ``ports.whitelist_clear``）逐行原样；
      - DB 读写口（state_store.get_start_equity / snapshot_close_equity / insert_trade_event /
        get_active_trades / get_position / _connect / _DEFAULT_DB）不变；
      - reconcile broker 权威红线（取数失败/返空绝不覆盖 position_book）+ CRITICAL 告警口径不变；
      - W3.4 CSV 归因降级（aggregate_fills 只展示不重写）不变。

================================================================================
模块级符号 patch 路径设计（brief 关键上下文 §3 · 保行为等价 + 测试全绿）
================================================================================
盘后对账路径是测试 patch 的重灾区（reconcile broker 取数失败 / 熔断三步 / CLOSED/TP_FILLED 落
DB / account_daily 收盘快照 / _post_close wrapper 读账本 / e2e probabilistic_broker 等）。逐符号
归类如下（与 pre_open.py / stop_loss.py 同口径）：

①经函数内 lazy ``import trading.engine as _eng_mod`` 反查（保 patch 命中 + 避循环 import）：
    **W1-A/T2-Task4 进展**：无状态符号（_mode / _alert_critical / _state_store /
    _cancel_all_open_orders）已切断 _eng_mod 反查 → 顶部直接 import 物理叶子（critical /
    state_store / io.breaker）。①段以下叙述保留历史；现行反查仅余 get_gateway /
    _resolve_account_id（Task 5/7 切断）。
    以下符号均在 post_close 相关测试中经 ``patch("trading.engine._xxx")`` /
    ``monkeypatch.setattr(engine, "_xxx", ...)`` / ``monkeypatch.setattr(engine, "xxx", ...)`` 注入
    mock，且 engine 顶部 re-export 本模块会触发循环——故必须函数体内 lazy 引用，绝不能顶部
    ``from trading.engine import _xxx``：
    - ``get_gateway``：engine 模块级函数。test_engine.py test_post_close_snapshot_close_equity /
      test_post_close_inserts_closed_event / test_post_close_tp1_filled_event 经
      ``monkeypatch.setattr(engine, "get_gateway", lambda: ...)`` 驱动 gw=None 路径
      （post_close 内 ``if gw is None: gw = get_gateway()``）；test_post_close_no_gw_is_noop /
      test_post_close query_trades skipped_when_no_gw 直调 post_close(date)（gw 缺省 None）走
      get_gateway 兜底。走 ``_eng_mod.get_gateway()`` 在 call-time 解析保命中。
    - ``_mode`` / ``_alert_critical``：critical 定义、engine re-export，但 post_close live 告警
      分支（broker 取数失败 / 熔断触发）测试经 ``patch("trading.engine._mode")`` /
      ``patch("trading.engine._alert_critical")`` 驱动（test_l2_aggregated_critical / e2e）——若
      本模块顶部 ``from trading.critical import _mode`` 则拿到 critical 引用，patch engine._mode
      不命中，破坏 live 告警断言。故走 _eng_mod（与 pre_open / stop_loss 同结论）。
    - ``_cancel_all_open_orders``：engine 经 ``from trading.io.breaker import cancel_all_open_orders
      as _cancel_all_open_orders`` 引入，测试经 ``patch("trading.engine._cancel_all_open_orders")``
      驱动（e2e probabilistic_broker 熔断撤单 spy / test_cancel_all_account_id / test_l2 等）。
      熔断触发时 post_close 调 ``_cancel_all_open_orders(gw)``——**W1-A/T2-Task4 已切顶部直接
      import io.breaker 真身**（原走 _eng_mod 反查保命中，现 patch engine._cancel_all_open_orders
      失效 → Task 8-19 迁 patch io.breaker.cancel_all_open_orders）。
    - ``_state_store``：engine 经 ``from trading import state_store as _state_store`` 引入，测试既
      ``patch("trading.engine._state_store")``（整体 MagicMock · test_post_close_circuit_breaker 系列
      等）又 ``monkeypatch`` 属性级 + 直接隔离 ``state_store._DEFAULT_DB``（共享模块对象）。
      **W1-A/T2-Task4 已切顶部直接 import state_store 真身**（原走 engine._state_store 在
      call-time 解析，现整体 patch engine._state_store 失效，属性级 patch 仍命中共享模块对象 →
      Task 8-19 迁整体 patch 路径）。
    - ``_resolve_account_id``：engine 模块级函数（account_id 解析）。test_post_close_snapshot_close_equity
      / test_post_close_inserts_closed_event / test_post_close_tp1_filled_event 用
      ``account_id = engine._resolve_account_id()`` 预置 state_store，post_close 内必须读同一
      account_id 才能命中预置行——走 ``_eng_mod._resolve_account_id()`` 在 call-time 解析（与
      pre_open.py / stop_loss.py / order_state.py 同范式，待 Task 9 收口）。

②顶部直接 import（不涉及 engine patch，无循环风险 · 共享模块对象属性 patch 命中）：
    - ``clock``（``from trading import clock``）：单一时间源（测试经 ``monkeypatch clock.today``
      在共享模块对象上 patch → 命中；post_close 路径无 ``patch("trading.engine.clock")`` 整体
      mock 驱动熔断/对账）。C-6 V2 业务日期 key（熔断基线 / trade_event / account_daily）均走
      clock.today。
    - ``dynamic_whitelist``（``from trading import dynamic_whitelist``）：仅 ports=None 防御回退分支
      用（``# pragma: no cover``，测试不触达——cron wrapper 恒传 ports）。
    - ``reconcile_job``（``from trading import reconcile_job``）：持仓对账薄编排壳。test_post_close_*
      / test_engine test_post_close_runs_reconcile 经 ``monkeypatch.setattr(engine.reconcile_job,
      "run_reconcile", _fake)`` 注入——patch 的是【共享模块对象】trading.reconcile_job 的属性，
      本模块 reconcile_job 同对象 → ``.run_reconcile`` 属性访问命中（无 ``patch("trading.engine.
      reconcile_job")`` 整体绑定替换）。
    - ``_position_book``（``from trading import position_book as _position_book``）：持仓账本（
      get_local_positions 读口）。test_post_close_reconcile 经 ``monkeypatch.setattr(position_book,
      "_DEFAULT_DB", ...)`` 在共享模块对象上 patch → 顶部 import 同对象命中；无 ``patch("trading.
      engine._position_book")`` 整体 mock 驱动 post_close。
    - ``_check_daily_loss_limit``（``from trading.compute.breaker import check_daily_loss_limit as
      _check_daily_loss_limit``）：纯判定 functional core，post_close 路径无 ``patch("trading.engine.
      _check_daily_loss_limit")`` 测试（test_circuit_breaker 直测 compute.breaker 真身，不经 engine）。
    - ``EnginePorts``（``from trading.ports import EnginePorts``）：窄依赖接口（stdlib-only，无循环）。
      Task 1 缝合点 #1：whitelist_clear 经 ports（清盘后白名单），cron wrapper 传 self._ports。

logger 名硬编码 ``trading.engine``（与 pre_open.py / stop_loss.py / order_state.py / eod_plan.py
同口径）：post_close 原是 engine 模块级函数，日志/异常打到 trading.engine logger。迁出后保
logger 名不变 = 观测面等价（运维按 trading.engine 过滤盘后对账/熔断日志不断 + test_post_close_*
/ test_l2 caplog 断言 ``logger="trading.engine"`` 命中）。
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

# 窄依赖接口（Task 1 EnginePorts：whitelist_clear，纯 stdlib 依赖无循环）。
from trading.ports import EnginePorts
# 项目级单例（共享模块对象属性 patch 命中）：
# clock=单一时间源（C-6 V2 业务日期 key）/ dynamic_whitelist=ports=None 防御回退分支（pragma no cover）/
# reconcile_job=持仓对账薄编排壳（属性级 patch 经共享模块对象命中）/ position_book=持仓账本（get_local_positions）。
# W1-A/T2-Task4：state_store 从 _eng_mod 反查切断 → 顶部直接 import（底层叶子无环 · 整体 patch
# engine._state_store 失效 → Task 8-19 迁 patch 物理路径）。
from trading import clock, dynamic_whitelist, reconcile_job
from trading import position_book as _position_book
from trading import state_store as _state_store
# W1-A/T2-Task4：_mode / _alert_critical 从 _eng_mod 反查切断 → 顶部直接 import critical 真身
# （critical 是 SSoT 基础设施域叶子 · patch engine._mode / engine._alert_critical 失效 → Task 8-19 迁）。
from trading.critical import _mode, _alert_critical
# _check_daily_loss_limit（熔断 -3% 纯判定 functional core · compute 单源 · post_close 路径无 engine patch）。
from trading.compute.breaker import check_daily_loss_limit as _check_daily_loss_limit
# W1-A/T2-Task4：_cancel_all_open_orders 从 _eng_mod 反查切断 → 顶部直接 import io.breaker 真身
# （patch engine._cancel_all_open_orders 失效 → Task 8-19 迁 patch io.breaker.cancel_all_open_orders）。
from trading.io.breaker import cancel_all_open_orders as _cancel_all_open_orders

# logger 名硬编码 trading.engine（而非 __name__=trading.phases.post_close）：post_close 原是 engine
# 模块级函数，日志打到 trading.engine logger。迁出后保 logger 名不变 = 观测面等价（运维按
# trading.engine 过滤盘后对账/熔断日志不断 + test_post_close_* caplog 断言命中）。
logger = logging.getLogger("trading.engine")


# ============================================================================
# 触发点 4：post_close —— T 日盘后：对账 + 兜底 + 日内熔断 + 清白名单
# ============================================================================
async def post_close(
    date: str,
    *,
    gw: Any = None,
    local_positions: Optional[Mapping[str, float]] = None,
    tolerance: float = 0.0,
    ports: EnginePorts | None = None,
) -> dict:
    """盘后：对账（run_reconcile） + 盘后兜底 + 日内熔断 + 清动态白名单。

    Args:
        date:            T 日。
        gw:              网关（None 时内部 get_gateway）。
        local_positions: 本地理论持仓 {symbol: qty}；None 则跳过对账。
        tolerance:       持仓偏差容忍度（默认 0 零容忍）。
        ports:           T1 缝合点 #1——engine 实例特有依赖（清动态白名单）。
            cron wrapper 传 ``self._ports``；未传（None）走防御分支回退模块级全局（pragma no cover）。

    Returns:
        {"date":..., "drift":bool, "circuit_breaker":bool, "breaker_skipped"?:bool}
        - drift=True：对账有偏差（run_reconcile 已告警）
        - circuit_breaker=True：日内 -3% 熔断已触发（cancel_all + emergency_halt 已执行）
        - breaker_skipped=True：无基线跳过熔断（start_equity 未抓到，不拿 0 误触发）

    编排顺序（plan 红线 · spec §6 数据流）：
        ① reconcile（持仓对账）→ ② query_trades 兜底（Task 11 follow-up）
        → ③ 熔断（本 Task 10）→ ④ trailing（Task 9，**已删除 SSoT review P2 死计算**）
        → ⑤ max_holding 标记（Task 8，已迁 pre_open B1）
        各段独立 try-except 软降级（单段异常不阻塞下一段）。

    ⚠️ 熔断三步（Task 10 · R-2 日内熔断 · spec §5.2）：
        1) state_store.get_start_equity(account_id, today) → start_equity
           （pre_open 写 account_daily.start_total_asset 的基线；W4 + C-1 已迁同表读口）
        2) gw.query_asset → curr_equity（盘后总资产）
        3) check_daily_loss_limit(start, curr) → True 即 cancel_all_open_orders +
           emergency_halt + ERROR 告警
        缺基线（start=None）→ 跳过 + WARN（不拿 0 触发，防 check(0,X) 永远 False 反永不熔断）。
    """
    # T1-Task8：经 _eng_mod 引用调 engine 模块级符号（保 patch("trading.engine._xxx") /
    # monkeypatch.setattr(engine, "_xxx"/"xxx") 命中——test_post_close_snapshot_close_equity 等 4 测
    # patch engine.get_gateway / engine._resolve_account_id；test_post_close_circuit_breaker 系列 /
    # e2e patch engine._state_store / engine._cancel_all_open_orders / engine._mode / engine._alert_critical；
    # 详见本文件模块 docstring「模块级符号 patch 路径设计」）+ 避循环 import（engine 顶部 re-export 本模块）。
    # call-time 解析：测试 patch 整体属性时拿 mock，patch 属性级（_state_store.xxx）时拿真模块对象。
    # clock / reconcile_job / _position_book / _check_daily_loss_limit / dynamic_whitelist 顶部直接 import
    # （不涉及 engine patch · 共享模块对象属性 patch 命中）。
    import trading.engine as _eng_mod
    get_gateway = _eng_mod.get_gateway
    _resolve_account_id = _eng_mod._resolve_account_id

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
            from trading.gateway_service import \
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
        # 补基线兜底（account_daily.start 漏采修复 · 2026-08-11）：pre_open 抓基线失败
        # （query_asset 返空 / gw=None / 非盘前启动）→ start 缺失。读 T-1 close_total_asset
        # 作 start 近似（隔夜无交易，T-1 收盘 ≈ T 开盘），让熔断仍能工作（不裸奔）。
        # 详见 docs/superpowers/specs/2026-08-11-account-daily-start-baseline-design.md。
        if start_equity is None or start_equity <= 0:
            prev_close = _state_store.get_prev_close_equity(_resolve_account_id(), today_eq)
            if prev_close is not None and prev_close > 0:
                start_equity = prev_close
                _msg = (f"post_close 用 T-1 close={prev_close} 作 start 基线近似 date={today_eq}"
                        f"（pre_open 未抓到开盘基线）")
                logger.warning(_msg)
                if _mode() == "live":
                    _alert_critical(_msg + "（C-1 熔断基线为近似值，人工复盘知悉精度边界）")
        if start_equity is None or start_equity <= 0:
            # 无基线（pre_open + T-1 close 都缺）→ 跳过熔断 + WARN
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
                        from trading.gateway_service import (
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

    # ④ trailing 盘后演进（Task 9 · R-3）：已删除（SSoT review P2 · 死计算）
    # Why 删除：C3 删 save_plan 写回 + C2c 切 _stoploss 读 DB SIGNAL.meta 后，演进值无消费方
    #   （可观测字段 ``trailing_evolved`` 误导「演进 N 单」），整个 trailing 收紧链路停摆。
    #   详见本模块 ``_evolve_trailing_stops`` 删除注释（follow-up：独立 live P0 task 重实现，
    #   post_close 写 position.current_stop + _stoploss 读最新）。post_close 其他段不受影响。
    # 熔断优先约束（原 if not circuit_breaker_triggered）随 ④ 段一并失效——④ 已空，
    #   ⑤ max_holding 早已迁 pre_open（B1），post_close 不再有熔断后跳过逻辑。

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

    # 清动态白名单（Task5 → C-2 W1 改实例属性 · T1 经 ports.whitelist_clear）：保证下一交易日从干净状态开始。
    # 改造后清 engine 实例 self._dynamic_whitelist（经 EnginePorts 显式清空，
    # 而非模块级 _DYNAMIC 全局——与 pre_open 注入对称：实例属性化两端隔离）。ports 由 cron
    # wrapper 传 self._ports，None 守卫为防御性兜底（未装配 engine 的裸调回退模块级全局）。
    try:
        if ports is not None:
            ports.whitelist_clear()
        else:  # pragma: no cover - 防御性回退：未注入 ports（未装配 engine 的裸调，理论仅测试）
            dynamic_whitelist.clear_dynamic_whitelist()
    except Exception:
        logger.exception("post_close 清动态白名单异常")

    logger.info("post_close 完成 date=%s drift=%s circuit_breaker=%s breaker_skipped=%s",
                date, result.get("drift"),
                result.get("circuit_breaker"), result.get("breaker_skipped"))
    return result


# ============================================================================
# 对账辅助：seq↔real_oid 反查 + OrderState → DB state 映射（T1-Task8 与 post_close 一起迁）
# ============================================================================
# T1-Task8：原 ``_seq_for_real_oid`` / ``_order_state_to_db`` 去 `_` 前缀 → ``seq_for_real_oid`` /
# ``order_state_to_db``（engine re-export 双名保 ``patch("trading.engine._seq_for_real_oid")`` +
# order_state.py ``_eng_mod._seq_for_real_oid`` / ``_eng_mod._order_state_to_db`` 命中）。逻辑逐字原样。


def seq_for_real_oid(gw, real_oid: str) -> int | None:
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


def order_state_to_db(state) -> str:
    """OrderState 枚举/字符串 → order 表 state 列约定（PARTIAL/FILLED/CANCELLED/REJECTED/...）。"""
    name = state.name if hasattr(state, "name") else str(state)
    return {
        "PARTIAL_FILLED": "PARTIAL",
        "FILLED": "FILLED",
        "CANCELLED": "CANCELLED",
        "REJECTED": "REJECTED",
        "PARTIAL_CANCELLED": "PARTIAL_CANCELLED",
    }.get(name, "SUBMITTED")
