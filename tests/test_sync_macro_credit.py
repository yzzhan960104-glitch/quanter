"""Task 5：宏观信贷同步脚本 —— 月频宏观 → 日频对齐，前视红线守护。

拷问红线（量化风控极度拷问）：
- 社融/M1M2 是【月频】数据，reindex 到日频后【仅向前 ffill】（用过去值解释现在），
  绝不可 bfill 回填未来月度值——否则会把"未来才知道的月度数据"提前泄漏给历史日，
  构成前视偏差（look-ahead bias），回测看起来完美、实盘直接崩盘。
- DR007 走 Task 4 新鲜度守卫（过期/错列→返空）；本测试 mock client，不依赖真实接口，
  且对【空 DR007】容错（缺了就少一列，不崩整个 fetch_macro_series）。

⚠️ Plan C Task 2 源切换后签名变更：fetch_macro_series 不再接收 client 参数——
    M0/M1/M2 已切到 Tushare cn_m（_fetch_with_guard 内部 get_pro），社融/DR007
    内部自建 AKShareClient()。本测试同步更新为 monkeypatch AKShareClient + cn_m
    fake_pro 风格（与 test_tushare_datasets_macro.py 一致）。
"""
import pandas as pd
import pytest


class _FakePro:
    """tushare pro 替身：按 api_name 返回可控 DataFrame（cn_m M0/M1/M2 月频）。"""

    def __init__(self):
        self._data = {}

    def set(self, api, df):
        self._data[api] = df

    def __getattr__(self, api):
        def _c(**kw):
            return self._data.get(api, pd.DataFrame())
        return _c


@pytest.fixture
def fake_pro(monkeypatch):
    """mock get_pro + 限频/熔断器（与 test_tushare_datasets_macro 同手法）。"""
    fake = _FakePro()
    monkeypatch.setattr("scripts.sync_macro_credit.get_pro", lambda: fake)
    monkeypatch.setattr("scripts.sync_macro_credit.tushare_rate_limiter",
                        type("L", (), {"acquire": lambda self, n: None})())
    monkeypatch.setattr("scripts.sync_macro_credit.tushare_breaker",
                        type("B", (), {"allow_request": lambda self: True,
                                       "record_success": lambda self: None,
                                       "record_failure": lambda self: None})())
    return fake


def test_align_to_daily_forward_fill_only():
    """月频宏观 → 日频，仅向前 ffill（无未来值回填）。

    构造 1 月值=1.0、2 月值=2.0 的月频序列，reindex 到 1 月日历日后，
    1 月内所有工作日的值必须恒为 1.0（用过去值解释现在）；
    若出现 2.0，说明发生了 bfill 回填未来月度值 → 前视偏差，红线被破。
    """
    from scripts.sync_macro_credit import align_to_daily

    m = pd.DataFrame({"月份": ["2024-01-01", "2024-02-01"], "x": [1.0, 2.0]})
    m["月份"] = pd.to_datetime(m["月份"])
    daily = align_to_daily(m, date_col="月份", start="2024-01-01", end="2024-01-31")
    # 1 月内所有工作日都应为 1.0（1 月值向前填），绝不应出现 2.0（2 月未来值）
    assert (daily["x"] == 1.0).all()


def test_fetch_macro_series_derives_m1m2_gap(fake_pro, monkeypatch):
    """fetch_macro_series 须合并 cn_m(M1/M2) + 社融/DR007 并衍生 M1M2_gap 剪刀差列。

    M1M2_gap = M1同比 - M2同比，是货币活性剪刀差，正向扩张代表资金活化（M1 增速
    快于 M2），CreditRegime 据此判断宽信用/紧信用状态。

    源切换后（Plan C Task 2）：M1/M2 走 Tushare cn_m（fake_pro mock），社融/DR007
    走 akshare fallback（monkeypatch AKShareClient.fetch_macro_raw）。本测试验证
    双源合并后 M1M2_gap 衍生列存在、且缺 SHIBOR/部分档不崩。
    """
    # Tushare cn_m：M0/M1/M2 同比（brief 字段名假设，待真 token 探测）
    fake_pro.set("cn_m", pd.DataFrame({
        "month": ["202401"],
        "m0_yoy": [8.0], "m1_yoy": [5.0], "m2_yoy": [9.0]}))
    # akshare 社融/DR007 fallback（monkeypatch，避开真实网络）
    import data.clients.akshare_client as akc
    monkeypatch.setattr(akc.AKShareClient, "fetch_macro_raw",
                        lambda self, kind: {
                            "shrzgm": pd.DataFrame({"月份": ["2024-01"],
                                                    "社会融资规模增量": [100]}),
                            "dr007": pd.DataFrame({"日期": ["2024-01-02"], "利率": [2.1]}),
                        }.get(kind, pd.DataFrame()))

    from scripts.sync_macro_credit import fetch_macro_series
    s = fetch_macro_series("2024-01-01", "2024-01-31")
    assert "M1M2_gap" in s.columns   # 剪刀差衍生列
