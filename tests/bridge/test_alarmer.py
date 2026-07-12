# -*- coding: utf-8 -*-
"""alarmer 测试：高危模式命中即触发告警（mock notifier，不发真消息）。"""
from unittest.mock import MagicMock, patch

from bridge.alarmer import Alarmer


def _tool_use_event(name: str, inp: dict) -> dict:
    """构造 assistant 帧，content 含一个 tool_use 项。

    帧字段以 Task 7 Step 0 实测为准（claude CLI stream-json 真实输出）：
        {"type":"assistant","message":{"content":[{"type":"tool_use",
         "id":"call_xxx","name":"Read","input":{...}}]}}
    测试用最小子集即可触发 _extract_tool_use。
    """
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]},
    }


def test_safe_tool_no_alert():
    """读普通文件不告警。"""
    mgr = MagicMock()
    al = Alarmer(notify=lambda msg, level: mgr(msg, level))
    al.check_event(_tool_use_event("Read", {"file_path": "caisen/w_bottom.py"}),
                   sender_staff_id="staff")
    mgr.assert_not_called()


def test_dangerous_path_triggers_alert():
    """碰 trading/ 路径 → 告警。"""
    mgr = MagicMock()
    al = Alarmer(notify=lambda msg, level: mgr(msg, level))
    al.check_event(
        _tool_use_event("Edit", {"file_path": "trading/emt_gateway.py"}),
        sender_staff_id="staffA",
    )
    mgr.assert_called_once()
    msg = mgr.call_args.args[0]
    assert "trading" in msg
    assert "staffA" in msg


def test_dangerous_bash_command_triggers_alert():
    """Bash 里含 rm → 告警。"""
    mgr = MagicMock()
    al = Alarmer(notify=lambda msg, level: mgr(msg, level))
    al.check_event(
        _tool_use_event("Bash", {"command": "rm -rf data_lake/"}),
        sender_staff_id="staffA",
    )
    mgr.assert_called_once()
    assert "rm" in mgr.call_args.args[0]


def test_non_tool_event_ignored():
    """非工具调用帧（result/thinking）不触发。"""
    mgr = MagicMock()
    al = Alarmer(notify=lambda msg, level: mgr(msg, level))
    al.check_event({"type": "result", "result": "done"}, sender_staff_id="staff")
    al.check_event({"type": "system", "subtype": "thinking_tokens"}, sender_staff_id="staff")
    mgr.assert_not_called()


def test_dingtalk_send_keywords_dont_alert_on_safe_text():
    """命中 'dingtalk_send' 等下单函数关键字也不误伤普通文本——
    只在工具参数里命中才告警，普通 Read 不会触发。"""
    mgr = MagicMock()
    al = Alarmer(notify=lambda msg, level: mgr(msg, level))
    # 普通读 README，不应触发（回归保护）
    al.check_event(_tool_use_event("Read", {"file_path": "README.md"}),
                   sender_staff_id="staff")
    mgr.assert_not_called()


def test_place_order_function_alerts():
    """碰下单函数名 place_order → 告警（业务类模式）。"""
    mgr = MagicMock()
    al = Alarmer(notify=lambda msg, level: mgr(msg, level))
    al.check_event(
        _tool_use_event("Bash", {"command": "python -c 'place_order(\"000001\")'"}),
        sender_staff_id="trader",
    )
    mgr.assert_called_once()
    assert "下单" in mgr.call_args.args[0]


def test_default_notify_uses_core_notifier():
    """默认 notify 不传时，应回退到 core.notifier.fire_and_forget。
    验证：不传 notify 触发高危，会调用 core.notifier 的 fire_and_forget。"""
    al = Alarmer()  # 用默认 notify
    with patch("core.notifier.fire_and_forget") as ff, \
         patch("core.notifier.NotificationManager") as Mgr:
        Mgr.get_default.return_value.notify_risk_event = MagicMock(
            return_value="dummy_coro")
        al.check_event(
            _tool_use_event("Bash", {"command": "rm -rf x"}),
            sender_staff_id="staff",
        )
        ff.assert_called_once()


def test_notify_exception_does_not_raise():
    """notify 抛异常时，check_event 不应向外抛（保护 claude 主流程）。"""
    def boom(msg, level):
        raise RuntimeError("dingtalk down")
    al = Alarmer(notify=boom)
    # 不应抛
    al.check_event(
        _tool_use_event("Bash", {"command": "rm -rf x"}),
        sender_staff_id="staff",
    )
