# -*- coding: utf-8 -*-
"""trading.phases.exit — 挂限价止盈卖单（集群 H · 止盈 · #4 差额补挂防超卖）。

物理定位（T1 模块化拆分 Task 9 · 集群 H）：
    本模块承载止盈挂单的单符号（原 ``trading/engine.py`` 模块级函数，逐字搬移 · 行为零变更）：
    - ``place_take_profit(symbol, filled_qty, fill_price, order_id)``（~110 行）：买单成交后
      挂限价止盈卖单（#4 差额补挂：目标量 − 已挂量，防超卖/防覆盖缺口）。含两个内嵌闭包
      ``_placed``（DB 查已挂未终态量 · 差额基准）/ ``_record_tp``（DB 落 order UPSERT 累加），
      闭包随本体一并迁入（整体移动）。主路径：load_plan 读 tp1/tp1_portion/take_profit(tp2) →
      use_two_legs 分级两腿（tp1 锁利 + tp2 全平）或单腿全平 tp2 → 各腿差额补挂（need = target −
      _placed(purpose)），_submit 发限价卖单 + _record_tp 落 DB。

    ``TradingEngine._place_take_profit`` 实例方法（薄 wrapper · 原 L1582）**已删**——成交回报
    链路（order_state.handle_order_update）+ 盘中 TP 漏挂兜底（stop_loss_monitor）均直接调模块级
    ``place_take_profit``（经 engine re-export 命中 ``patch("trading.engine.place_take_profit")``），
    不再经 engine 实例引用（Task 9 收口缝合点 #2：消除 order_state→engine 实例的 take_profit 耦合）。

迁出纪律（strangler 红线①）：
    止盈挂单算法（load_plan 读价/量 → 差额补挂 → _submit 限价卖 + _record_tp 落 DB）【逐行原样】，
    只搬位置（trading/engine.py → trading/phases/exit.py）。止盈核心零逻辑改动：
      - 单腿/两腿分流（use_two_legs = tp1>0 ∧ portion>0 ∧ tp1<tp2）不变；
      - 差额补挂（need = target − _placed(purpose)，防超卖/防覆盖缺口）不变；
      - DB 幂等（add_order_qty UPSERT 累加 · get_order_placed_qty 排除终态）不变；
      - A 股整手（int(filled_qty) 截断零股 · tp1_target=int(filled×portion/100)*100 向下整手）不变；
      - veto 终局防线（latest_action=VETOED 显式跳过，C3 follow-up）不变。

================================================================================
模块级符号 patch 路径设计（brief 关键上下文 §4 · 保行为等价 + 测试全绿）
================================================================================
止盈挂单路径是成交回报链路（order_state）+ 盘中 TP 漏挂兜底（stop_loss）的关键交汇点。
测试 patch ``trading.engine._submit`` / ``trading.engine.trading_plan.load_plan`` 驱动
place_take_profit 单元测（test_place_take_profit_two_legs 等 8 测直调本函数），以及
``trading.engine.place_take_profit`` 整体 mock（test_tp_fallback_preserves_positions 经
stop_loss 路径补挂）。逐符号归类如下（与 pre_open.py / stop_loss.py 同口径）：

①经函数内 lazy ``import trading.engine as _eng_mod`` 反查（保 patch 命中 + 避循环 import）：
    **W1-A/T2-Task4 进展**：_state_store 已切断 _eng_mod 反查 → 顶部直接 import。①段以下
    叙述保留历史；现行反查仅余 _submit（Task 6 切断）。
    **W1-A/T2-Task5 进展**：_resolve_account_id 已切断 _eng_mod 反查 → 顶部直接 import
    trading.account SSoT 真身（account 是叶子模块无环 · patch engine._resolve_account_id
    失效 → Task 8-19 迁 monkeypatch account.resolve_account_id 或 setenv QMT_ACCOUNT_ID）。
    - ``_submit``：engine 模块级函数（下单分流），测试 ``patch("trading.engine._submit")``
      （test_place_take_profit_two_legs / _skips_vetoed_symbol / _tp1_qty_round_to_lot /
      _portion_zero/full / _tp1_ge_tp2 / _no_tp1 / _truncates_fractional + stop_loss 路径）。
      走 ``_eng_mod._submit`` 在 call-time 解析（local alias 绑定于函数入口，patch 先于调用）。
    - ``_state_store``：engine 经 ``from trading import state_store as _state_store`` 引入，
      测试既 ``patch("trading.engine._state_store")``（整体 mock · test_stop_loss_l1_halt）又
      ``patch("trading.engine._state_store.has_order")`` / ``add_order_qty`` 等（属性级）。
      **W1-A/T2-Task4 已切顶部直接 import state_store 真身**（原走 engine._state_store 反查，
      现整体 patch engine._state_store 失效，属性级 patch 仍命中 → Task 8-19 迁）。
    - ``_resolve_account_id``：engine 模块级函数（account_id 解析），测试经 env var
      ``QMT_ACCOUNT_ID`` 驱动（monkeypatch.setenv）。**W1-A/T2-Task5 已切顶部直接 import
      trading.account.resolve_account_id 真身**（原走 engine._resolve_account_id() 在
      call-time 解析，现 patch engine._resolve_account_id 失效 → Task 8-19 迁 patch 物理路径）。

②顶部直接 import（不涉及 engine patch，无循环风险）：
    - ``clock``（``from trading import clock``）：单一时间源（``clock.today()``）。测试经
      ``monkeypatch clock.today`` 在共享模块对象上 patch → 命中；无整体 mock。
    - ``trading_plan``（``from trading import trading_plan``）：计划加载模块。测试经
      ``patch("trading.engine.trading_plan.load_plan", return_value=plan)`` 驱动——该 patch
      修改的是 ``trading_plan`` 模块对象的 ``load_plan`` 属性（共享单例），顶部 import 同对象命中。
    - ``OrderRequest``：函数内 local import（原 engine 同位置，``from trading.compute.types``）。

logger 名硬编码 ``trading.engine``（与 pre_open.py / stop_loss.py / order_state.py 同口径）：
    place_take_profit 原是 engine 模块级函数，日志/异常打到 trading.engine logger。迁出后保
    logger 名不变 = 观测面等价（运维按 trading.engine 过滤止盈挂单日志不断 + caplog 断言命中）。
"""
from __future__ import annotations

import logging

# 项目级单例（共享模块对象属性 patch 命中）：
# clock=单一时间源（clock.today）/ trading_plan=计划加载（trading_plan.load_plan）。
# W1-A/T2-Task4：state_store 从 _eng_mod 反查切断 → 顶部直接 import（底层叶子无环 · 整体 patch
# engine._state_store 失效 → Task 8-19 迁 patch 物理路径）。
from trading import clock
from trading import trading_plan
from trading import state_store as _state_store
# W1-A/T2-Task5：_resolve_account_id 从 _eng_mod 反查切断 → 顶部直接 import trading.account
# SSoT 真身（account 是叶子模块无环 · patch engine._resolve_account_id 失效 → Task 8-19 迁）。
from trading.account import resolve_account_id as _resolve_account_id

# logger 名硬编码 trading.engine（而非 __name__=trading.phases.exit）：place_take_profit 原是
# engine 模块级函数，日志打到 trading.engine logger。迁出后保 logger 名不变 = 观测面等价（运维按
# trading.engine 过滤止盈挂单日志不断 + test_place_take_profit_* / 止盈幂等 caplog 断言命中）。
logger = logging.getLogger("trading.engine")


# ============================================================================
# 集群 H：place_take_profit —— 买单成交后挂限价止盈卖单（#4 差额补挂 · 防超卖）
# ============================================================================
async def place_take_profit(symbol: str, filled_qty: float, fill_price: float,
                            order_id: str) -> None:
    """挂限价止盈卖单（#4 差额补挂：目标量 − 已挂量，防超卖/防覆盖缺口）。

    Why 模块级：stop_loss_monitor（模块级函数）盘中 TP 漏挂兜底也要调它（#10），
    实例方法无法被模块级函数引用（原 plan E4 的 self 错误根因）。
    """
    # T1-Task9：engine 模块级符号经 _eng_mod 引用（保 patch("trading.engine._xxx") /
    # monkeypatch.setattr(engine, "_xxx") 命中 + 避循环 import：engine 顶部 re-export 本模块）。
    # _state_store / clock / trading_plan 顶部 import（共享模块对象属性 patch 命中，无循环）。
    # local alias 绑定于函数入口：测试 patch 先于调用 → alias 拿 mock；无 patch 拿真值。
    import trading.engine as _eng_mod
    _submit = _eng_mod._submit

    today = clock.today()
    plan = trading_plan.load_plan(today)
    if not plan:
        logger.warning("挂止盈跳过：无活跃计划 symbol=%s（计划未落盘/已失效）", symbol)
        return
    # SSoT C3 follow-up（C2c reviewer 标注）：per-symbol veto 守卫。
    # 物理意图：place_take_profit 经 load_plan 读 plan.orders，C3 load_plan DB 优先返所有
    # SIGNAL.meta 行（含已被 veto 的标的）。pre_open 已 per-symbol 跳过 vetoed 不挂单（C2c），
    # 故 vetoed 标的永不成交、本函数理论上不会被 vetoed 标的触发；但保险起见，此处再查一次
    # latest_action=VETOED 显式跳过（防 pre_open/veto 时序窗口漏挂导致 vetoed 标的意外成交）。
    _aid_pre_tp = _resolve_account_id()
    _tid_tp = _state_store.build_trade_id(_aid_pre_tp, symbol, today)
    if _state_store.get_latest_action(_tid_tp) == "VETOED":
        logger.warning("挂止盈跳过：标的已被否决 symbol=%s（veto 终局防线，C3 follow-up）", symbol)
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
            OrderRequest(symbol=symbol, qty=need2, side="sell", price=tp2))
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
            OrderRequest(symbol=symbol, qty=need1, side="sell", price=tp1))
        if r1.get("state") not in ("REJECTED", "FAILED"):
            _record_tp("TP1", need1, tp1)
        else:
            logger.warning("止盈单挂失败 symbol=%s leg=tp1 state=%s msg=%s（人工补挂）",
                           symbol, r1.get("state"), r1.get("message"))
    if need2 > 0:
        r2 = await _submit(
            OrderRequest(symbol=symbol, qty=need2, side="sell", price=tp2))
        if r2.get("state") not in ("REJECTED", "FAILED"):
            _record_tp("TP2", need2, tp2)
        else:
            logger.warning("止盈单挂失败 symbol=%s leg=tp2 state=%s msg=%s（人工补挂）",
                           symbol, r2.get("state"), r2.get("message"))
