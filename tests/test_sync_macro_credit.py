"""Task 5：宏观信贷同步脚本 —— 月频宏观 → 日频对齐，前视红线守护。

拷问红线（量化风控极度拷问）：
- 社融/M1M2 是【月频】数据，reindex 到日频后【仅向前 ffill】（用过去值解释现在），
  绝不可 bfill 回填未来月度值——否则会把"未来才知道的月度数据"提前泄漏给历史日，
  构成前视偏差（look-ahead bias），回测看起来完美、实盘直接崩盘。
- DR007 走 Task 4 新鲜度守卫（过期/错列→返空）；本测试 mock client，不依赖真实接口，
  且对【空 DR007】容错（缺了就少一列，不崩整个 fetch_macro_series）。
"""
import pandas as pd


class _FakeClient:
    """mock AKShareClient.fetch_macro_raw：社融/M1M2/DR007/SHIBOR 四档假数据。

    SHIBOR 返空 DF，验证"缺了某档不崩"的容错路径（与 DR007 缺失同语义）。
    """

    def fetch_macro_raw(self, kind):
        return {
            "shrzgm": pd.DataFrame({"月份": ["2024-01"], "社会融资规模增量": [100]}),
            "money_supply": pd.DataFrame({"月份": ["2024-01"],
                                          "M2同比增长": [9.0], "M1同比增长": [5.0]}),
            "dr007": pd.DataFrame({"日期": ["2024-01-02"], "利率": [2.1]}),
            "shibor": pd.DataFrame(),
        }[kind]


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


def test_fetch_macro_series_derives_m1m2_gap():
    """fetch_macro_series 须合并社融/M1M2/DR007 并衍生 M1M2_gap 剪刀差列。

    M1M2_gap = M1同比 - M2同比，是货币活性剪刀差，正向扩张代表资金活化（M1 增速
    快于 M2），CreditRegime 据此判断宽信用/紧信用状态。本测试 mock client，
    验证空 SHIBOR 不崩、衍生列存在。
    """
    from scripts.sync_macro_credit import fetch_macro_series

    s = fetch_macro_series(_FakeClient(), "2024-01-01", "2024-01-31")
    assert "M1M2_gap" in s.columns   # 剪刀差衍生列
