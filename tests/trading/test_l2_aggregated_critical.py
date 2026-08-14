# -*- coding: utf-8 -*-
"""U4: L2 单只失败聚合 CRITICAL, 不停调度 (_halted 保持 False).

物理意图 (spec §3 L2 / R3 防告警风暴):
    pre_open / stop_loss_monitor 单只 _submit RuntimeError (涨跌停挡板 / 资金不足 /
    限频拒单) 当前只 logger.warning - 研究员不知情 ([[qmt-connect-1-rootcause]] 全天
    锁死无告警教训). 但 N 只全拒时逐只 CRITICAL 会风暴. 改 L2 聚合: 循环末尾汇总一条
    _alert_critical ("pre_open 部分挂单被拒 N/M"), 研究员知情 + 不刷屏.

判定线 (严守 plan review 分层提示, 三层不混):
    - 框架级 L1 (U3a/U3b 已做): DB 写/幂等读/查持仓异常 -> raise _CriticalHalt 停调度;
    - 业务级 L2 (本 task): 单只 _submit 业务拒单 -> 计数 + 循环末尾聚合一条 CRITICAL,
      _halted 保持 False (整批继续, 不停调度);
    - 整批 submitted=0 已有 CRITICAL (engine.py:882, U3a 前就有), 保留; 本 task 加
      "部分拒" (n_rejected>0 且 n_submitted>0) 聚合 CRITICAL.

测试范式 (沿袭 test_critical_guard.py @pytest.mark.asyncio + brief Step 1 语义):
    缩进采用 test_pre_open_l1_halt.py 标准范式 (with 在 4 空格 / continuation 8 空格 /
    with 体 12 空格). _submit 的 AsyncMock(side_effect=[...]) 写成单行 - Python
    解析器对 [隐式行连接] 跨行 + with 显式 \ continuation 后跟 patch 存在歧义,
    单行写法绕开该坑. 断言: "聚合" + "不停调度", 覆盖 pre_open 部分拒 + stop_loss 部分发卖失败.
"""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from trading.engine import TradingEngine


@pytest.mark.asyncio
async def test_pre_open_partial_reject_aggregates_critical_not_halt(monkeypatch):
    """部分挂单被拒 -> 聚合一条 CRITICAL; _halted=False (L2 不停调度).

    构造: 2 只计划单, 第 1 只 _submit 返 SUBMITTED (挂成), 第 2 只 raise RuntimeError
    (涨停挡板业务拒单) -> n_rejected=1, n_submitted=1 -> 部分拒触发聚合 CRITICAL.
    断言: 聚合 CRITICAL 含 "被拒" 语义 + _halted 保持 False (L2 不停调度).
    """
    eng = TradingEngine()
    # W1-A/T2-Task14：pre_open 路径符号在 _pre_open_impl 函数体内读 phases.pre_open 顶部
    # import 的本地绑定（Task 4/6/7 切断 engine 反查后 patch trading.engine.X 全失效）→
    # 按「符号被读取时的 __globals__ 归属」迁物理路径 trading.phases.pre_open.X。
    # trading_plan 保 engine：pre_open 路径用 _state_store.list_signals_with_meta_by_plan_date
    # 读计划，根本不读 trading_plan（gate 被 monkeypatch 跳过）→ 死 patch 无副作用，保 engine 最安全。
    with patch("trading.phases.pre_open.get_gateway", return_value=None), \
            patch("trading.phases.pre_open._cancel_all_open_orders",
                  new=AsyncMock(return_value={"cancelled": 0, "unconfirmed": 0})), \
            patch("trading.phases.pre_open._scan_expired_positions", return_value=[]), \
            patch("trading.phases.pre_open._mode", return_value="live"), \
            patch("trading.phases.pre_open._alert_critical") as ac, \
            patch("trading.phases.pre_open._submit", new=AsyncMock(side_effect=[
                {"state": "SUBMITTED", "order_id": "seq1"},
                RuntimeError("涨停拒单")])), \
            patch("trading.phases.pre_open._state_store") as ss, \
            patch("trading.engine.trading_plan") as tp:
        # C2c：pre_open 直读 DB list_signals_with_meta_by_plan_date
        ss.list_signals_with_meta_by_plan_date.return_value = [
            {"symbol": "300214.SZ",
             "order": {"symbol": "300214.SZ", "qty": 100, "side": "buy", "price": 10.0},
             "formed_at": None, "stop_price": 9.0, "take_profit": 11.0, "max_wait": 5},
            {"symbol": "300215.SZ",
             "order": {"symbol": "300215.SZ", "qty": 100, "side": "buy", "price": 10.0},
             "formed_at": None, "stop_price": 9.0, "take_profit": 11.0, "max_wait": 5}]
        ss.build_trade_id.side_effect = lambda aid, sym, d: f"{aid}_{sym}_{d}"
        ss.get_account.return_value = MagicMock()
        ss.get_latest_action.return_value = "CONFIRMED"  # 确认闸 + per-symbol veto 通过
        ss.has_order.return_value = False
        # G6 语义（2026-08-14）：pre_open 现在消费 insert_order 返回值——False/None=UNIQUE
        # 占位中止 _submit（防 DB/柜台脱节幽灵单）。本测试构造「落库成功、柜台业务拒单」，
        # 故 mock 必须返 True（旧 None 在 G6 下会被误判占位→两只全中止→submitted=0 假失败）。
        ss.insert_order.return_value = True
        ss.update_order_state.return_value = None
        monkeypatch.setattr(eng, "_pre_open_gate", AsyncMock(return_value=(True, "")))
        from trading.engine import pre_open
        result = await pre_open("2026-07-31", ports=eng._ports)
    assert result["submitted"] == 1   # 第 1 只挂成 / 第 2 只业务拒单 (部分拒触发 L2 聚合)
    # 聚合 CRITICAL 含 "被拒" 语义, _halted 保持 False (L2 不停调度)
    assert any("被拒" in str(c) for c in ac.call_args_list)
    # 「聚合一条」守护：只计「被拒」语义告警（防聚合退化为逐只告警风暴）。
    # gw=None + live 另触发的「熔断基线缺失」CRITICAL（account_daily 修复 2026-08-11）
    # 是独立维度告警，不计入本聚合计数（总 alert 数因 gw 装配态而异，不硬断言总数）。
    reject_alerts = [c for c in ac.call_args_list if "被拒" in str(c)]
    assert len(reject_alerts) == 1
    assert eng._halted is False


@pytest.mark.asyncio
async def test_stop_loss_partial_submit_fail_aggregates_critical_not_halt(monkeypatch):
    """stop_loss 部分卖出失败 -> 聚合一条 CRITICAL; _halted=False (L2 不停调度).

    构造: 2 只持仓均触发 decide_exit CLOSE/STOP_LOSS, 第 1 只 _submit 成功 (发卖成),
    第 2 只 raise RuntimeError (gw 挡板/lock_down) -> n_submit_failed=1 -> 末尾聚合 CRITICAL.
    断言: 聚合 CRITICAL 含 "卖出失败" 语义 + _halted 保持 False (漏止损须人工补单, 但
    整批监控不停, 其他标的继续巡检).
    """
    eng = TradingEngine()
    from trading import engine as eng_mod

    # 放行盘中时段判定 (否则 monitor 第一行就 return checked:0)
    monkeypatch.setattr(eng_mod.calendar, "is_intraday_session", lambda _dt: True)

    gw = AsyncMock()
    gw._fetch_broker_positions.return_value = {
        "300214.SZ": {"volume": 100, "avg_price": 10.0},
        "300215.SZ": {"volume": 100, "avg_price": 10.0}}
    monkeypatch.setattr(eng_mod.qmt_market_data, "get_quotes",
                        AsyncMock(return_value={
                            "300214.SZ": {"last_price": 8.5, "high": 10.5, "low": 8.4},
                            "300215.SZ": {"last_price": 8.5, "high": 10.5, "low": 8.4}}))

    # 两只均 decide_exit CLOSE/STOP_LOSS portion=1.0 (全平 -> 触发卖出分支)
    # ExitAction / ExitReason 是枚举单例（engine re-export 与 phases.stop_loss 顶部 import
    # 同源同类 → eng_mod.ExitAction.CLOSE IS phases.stop_loss.ExitAction.CLOSE，is 比较成立）。
    fake_dec = MagicMock()
    fake_dec.action = eng_mod.ExitAction.CLOSE
    fake_dec.reason = eng_mod.ExitReason.STOP_LOSS
    fake_dec.portion = 1.0
    # W1-A/T2-Task14：decide_exit 迁 trading.phases.stop_loss.decide_exit——stop_loss_monitor
    # 行 354 读 phases.stop_loss 顶部 from execution import decide_exit 本地绑定（Task 4 切断
    # engine 反查后 monkeypatch.setattr(eng_mod, "decide_exit") 失效，真实 decide_exit 抛
    # KeyError 'neckline' 触发 D12 降级 → stop_triggered=0）。改字符串路径形式 patch 模块属性。
    monkeypatch.setattr("trading.phases.stop_loss.decide_exit", lambda *a, **kw: fake_dec)

    # 第 1 只 _submit 成功; 第 2 只 raise RuntimeError (业务拒单/挡板)
    # W1-A/T2-Task14：_submit 迁 trading.phases.stop_loss._submit——主路径 CLOSE/STOP_LOSS
    # 分支（stop_loss.py:408）与 fallback 分支（:452）均读 phases.stop_loss 顶部 from
    # gateway_service import _submit 本地绑定（Task 7 切断后 monkeypatch eng_mod 失效）。
    monkeypatch.setattr("trading.phases.stop_loss._submit", AsyncMock(side_effect=[
        {"state": "FILLED", "order_id": "seq1"},
        RuntimeError("gw lock_down 拒单")]))

    # W1-A/T2-Task14：_mode / _alert_critical / _state_store 迁 trading.phases.stop_loss.X
    # ——stop_loss_monitor 函数体读 phases.stop_loss 顶部 import 本地绑定（Task 4 切断后
    # patch trading.engine.X 失效）。_state_store 是整体 patch（替换 phases.stop_loss 本地
    # 名 _state_store 为 MagicMock；与 Task13 属性级 patch state_store.xxx 命中共享对象区分）。
    with patch("trading.phases.stop_loss._mode", return_value="live"), \
            patch("trading.phases.stop_loss._alert_critical") as ac, \
            patch("trading.phases.stop_loss._state_store") as ss:
        ss.has_order.return_value = False   # 幂等读通过 (无已挂 STOP)
        ss.get_account.return_value = MagicMock()
        from trading.engine import stop_loss_monitor
        result = await stop_loss_monitor(
            stop_prices=None, gw=gw,
            monitor_ctx={
                "300214.SZ": {"state": {"stop": 9.0}, "cfg": {}},
                "300215.SZ": {"state": {"stop": 9.0}, "cfg": {}}})

    assert result["stop_triggered"] == 1   # 第 1 只发卖成; 第 2 只失败不计
    # 聚合 CRITICAL 含 "卖出失败" 语义, _halted 保持 False (L2 不停调度)
    assert any("卖出失败" in str(c) for c in ac.call_args_list)
    assert len(ac.call_args_list) == 1   # 守护「聚合一条」防未来回归成逐只告警风暴
    assert eng._halted is False
