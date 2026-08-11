# -*- coding: utf-8 -*-
"""sync_daily_incremental 写入守卫接入测试（T13-A · Task 4）。

物理意图：增量 append 日常 combined >= 现有（守卫放行）；本测试证明接入点存在且
守卫拒写能传播（不静默吞）。守卫本身的 shrink 判定已在 tests/test_integrity.py 覆盖。

Why 独立根级文件（非 tests/scripts/test_sync_daily_incremental.py）：scripts 版覆盖
正常 append 的数学正确性（分页/前复权/除权检测）；本文件专注 T13-A 守卫接入语义
（拒写传播）。全链 mock 范式（_build_pro + patch.object datetime）参照 scripts 版，
让 sync_daily_incremental 真正跑到落盘点 :244，再 stub 守卫验证异常不被吞。
"""
import pandas as pd
import pytest


def _build_pro(trade_days_list, raw_by_day, adj_by_day):
    """构造 mock pro：trade_cal + daily(trade_date) + adj_factor(trade_date) 全 mock。

    复用 tests/scripts/test_sync_daily_incremental.py 同款范式（_build_pro）。
    """
    from unittest.mock import MagicMock
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


class _FakeDT:
    """替身 datetime：today().strftime() 返回固定日期，让 sync 跑增量分支（d0 < today）。

    Why 替身而非 patch 真实 datetime：sync_daily_incremental 顶部 `from datetime import
    datetime` 已把 datetime 类绑定到模块名，monkeypatch.setattr(sdi, "datetime", _FakeDT)
    才能拦住 `datetime.today()`（patch datetime.datetime 拦不住模块级绑定，与 get_pro
    局部 import 同理）。
    """
    @staticmethod
    def today():
        class _D:
            def strftime(self, fmt):
                return "2024-01-03"
        return _D()


def test_sync_daily_guard_propagates_reject(tmp_path, monkeypatch):
    """增量同步落盘前守卫拒写 → WriteGuardError 传播（不静默吞）。

    全链 mock（_build_pro + read_parquet + get_pro + datetime）让 sync_daily_incremental
    跑到落盘点 combined.to_parquet；stub assert_safe_overwrite 抛错，证明接入点存在且
    异常不被吞。守卫本身的 shrink 判定已在 test_integrity 覆盖（append 日常必增长，
    真实 shrink 极罕见，故用 stub 反证接入）。
    """
    from data.integrity import WriteGuardError
    from data.tools import sync_daily_incremental as sdi

    lake_path = tmp_path / "a_shares_daily.parquet"
    # 现有 lake（d0=2024-01-01，1 行）
    fake_lake = pd.DataFrame(
        {"close": [10.0]},
        index=pd.MultiIndex.from_tuples(
            [(pd.Timestamp("2024-01-01"), "000001.SZ")], names=["date", "symbol"]))

    trade_days = ["20240102", "20240103"]
    raw_by_day = {
        "20240102": pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20240102"],
                                  "open": [20.0], "high": [21.0], "low": [19.5],
                                  "close": [20.0], "vol": [1000], "amount": [20000.0]}),
        "20240103": pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20240103"],
                                  "open": [22.0], "high": [22.5], "low": [21.5],
                                  "close": [22.0], "vol": [1100], "amount": [24000.0]}),
    }
    adj_by_day = {
        "20240101": pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20240101"], "adj_factor": [1.0]}),
        "20240102": pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20240102"], "adj_factor": [1.0]}),
        "20240103": pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20240103"], "adj_factor": [1.0]}),
    }
    pro = _build_pro(trade_days, raw_by_day, adj_by_day)

    monkeypatch.setattr(sdi, "LAKE", str(lake_path))
    monkeypatch.setattr(sdi.pd, "read_parquet", lambda p: fake_lake)
    monkeypatch.setattr(sdi, "get_pro", lambda: pro)
    monkeypatch.setattr(sdi, "datetime", _FakeDT)
    # stub 守卫抛错，证明 sync 不静默吞（接入前此属性不存在 → monkeypatch raising error，
    # 即 red；接入后 mock 生效 → WriteGuardError 传播 → green）
    def _raise(*a, **k):
        raise WriteGuardError("stub: combined 异常收缩")
    monkeypatch.setattr(sdi, "safe_overwrite", _raise)

    # no_backscan/no_recompute_div=True 禁用回扫与除权重算，聚焦落盘点守卫路径
    with pytest.raises(WriteGuardError):
        sdi.sync_daily_incremental(no_backscan=True, no_recompute_div=True)


def test_fetch_paged_acquires_rate_limit_per_page(monkeypatch):
    """_fetch_paged 每页前 acquire basic 桶令牌（T13-B #5，防 Tushare 限频封禁）。

    物理意图：repair 多日补采 + sync 增量连续分页，无限频会撞 Tushare 500/min 封禁。
    _fetch_paged 每页前 acquire basic 桶，一处改两处受益（sync + repair 共用）。
    """
    from unittest.mock import MagicMock
    from data.tools import sync_daily_incremental as sdi
    from data.resilience import tushare_rate_limiter_basic

    # mock 限频器 acquire 计数（避免真实令牌消耗 + 断言调用次数 = 页数）
    acq_calls = []
    monkeypatch.setattr(tushare_rate_limiter_basic, "acquire",
                        lambda tokens=1.0: acq_calls.append(tokens))

    # mock pro：第 1 页返 PAGE 行（满页），第 2 页返 100 行（< PAGE，终止）
    page1 = pd.DataFrame({"ts_code": [f"{i:06d}.SZ" for i in range(sdi.PAGE)],
                          "trade_date": ["20260101"] * sdi.PAGE})
    page2 = pd.DataFrame({"ts_code": [f"{i:06d}.SZ" for i in range(100)],
                          "trade_date": ["20260101"] * 100})
    pages = [page1, page2]
    pro = MagicMock()
    pro.daily = MagicMock(side_effect=lambda **kw: pages.pop(0) if pages else pd.DataFrame())

    df = sdi._fetch_paged(pro, "daily", "20260101")

    # 2 页 → acquire 2 次（每页前一次：page1 acquire→500 满→page2 acquire→100 末页 break）
    assert len(acq_calls) == 2, f"每页应 acquire 一次（2 页期望 2 次），实际 {len(acq_calls)}"
    assert len(df) == sdi.PAGE + 100  # 500 + 100
