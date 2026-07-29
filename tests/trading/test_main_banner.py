# -*- coding: utf-8 -*-
"""M3 启动 banner 单测：打印关键配置 + 口径版本（漂移可见）。"""
import logging, importlib
from unittest.mock import patch

def test_startup_banner_logs_key_config(caplog):
    """banner 必须含 session_id/account/mode/口径四要素。"""
    from trading import __main__ as m
    with patch.dict("os.environ", {
        "QMT_SESSION_ID": "123458", "QMT_ACCOUNT_ID": "10110356",
        "AUTO_TRADE_MODE": "live", "AUTO_CONFIRM_PLAN": "true",
    }, clear=False):
        with caplog.at_level(logging.INFO):
            m.log_startup_banner()  # 抽出的纯函数，便于单测
    blob = " ".join(r.getMessage() for r in caplog.records)
    assert "123458" in blob and "10110356" in blob
    assert "live" in blob
    assert "next_trading_day" in blob  # 口径版本
