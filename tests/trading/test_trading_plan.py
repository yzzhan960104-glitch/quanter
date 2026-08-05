# -*- coding: utf-8 -*-
"""T-1 交易计划单测（Task 8 + SSoT Phase C · C3）。

覆盖：C3 load_plan DB 优先 + 钉钉推送格式化 + legacy shim（测试专用 JSON 落盘/确认）。
orders 统一用嵌套格式（与 Task 9 engine.eod_plan 生产侧、push_plan_to_dingtalk 消费侧
全链路一致）：
    {"order": {symbol/qty/side/price}, "stop_price": ..., "take_profit": ...}

C3：生产 save_plan/confirm_plan 已删（DB SIGNAL/CONFIRMED 真相源），原 save/confirm 落盘
测试改测 tests/_legacy_plan_io.py 的 legacy shim（保留测试种子能力，~50 处历史调用依赖）。
"""
import json

from trading import state_store, trading_plan as tp


def _sample_nested_orders():
    """构造嵌套格式 orders（与 engine.eod_plan 产物同构）。"""
    return [
        {
            "order": {"symbol": "600000.SH", "qty": 5000, "side": "buy", "price": 10.0},
            "stop_price": 8.5,
            "take_profit": 11.5,
        }
    ]


def test_save_load_confirm(tmp_db, tmp_path, monkeypatch):
    """C3：测试专用 legacy shim save_plan_legacy/confirm_plan_legacy 仍可用（保留测试种子能力）。

    物理：生产 save_plan/confirm_plan 已删（DB SIGNAL/CONFIRMED 真相源）；本测试验证
    tests/_legacy_plan_io.py 的 shim 仍能正确落盘 JSON + 写 DB CONFIRMED——历史测试种子
    继续工作（覆盖 test_critical_guard / test_engine_alerts / test_e2e_trading_flow 等
    ~50 处种子调用）。
    """
    from tests import _legacy_plan_io as legacy
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))
    orders = _sample_nested_orders()
    p = legacy.save_plan_legacy("2026-07-22", orders)
    assert p.exists()
    # legacy.confirm_plan_legacy 写 DB CONFIRMED + JSON confirmed=true
    assert legacy.confirm_plan_legacy("2026-07-22") is True
    plan = json.loads(p.read_text(encoding="utf-8"))
    assert plan["confirmed"] is True


def test_load_plan_missing(tmp_path, monkeypatch):
    """计划不存在返 None（pre_open 检查时会据此跳过挂单）。"""
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))
    assert tp.load_plan("2099-01-01") is None


# ============================================================================
# SSoT Phase C · C3：load_plan DB 优先（SIGNAL.meta 真相源）
# ============================================================================
def _seed_signal_db(tmp_db, account_id, symbol, date, *, confirmed=False, vetoed=False,
                    order_dict_extra=None):
    """写 DB SIGNAL + 可选 CONFIRMED/VETOED 行（C3 load_plan DB 优先种子）。

    顺序约束（与 eod_plan/_confirmed_plan_one_order 同口径）：SIGNAL 必须先写保证
    event_id < CONFIRMED/VETOED（latest_action ORDER BY event_id DESC 才会返最新 action）。
    """
    order = {"symbol": symbol, "qty": 100, "side": "buy", "price": 10.0}
    od = {"order": order, "stop_price": 9.0, "take_profit": 11.0,
          "formed_at": date, "max_wait": 5}
    if order_dict_extra:
        od.update(order_dict_extra)
    meta_obj = {**od, "plan_date": date, "strategy_name": "neckline",
                "rationale": f"颈线法@{od.get('formed_at', '')}"}
    tid = state_store.build_trade_id(account_id, symbol, date)
    state_store.insert_trade_event(
        account_id, tid, symbol, "SIGNAL",
        meta=json.dumps(meta_obj, ensure_ascii=False))
    if confirmed:
        state_store.insert_trade_event(account_id, tid, symbol, "CONFIRMED")
    if vetoed:
        # VETOED 必须在 CONFIRMED 之后写（latest_action 返最新=VETOED）
        state_store.insert_trade_event(account_id, tid, symbol, "VETOED")


def test_load_plan_db_first_confirmed(tmp_db, monkeypatch):
    """C3：DB 优先 · 全部标的 latest=CONFIRMED → confirmed=True（消费方契约不变）。"""
    monkeypatch.setenv("QMT_ACCOUNT_ID", "ACC_TEST")
    monkeypatch.setenv("TRADE_PLAN_DIR", "/nonexistent/no_json_fallback")  # 防 JSON 回退
    date = "2026-08-05"
    _seed_signal_db(tmp_db, "ACC_TEST", "600000.SH", date, confirmed=True)
    _seed_signal_db(tmp_db, "ACC_TEST", "600001.SH", date, confirmed=True)

    plan = tp.load_plan(date)

    assert plan is not None
    assert plan["date"] == date
    assert plan["confirmed"] is True  # 全部 CONFIRMED
    assert len(plan["orders"]) == 2   # 两个 SIGNAL meta
    # orders 来自 SIGNAL.meta（含 C1 字段 plan_date/strategy_name）
    syms = [(o.get("order") or {}).get("symbol") for o in plan["orders"]]
    assert "600000.SH" in syms and "600001.SH" in syms


def test_load_plan_db_first_signal_only_unconfirmed(tmp_db, monkeypatch):
    """C3：DB 有 SIGNAL 但无 CONFIRMED 行 → confirmed=False（SIGNAL-only = 未确认）。"""
    monkeypatch.setenv("QMT_ACCOUNT_ID", "ACC_TEST")
    monkeypatch.setenv("TRADE_PLAN_DIR", "/nonexistent/no_json_fallback")
    date = "2026-08-05"
    _seed_signal_db(tmp_db, "ACC_TEST", "600000.SH", date, confirmed=False)

    plan = tp.load_plan(date)

    assert plan is not None
    assert plan["confirmed"] is False  # latest=SIGNAL 非 CONFIRMED
    assert len(plan["orders"]) == 1


def test_load_plan_vetoed_after_confirmed(tmp_db, monkeypatch):
    """C3 边界（veto 终局防线）：VETOED 事件晚于 CONFIRMED → load_plan confirmed=False。

    物理红线：研究员否决是 opt-out 终局动作。即使 eod_plan auto_confirm 已写 CONFIRMED，
    研究员 pre_open 前 veto 写 VETOED（event_id > CONFIRMED），get_latest_action
    按 event_id DESC 返 VETOED → load_plan 视作未确认 → pre_open/place_take_profit
    跳过该标的。这是全自动模式下 veto 防线的最后一道闸（spec §6 C3）。
    """
    monkeypatch.setenv("QMT_ACCOUNT_ID", "ACC_TEST")
    monkeypatch.setenv("TRADE_PLAN_DIR", "/nonexistent/no_json_fallback")
    date = "2026-08-05"
    _seed_signal_db(tmp_db, "ACC_TEST", "600000.SH", date,
                    confirmed=True, vetoed=True)  # VETOED 晚于 CONFIRMED

    plan = tp.load_plan(date)

    assert plan is not None
    assert plan["confirmed"] is False  # VETOED 是最新 action


def test_load_plan_db_exception_falls_back_to_json(tmp_path, monkeypatch):
    """C3 兼容窗口：DB 异常 → 回退读 plan_*.json（只读兼容，保留一发布周期）。

    物理：DB 未初始化 / 表不存在 / 文件锁等异常时，load_plan 不抛错，回退 JSON。
    历史/老测试 JSON 仍可读，平滑迁移。
    """
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))
    # 制造 DB 异常：monkeypatch list_signals_with_meta_by_plan_date 抛错
    def boom(*a, **kw):
        raise RuntimeError("DB down")
    monkeypatch.setattr(state_store, "list_signals_with_meta_by_plan_date", boom)
    # 落 JSON（模拟历史 plan）
    orders = _sample_nested_orders()
    p = tp._plan_path("2026-08-05")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"date": "2026-08-05", "confirmed": True, "orders": orders}),
                 encoding="utf-8")

    plan = tp.load_plan("2026-08-05")

    assert plan is not None
    assert plan["confirmed"] is True
    assert plan["orders"] == orders


def test_load_plan_no_signal_no_json_returns_none(tmp_db, monkeypatch):
    """C3：DB 无 SIGNAL + JSON 不存在 → None（pre_open 保守跳过）。"""
    monkeypatch.setenv("QMT_ACCOUNT_ID", "ACC_TEST")
    monkeypatch.setenv("TRADE_PLAN_DIR", "/nonexistent/no_json_either")
    assert tp.load_plan("2099-01-01") is None


def test_load_plan_corrupt(tmp_path, monkeypatch):
    """计划文件损坏（非法 JSON）返 None 不抛，避免阻塞次日流程。"""
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))
    p = tp._plan_path("2026-07-23")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not valid json", encoding="utf-8")
    assert tp.load_plan("2026-07-23") is None


def test_confirm_plan_missing_legacy_shim(tmp_path, monkeypatch):
    """C3：legacy shim confirm_plan_legacy 对不存在的计划返 False（防幻觉确认）。"""
    from tests import _legacy_plan_io as legacy
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))
    assert legacy.confirm_plan_legacy("2099-01-01") is False


def test_confirm_plan_idempotent_legacy_shim(tmp_db, tmp_path, monkeypatch):
    """C3：legacy shim 重复确认幂等（二次调用仍 True，DB CONFIRMED 幂等跳过）。"""
    from tests import _legacy_plan_io as legacy
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))
    monkeypatch.setenv("QMT_ACCOUNT_ID", "ACC_TEST")
    orders = _sample_nested_orders()
    legacy.save_plan_legacy("2026-07-22", orders)
    assert legacy.confirm_plan_legacy("2026-07-22") is True
    assert legacy.confirm_plan_legacy("2026-07-22") is True  # 二次确认仍成功（DB UNIQUE 幂等）
    plan = json.loads(legacy._plan_path("2026-07-22").read_text(encoding="utf-8"))
    assert plan["confirmed"] is True


def test_save_plan_legacy_uses_custom_dir(tmp_path, monkeypatch):
    """C3：legacy shim TRADE_PLAN_DIR 自定义路径生效，自动建父目录。"""
    from tests import _legacy_plan_io as legacy
    custom = tmp_path / "nested" / "plans"
    monkeypatch.setenv("TRADE_PLAN_DIR", str(custom))
    orders = _sample_nested_orders()
    p = legacy.save_plan_legacy("2026-07-24", orders)
    assert p.parent == custom
    assert p.exists()


def test_push_plan_to_dingtalk_format_and_passthrough(tmp_path, monkeypatch):
    """push_plan_to_dingtalk：格式化嵌套 orders 不 KeyError，且透传 push_brief 返回值。

    影子模式红线：monkeypatch 掉 broadcast.push.push_brief，绝不真发 dws。
    """
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))
    monkeypatch.setenv("TRADING_BOT_ROBOT_CODE", "robot-xyz")
    monkeypatch.setenv("BROADCAST_GROUP_ID", "group-abc")

    captured = {}

    def fake_push_brief(title, markdown, *, robot_code, group_id, dry_run=False, timeout=30):
        captured["title"] = title
        captured["markdown"] = markdown
        captured["robot_code"] = robot_code
        captured["group_id"] = group_id
        captured["call_count"] = captured.get("call_count", 0) + 1
        return True

    # 替换 trading_plan 模块内 import 的 push_brief 引用（from broadcast.push import push_brief）
    monkeypatch.setattr(tp, "push_brief", fake_push_brief)

    orders = _sample_nested_orders()
    result = tp.push_plan_to_dingtalk("2026-07-22", orders)

    assert result is True
    assert captured["call_count"] == 1
    assert captured["robot_code"] == "robot-xyz"
    assert captured["group_id"] == "group-abc"
    assert captured["title"] == "交易计划 2026-07-22"
    md = captured["markdown"]
    o = orders[0]["order"]
    assert o["symbol"] in md
    assert o["side"] in md
    assert str(o["qty"]) in md
    assert str(o["price"]) in md
    assert str(orders[0]["stop_price"]) in md
    assert str(orders[0]["take_profit"]) in md
    assert "2026-07-22" in md


def test_push_plan_to_dingtalk_returns_false_on_push_failure(tmp_path, monkeypatch):
    """push_brief 返 False（如缺凭证/超时/dws 不在）时透传 False，不抛。"""
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))

    def fake_push_brief(title, markdown, *, robot_code, group_id, dry_run=False, timeout=30):
        return False

    monkeypatch.setattr(tp, "push_brief", fake_push_brief)
    assert tp.push_plan_to_dingtalk("2026-07-22", _sample_nested_orders()) is False


def test_push_plan_md_includes_rr(monkeypatch):
    """R3：push_plan_to_dingtalk md 含实际盈亏比 rr（研究员人审看真实风险报酬比）。

    物理意图（颈线法算法修复 R3）：detect 已算出实际口径 rr（基于真实止损/止盈价），
    全链路 Signal.rr → PlannedOrder.rr → order_dict["rr"] → 此处 md 渲染「盈亏比N.N」，
    让研究员 T-1 晚人审时看到每单的真实风险报酬比——而非历史写死的 "2.0"。
    缺此渲染 → 人审凭直觉拍，盈亏比 1.2 的弱信号可能蒙混过确认闸，实盘敞口失稳。
    影子模式红线：monkeypatch push_brief 绝不真发 dws。
    """
    from trading import trading_plan
    captured = {}
    # 与既有 test 同款 monkeypatch：替换 trading_plan 模块内 push_brief 引用
    monkeypatch.setattr(trading_plan, "push_brief",
                        lambda title, md, **kw: captured.update(md=md) or True)
    orders = [{"order": {"symbol": "T.SZ", "side": "buy", "qty": 100, "price": 10.0},
               "stop_price": 9.0, "take_profit": 12.0, "rr": 2.5}]
    trading_plan.push_plan_to_dingtalk("2026-07-27", orders)
    assert "2.5" in captured["md"]  # md 显示 rr
