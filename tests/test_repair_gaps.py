# -*- coding: utf-8 -*-
"""repair_gaps 补采脚本单测（规则 3）。

物理意图：scan_integrity 扫出的 unjustified GapRange（漏采段）→ 按 missing_dates
重采 Tushare daily + adj_factor → 前复权 → merge 回 a_shares_daily 湖（dedup keep last）。

核心逻辑复用 sync_daily_incremental 的 _fetch_paged（按日分页）+ 前复权 + concat dedup。
本测试 mock pro（daily/adj_factor），验证补采段正确落湖 + 去重 + 仅补 gap 涉及的 symbol。

前复权基准说明：本次用「缺口段窗口最新 adj」（与 sync_daily_incremental 同口径），
不重算除权标的全历史（memory 标注的 follow-up）。
"""
from __future__ import annotations

import pandas as pd

from data.integrity import GapRange


def _mk_lake(rows):
    """构造 daily 湖测试 DataFrame。rows = [(date_str, symbol), ...]。"""
    dates = [pd.Timestamp(d) for d, _ in rows]
    syms = [s for _, s in rows]
    n = len(rows)
    return pd.DataFrame(
        {"open": range(n), "high": range(n), "low": range(n),
         "close": range(n), "volume": range(n)},
        index=pd.MultiIndex.from_arrays([dates, syms], names=["date", "symbol"]),
    )


class _FakePro:
    """Tushare pro 替身：按 trade_date(YYYYMMDD) 返 daily/adj_factor。

    忽略 limit/offset（_fetch_paged 一次返完，因 mock 数据 < 500 行/页）。
    """

    def __init__(self, daily_data, adj_data):
        self.daily_data = daily_data   # {yyyymmdd: [row_dict, ...]}
        self.adj_data = adj_data

    def daily(self, trade_date, limit=500, offset=0, **kw):
        return pd.DataFrame(self.daily_data.get(trade_date, []))

    def adj_factor(self, trade_date, limit=500, offset=0, **kw):
        return pd.DataFrame(self.adj_data.get(trade_date, []))


def test_repair_gaps_fills_missing_segment():
    """补采：lake 缺 09-04/09-05，pro 返这两日数据 → repair 后 lake 含这两日。"""
    from data.tools.repair_gaps import repair_gaps

    lake = _mk_lake([
        ("2024-09-02", "000001.SZ"), ("2024-09-03", "000001.SZ"),
        ("2024-09-06", "000001.SZ"),  # 缺 09-04, 09-05
    ])
    gap = GapRange("000001.SZ", "2024-09-04", "2024-09-05",
                   ("2024-09-04", "2024-09-05"), suspend_justified=False)
    pro = _FakePro(
        daily_data={
            "20240904": [{"ts_code": "000001.SZ", "trade_date": "20240904",
                          "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "vol": 1000}],
            "20240905": [{"ts_code": "000001.SZ", "trade_date": "20240905",
                          "open": 10.5, "high": 11.0, "low": 10.0, "close": 11.0, "vol": 1100}],
        },
        adj_data={
            "20240904": [{"ts_code": "000001.SZ", "trade_date": "20240904", "adj_factor": 1.0}],
            "20240905": [{"ts_code": "000001.SZ", "trade_date": "20240905", "adj_factor": 1.0}],
        },
    )

    new_lake = repair_gaps([gap], lake, pro)

    dates = (new_lake.xs("000001.SZ", level="symbol").index
             .strftime("%Y-%m-%d").tolist())
    assert "2024-09-04" in dates, "09-04 应被补采"
    assert "2024-09-05" in dates, "09-05 应被补采"
    # 原有日不丢
    assert "2024-09-02" in dates and "2024-09-06" in dates


def test_repair_gaps_dedup_keep_new_on_overlap():
    """补采日与 lake 已有日重叠 → dedup keep last（新覆盖旧），无重复行。"""
    from data.tools.repair_gaps import repair_gaps

    lake = _mk_lake([("2024-09-02", "000001.SZ"), ("2024-09-03", "000001.SZ")])
    # gap 含 09-03（lake 已有）→ 补采的 09-03 应覆盖 lake 的（dedup keep last）
    gap = GapRange("000001.SZ", "2024-09-03", "2024-09-03",
                   ("2024-09-03",), suspend_justified=False)
    pro = _FakePro(
        daily_data={"20240903": [{"ts_code": "000001.SZ", "trade_date": "20240903",
                                   "open": 99.0, "high": 99.0, "low": 99.0, "close": 99.0, "vol": 9999}]},
        adj_data={"20240903": [{"ts_code": "000001.SZ", "trade_date": "20240903", "adj_factor": 1.0}]},
    )

    new_lake = repair_gaps([gap], lake, pro)

    sym_df = new_lake.xs("000001.SZ", level="symbol").sort_index()
    # 09-03 只有一行（dedup），且 close 是新值 99.0（keep last）
    assert (sym_df.index == pd.Timestamp("2024-09-03")).sum() == 1, "重叠日应 dedup"
    assert sym_df.loc["2024-09-03", "close"] == 99.0


def test_repair_gaps_skips_justified_gaps():
    """suspend_justified=True 的 gap（停牌合法跳空）不补采（Tushare 停牌日返空）。"""
    from data.tools.repair_gaps import repair_gaps

    lake = _mk_lake([("2024-09-02", "000413.SZ"), ("2024-09-05", "000413.SZ")])
    gap = GapRange("000413.SZ", "2024-09-03", "2024-09-04",
                   ("2024-09-03", "2024-09-04"), suspend_justified=True)
    pro = _FakePro({}, {})  # 无数据（停牌日 Tushare 也返空）

    new_lake = repair_gaps([gap], lake, pro)

    # justified gap 不触发补采，lake 不变
    assert len(new_lake) == len(lake)
