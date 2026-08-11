"""AKShareClient：手动熔断+限流，失败返空 DF 不抛；wrapper 洗净列。

拷问边界（对齐 yfinance_client 范式）：
- 熔断 OPEN 期间绝不触达底层 ak.* —— 防止限频连环超时被封禁。
- 任何异常（网络/限频/解析/空返回）一律 catch → 返回空 DF，绝不外抛。
- 日线返回值须为标准 schema（open/high/low/close/volume/amount + DatetimeIndex）。
"""
import pandas as pd
from data.clients.akshare_client import AKShareClient, akshare_breaker
from data.resilience import CircuitState

# 注：breaker/limiter 单例跨用例 reset 由根 conftest _reset_resilience_singletons
# autouse fixture 兜底，本文件不再各自裸写 _state/_failure_count。


def test_fetch_daily_hist_cleanses(monkeypatch):
    """日线返回须洗净中文列名为标准 schema（open/high/low/close/volume/amount）。"""
    fake = pd.DataFrame({"日期": ["2024-01-02"], "开盘": [10], "最高": [11], "最低": [9],
                         "收盘": [10.5], "成交量": [1000], "成交额": [1e7]})
    monkeypatch.setattr("akshare.stock_zh_a_hist", lambda *a, **k: fake)
    df = AKShareClient().fetch_daily_hist("000001.SZ", "2024-01-02", "2024-01-03")
    # 标准列名前 6 列固定顺序（amount 在 turnover 之前，turnover 可选）
    assert list(df.columns)[:6] == ["open", "high", "low", "close", "volume", "amount"]
    assert len(df) == 1


def test_fetch_daily_hist_strips_exchange_suffix(monkeypatch):
    """symbol 带 .SZ/.SH 后缀时，wrapper 须剥离后缀再调 ak.stock_zh_a_hist。

    拷问背景：活跃股池的 symbol 多源自融资融券明细（形如 '000001.SZ'，带交易所后缀），
    而 akshare stock_zh_a_hist 只要纯数字代码——原样透传会实网返空，导致整个活跃池日线
    全量拉空。本测试捕获透传给 ak.* 的真实 symbol，断言已剥离为纯数字。
    """
    captured = {}
    fake = pd.DataFrame({"日期": ["2024-01-02"], "开盘": [10], "最高": [11], "最低": [9],
                         "收盘": [10.5], "成交量": [1000], "成交额": [1e7]})

    def _fake_hist(**kwargs):
        captured["symbol"] = kwargs.get("symbol")
        return fake

    monkeypatch.setattr("akshare.stock_zh_a_hist", lambda **k: _fake_hist(**k))
    AKShareClient().fetch_daily_hist("000001.SZ", "2024-01-02", "2024-01-03")
    assert captured["symbol"] == "000001"   # ★ 剥离后缀，纯数字喂 akshare


def test_failure_returns_empty_df(monkeypatch):
    """底层 ak.* 抛错时必须返回空 DF，绝不向外抛（红线契约）。"""
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr("akshare.stock_zh_a_hist", boom)
    df = AKShareClient().fetch_daily_hist("000001.SZ", "2024-01-02", "2024-01-03")
    assert df.empty   # 绝不抛


def test_call_ak_timeout_returns_empty(monkeypatch):
    """#8：ak.* 挂死（内部 requests 无超时）→ _call_ak 超时抛 TimeoutError → fetch 返空 DF。

    物理意图：akshare 内部 requests 无 timeout，柜台/网络无响应会永久阻塞工作线程。
    _call_ak 用 ThreadPoolExecutor + future.result(timeout) 兜底，超时抛 TimeoutError，
    由 fetch 外层 except 捕获 → 熔断 record_failure + 空降级（与既有异常同口径）。
    """
    import time
    monkeypatch.setattr("data.clients.akshare_client._AK_TIMEOUT", 0.05)

    def _slow(*a, **k):
        time.sleep(0.3)   # 模拟柜台无响应（>0.05s 超时）
    monkeypatch.setattr("akshare.stock_zh_a_hist", _slow)
    df = AKShareClient().fetch_daily_hist("000001.SZ", "2024-01-02", "2024-01-03")
    assert df.empty   # 超时→TimeoutError→外层 except→空降级


def test_dr007_stale_data_returns_empty(monkeypatch):
    """DR007 接口返回过期数据(停在2020)时必须返空 DF，绝不泄漏给下游 CreditRegime。

    拷问背景：ak.repo_rate_hist() 是 dead 接口（数据停 2020-10-29，且返 FR/FDR 非 DR007），
    若透传会静默泄漏过期+错列数据。本测试 mock 一个最新日期为 2020 的 DF，断言新鲜度守卫
    生效 → 返空 DF。
    """
    # 模拟 dead 接口的过期返回（最新日期 2020-10-29，远超 7 天新鲜度阈值）
    stale = pd.DataFrame({
        "日期": ["2020-10-27", "2020-10-28", "2020-10-29"],
        "FR007": [2.0, 2.1, 2.2],
    })
    monkeypatch.setattr("akshare.repo_rate_hist", lambda *a, **k: stale)
    df = AKShareClient().fetch_macro_raw("dr007")
    assert df.empty   # 新鲜度守医生效，过期数据不泄漏


def test_circuit_open_returns_empty_without_calling_ak(monkeypatch):
    """熔断 OPEN 期间须快速返回空 DF，且绝不触达底层 ak.*（防连环超时）。"""
    import time as _time
    # 刻意置 OPEN 验证 OPEN-path：用 monkeypatch 自动还原，不裸写私有字段
    # （conftest _reset_resilience_singletons 已在本用例前 reset 为 CLOSED）
    monkeypatch.setattr(akshare_breaker, "_state", CircuitState.OPEN)
    monkeypatch.setattr(akshare_breaker, "_opened_at", _time.monotonic())

    called = {"n": 0}

    def should_not_be_called(*a, **k):
        called["n"] += 1
        raise AssertionError("熔断 OPEN 期间不应触达底层 ak.*")

    monkeypatch.setattr("akshare.stock_zh_a_hist", should_not_be_called)
    df = AKShareClient().fetch_daily_hist("000001.SZ", "2024-01-02", "2024-01-03")
    assert df.empty
    assert called["n"] == 0   # 熔断守医生效，底层未被触达
