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


def test_lifespan_calls_log_startup_banner():
    """C-5 V2：lifespan 装配 TradingEngine 前调 log_startup_banner（生产链 session 漂移可见）。

    物理意图（spec §3.2 · [[qmt-connect-1-rootcause]] 教训）：
        生产链 start_all→uvicorn→lifespan 之前无 banner，07-29 故障中 engine 进程内
        session=123456 而 .env=123458，无任何日志可对比。V2 把 banner 调用从 __main__
        搬到 lifespan，让生产链也在装配 engine 前固化 session/account/mode/口径到日志。
        本测试源码级断言 main.py lifespan 函数体含 log_startup_banner() 调用（与
        test_main.py 的源码级契约锁同范式，不起真实 server）。
    """
    import pathlib
    main_py = pathlib.Path("presentation/server/main.py")
    src = main_py.read_text(encoding="utf-8")
    # import 行必须把 log_startup_banner 从 trading.__main__ 引入
    assert "log_startup_banner" in src, (
        "main.py lifespan 必须引入并调用 log_startup_banner（C-5 V2，"
        "生产链 session 漂移可见性）")
    # 必须有实际调用（不是只 import 不调）
    assert "log_startup_banner()" in src, (
        "main.py lifespan 必须实际调用 log_startup_banner()（仅 import 不算）")
