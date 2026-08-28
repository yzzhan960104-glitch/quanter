# -*- coding: utf-8 -*-
"""data/tools/sync_daily_incremental.py 单元测试（Phase 1.5 任务2 TDD）。

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
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from data.tools import sync_daily_incremental as mod


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
         patch.object(mod, "datetime") as mock_dt:
        # today = d0 = 2026-07-24 → 早返
        mock_dt.today.return_value.strftime.return_value = "2026-07-24"
        msg = mod.sync_daily_incremental(allow_intraday=True)
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
        msg = mod.sync_daily_incremental(allow_intraday=True)
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
    # 守卫放行 stub（T13-A + G5）：safe_overwrite 升级为「守卫 + 原子写」单点后，
    # 落盘 to_parquet 在 safe_overwrite 内部完成；本测聚焦前复权数学（combined 是 3 行
    # 小数据 vs 生产湖 1020万，真实守卫会拒写 + 真实 to_parquet 写生产 LAKE 路径污染），
    # 故 stub safe_overwrite 为「捕获 df 不真写」——拿到 combined 验证数学断言。
    def fake_safe_overwrite(path, df, **k):
        written["df"] = df
    with patch.object(mod.pd, "read_parquet", return_value=fake_lake), \
         patch.object(mod, "get_pro", return_value=pro), \
         patch.object(mod, "safe_overwrite", fake_safe_overwrite), \
         patch("datetime.datetime") as mock_dt:
        mock_dt.today.return_value.strftime.return_value = "2026-07-24"
        msg = mod.sync_daily_incremental(allow_intraday=True)

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
    # 守卫放行 stub（T13-A + G5）：safe_overwrite 升级为「守卫 + 原子写」单点，本测聚焦
    # 除权 detect（combined 是 2 行小数据 vs 生产湖 1020万，真实守卫拒写 + 真实 to_parquet
    # 写生产 LAKE 污染），stub safe_overwrite 为「捕获 df 不真写」——拿到 combined 验证除权。
    def fake_safe_overwrite(path, df, **k):
        written["df"] = df
    with patch.object(mod.pd, "read_parquet", return_value=fake_lake), \
         patch.object(mod, "get_pro", return_value=pro), \
         patch.object(mod, "safe_overwrite", fake_safe_overwrite), \
         patch("datetime.datetime") as mock_dt:
        mock_dt.today.return_value.strftime.return_value = "2026-07-24"
        msg = mod.sync_daily_incremental(allow_intraday=True)
    # msg 含「除权标的 1 只待重算」（adj 1.0 → 2.0 跳变被 detect）
    assert "除权标的" in msg and "1" in msg


# ============================================================================
# ⑤ 规则5：_backscan_recent 近期连续性回扫
# ============================================================================
def test_backscan_recent_returns_unjustified_gaps():
    """回扫：近期 df 含漏采段（非停牌）→ 返 unjustified GapRange。

    物理意图：sync 增量只补 d0→today，d0 之前近期缺口不补；回扫抽查近期连续性，
    发现漏采则告警/触发 repair_gaps（规则5，防历史缺口累积）。
    """
    df = pd.DataFrame(
        {"close": [1, 1, 1, 1]},
        index=pd.MultiIndex.from_tuples([
            (pd.Timestamp("2024-09-02"), "000001.SZ"),
            (pd.Timestamp("2024-09-03"), "000001.SZ"),
            (pd.Timestamp("2024-09-06"), "000001.SZ"),  # 缺 09-04, 09-05
            (pd.Timestamp("2024-09-09"), "000001.SZ"),
        ], names=["date", "symbol"]),
    )
    trade_days = {"2024-09-02", "2024-09-03", "2024-09-04",
                  "2024-09-05", "2024-09-06", "2024-09-09"}
    gaps = mod._backscan_recent(df, trade_days, suspend_intervals={}, days=30)
    assert len(gaps) == 1
    assert gaps[0].symbol == "000001.SZ"
    assert not gaps[0].suspend_justified


def test_backscan_recent_clean_returns_empty():
    """回扫：近期完整（无缺口）→ []。"""
    df = pd.DataFrame(
        {"close": [1, 1, 1]},
        index=pd.MultiIndex.from_tuples([
            (pd.Timestamp("2024-09-02"), "000001.SZ"),
            (pd.Timestamp("2024-09-03"), "000001.SZ"),
            (pd.Timestamp("2024-09-04"), "000001.SZ"),
        ], names=["date", "symbol"]),
    )
    trade_days = {"2024-09-02", "2024-09-03", "2024-09-04"}
    assert mod._backscan_recent(df, trade_days, suspend_intervals={}, days=30) == []


# ============================================================================
# ⑥ 写入守卫接入 + 分页限频（原根级 test_sync_daily_incremental.py 并入，2026-08-19 W1）
# ============================================================================
class _FakeDT:
    """替身 datetime：today().strftime() 返回固定日期，让 sync 跑增量分支（d0 < today）。

    Why 替身而非 patch 真实 datetime：sync_daily_incremental 顶部 `from datetime import
    datetime` 已把 datetime 类绑定到模块名，monkeypatch.setattr(mod, "datetime", _FakeDT)
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
    真实 shrink 极罕见，故用 stub 反证接入）。上方 ② 组用例 stub safe_overwrite 为
    「捕获不真写」，本测是唯一验证「拒写异常不被吞」的接入语义。
    """
    from data.integrity import WriteGuardError

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

    monkeypatch.setattr(mod, "LAKE", str(lake_path))
    monkeypatch.setattr(mod.pd, "read_parquet", lambda p: fake_lake)
    monkeypatch.setattr(mod, "get_pro", lambda: pro)
    monkeypatch.setattr(mod, "datetime", _FakeDT)
    # stub 守卫抛错，证明 sync 不静默吞（接入前此属性不存在 → monkeypatch raising error，
    # 即 red；接入后 mock 生效 → WriteGuardError 传播 → green）
    def _raise(*a, **k):
        raise WriteGuardError("stub: combined 异常收缩")
    monkeypatch.setattr(mod, "safe_overwrite", _raise)

    # no_backscan/no_recompute_div=True 禁用回扫与除权重算，聚焦落盘点守卫路径
    with pytest.raises(WriteGuardError):
        mod.sync_daily_incremental(no_backscan=True, no_recompute_div=True,
                                   allow_intraday=True)


def test_fetch_paged_acquires_rate_limit_per_page(monkeypatch):
    """_fetch_paged 每页前 acquire basic 桶令牌（T13-B #5，防 Tushare 限频封禁）。

    物理意图：repair 多日补采 + sync 增量连续分页，无限频会撞 Tushare 500/min 封禁。
    _fetch_paged 每页前 acquire basic 桶，一处改两处受益（sync + repair 共用）。
    上方 ① 组的分页机制断言（offset 递增）与限频是正交两维——本测只守限频维。
    """
    from data.resilience import tushare_rate_limiter_basic

    # mock 限频器 acquire 计数（避免真实令牌消耗 + 断言调用次数 = 页数）
    acq_calls = []
    monkeypatch.setattr(tushare_rate_limiter_basic, "acquire",
                        lambda tokens=1.0: acq_calls.append(tokens))

    # mock pro：第 1 页返 PAGE 行（满页），第 2 页返 100 行（< PAGE，终止）
    page1 = pd.DataFrame({"ts_code": [f"{i:06d}.SZ" for i in range(mod.PAGE)],
                          "trade_date": ["20260101"] * mod.PAGE})
    page2 = pd.DataFrame({"ts_code": [f"{i:06d}.SZ" for i in range(100)],
                          "trade_date": ["20260101"] * 100})
    pages = [page1, page2]
    pro = MagicMock()
    pro.daily = MagicMock(side_effect=lambda **kw: pages.pop(0) if pages else pd.DataFrame())

    df = mod._fetch_paged(pro, "daily", "20260101")

    # 2 页 → acquire 2 次（每页前一次：page1 acquire→500 满→page2 acquire→100 末页 break）
    assert len(acq_calls) == 2, f"每页应 acquire 一次（2 页期望 2 次），实际 {len(acq_calls)}"
    assert len(df) == mod.PAGE + 100  # 500 + 100


# ============================================================================
# W2（2026-08-28 全库评审清偿）：交易时段闸 / adj 完整性拒写 / 当日重拉 / 除权重算 sidecar
# ============================================================================
from datetime import datetime as _dt


def test_intraday_guard_blocks_weekday_market_hours():
    """工作日 09:15–15:05 拒跑（半截 bar 落湖防护，P0-3 触发面）。"""
    for h, m in [(9, 15), (10, 30), (14, 59), (15, 4)]:
        with pytest.raises(RuntimeError, match="交易时段拒跑"):
            mod._intraday_guard(False, now=_dt(2026, 8, 28, h, m))   # 周五


def test_intraday_guard_allows_outside_hours_and_weekend():
    """盘前/收盘后/周末放行（17:30 pipeline 与跨 18:00 补跑在窗外）。"""
    for d, h, m in [(28, 9, 14), (28, 15, 5), (28, 23, 0), (29, 10, 0), (30, 12, 0)]:
        mod._intraday_guard(False, now=_dt(2026, 8, d, h, m))        # 不抛
    mod._intraday_guard(True, now=_dt(2026, 8, 28, 10, 0))           # 逃生口


def test_adj_nan_refuses_write():
    """W2-1：merge 后 adj_factor NaN → 整批拒落盘 raise（绝不写 NaN 价格行）。

    构造：07-23 raw 有 2 标的，adj 只回 1 只 → 缺失行 adj_factor=NaN → 必炸且
    safe_overwrite 不被触达（湖零污染）。"""
    fake_lake = pd.DataFrame(
        {"close": [10.0]},
        index=pd.MultiIndex.from_tuples(
            [(pd.Timestamp("2026-07-20"), "000001.SZ")], names=["date", "symbol"]))
    trade_days = ["20260723"]
    raw_by_day = {
        "20260723": pd.DataFrame({
            "ts_code": ["000001.SZ", "000002.SZ"], "trade_date": ["20260723"] * 2,
            "open": [22.0, 8.0], "high": [22.5, 8.5], "low": [21.5, 7.5],
            "close": [22.0, 8.0], "vol": [1100, 2200], "amount": [24000.0, 17000.0]}),
    }
    adj_by_day = {   # 只有 000001 的 adj —— 000002 merge 后 NaN
        "20260720": pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20260720"], "adj_factor": [1.0]}),
        "20260723": pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20260723"], "adj_factor": [1.0]}),
    }
    pro = _build_pro(trade_days, raw_by_day, adj_by_day)
    called = []

    def fail_if_called(path, df, **k):
        called.append(path)

    with patch.object(mod.pd, "read_parquet", return_value=fake_lake), \
         patch.object(mod, "get_pro", return_value=pro), \
         patch.object(mod, "safe_overwrite", fail_if_called), \
         patch("datetime.datetime") as mock_dt:
        mock_dt.today.return_value.strftime.return_value = "2026-07-24"
        with pytest.raises(RuntimeError, match="adj 完整性闸"):
            mod.sync_daily_incremental(no_backscan=True, no_recompute_div=True,
                                       allow_intraday=True)
    assert called == [], "adj NaN 时绝不落盘"


def test_refetch_today_repulls_and_overwrites():
    """W2-2 refetch_today：d0==today 仍重取当日，dedup keep=last 覆盖旧行（半截修复口）。"""
    fake_lake = pd.DataFrame(
        {"close": [9.5]},   # 盘中写的半截 close
        index=pd.MultiIndex.from_tuples(
            [(pd.Timestamp("2026-07-23"), "000001.SZ")], names=["date", "symbol"]))
    trade_days = ["20260723"]
    raw_by_day = {
        "20260723": pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20260723"],
                                  "open": [22.0], "high": [22.5], "low": [21.5],
                                  "close": [22.0], "vol": [1100], "amount": [24000.0]}),
    }
    adj_by_day = {
        "20260723": pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20260723"], "adj_factor": [1.0]}),
    }
    pro = _build_pro(trade_days, raw_by_day, adj_by_day)
    written = {}

    class _DT0723:
        """模块级 datetime 绑定的替身（patch("datetime.datetime") 拦不住 from-import 绑定）。"""
        @staticmethod
        def today():
            class _D:
                def strftime(self, fmt):
                    return "2026-07-23"
            return _D()

    def fake_safe_overwrite(path, df, **k):
        written["df"] = df

    real_dt = mod.datetime
    mod.datetime = _DT0723
    try:
        with patch.object(mod.pd, "read_parquet", return_value=fake_lake), \
             patch.object(mod, "get_pro", return_value=pro), \
             patch.object(mod, "safe_overwrite", fake_safe_overwrite):
            msg = mod.sync_daily_incremental(no_backscan=True, no_recompute_div=True,
                                             refetch_today=True, allow_intraday=True)
    finally:
        mod.datetime = real_dt
    assert "OK 最新日" in msg
    df = written["df"]
    assert abs(df.loc[(pd.Timestamp("2026-07-23"), "000001.SZ")]["close"] - 22.0) < 1e-6, \
        "半截 close 9.5 必须被重取的 22.0 覆盖"


def test_pending_recompute_sidecar_roundtrip(tmp_path, monkeypatch):
    """W2-5：失败清单原子落盘 + 下轮读回优先重试。"""
    import json
    monkeypatch.setattr(mod, "_PENDING_RECOMPUTE", str(tmp_path / "pending.json"))
    mod._save_pending_recompute(["000002.SZ", "000003.SZ"])
    assert mod._load_pending_recompute() == ["000002.SZ", "000003.SZ"]
    mod._save_pending_recompute([])          # 成功清账
    assert mod._load_pending_recompute() == []
    # 损坏 sidecar 读失败 → []（不阻断主流程）
    (tmp_path / "pending.json").write_text("not-json", encoding="utf-8")
    assert mod._load_pending_recompute() == []
