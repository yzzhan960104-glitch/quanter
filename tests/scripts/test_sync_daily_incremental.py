# -*- coding: utf-8 -*-
"""scripts/sync_daily_incremental.py 单元测试（Phase 1.5 任务2 TDD）。

Why 不真调 tushare：本脚本是数据链路最后一公里（每日增量 raw daily + adj_factor 重建
前复权 append 到 a_shares_daily.parquet），全市场 ~5500 标的 × 2 请求 × N 交易日，
真调会撞限频/扣积分且不可重复；mock pro + 验证数学正确性（分页/前复权/除权检测）
是物理意图唯一可重复的回归口径。

覆盖 4 类核心物理路径：
  ① _fetch_paged 分页：500 满 → 续页，<500 终止（绕过 ConnectionReset 的核心机制）
  ② 前复权计算：raw × adj / latest（latest = 新窗口每标的最新 adj_factor）
  ③ 除权检测：adj_d0 ≠ adj_today 标注（历史基准偏移，follow-up 全量重算）
  ④ 早返短路径：已最新 / 无新交易日（节假日空跑保护）
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import sync_daily_incremental as mod


# ============================================================================
# ① _fetch_paged 分页机制
# ============================================================================
def test_fetch_paged_paginates_until_short_page():
    """分页续拉：第一次返 limit=500 满 → offset 续拉，<500 终止。

    物理意图：全市场 5500 行单次响应会 ConnectionReset，按 limit=500 分页 + offset
    累加绕过；终止判据是「返回行数 < limit」（接口隐含的 EOF 信号）。
    """
    pro = MagicMock()
    # 构造 500 + 500 + 100 = 1100 行（两次满页 + 末次不足页终止）
    full1 = pd.DataFrame({"ts_code": ["000001.SZ"] * mod.PAGE, "trade_date": ["20260723"] * mod.PAGE})
    full2 = pd.DataFrame({"ts_code": ["000002.SZ"] * mod.PAGE, "trade_date": ["20260723"] * mod.PAGE})
    tail = pd.DataFrame({"ts_code": ["000003.SZ"] * 100, "trade_date": ["20260723"] * 100})
    pro.daily = MagicMock(side_effect=[full1, full2, tail])

    df = mod._fetch_paged(pro, "daily", "20260723")
    assert len(df) == mod.PAGE * 2 + 100  # 1100 行全合并
    assert pro.daily.call_count == 3  # 三次分页
    # offset 参数递增（0 → 500 → 1000），是分页续拉的核心断言
    offsets = [call.kwargs.get("offset") for call in pro.daily.call_args_list]
    assert offsets == [0, mod.PAGE, mod.PAGE * 2]


def test_fetch_paged_empty_returns_empty_df():
    """空响应直接返空 DataFrame（不报错、不分页）。"""
    pro = MagicMock()
    pro.daily = MagicMock(return_value=pd.DataFrame())
    df = mod._fetch_paged(pro, "daily", "20260723")
    assert df.empty
    assert pro.daily.call_count == 1  # 空即终止，不再续拉


# ============================================================================
# ② 前复权计算 + ④ 早返路径 + 整体 sync_daily_incremental 数学正确性
# ============================================================================
def _build_pro(trade_days_list, raw_by_day, adj_by_day):
    """构造 mock pro：trade_cal + daily(trade_date) + adj_factor(trade_date) 全 mock。

    trade_days_list: [cal_date, ...]（已过滤 is_open=1 的交易日，YYYYMMDD 字符串）
    raw_by_day: {trade_date_str: pd.DataFrame}（pro.daily 按 trade_date 返）
    adj_by_day: {trade_date_str: pd.DataFrame}（pro.adj_factor 按 trade_date 返）
    """
    pro = MagicMock()
    cal_df = pd.DataFrame({
        "cal_date": trade_days_list,
        "is_open": [1] * len(trade_days_list),
    })
    pro.trade_cal = MagicMock(return_value=cal_df)
    pro.daily = MagicMock(side_effect=lambda trade_date, **kw: raw_by_day.get(trade_date, pd.DataFrame()))
    pro.adj_factor = MagicMock(
        side_effect=lambda trade_date, **kw: adj_by_day.get(trade_date, pd.DataFrame()))
    return pro


def test_sync_already_latest_returns_early():
    """d0 >= today 早返（不拉 tushare，节假日空跑保护）。"""
    fake_lake = pd.DataFrame(
        {"close": [10.0]},
        index=pd.MultiIndex.from_tuples(
            [(pd.Timestamp("2026-07-24"), "000001.SZ")], names=["date", "symbol"]))
    with patch.object(mod.pd, "read_parquet", return_value=fake_lake), \
         patch.object(mod, "get_pro") as mock_get_pro, \
         patch("datetime.datetime") as mock_dt:
        # today = d0 = 2026-07-24 → 早返
        mock_dt.today.return_value.strftime.return_value = "2026-07-24"
        msg = mod.sync_daily_incremental()
    assert "已最新" in msg
    assert mock_get_pro.call_count == 0  # 早返不拉 pro


def test_sync_no_new_trade_day_returns_early():
    """d0 < today 但 [d0+1, today] 无交易日（节假日空窗）→ 早返不拉 daily。"""
    fake_lake = pd.DataFrame(
        {"close": [10.0]},
        index=pd.MultiIndex.from_tuples(
            [(pd.Timestamp("2026-07-20"), "000001.SZ")], names=["date", "symbol"]))
    pro = _build_pro(trade_days_list=[], raw_by_day={}, adj_by_day={})
    with patch.object(mod.pd, "read_parquet", return_value=fake_lake), \
         patch.object(mod, "get_pro", return_value=pro), \
         patch("datetime.datetime") as mock_dt:
        mock_dt.today.return_value.strftime.return_value = "2026-07-24"
        msg = mod.sync_daily_incremental()
    assert "无新交易日" in msg
    assert pro.daily.call_count == 0  # 节假日空窗不拉 daily


def test_sync_incremental_recomputes_qfq_and_appends():
    """端到端：拉 raw daily + adj_factor → 重建前复权 → append 落盘。

    数学断言：price_qfq = raw × adj / latest（latest = 新窗口每标的最新 adj）。
    构造单标的 2 新交易日，验证：
      ① 落盘行数 = 原始 1 行 + 新增 2 行 = 3 行；
      ② 新行 close = raw × adj_d / adj_latest（手算可对照）。
    """
    # 原 lake：1 行 d0=2026-07-20（前复权基准锚）
    fake_lake = pd.DataFrame(
        {"close": [10.0]},
        index=pd.MultiIndex.from_tuples(
            [(pd.Timestamp("2026-07-20"), "000001.SZ")], names=["date", "symbol"]))
    # 新交易日 07-22 / 07-23（07-21 周末已剔除）
    trade_days = ["20260722", "20260723"]
    # raw daily：07-22 raw=20，07-23 raw=22（未复权原价）
    raw_by_day = {
        "20260722": pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20260722"],
                                  "open": [20.0], "high": [21.0], "low": [19.5],
                                  "close": [20.0], "vol": [1000], "amount": [20000.0]}),
        "20260723": pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20260723"],
                                  "open": [22.0], "high": [22.5], "low": [21.5],
                                  "close": [22.0], "vol": [1100], "amount": [24000.0]}),
    }
    # adj_factor：d0(07-20)=1.0（锚），07-22=1.0，07-23=2.0（除权日：复权因子跳变）
    # → latest = 2.0（新窗口末值）
    # → 07-22 qfq = 20 × 1.0 / 2.0 = 10.0；07-23 qfq = 22 × 2.0 / 2.0 = 22.0
    adj_by_day = {
        "20260720": pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20260720"],
                                  "adj_factor": [1.0]}),
        "20260722": pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20260722"],
                                  "adj_factor": [1.0]}),
        "20260723": pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20260723"],
                                  "adj_factor": [2.0]}),
    }
    pro = _build_pro(trade_days, raw_by_day, adj_by_day)
    written = {}
    def fake_to_parquet(df, path, **kw):
        written["df"] = df
    with patch.object(mod.pd, "read_parquet", return_value=fake_lake), \
         patch.object(mod, "get_pro", return_value=pro), \
         patch.object(mod.pd.DataFrame, "to_parquet", fake_to_parquet), \
         patch("datetime.datetime") as mock_dt:
        mock_dt.today.return_value.strftime.return_value = "2026-07-24"
        msg = mod.sync_daily_incremental()

    assert "OK 最新日" in msg
    df = written["df"]
    # 落盘 = 原 1 行 + 新增 2 行
    assert len(df) == 3
    # 07-22 qfq close = 20 × 1.0 / 2.0 = 10.0（手算对照）
    r_22 = df.loc[(pd.Timestamp("2026-07-22"), "000001.SZ")]
    assert abs(r_22["close"] - 10.0) < 1e-6
    # 07-23 qfq close = 22 × 2.0 / 2.0 = 22.0（latest 即当日，自身不缩放）
    r_23 = df.loc[(pd.Timestamp("2026-07-23"), "000001.SZ")]
    assert abs(r_23["close"] - 22.0) < 1e-6


def test_sync_detects_dividend_when_adj_changes():
    """除权检测：adj_d0 ≠ adj_today 标注「除权标的待重算」。

    物理意图：除权日 adj_factor 跳变 → append 新窗口数据时历史 qfq 基准已偏移，
    脚本仅 append 不重算历史（follow-up），故必须 detect 出除权标的并告警。
    断言：msg 含「除权标的」字样（warning 已被 detect 触发）。
    """
    fake_lake = pd.DataFrame(
        {"close": [10.0]},
        index=pd.MultiIndex.from_tuples(
            [(pd.Timestamp("2026-07-20"), "000001.SZ")], names=["date", "symbol"]))
    trade_days = ["20260723"]
    raw_by_day = {
        "20260723": pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20260723"],
                                  "open": [22.0], "high": [22.5], "low": [21.5],
                                  "close": [22.0], "vol": [1100], "amount": [24000.0]}),
    }
    # d0(07-20)=1.0 vs today(07-23)=2.0 → 除权
    adj_by_day = {
        "20260720": pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20260720"],
                                  "adj_factor": [1.0]}),
        "20260723": pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20260723"],
                                  "adj_factor": [2.0]}),
    }
    pro = _build_pro(trade_days, raw_by_day, adj_by_day)
    written = {}
    with patch.object(mod.pd, "read_parquet", return_value=fake_lake), \
         patch.object(mod, "get_pro", return_value=pro), \
         patch.object(mod.pd.DataFrame, "to_parquet",
                      lambda df, path, **kw: written.update(df=df)), \
         patch("datetime.datetime") as mock_dt:
        mock_dt.today.return_value.strftime.return_value = "2026-07-24"
        msg = mod.sync_daily_incremental()
    # msg 含「除权标的 1 只待重算」（adj 1.0 → 2.0 跳变被 detect）
    assert "除权标的" in msg and "1" in msg
