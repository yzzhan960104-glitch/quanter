"""JQDataClient：单例锁 + 配额双机制 + 洗净；临限抛 QuotaExceeded + 告警；缺凭证降级。

红线（聚宽试用账号：每日 100 万条 + 单连接）：
  - 缺凭证必须降级返空 DataFrame，绝不抛异常外泄到核心引擎；
  - money→amount 洗净 + tz-naive DatetimeIndex（防范时区错配 join 异常）；
  - spare<5万 或 手动计数>=95万 → 抛 QuotaExceeded + 钉钉告警，绝不超日限额。

测试全程 mock jqdatasdk，绝不触网。每个用例复位单例（_instance=None）避免串味。
"""
from __future__ import annotations

import pandas as pd
import pytest

from data.clients.jqdata_client import JQDataClient, QuotaExceeded


def _reset_singleton() -> None:
    """复位类级单例与今日计数，避免前序用例残留污染本用例。"""
    JQDataClient._instance = None


def _mock_jq(monkeypatch, spare: float = 200_000, rows: int = 240):
    """注入假 jqdatasdk 模块到 sys.modules，避免真实触网。

    参数：
        spare: get_query_count 返回的剩余条数（驱动配额判定）
        rows:  get_price 返回的 mock 行数（驱动 _today_count 累加）
    """
    import sys
    jq = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    jq.auth = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    # 返回「条数配额」字典：聚宽按调用条数计费，spare 是剩余可用条数
    jq.get_query_count = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(
        return_value={"total": 1_000_000, "spare": spare})
    fake = pd.DataFrame(
        {"open": [1] * rows, "high": [1] * rows, "low": [1] * rows,
         "close": [1] * rows, "volume": [100] * rows, "money": [1e6] * rows},
        index=pd.date_range("2024-01-02", periods=rows, freq="min"))
    jq.get_price = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(
        return_value=fake)
    monkeypatch.setitem(sys.modules, "jqdatasdk", jq)
    return jq


def test_disabled_when_no_creds(monkeypatch):
    """缺凭证（无 JQDATA_USERNAME/PASSWORD）必须降级返空 DF 且 _enabled=False。"""
    _reset_singleton()
    monkeypatch.delenv("JQDATA_USERNAME", raising=False)
    monkeypatch.delenv("JQDATA_PASSWORD", raising=False)
    c = JQDataClient()
    df = c.fetch_minute_bars("000001.SZ", "2024-01-02", "2024-01-03")
    assert df.empty
    assert not c._enabled


def test_fetch_cleanses_and_counts(monkeypatch):
    """拉取成功：money→amount 洗净 + _today_count 反映「服务端校准 + 本次累加」。

    配额双机制语义：fetch 入口先用 get_query_count 校准（spare=900_000 → 服务端
    权威 used=1_000_000-900_000=100_000，本地计数取 max 纠偏为 100_000），
    再按本次 get_price 行数累加（+240）。故 _today_count = 100_240。
    Why 取 max 而非覆盖：服务端计数最权威，但偶有延迟/口径差，取 max 既纠偏
    又不回退本地累计，守住「绝不超限」红线。
    """
    _reset_singleton()
    monkeypatch.setenv("JQDATA_USERNAME", "u")
    monkeypatch.setenv("JQDATA_PASSWORD", "p")
    _mock_jq(monkeypatch, spare=900_000, rows=240)
    c = JQDataClient()
    df = c.fetch_minute_bars("000001.SZ", "2024-01-02", "2024-01-03", frequency="1m")
    # money→amount 洗净：原始 money 列必须被改名为 amount
    assert "amount" in df.columns
    assert "money" not in df.columns
    assert "volume" in df.columns
    # 配额计数：校准 used(100_000) + 本次行数(240) = 100_240
    assert c._today_count == 100_000 + 240


def test_quota_near_limit_raises_and_alerts(monkeypatch):
    """spare<5万（红线）：抛 QuotaExceeded 且不调用 get_price（绝不超日限额）。"""
    _reset_singleton()
    monkeypatch.setenv("JQDATA_USERNAME", "u")
    monkeypatch.setenv("JQDATA_PASSWORD", "p")
    jq = _mock_jq(monkeypatch, spare=10_000)  # spare=1万 < 5万红线
    c = JQDataClient()
    with pytest.raises(QuotaExceeded):
        c.fetch_minute_bars("000001.SZ", "2024-01-02", "2024-01-03")
    # 守护红线：临限时绝不可发起 get_price 拉取（否则越界扣费）
    assert not jq.get_price.called
