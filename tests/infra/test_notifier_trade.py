"""成交通知（区别于风控告警 notify_risk_event）。

Why 独立测试文件：notify_risk_event 走 ⚠️/❌/🚨 风控前缀，notify_trade_event 走
💰【成交】业务流水前缀——两者物理语义不同（风控告警 vs 业务播报），故格式化与前缀
均需独立守护，防止后续重构把两类通知的前缀/正文格式串味。

TDD 约定：本仓库 pytest-asyncio 为 strict 模式（pytest.ini 未配 asyncio_mode），
且 tests/test_notifier.py 历史用例一律 asyncio.run(...) 同步驱动 async。本测试
沿袭该范式，避免引入 @pytest.mark.asyncio 装饰器的风格分叉。
"""
import asyncio
from unittest.mock import patch, AsyncMock

from infra.notifier import NotificationManager


def test_notify_trade_event_formats_trade_info():
    """成交通知含标的/方向/量/价 + 成交前缀。

    守护点：
      1) symbol/direction/qty/price 四要素逐字进正文（on_stock_trade 回调契约）；
      2) 💰【成交】前缀出现——与风控告警 ⚠️/❌/🚨 前缀在群里一眼可辨；
      3) 复用 _broadcast（与 notify_risk_event 同一并发广播内核），不再复制粘贴
         asyncio.gather 逻辑（DRY 守护，防未来分叉）。
    """
    mgr = NotificationManager()
    mgr._channels = []  # 不依赖真实 webhook，且让 _broadcast 走空通道短路分支
    # mock _broadcast：隔离「格式化」与「广播」两件事，本用例只验格式化正文
    with patch.object(mgr, "_broadcast", new=AsyncMock(return_value=[])) as bc:
        asyncio.run(
            mgr.notify_trade_event("300001.SZ", "BUY", 100, 10.5, extra="tp=12.0")
        )
    # _broadcast 被调一次，首参即拼好的正文
    assert bc.call_count == 1
    msg = bc.call_args.args[0]
    # 四要素逐字进正文（防未来重构把某字段拼丢）
    assert "300001.SZ" in msg
    assert "BUY" in msg
    assert "100" in msg
    assert "10.5" in msg
    # 成交前缀（与风控告警区分的核心标识）
    assert "成交" in msg
    assert "💰" in msg
    # 附加信息透传（止盈价/实验归因等业务上下文）
    assert "tp=12.0" in msg
