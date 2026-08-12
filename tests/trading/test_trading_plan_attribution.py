# -*- coding: utf-8 -*-
"""eod_plan order_dict 透传归因 + DB SIGNAL.meta 往返保真（C3：legacy shim + DG-5 收尾）。

物理意图：Task 5 让 PlannedOrder 携带 experiment_id/experiment_weight，
本套测试验证 DB SIGNAL.meta 透传这两个归因字段——既保证新 plan
往返保真（report 阶段聚合实验归因的物理基础），又保证老 plan（无归因字段）
向后兼容不崩（report 归「未归因」桶）。

C3 → DG-5 收尾（2026-08-12）：生产 save_plan 已删（DB SIGNAL 真相源）；测试用
``tests/_legacy_plan_io.save_plan_legacy`` 落盘 JSON 镜像（C3 后是导出产物）。
``load_plan`` JSON 读侧窗口已关闭——归因字段经 DB SIGNAL.meta 读出，故本套测试
显式种 DB SIGNAL 行（meta JSON 含 experiment_id/experiment_weight）。
"""
import json as _json

from trading import state_store, trading_plan
from tests._legacy_plan_io import save_plan_legacy


def _seed_db_signal_meta(account_id: str, symbol: str, date: str, order_dict: dict) -> None:
    """种 DB trade_event(SIGNAL) 行——meta JSON 含完整 order_dict + C1 归一字段。

    DG-5 收尾：``load_plan`` 关闭 JSON 读侧 fallback 后，归因字段必须经 DB SIGNAL.meta
    才能被 load_plan 读到。本 helper 与 ``tests/trading/test_trading_plan._seed_signal_db``
    同口径（meta 形状 = ``{**order_dict, plan_date, strategy_name, rationale}``）。
    """
    meta_obj = {**order_dict, "plan_date": date, "strategy_name": "neckline",
                "rationale": f"颈线法@{order_dict.get('formed_at', '')}"}
    tid = state_store.build_trade_id(account_id, symbol, date)
    state_store.insert_trade_event(
        account_id, tid, symbol, "SIGNAL",
        meta=_json.dumps(meta_obj, ensure_ascii=False))


def test_save_plan_preserves_experiment_attribution(tmp_db, tmp_path, monkeypatch):
    """orders 嵌套 dict 带 experiment_id/experiment_weight，DB SIGNAL.meta 往返保真。

    Why：Task8 report 要按 experiment_id 聚合归因，DB SIGNAL.meta 必须原样带回
    归因字段，否则 report 阶段拿不到实验分组信息。

    DG-5 收尾：load_plan JSON 读侧窗口关闭，归因字段改经 DB SIGNAL.meta 读出。
    save_plan_legacy 仍写 JSON 镜像（C3 后是导出产物），但 load_plan 不再读它。
    """
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))
    monkeypatch.setenv("QMT_ACCOUNT_ID", "ACC_TEST")
    orders = [{
        "order": {"symbol": "000001.SZ", "qty": 1000, "side": "buy", "price": 10.0},
        "stop_price": 9.0, "take_profit": 11.0,
        "experiment_id": "neckline_v6_20260722", "experiment_weight": 0.2,
    }]
    save_plan_legacy("2026-07-22", orders)
    # DG-5 收尾：种 DB SIGNAL（meta 含归因字段）——load_plan JSON 读侧窗口已关闭
    _seed_db_signal_meta("ACC_TEST", "000001.SZ", "2026-07-22", orders[0])

    loaded = trading_plan.load_plan("2026-07-22")
    assert loaded["orders"][0]["experiment_id"] == "neckline_v6_20260722"
    assert loaded["orders"][0]["experiment_weight"] == 0.2


def test_old_plan_without_attribution_loads_ok(tmp_db, tmp_path, monkeypatch):
    """老 plan（无归因字段）load 不崩（向后兼容，report 归「未归因」桶）。

    Why：实验系统上线前已有大量历史 plan 文件不带归因字段，load 时不能因
    KeyError 崩掉——Task8 report 应将这类订单归入「未归因」桶单独统计。

    DG-5 收尾：load_plan JSON 读侧窗口关闭——老 plan 无归因字段的语义改经
    DB SIGNAL.meta 体现（meta 不带 experiment_id → load_plan 读出的 dict 不含该键）。
    """
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))
    monkeypatch.setenv("QMT_ACCOUNT_ID", "ACC_TEST")
    orders = [{"order": {"symbol": "X", "qty": 100, "side": "buy", "price": 10},
               "stop_price": 9, "take_profit": 11}]
    save_plan_legacy("2026-07-20", orders)
    # DG-5 收尾：种 DB SIGNAL（meta 无归因字段）——模拟老 plan
    _seed_db_signal_meta("ACC_TEST", "X", "2026-07-20", orders[0])

    loaded = trading_plan.load_plan("2026-07-20")
    assert "experiment_id" not in loaded["orders"][0]   # 老无字段，不崩
