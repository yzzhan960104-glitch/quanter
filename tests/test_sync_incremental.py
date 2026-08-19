# -*- coding: utf-8 -*-
"""data/tools/sync_incremental.py 增量同步测试。

设计意图（反黑盒 + 全 mock）：
- 完全 mock 掉 data.tushare_sync.sync_dataset（避免触发真实 Tushare 调用/限频/熔断），
  让测试聚焦增量算法：d0 推导 / 旧+新 merge / 去重保留新 / 空数据防护 / 首次回退。
- 通过 TUSHARE_DATASETS[key] 临时覆盖 lake 路径到 tmp_path，保证测试隔离无副作用（与
  test_tushare_sync._isolate_tushare_registry 同范式）。
- 每个测试构造一个具体的「旧 parquet + 新拉数据」场景，断言合并后行为符合物理意图。

关键覆盖矩阵：
  1. MultiIndex(date, symbol) 时序（by=date 范式）：旧数据全保留 + 新数据 append + 同 (date,symbol) 去重保留新
  2. DatetimeIndex 时序（by=single index_mode=datetime 范式，宏观指标）：同上但单层索引
  3. _unavailable 数据集被跳过（不调 sync_dataset，不落盘）
  4. parquet 不存在 → 全量回退窗口（start = today - 365*years）
  5. 新数据为空 → 不覆盖旧 parquet（接口故障/节假日防线）
  6. --days 回看上限：d0+1 早于 today-max_days 时，start 被截到 today-max_days
"""
import copy
import os
import sys
from datetime import datetime, timedelta

import pandas as pd
import pytest

# 把项目根加进 sys.path，保证 import data.tools.sync_incremental 可达
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _isolate_registry():
    """深拷贝 TUSHARE_DATASETS + LAKE_CONFIG['lakes']，测试后还原原对象。

    Why autouse 深拷贝：测试内就地覆盖全局 TUSHARE_DATASETS[key]['lake']（重定向到 tmp_path），
    若不还原会污染后续测试。与 test_tushare_sync._isolate_tushare_registry 同范式。
    """
    from config import TUSHARE_DATASETS, LAKE_CONFIG
    saved_datasets = copy.deepcopy(TUSHARE_DATASETS)
    saved_lakes = copy.deepcopy(LAKE_CONFIG["lakes"])
    yield
    TUSHARE_DATASETS.clear()
    TUSHARE_DATASETS.update(saved_datasets)
    LAKE_CONFIG["lakes"].clear()
    LAKE_CONFIG["lakes"].update(saved_lakes)


# ============ _latest_date：从 parquet 推 d0 ============

def test_latest_date_multiindex():
    """MultiIndex(date, symbol) → 返回 date 层 max。"""
    from data.tools.sync_incremental import _latest_date
    idx = pd.MultiIndex.from_tuples([
        (pd.Timestamp("2024-01-01"), "000001.SZ"),
        (pd.Timestamp("2024-01-05"), "600000.SH"),
        (pd.Timestamp("2024-01-03"), "000001.SZ"),
    ], names=["date", "symbol"])
    df = pd.DataFrame({"v": [1, 2, 3]}, index=idx)
    assert _latest_date(df) == pd.Timestamp("2024-01-05")


def test_latest_date_datetimeindex():
    """DatetimeIndex（宏观指标 index_mode=datetime）→ 返回 idx max。"""
    from data.tools.sync_incremental import _latest_date
    df = pd.DataFrame({"cpi": [1, 2, 3]},
                      index=pd.DatetimeIndex(["2024-01-01", "2024-03-01", "2024-02-01"]))
    assert _latest_date(df) == pd.Timestamp("2024-03-01")


def test_latest_date_empty_or_static():
    """空 df 或无时序索引（静态快照）→ 返 None，触发全量回退。"""
    from data.tools.sync_incremental import _latest_date
    assert _latest_date(pd.DataFrame()) is None
    assert _latest_date(None) is None
    # 静态快照扁平 df（无时间索引）→ None
    df_static = pd.DataFrame({"ts_code": ["000001.SZ"]}, index=[0])
    assert _latest_date(df_static) is None


# ============ _merge_dedup：旧+新合并去重保留新 ============

def test_merge_dedup_multiindex_keep_new():
    """MultiIndex 时序：旧数据全保留 + 新数据 append + 同 (date,symbol) 去重保留新。

    物理意图：Tushare 偶发数据修订（如 ann_date 重述），同 (date, symbol) 行应以新拉的为准。
    """
    from data.tools.sync_incremental import _merge_dedup
    old_idx = pd.MultiIndex.from_tuples([
        (pd.Timestamp("2024-01-01"), "000001.SZ"),
        (pd.Timestamp("2024-01-02"), "000001.SZ"),
    ], names=["date", "symbol"])
    old = pd.DataFrame({"v": [10, 20]}, index=old_idx)

    new_idx = pd.MultiIndex.from_tuples([
        # 1月2日数据被修订（v 从 20 → 25）+ 新增 1月3日
        (pd.Timestamp("2024-01-02"), "000001.SZ"),
        (pd.Timestamp("2024-01-03"), "000001.SZ"),
    ], names=["date", "symbol"])
    new = pd.DataFrame({"v": [25, 30]}, index=new_idx)

    merged = _merge_dedup(old, new)
    # 3 行：1月1日(旧保留) + 1月2日(新覆盖=25) + 1月3日(新 append)
    assert len(merged) == 3
    # 同 (date, symbol) 保留新：1月2日 v 应为 25（新拉的），不是 20（旧）
    assert merged.loc[(pd.Timestamp("2024-01-02"), "000001.SZ"), "v"] == 25
    # 旧数据保留：1月1日 v=10
    assert merged.loc[(pd.Timestamp("2024-01-01"), "000001.SZ"), "v"] == 10
    # 新增：1月3日 v=30
    assert merged.loc[(pd.Timestamp("2024-01-03"), "000001.SZ"), "v"] == 30


def test_merge_dedup_datetimeindex_keep_new():
    """DatetimeIndex（宏观指标）：单层索引去重保留新。"""
    from data.tools.sync_incremental import _merge_dedup
    old = pd.DataFrame({"cpi": [1.0, 2.0]},
                       index=pd.DatetimeIndex(["2024-01-01", "2024-02-01"]))
    new = pd.DataFrame({"cpi": [2.5, 3.0]},  # 2月1日修订 + 3月新增
                       index=pd.DatetimeIndex(["2024-02-01", "2024-03-01"]))
    merged = _merge_dedup(old, new)
    assert len(merged) == 3
    assert merged.loc[pd.Timestamp("2024-02-01"), "cpi"] == 2.5  # 新覆盖旧
    assert merged.loc[pd.Timestamp("2024-01-01"), "cpi"] == 1.0  # 旧保留
    assert merged.loc[pd.Timestamp("2024-03-01"), "cpi"] == 3.0  # 新增


def _setup_key(key: str, lake_path: str, by: str = "date", **extra):
    """注册/覆盖一个测试数据集到 TUSHARE_DATASETS + LAKE_CONFIG。"""
    from config import TUSHARE_DATASETS, LAKE_CONFIG
    cfg = {
        "api": "fake_api", "by": by,
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,v",
        "lake": lake_path,
    }
    cfg.update(extra)
    TUSHARE_DATASETS[key] = cfg
    LAKE_CONFIG["lakes"][key] = lake_path


def _make_multiindex_df(rows):
    """构造 MultiIndex(date, symbol) DataFrame，rows=[(date_str, symbol, v), ...]。"""
    idx = pd.MultiIndex.from_tuples(
        [(pd.Timestamp(d), s) for d, s, _ in rows], names=["date", "symbol"])
    return pd.DataFrame({"v": [v for _, _, v in rows]}, index=idx)


@pytest.fixture
def mock_sync_dataset(monkeypatch):
    """mock data.tushare_sync.sync_dataset，按预设回调决定写什么数据到 lake。

    Why 不直接 mock data.tools.sync_incremental.sync_dataset：sync_incremental.py 顶部
    `from data.tushare_sync import sync_dataset` 把函数对象绑到 sync_incremental 模块全局，
    必须 patch data.tools.sync_incremental.sync_dataset 才能短路（与 test_tushare_sync 的
    fake_pro 双重 patch 同理——模块顶部 from import 的绑定语义）。
    """
    calls = []

    def _fake_sync_dataset(key, start, end, symbols=None, resume=True):
        calls.append({"key": key, "start": start, "end": end,
                      "symbols": symbols, "resume": resume})
        # 让回调决定写什么（默认回调由测试提供）
        writer = _fake_sync_dataset.writer
        if writer is not None:
            writer(key, start, end)
        return None

    _fake_sync_dataset.writer = None  # 测试可覆盖
    monkeypatch.setattr("data.tools.sync_incremental.sync_dataset", _fake_sync_dataset)
    return calls, _fake_sync_dataset


def test_sync_one_key_incremental_multiindex(tmp_path, mock_sync_dataset):
    """增量场景：旧 parquet 有 1月1-2日 → sync_dataset 拉到 1月3-4日 → merge 后 4 行。

    验证：
      - d0=旧 parquet 最新日（1月2日）
      - sync_dataset 收到 start='2024-01-03'（d0 次日），end=today
      - merge 后旧+新都在，无重复行
    """
    from data.tools.sync_incremental import sync_one_key
    from config import TUSHARE_DATASETS

    lake = str(tmp_path / "moneyflow.parquet")
    _setup_key("_test_inc", lake, by="date")
    # 预置旧 parquet（1月1-2日，2 行）
    old_df = _make_multiindex_df([
        ("2024-01-01", "000001.SZ", 10),
        ("2024-01-02", "000001.SZ", 20),
    ])
    old_df.to_parquet(lake, engine="pyarrow")

    today_str = "2024-01-04"
    # sync_dataset 写入新数据（1月3-4日，2 行）
    def writer(key, start, end):
        new_df = _make_multiindex_df([
            ("2024-01-03", "000001.SZ", 30),
            ("2024-01-04", "000001.SZ", 40),
        ])
        new_df.to_parquet(TUSHARE_DATASETS[key]["lake"], engine="pyarrow")
    mock_sync_dataset[1].writer = writer

    import io
    log = io.StringIO()
    ok, msg = sync_one_key("_test_inc", today_str, fallback_years=3, max_days=None, log=log)

    assert ok, f"应成功：{msg}"
    calls = mock_sync_dataset[0]
    # sync_dataset 被以 start='2024-01-03'（d0=1月2日的次日）调用
    assert calls[0]["start"] == "2024-01-03"
    assert calls[0]["end"] == "2024-01-04"
    # merge 后共 4 行（旧 1-2 + 新 3-4）
    merged = pd.read_parquet(lake)
    assert len(merged) == 4
    # 旧数据保留
    assert merged.loc[(pd.Timestamp("2024-01-01"), "000001.SZ"), "v"] == 10
    # 新数据 append
    assert merged.loc[(pd.Timestamp("2024-01-04"), "000001.SZ"), "v"] == 40


def test_sync_one_key_skip_unavailable(tmp_path, mock_sync_dataset):
    """_unavailable 数据集被跳过：不调 sync_dataset，不写盘。"""
    from data.tools.sync_incremental import sync_one_key
    from config import TUSHARE_DATASETS

    lake = str(tmp_path / "unavailable.parquet")
    _setup_key("_test_unavail", lake, by="date",
               _unavailable="测试：代理无此接口")
    # 即使旧 parquet 存在，sync_one_key 也不应触碰它
    pd.DataFrame({"v": [1]}).to_parquet(lake, engine="pyarrow")

    import io
    log = io.StringIO()
    ok, msg = sync_one_key("_test_unavail", "2024-01-04", 3, None, log)

    assert ok
    # sync_dataset 完全未被调用（_unavailable 早返）
    assert len(mock_sync_dataset[0]) == 0
    # 旧 parquet 未被改写
    assert pd.read_parquet(lake)["v"].tolist() == [1]


def test_sync_one_key_first_time_fallback(tmp_path, mock_sync_dataset):
    """parquet 不存在 → 全量回退：start = today - 365*years。"""
    from data.tools.sync_incremental import sync_one_key
    from config import TUSHARE_DATASETS

    lake = str(tmp_path / "margin.parquet")  # 不创建，触发首次回退
    _setup_key("_test_first", lake, by="date")

    today_str = "2024-01-10"
    years = 3

    # sync_dataset 写入一些数据
    def writer(key, start, end):
        df = _make_multiindex_df([("2024-01-10", "000001.SZ", 100)])
        df.to_parquet(TUSHARE_DATASETS[key]["lake"], engine="pyarrow")
    mock_sync_dataset[1].writer = writer

    import io
    log = io.StringIO()
    ok, msg = sync_one_key("_test_first", today_str, fallback_years=years,
                           max_days=None, log=log)

    assert ok
    # sync_dataset 的 start 应是 today - 3 年（约 2021-01-11，允许 ±1 天闰年偏差）
    start_ts = pd.Timestamp(mock_sync_dataset[0][0]["start"])
    expected_min = pd.Timestamp(today_str) - pd.Timedelta(days=365 * years + 2)
    expected_max = pd.Timestamp(today_str) - pd.Timedelta(days=365 * years - 2)
    assert expected_min <= start_ts <= expected_max, (
        f"首次回退 start 应 ≈ today-3年，实际 {start_ts}")


def test_sync_one_key_empty_new_keeps_old(tmp_path, mock_sync_dataset):
    """新数据为空（节假日/接口故障）→ 不覆盖旧 parquet（关键防线）。

    Why 关键：节假日 sync_dataset 拉不到数据，single 模式可能落空 parquet；
    若直接 merge 会丢失旧历史。本测试钉死此防线：空数据时旧 parquet 必须完整保留。
    """
    from data.tools.sync_incremental import sync_one_key
    from config import TUSHARE_DATASETS

    lake = str(tmp_path / "cn_cpi.parquet")
    _setup_key("_test_empty", lake, by="single")
    # 预置旧数据（DatetimeIndex，宏观范式）
    old_df = pd.DataFrame({"cpi": [1.0, 2.0]},
                          index=pd.DatetimeIndex(["2024-01-01", "2024-02-01"]))
    old_df.to_parquet(lake, engine="pyarrow")

    # sync_dataset 写入空 parquet（模拟 single 模式节假日返空落盘）
    def writer(key, start, end):
        pd.DataFrame({"cpi": []}, index=pd.DatetimeIndex([]))\
            .to_parquet(TUSHARE_DATASETS[key]["lake"], engine="pyarrow")
    mock_sync_dataset[1].writer = writer

    import io
    log = io.StringIO()
    ok, msg = sync_one_key("_test_empty", "2024-04-01", 3, None, log)

    assert ok, "空数据应被视为可跳过（成功），不是失败"
    # 旧 parquet 必须完整保留（2 行未被空覆盖）
    df_after = pd.read_parquet(lake)
    assert len(df_after) == 2, f"旧数据应保留 2 行，实际 {len(df_after)}"
    assert df_after.loc[pd.Timestamp("2024-01-01"), "cpi"] == 1.0


def test_sync_one_key_max_days_caps_window(tmp_path, mock_sync_dataset):
    """--days N 限制：d0 距今 > N 天时，start 被截到 today-N（防一次性拉多年）。"""
    from data.tools.sync_incremental import sync_one_key
    from config import TUSHARE_DATASETS

    lake = str(tmp_path / "old.parquet")
    _setup_key("_test_cap", lake, by="date")
    # 预置很旧的 parquet（d0=2023-01-01，距今 1 年+）
    old_df = _make_multiindex_df([("2023-01-01", "000001.SZ", 1)])
    old_df.to_parquet(lake, engine="pyarrow")

    today_str = "2024-04-01"
    # --days 7：start 应被截到 today-7（而非 d0+1=2023-01-02）
    def writer(key, start, end):
        new_df = _make_multiindex_df([("2024-04-01", "000001.SZ", 2)])
        new_df.to_parquet(TUSHARE_DATASETS[key]["lake"], engine="pyarrow")
    mock_sync_dataset[1].writer = writer

    import io
    log = io.StringIO()
    ok, msg = sync_one_key("_test_cap", today_str, fallback_years=3,
                           max_days=7, log=log)

    assert ok
    start_ts = pd.Timestamp(mock_sync_dataset[0][0]["start"])
    expected = pd.Timestamp(today_str) - pd.Timedelta(days=7)
    assert start_ts == expected, (
        f"--days 7 应把 start 截到 today-7={expected}，实际 {start_ts}")


def test_daily_symbol_keys_registered_as_by_symbol():
    """DAILY/PERIODIC_SYMBOL_KEYS 必须已注册且为 by=symbol（否则日频扩展静默落空）。"""
    from config import TUSHARE_DATASETS
    from data.tools.sync_incremental import (
        DAILY_SYMBOL_KEYS, PERIODIC_SYMBOL_KEYS,
    )
    for k in DAILY_SYMBOL_KEYS + list(PERIODIC_SYMBOL_KEYS):
        assert k in TUSHARE_DATASETS, f"{k} 未注册到 TUSHARE_DATASETS"
        assert TUSHARE_DATASETS[k]["by"] == "symbol", f"{k} 应为 by=symbol"


def test_needs_period_refresh_age_guard(tmp_path):
    """周期条守卫：湖缺失/过旧 → True（重拉）；湖够新 → False（跳过）。"""
    from data.tools.sync_incremental import _needs_period_refresh

    lake = str(tmp_path / "weekly.parquet")
    _setup_key("_test_weekly", lake, by="symbol")

    # 湖缺失 → 需重拉
    assert _needs_period_refresh("_test_weekly", 6, "2024-01-10") is True
    # 旧湖（2024-01-01，距今 9 天 > 6）→ 需重拉
    _make_multiindex_df([("2024-01-01", "000001.SZ", 1)]).to_parquet(lake, engine="pyarrow")
    assert _needs_period_refresh("_test_weekly", 6, "2024-01-10") is True
    # 新湖（2024-01-08，距今 2 天 ≤ 6）→ 跳过
    _make_multiindex_df([("2024-01-08", "000001.SZ", 1)]).to_parquet(lake, engine="pyarrow")
    assert _needs_period_refresh("_test_weekly", 6, "2024-01-10") is False


def test_main_runs_quick_then_symbol_and_skips_fresh_periodic(monkeypatch, tmp_path):
    """main() 批顺序：quick 先、symbol 后；周期条湖已最新 → 跳过不调 sync_one_key。"""
    import data.tools.sync_incremental as si

    monkeypatch.setattr(si, "classify", lambda: (["moneyflow"], ["fina_income"]))
    monkeypatch.setattr(si, "DAILY_SYMBOL_KEYS", ["fund_daily"])
    monkeypatch.setattr(si, "PERIODIC_SYMBOL_KEYS", {"weekly": 6})
    monkeypatch.setattr(si, "TUSHARE_DATASETS", {
        "moneyflow": {"by": "date"},
        "fund_daily": {"by": "symbol"},
        "weekly": {"by": "symbol"},
    })
    monkeypatch.setattr(si, "_needs_period_refresh",
                        lambda key, age, today: key != "weekly")  # weekly 已最新
    called = []

    def _fake_sync_one_key(key, today, years, days, log):
        called.append(key)
        return True, "ok"

    monkeypatch.setattr(si, "sync_one_key", _fake_sync_one_key)
    log = str(tmp_path / "inc.log")
    monkeypatch.setattr(si.sys, "argv", ["sync_incremental.py", "--log", log])

    with pytest.raises(SystemExit) as exc:
        si.main()
    assert exc.value.code == 0
    # quick 先跑，symbol 日频后跑；weekly 被守卫跳过
    assert called == ["moneyflow", "fund_daily"]
    # 日志里应记录 weekly 跳过
    assert "weekly" in open(log, encoding="utf-8").read()


# ============ [6] merged 最终落盘守卫（T13-A · review P1 修复） ============
# 物理意图：date_range 数据集（shibor）的 _sync_single 窗口中间写放行后，[4] 已把 lake
# 覆盖成窗口；最终防线在 [6] 用内存 old_df 行数作基线（非读文件，因文件已被覆盖）。
# merged 骤降则拒写并用 old_df 还原 lake（回滚 [4] 的窗口中间写）。


def test_sync_one_key_merged_guard_rejects_shrink(tmp_path, mock_sync_dataset, monkeypatch):
    """[6] merged 守卫：merge/dedup bug 致 merged << old → 拒写 + 还原旧湖（不留窗口抹除态）。"""
    from data.tools.sync_incremental import sync_one_key
    from data.integrity import existing_row_count
    from config import TUSHARE_DATASETS

    lake = str(tmp_path / "shibor.parquet")
    _setup_key("_test_shrink", lake, by="single", date_range=True)
    # 现有大湖 1000 行
    old_df = pd.DataFrame({"rate": range(1000)},
                          index=pd.DatetimeIndex(pd.date_range("2023-01-01", periods=1000)))
    old_df.to_parquet(lake, engine="pyarrow")

    # sync_dataset 写窗口（1 行，模拟 _sync_single 窗口中间写）
    def writer(key, start, end):
        pd.DataFrame({"rate": [1.0]}, index=pd.DatetimeIndex(["2026-08-11"])) \
            .to_parquet(TUSHARE_DATASETS[key]["lake"], engine="pyarrow")
    mock_sync_dataset[1].writer = writer

    # mock _merge_dedup 返回异常小 df（10 行 << 旧 1000，模拟 bug 致 merged 收缩）
    import data.tools.sync_incremental as si
    tiny = pd.DataFrame({"rate": range(10)},
                        index=pd.DatetimeIndex(pd.date_range("2026-08-01", periods=10)))
    monkeypatch.setattr(si, "_merge_dedup", lambda old, new: tiny)

    import io
    log = io.StringIO()
    ok, msg = sync_one_key("_test_shrink", "2026-08-11", 3, None, log)
    assert ok is False, "merge 骤降应拒写 return False"
    assert "骤降" in msg or "拒写" in msg
    # 旧湖还原（1000 行，非 [4] 窗口的 1 行抹除态）
    assert existing_row_count(lake) == 1000
