# -*- coding: utf-8 -*-
"""T-1 交易计划单测（Task 8）。

覆盖：save/load/confirm 落盘 + 确认闸 + 钉钉推送格式化。
orders 统一用嵌套格式（与 Task 9 engine.eod_plan 生产侧、push_plan_to_dingtalk 消费侧
全链路一致）：
    {"order": {symbol/qty/side/price}, "stop_price": ..., "take_profit": ...}
"""
from trading import trading_plan as tp


def _sample_nested_orders():
    """构造嵌套格式 orders（与 engine.eod_plan 产物同构）。"""
    return [
        {
            "order": {"symbol": "600000.SH", "qty": 5000, "side": "buy", "price": 10.0},
            "stop_price": 8.5,
            "take_profit": 11.5,
        }
    ]


def test_save_load_confirm(tmp_path, monkeypatch):
    """save_plan 落盘 confirmed=false → load_plan 回读一致 → confirm_plan 置 true。"""
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))
    orders = _sample_nested_orders()
    p = tp.save_plan("2026-07-22", orders)
    assert p.exists()
    plan = tp.load_plan("2026-07-22")
    assert plan is not None and plan["orders"] == orders
    assert plan["confirmed"] is False
    assert tp.confirm_plan("2026-07-22") is True
    assert tp.load_plan("2026-07-22")["confirmed"] is True


def test_load_plan_missing(tmp_path, monkeypatch):
    """计划不存在返 None（pre_open 检查时会据此跳过挂单）。"""
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))
    assert tp.load_plan("2099-01-01") is None


def test_load_plan_corrupt(tmp_path, monkeypatch):
    """计划文件损坏（非法 JSON）返 None 不抛，避免阻塞次日流程。"""
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))
    p = tp._plan_path("2026-07-23")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not valid json", encoding="utf-8")
    assert tp.load_plan("2026-07-23") is None


def test_confirm_plan_missing(tmp_path, monkeypatch):
    """对不存在的计划调 confirm_plan 返 False（防幻觉确认）。"""
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))
    assert tp.confirm_plan("2099-01-01") is False


def test_confirm_plan_idempotent(tmp_path, monkeypatch):
    """重复确认幂等：二次调用仍返 True，文件保持 confirmed=true。"""
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))
    orders = _sample_nested_orders()
    tp.save_plan("2026-07-22", orders)
    assert tp.confirm_plan("2026-07-22") is True
    assert tp.confirm_plan("2026-07-22") is True  # 二次确认仍成功
    assert tp.load_plan("2026-07-22")["confirmed"] is True


def test_save_plan_uses_custom_dir(tmp_path, monkeypatch):
    """TRADE_PLAN_DIR 自定义路径生效，且自动建父目录。"""
    custom = tmp_path / "nested" / "plans"
    monkeypatch.setenv("TRADE_PLAN_DIR", str(custom))
    orders = _sample_nested_orders()
    p = tp.save_plan("2026-07-24", orders)
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
