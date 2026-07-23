# -*- coding: utf-8 -*-
"""成交回报 handler（Task 10 · 修 G5）：日志补写 + 钉钉成交通知 + 挂止盈三连。

物理意图（spec §6.2 C1）：
    on_stock_trade 回调（Task 11 注册 _on_order_update 后）推送 ``kind=="trade"``
    的成交回报 → TradingEngine._handle_order_update 被调度执行三件事：
      a. record_live_trade 补写成交回报日志（用真实成交价/量，非下单预估价）；
      b. notify_trade_event 推钉钉成交通知（fire_and_forget 不阻塞回调链）；
      c. 若为买单成交（查 gw._orders 拿 side）且该 symbol 未挂止盈（幂等）→
         挂限价止盈卖单（Phase1 简化版全额）。

测试边界（Grill Me · 控制器 scope #5）：
    绝不真起 APScheduler、绝不做真行情/真单/真钉钉：TradingEngine 仅实例化（装配
    4 job 不 start），``record_live_trade`` / ``NotificationManager`` /
    ``trading_plan.load_plan`` / ``_place_take_profit`` 均 patch 拦截。

TDD 约定（与 Task 7/8/9 一致）：
    本仓库 pytest-asyncio 为 strict 模式（pytest.ini 未配 asyncio_mode），
    历史 engine 测试一律 ``asyncio.run(...)`` 同步驱动 async。本测试沿袭该范式，
    避免引入 @pytest.mark.asyncio 装饰器造成风格分叉（见 Task8 fix 备注）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from trading.engine import TradingEngine


def test_trade_update_writes_log_and_notifies():
    """成交回报 → 补写成交日志 + 推钉钉成交通知（三连中的 a + b）。

    断言：
      1) ``record_live_trade`` 被调一次（成交日志补写，方向由 gw._orders 判定）；
      2) 成交日志首参（symbol）含回报里的 stock_code（防字段拼错）；
      3) ``notify_trade_event`` 被调一次（钉钉成交通知）。
    """
    eng = TradingEngine()
    # 成交回报 update（on_stock_trade 推送的真实契约：kind=trade + 量价齐全）
    update = {
        "kind": "trade",
        "order_id": "123",
        "stock_code": "300001.SZ",
        "traded_volume": 100,
        "traded_price": 10.5,
        "traded_amount": 1050.0,
        "traded_time": 20260723,
        "state": "FILLED",
    }
    eng._tp_placed = set()           # 幂等标记初始化（与 __init__ 同语义，显式重申）
    eng._gw = MagicMock()
    eng._gw._orders = {"123": {"order_type": 23}}  # 23=STOCK_BUY（买单标记）
    # patch 真实模块路径（_handle_order_update 内 lazy import 这些符号，故 patch 真身模块
    # 而非 trading.engine —— engine 模块顶层不 import 这两个符号，避免循环依赖）：
    #   - record_live_trade 实身：server.services.trading_service
    #   - NotificationManager 实身：infra.notifier（core.notifier 是转发垫片）
    fake_mgr = MagicMock()
    fake_mgr.notify_trade_event = AsyncMock(return_value=[])
    with patch("server.services.trading_service.record_live_trade") as rec, \
         patch("infra.notifier.NotificationManager") as NM:
        NM.get_default.return_value = fake_mgr
        asyncio.run(eng._handle_order_update(update))
    # a. 成交日志补写：record_live_trade 被调一次，首参=symbol
    rec.assert_called_once()
    assert "300001.SZ" in str(rec.call_args)
    # b. 钉钉成交通知：notify_trade_event 被调一次（symbol/direction/qty/price 四要素）
    fake_mgr.notify_trade_event.assert_called_once()
    ntf_args, _ = fake_mgr.notify_trade_event.call_args
    assert ntf_args[0] == "300001.SZ"  # symbol
    assert ntf_args[1] == "BUY"        # direction（据 order_type=23=STOCK_BUY 判定）
    assert ntf_args[2] == 100          # qty
    assert ntf_args[3] == 10.5         # price


def test_buy_fill_places_take_profit_once_idempotent():
    """买单成交 → 挂止盈；重复回报幂等不重挂（三连中的 c + 幂等防重挂红线）。

    物理意图（幂等为何是红线）：
        on_stock_trade 在部分成交/柜台重推时会多次推送同一 order_id 的 trade 回报。
        若每次都重挂止盈卖单，会导致同一笔持仓挂出 N 张止盈单 → 超卖敞口致命。
        ``_tp_placed`` 集合以 symbol 为 key 标记已挂止盈，二次回报命中即跳过。

    断言：
      1) 首次买单成交 → ``_place_take_profit`` 被调一次（挂止盈）；
      2) 重复回报（同 symbol）→ ``_place_take_profit`` 总调用次数仍为 1（幂等）。
    """
    eng = TradingEngine()
    eng._tp_placed = set()
    update = {
        "kind": "trade",
        "order_id": "123",
        "stock_code": "300001.SZ",
        "traded_volume": 100,
        "traded_price": 10.5,
        "state": "FILLED",
    }
    plan = {
        "confirmed": True,
        "orders": [
            {
                "order": {"symbol": "300001.SZ", "qty": 100, "side": "buy", "price": 10.0},
                "stop_price": 9.5,
                "take_profit": 12.0,
            }
        ],
    }
    gw = MagicMock()
    gw._orders = {"123": {"order_type": 23}}  # 23=STOCK_BUY（买单标记）
    eng._gw = gw
    # patch 全部真实副作用：止盈挂单 mock 成 AsyncMock（不触达 gw/_submit）。
    # trading_plan 是 engine 顶层 import（``from trading import trading_plan``），
    # 故 patch ``trading.engine.trading_plan.load_plan``；其余两符号走真实模块路径
    # （同 test_trade_update_writes_log_and_notifies 注释）。
    with patch("trading.engine.trading_plan.load_plan", return_value=plan), \
         patch("server.services.trading_service.record_live_trade"), \
         patch("infra.notifier.NotificationManager"), \
         patch.object(eng, "_place_take_profit", new=AsyncMock()) as tp:
        asyncio.run(eng._handle_order_update(update))  # 首次成交回报
        asyncio.run(eng._handle_order_update(update))  # 重复回报（部分成交重推/柜台重推）
    # 幂等断言：_place_take_profit 只被调一次（防超卖敞口）
    tp.assert_called_once()


def test_place_take_profit_truncates_fractional_qty_to_int():
    """``_place_take_profit`` 用 ``int(filled_qty)`` 截断部分成交的零股（A 股整手红线）。

    物理意图（A 股整手约束 · spec §6.2 C1 数量来源）：
        成交回报 ``traded_volume`` 在柜台部分成交回报里可能带小数（如 100.5，源于
        柜台内部整股+零股混合计量或行情推送精度）。A 股卖出**必须整手**（100 股整数倍，
        北交所/科创板保留 1 股粒度但仍须整数）——若把 100.5 直接喂给 broker 下单接口，
        轻则 broker 拒单（无效数量），重则部分柜台按 100.5 解释成 10050 股致超卖敞口。
        故 ``_place_take_profit`` 内 ``OrderRequest(qty=int(filled_qty), ...)`` 是
        live 安全红线（与 stop_loss ``qty 必须来自 gw 持仓整手``同源）。

    断言：
        patch 真实 ``_submit``（非 patch ``_place_take_profit``），调
        ``_handle_order_update`` 传 traded_volume=100.5，断言 ``_submit`` 收到的
        ``OrderRequest.qty == 100``（int 截断，非 100.5 也非 10050）。
    """
    eng = TradingEngine()
    eng._tp_placed = set()
    update = {
        "kind": "trade",
        "order_id": "456",
        "stock_code": "300002.SZ",
        "traded_volume": 100.5,   # 带小数的部分成交（柜台精度产物）
        "traded_price": 11.0,
        "state": "FILLED",
    }
    plan = {
        "confirmed": True,
        "orders": [
            {
                "order": {"symbol": "300002.SZ", "qty": 100, "side": "buy", "price": 10.8},
                "stop_price": 10.0,
                "take_profit": 12.5,
            }
        ],
    }
    gw = MagicMock()
    gw._orders = {"456": {"order_type": 23}}  # 23=STOCK_BUY（买单触发挂止盈）
    eng._gw = gw
    # patch 真身路径（同既有 test_buy_fill_places_take_profit_once_idempotent）：
    #   - ``_submit`` patch 成 AsyncMock 拦截真实下单（拿到 OrderRequest 检查 qty）；
    #   - ``trading.engine.trading_plan.load_plan`` 返含 take_profit 的计划；
    #   - record_live_trade / NotificationManager patch 掉日志与通知副作用。
    with patch("trading.engine.trading_plan.load_plan", return_value=plan), \
         patch("server.services.trading_service.record_live_trade"), \
         patch("infra.notifier.NotificationManager"), \
         patch("trading.engine._submit", new=AsyncMock(return_value={"state": "FILLED"})) as submit_mock:
        asyncio.run(eng._handle_order_update(update))
    # _submit 必被调一次（挂止盈）；OrderRequest.qty 必须 == 100（int 截断零股）
    submit_mock.assert_called_once()
    order_req = submit_mock.call_args.args[0]
    assert order_req.qty == 100          # int(100.5) == 100（A 股整手红线）
    assert isinstance(order_req.qty, int)  # 类型必须是 int（防 broker 按 float 解释成 10050）
