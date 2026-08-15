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

import json

import pandas as pd
import pytest

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


# ============================================================================
# main 写入守卫接入（T13-A · Task 4）
# ============================================================================
# 物理意图：repair_gaps.main 重写 a_shares_daily 湖（全量覆盖，非 append），是潜在抹除
# 路径。接入 assert_safe_overwrite 后，若 repair_gaps 产出的 new_lake 异常收缩（dedup/
# recompute bug）→ 落盘前守卫拒写，原湖完好。本测试用 --report 模式绕开 --auto 的 scan
# 触网，mock repair_gaps 返回收缩湖，证明守卫拒写能传播、不静默落盘。


def _big_lake(n):
    """造 n 行连续日期的 daily 湖（守卫只看 parquet num_rows，OHLCV 值任意）。"""
    idx = pd.MultiIndex.from_product(
        [pd.date_range("2024-01-01", periods=n), ["000001.SZ"]], names=["date", "symbol"])
    return pd.DataFrame({"close": range(n)}, index=idx)


def test_repair_gaps_main_refuses_shrink(tmp_path, monkeypatch):
    """repair_gaps 重写湖：若 new_lake 异常收缩（mock 出 bug）→ 守卫拒写，文件不变。

    --report 模式（传报告 JSON）绕开 --auto 的 scan 触网；mock repair_gaps 返回异常小 df
    模拟 dedup/recompute bug 致 5000→100，证明守卫拒写能传播、不静默落盘。
    """
    from data.integrity import WriteGuardError, existing_row_count
    from data.tools import repair_gaps as rg

    lake_path = tmp_path / "a_shares_daily.parquet"
    _big_lake(5000).to_parquet(lake_path)

    # 报告 JSON：1 个 unjustified 漏采段（--report 模式，绕开 scan）
    report = {"gaps": [{"symbol": "000001.SZ", "start": "2024-09-04",
                        "end": "2024-09-05",
                        "missing_dates": ["2024-09-04", "2024-09-05"],
                        "suspend_justified": False}]}
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    # 拦截 main 内局部 import 的 get_pro（`from data._tushare_compat import get_pro`）
    # Why patch 源模块属性：局部 import 绑定的是 data._tushare_compat.get_pro，patch
    # rg.get_pro 拦不住（与 tushare_sync.get_pro 双绑定的 get_pro fixture 同理）。
    monkeypatch.setattr("data._tushare_compat.get_pro", lambda: object())
    # mock repair_gaps 返回异常收缩的湖（模拟 dedup/recompute bug 致 5000→100）；
    # **kw 吸收 N1 新增的 lake_dir 关键字（sidecar 锚点，与本用例守卫语义无关）
    monkeypatch.setattr(rg, "repair_gaps", lambda gaps, lake_df, pro, **kw: _big_lake(100))

    with pytest.raises(WriteGuardError):
        rg.main(["--report", str(report_path), "--lake-dir", str(tmp_path)])
    # 原湖未被覆盖（守卫在 to_parquet 前拒写）
    assert existing_row_count(str(lake_path)) == 5000


# ============ T13-B #2：配额 + 熔断（自动补采保护）===========


def test_repair_breaker_opens_after_threshold_failures(tmp_path):
    """连续 K 次失败 → 熔断开启；恢复期后解除（blueprint §2.3 不吞代理失败）。"""
    from data.tools import repair_gaps as rg
    lake_dir = str(tmp_path)
    # 成功清计数 → 未熔断
    rg.record_repair_result(success=True, lake_dir=lake_dir, now=1000.0)
    assert rg.is_repair_breaker_open(lake_dir, now=1000.0) == (False, "")
    # 连续 K-1 次失败（未达阈值）
    for _ in range(rg.REPAIR_FAILURE_THRESHOLD - 1):
        rg.record_repair_result(success=False, lake_dir=lake_dir, now=1000.0)
    assert rg.is_repair_breaker_open(lake_dir, now=1000.0) == (False, "")
    # 第 K 次失败 → 熔断开启
    rg.record_repair_result(success=False, lake_dir=lake_dir, now=1000.0)
    _open, _reason = rg.is_repair_breaker_open(lake_dir, now=1000.0)
    assert _open is True and "熔断" in _reason
    # 恢复期后 → 解除
    _after = 1000.0 + rg.REPAIR_RECOVERY_HOURS * 3600 + 1
    assert rg.is_repair_breaker_open(lake_dir, now=_after) == (False, "")


def test_repair_gaps_max_segments_quota(monkeypatch):
    """repair_gaps(max_segments=N) 截断 unjustified 到 N 段（自动补采防过载）。"""
    from data.tools import repair_gaps as rg
    # 10 段 unjustified gaps
    gaps = [GapRange(symbol=f"{i:06d}.SZ", start="2026-01-01", end="2026-01-02",
                     missing_dates=("2026-01-01",), suspend_justified=False) for i in range(10)]
    # mock _fetch_paged 返空（聚焦配额截断，不验证补采数学）
    monkeypatch.setattr(rg, "_fetch_paged", lambda *a, **k: pd.DataFrame())
    lake_df = _mk_lake([("2026-01-01", "000000.SZ")])
    # max_segments=3 截断（_fetch_paged 返空 → 无补采返原样；不抛即截断逻辑执行）
    result = rg.repair_gaps(gaps, lake_df, object(), max_segments=3)
    assert len(result) == len(lake_df)


# ============ CR-6：单日拉取异常 → 部分落盘（补采回路复活）===========
# 物理意图（repair_auto.log 实锤 25 连败熔断循环根因）：Tushare 服务端 500/min 计数
# 窗口与客户端令牌桶错位 → `_fetch_paged` 抛 Exception("频率超限") → 旧实现直接 raise，
# 已拉的 230/350 日全部丢弃白拉，且 main 记熔断失败 → 连续失败 → 熔断 6h → 回路停摆。
# 修复语义（与 :134-144 超时分支同构）：异常 break 出拉新日循环 + 已拉日继续 merge 落盘
# （部分补采 > 完全不补），partial 标记透传 main 记熔断失败计数（限频中断连续 3 次
# 才熔断退避，而非单次中断即雪崩）。


def test_partial_persist_on_fetch_error(tmp_path, monkeypatch, capsys):
    """CR-6：第 N 日 _fetch_paged 抛「频率超限」→ 已拉 1..N-1 日仍 merge 落盘、
    熔断计数 +1、进程不 raise（exit 0 带 partial 计数）。

    场景：gap 缺 09-03/09-04/09-05 三日，第 3 日（09-05）daily 拉取抛频率超限 →
    09-03/09-04（已拉）必须落湖，09-05 不在；--auto 模式记 sidecar fail_count=1
    （连续 3 次才熔断，本次仅计数不熔断）；main 返回 0 且 stdout 带 partial 标记。
    """
    from data.tools import repair_gaps as rg

    # 湖：09-02 与 09-06 在，中间 09-03~09-05 缺（一个 unjustified gap 三日）
    lake_path = tmp_path / "a_shares_daily.parquet"
    _mk_lake([("2024-09-02", "000001.SZ"), ("2024-09-06", "000001.SZ")]).to_parquet(lake_path)

    # mock --auto 内部 scan（局部 import，patch 源模块属性；同 get_pro 手法）
    monkeypatch.setattr(
        "data.tools.scan_integrity.scan",
        lambda *a, **k: {"gaps": [{"symbol": "000001.SZ", "start": "2024-09-03",
                                   "end": "2024-09-05",
                                   "missing_dates": ["2024-09-03", "2024-09-04", "2024-09-05"],
                                   "suspend_justified": False}]},
    )
    # mock get_pro（pro 本体不被真调——_fetch_paged 也被 mock）
    monkeypatch.setattr("data._tushare_compat.get_pro", lambda: object())
    # 日间隔降速默认 1.5s——测试置 0 免 3s 空转（降速逻辑由 env 默认值保证，不在此验证）
    monkeypatch.setattr(rg, "REPAIR_DAY_SLEEP", 0.0)

    def _fake_fetch_paged(pro, api, tdc):
        """前 2 日返真数据；第 3 日（20240905）daily 抛 Tushare 服务端限频原文。"""
        if tdc == "20240905" and api == "daily":
            raise Exception("抱歉，您访问接口(daily)频率超限(500次/分钟)")
        if api == "daily":
            return pd.DataFrame([{"ts_code": "000001.SZ", "trade_date": tdc,
                                  "open": 10.0, "high": 11.0, "low": 9.0,
                                  "close": 10.5, "vol": 1000}])
        return pd.DataFrame([{"ts_code": "000001.SZ", "trade_date": tdc,
                              "adj_factor": 1.0}])

    monkeypatch.setattr(rg, "_fetch_paged", _fake_fetch_paged)

    # 旧实现：Exception 从 _fetch_paged 一路 raise 出 main（230/350 白拉教训）→ 此处红
    rc = rg.main(["--auto", "--lake-dir", str(tmp_path)])

    # ① 进程不 raise，exit 0，stdout 带 partial 标记与净增计数
    assert rc == 0
    out = capsys.readouterr().out
    assert "partial" in out, "stdout 必须带 partial 标记（供 pipeline log 识别部分落盘）"
    assert "+2" in out

    # ② 已拉 1..N-1 日（09-03/09-04）merge 落盘；被打断日（09-05）不在；原有日不丢
    df = pd.read_parquet(lake_path)
    dates = df.xs("000001.SZ", level="symbol").index.strftime("%Y-%m-%d").tolist()
    assert "2024-09-03" in dates and "2024-09-04" in dates, "已拉日必须部分落盘"
    assert "2024-09-05" not in dates, "被打断日无数据，不应出现"
    assert "2024-09-02" in dates and "2024-09-06" in dates
    assert len(df) == 4

    # ③ 熔断计数 +1（限频中断记失败：连续 3 次才开熔断；本次 1 次不熔断）
    breaker = json.loads((tmp_path / ".repair_breaker.json").read_text(encoding="utf-8"))
    assert breaker["fail_count"] == 1
    assert breaker.get("open_until", 0) == 0


# ============ T6 评审 #1：daily/adj 原子入列（被打断日零 NaN 落湖）===========
# 物理意图（P1-A 红线同案）：旧实现 daily fetch 成功即 append、adj_factor 随后抛限频
# → break → 该日 daily 已入列而 adj 永缺 → merge how="left" 后 adj_factor=NaN →
# 前复权价格全 NaN 落湖，且 scan 按 index 在场判定「已补」永不复查（sync_daily_
# incremental 的 adj NaN 守卫先例明文此污染）。修复语义：d/a 两个 fetch 都成功后才
# 一起 append——被打断日本身不应出现（brief 字面「已拉 1..N-1 日落盘」）。


def test_partial_persist_on_adj_error_atomic_day(tmp_path, monkeypatch, capsys):
    """T6 评审 #1：第 N 日 adj_factor 抛限频（该日 daily 已成功）→ 该日整体不入湖。

    场景：gap 缺 09-03/09-04/09-05 三日，第 3 日（09-05）daily 拉取成功、adj_factor
    抛频率超限。旧实现 09-05 的 daily 已 append → 该日以 adj_factor=NaN → 前复权
    价格全 NaN 落湖（+3 行）；修复后 daily/adj 原子入列 → 09-05 零贡献（+2 行），
    已拉 09-03/09-04 照常部分落盘，partial/熔断计数语义不变。
    """
    from data.tools import repair_gaps as rg

    # 湖：09-02 与 09-06 在，中间 09-03~09-05 缺（一个 unjustified gap 三日）
    lake_path = tmp_path / "a_shares_daily.parquet"
    _mk_lake([("2024-09-02", "000001.SZ"), ("2024-09-06", "000001.SZ")]).to_parquet(lake_path)

    # mock --auto 内部 scan（局部 import，patch 源模块属性；同 get_pro 手法）
    monkeypatch.setattr(
        "data.tools.scan_integrity.scan",
        lambda *a, **k: {"gaps": [{"symbol": "000001.SZ", "start": "2024-09-03",
                                   "end": "2024-09-05",
                                   "missing_dates": ["2024-09-03", "2024-09-04", "2024-09-05"],
                                   "suspend_justified": False}]},
    )
    # mock get_pro（pro 本体不被真调——_fetch_paged 也被 mock）
    monkeypatch.setattr("data._tushare_compat.get_pro", lambda: object())
    # 日间隔降速默认 1.5s——测试置 0 免 3s 空转（降速逻辑由 env 默认值保证，不在此验证）
    monkeypatch.setattr(rg, "REPAIR_DAY_SLEEP", 0.0)

    def _fake_fetch_paged(pro, api, tdc):
        """daily 全部成功；第 3 日（20240905）adj_factor 抛服务端限频原文。"""
        if tdc == "20240905" and api == "adj_factor":
            raise Exception("抱歉，您访问接口(adj_factor)频率超限(500次/分钟)")
        if api == "daily":
            return pd.DataFrame([{"ts_code": "000001.SZ", "trade_date": tdc,
                                  "open": 10.0, "high": 11.0, "low": 9.0,
                                  "close": 10.5, "vol": 1000}])
        return pd.DataFrame([{"ts_code": "000001.SZ", "trade_date": tdc,
                              "adj_factor": 1.0}])

    monkeypatch.setattr(rg, "_fetch_paged", _fake_fetch_paged)

    # 旧实现：09-05 的 daily 已入列而 adj 永缺 → adj_factor=NaN → 前复权价格全 NaN
    # 落湖（+3 行，P1-A 红线）→ 此处红
    rc = rg.main(["--auto", "--lake-dir", str(tmp_path)])

    # ① 进程不 raise，exit 0，stdout 带 partial 标记；净增 +2（不含被打断日）
    assert rc == 0
    out = capsys.readouterr().out
    assert "partial" in out, "stdout 必须带 partial 标记（供 pipeline log 识别部分落盘）"
    assert "+2" in out

    # ② 已拉 1..N-1 日（09-03/09-04）merge 落盘；被打断日（09-05）零贡献不在湖——
    #    旧实现该日以 adj_factor=NaN 落湖（前复权价格全 NaN 污染），此处必须不在
    dates = (pd.read_parquet(lake_path).xs("000001.SZ", level="symbol").index
             .strftime("%Y-%m-%d").tolist())
    assert "2024-09-03" in dates and "2024-09-04" in dates, "已拉日必须部分落盘"
    assert "2024-09-05" not in dates, "被打断日不得以 adj_factor=NaN 形式落湖（原子入列）"
    assert "2024-09-02" in dates and "2024-09-06" in dates
    assert len(pd.read_parquet(lake_path)) == 4

    # ③ 熔断计数 +1（adj 中断与 daily 中断同语义：记失败，连续 3 次才开熔断）
    breaker = json.loads((tmp_path / ".repair_breaker.json").read_text(encoding="utf-8"))
    assert breaker["fail_count"] == 1
    assert breaker.get("open_until", 0) == 0


# ============================================================================
# N1 停牌真值（2026-08-16）：日级补采 + unfillable sidecar + recency-first 配额
# ============================================================================
# 物理意图（勘探实证）：unjustified 16,371 段配额 [:50]（近于 symbol 序）被
# 2000-2004 盲区段永占（Tushare daily 无该年代数据，拉取恒空）——配额死循环。
# 修复三件：① 日级判定后只拉真缺日；② 「源全段零行」记 unfillable sidecar，
# 下轮 scan/repair 双侧跳过；③ 配额改 recency-first（最新段优先，天然跳过最旧
# 盲区年代，不硬编码年份）。


def test_repair_marks_unfillable_market_empty(tmp_path, monkeypatch):
    """盲区年代形态：源对补采日全市场返零行 → 段记 unfillable sidecar，湖不动。

    2000-2004 段的 Tushare daily 恒空——旧实现只 warning，下轮 scan 重新进队
    永占配额。修复：记 sidecar（symbol/range/reason/count），下轮跳过。
    """
    from data.tools import repair_gaps as rg

    lake = _mk_lake([("2024-09-02", "000921.SZ"), ("2024-09-06", "000921.SZ")])
    gap = GapRange("000921.SZ", "2024-09-03", "2024-09-05",
                   ("2024-09-03", "2024-09-04", "2024-09-05"), suspend_justified=False)
    # _fetch_paged 恒返空（盲区年代：全市场无该日数据）
    monkeypatch.setattr(rg, "_fetch_paged", lambda *a, **k: pd.DataFrame())
    # 日间隔降速置 0 免 3s 空转（降速逻辑由 env 默认值保证，不在此验证）
    monkeypatch.setattr(rg, "REPAIR_DAY_SLEEP", 0.0)

    new_lake = rg.repair_gaps([gap], lake, object(), lake_dir=str(tmp_path))

    assert len(new_lake) == len(lake), "无可补行，湖不动"
    side = json.loads((tmp_path / ".repair_unfillable.json").read_text(encoding="utf-8"))
    e = side["entries"][0]
    assert (e["symbol"], e["start"], e["end"]) == ("000921.SZ", "2024-09-03", "2024-09-05")
    assert e["reason"] == "market_empty", "全市场零行 → 盲区年代 reason"
    assert e["count"] == 3, "count = 该段不可补交易日数"


def test_repair_marks_unfillable_symbol_absent(tmp_path, monkeypatch):
    """真停牌残留形态：市场该日有行、唯独该标的无行 → reason=symbol_absent。

    suspend_d 没记到的停牌（2019 后稀疏化）：市场健康而该股缺席 = 源侧也无数据，
    不可补——与盲区年代同记 sidecar，但 reason 区分（排查时辨方向）。
    """
    from data.tools import repair_gaps as rg

    lake = _mk_lake([("2024-09-02", "000921.SZ"), ("2024-09-06", "000921.SZ")])
    gap = GapRange("000921.SZ", "2024-09-04", "2024-09-04",
                   ("2024-09-04",), suspend_justified=False)

    def _fake(pro, api, tdc):
        # 市场健康（600000.SH 有行），唯独 000921.SZ 缺席
        if api == "daily":
            return pd.DataFrame([{"ts_code": "600000.SH", "trade_date": tdc,
                                  "open": 1.0, "high": 1.0, "low": 1.0,
                                  "close": 1.0, "vol": 100}])
        return pd.DataFrame([{"ts_code": "600000.SH", "trade_date": tdc, "adj_factor": 1.0}])

    monkeypatch.setattr(rg, "_fetch_paged", _fake)
    new_lake = rg.repair_gaps([gap], lake, object(), lake_dir=str(tmp_path))

    assert len(new_lake) == len(lake), "该标的零行，湖不动"
    side = json.loads((tmp_path / ".repair_unfillable.json").read_text(encoding="utf-8"))
    e = side["entries"][0]
    assert e["symbol"] == "000921.SZ"
    assert e["reason"] == "symbol_absent", "市场有数据而该标的缺席 → 停牌残留 reason"


def test_repair_skips_marked_unfillable(tmp_path, monkeypatch):
    """sidecar 已标记段：repair 不再发起任何拉取（配额死循环根治）。"""
    from data.tools import repair_gaps as rg

    lake = _mk_lake([("2024-09-02", "000921.SZ"), ("2024-09-06", "000921.SZ")])
    gap = GapRange("000921.SZ", "2024-09-03", "2024-09-05",
                   ("2024-09-03", "2024-09-04", "2024-09-05"), suspend_justified=False)
    # 预写 sidecar：该段已标记不可补
    (tmp_path / ".repair_unfillable.json").write_text(json.dumps(
        {"entries": [{"symbol": "000921.SZ", "start": "2024-09-03",
                      "end": "2024-09-05", "reason": "market_empty", "count": 3}]},
        ensure_ascii=False), encoding="utf-8")

    calls = []

    def _fail_fetch(*a, **k):
        calls.append(a)  # 若被调用即失败（标记段不应再拉）
        raise AssertionError("已标记 unfillable 的段不应再发起拉取")

    monkeypatch.setattr(rg, "_fetch_paged", _fail_fetch)
    new_lake = rg.repair_gaps([gap], lake, object(), lake_dir=str(tmp_path))
    assert calls == [], "标记段零拉取"
    assert len(new_lake) == len(lake), "湖不动"


def test_repair_fetches_only_unjustified_days(tmp_path, monkeypatch):
    """日级补采：混合段（3 缺日中 2 日有 S）→ 只拉真缺的 1 日（真缺一日补一日）。

    旧实现拉整段 missing_dates——被 suspend_d 解释的日拉了恒空，白耗配额与限频
    预算（REPAIR_DAY_SLEEP 按日摊开，每省一日省 1.5s+2 次请求）。
    """
    from data.tools import repair_gaps as rg

    lake = _mk_lake([("2024-09-02", "000670.SZ"), ("2024-09-06", "000670.SZ")])
    # 混合段：09-03/09-04 已由 suspend_d 解释，仅 09-05 真缺
    gap = GapRange("000670.SZ", "2024-09-03", "2024-09-05",
                   ("2024-09-03", "2024-09-04", "2024-09-05"), suspend_justified=False,
                   unjustified_days=("2024-09-05",))
    fetched = []

    def _fake(pro, api, tdc):
        fetched.append(tdc)
        if api == "daily":
            return pd.DataFrame([{"ts_code": "000670.SZ", "trade_date": tdc,
                                  "open": 10.0, "high": 11.0, "low": 9.0,
                                  "close": 10.5, "vol": 1000}])
        return pd.DataFrame([{"ts_code": "000670.SZ", "trade_date": tdc, "adj_factor": 1.0}])

    monkeypatch.setattr(rg, "_fetch_paged", _fake)
    new_lake = rg.repair_gaps([gap], lake, object(), lake_dir=str(tmp_path))

    # 每日 daily + adj_factor 成对拉取 → tdc 恰好出现 2 次且只有真缺日
    assert fetched == ["20240905", "20240905"], "只拉真缺日 09-05（daily+adj 一对）"
    dates = new_lake.xs("000670.SZ", level="symbol").index.strftime("%Y-%m-%d").tolist()
    assert "2024-09-05" in dates, "真缺日被补上"


def test_repair_recency_first_quota(monkeypatch):
    """配额 recency-first：两段 unjustified（旧段在前）max_segments=1 → 新段优先。

    旧 [:50] 取 symbol 序——2000-2004 盲区段（最旧）永占配额，真缺的新段
    （影响实盘识别的近期数据）饿死。盲区=最旧，recency-first 天然跳过，
    不硬编码年代界线。
    """
    from data.tools import repair_gaps as rg

    old_gap = GapRange("000921.SZ", "2001-06-18", "2001-06-18",
                       ("2001-06-18",), suspend_justified=False)
    new_gap = GapRange("300214.SZ", "2026-07-14", "2026-07-21",
                       ("2026-07-14", "2026-07-21"), suspend_justified=False)
    fetched = []

    def _fake(pro, api, tdc):
        fetched.append(tdc)
        return pd.DataFrame()

    monkeypatch.setattr(rg, "_fetch_paged", _fake)
    monkeypatch.setattr(rg, "REPAIR_DAY_SLEEP", 0.0)
    lake_df = _mk_lake([("2024-09-02", "000001.SZ")])
    rg.repair_gaps([old_gap, new_gap], lake_df, object(),
                   max_segments=1, lake_dir=None)
    # 只拉 2026 新段（recency-first）；2001 盲区段被配额淘汰
    assert set(fetched) == {"20260714", "20260721"}, "只有 2026 新段被拉（recency-first）"


def test_repair_main_clear_unfillable(tmp_path, capsys):
    """--clear-unfillable 入口：清除 sidecar（防误标永久化的逃生口）。"""
    from data.tools import repair_gaps as rg

    sidecar = tmp_path / ".repair_unfillable.json"
    sidecar.write_text('{"entries": []}', encoding="utf-8")
    rc = rg.main(["--clear-unfillable", "--lake-dir", str(tmp_path)])
    assert rc == 0
    assert not sidecar.exists(), "sidecar 应被删除"
    out = capsys.readouterr().out
    assert "清除" in out or "clear" in out.lower()


# ============================================================================
# N1b 探针分类（2026-08-16）：--classify 中点采样标记（可逆、不写湖）
# ============================================================================
# 物理意图：T1 全量 --auto 严格标记需 13.8h（每 unjustified 子段全范围拉取），
# 13,767 子段收敛不动 → daemon fail-closed 闸（unjustified>0 拒跑）打不开。
# 探针降本：每子段只拉「中点 1 个交易日」（daily+adj 两接口），两接口完整拉取且
# 零行 → sidecar 记 reason=probe_zero_day + count=1（采样推定，与 symbol_absent=
# 全段实证不同级）；非零 → 不标（留给正规 --auto 补采）。--clear-unfillable 可逆。


def test_classify_probes_only_midpoint(tmp_path, monkeypatch):
    """① classify 只拉中点：5 日子段仅中点日被拉一对（daily+adj），其余日零拉取。

    旧思路（--auto 全段拉取）对 1.4 万子段需 13.8h；探针每段 1 日把收敛预算
    从「段长求和」降到「段子数」——本测锁定「只拉中点」这一降本契约。
    """
    from data.tools import repair_gaps as rg

    # N5 · D④ 守卫后语义：市场级全零的标记只在盲区年代（<2005）成立——用例迁 2001
    # （2024 现代交易日市场全零会被守卫判源侧故障不标记，另见守卫专测）。
    gap = GapRange("000921.SZ", "2001-06-18", "2001-06-22",
                   ("2001-06-18", "2001-06-19", "2001-06-20", "2001-06-21",
                    "2001-06-22"), suspend_justified=False)
    fetched = []

    def _fake(pro, api, tdc):
        fetched.append((api, tdc))
        return pd.DataFrame()  # 两接口全零行（盲区年代形态）

    monkeypatch.setattr(rg, "_fetch_paged", _fake)
    monkeypatch.setattr(rg, "REPAIR_DAY_SLEEP", 0.0)

    stats = rg.classify_gaps([gap], object(), lake_dir=str(tmp_path))

    # 5 日子段中点 = 06-20（index len//2）：恰好一对拉取，首尾/其余日零调用
    assert fetched == [("daily", "20010620"), ("adj_factor", "20010620")], \
        "只拉中点 1 日 ×（daily+adj）一对，不得全段拉取"
    assert stats["marked"] == 1


def test_classify_zero_midpoint_marks_probe_zero_day(tmp_path, monkeypatch):
    """② 中点两接口零行 → sidecar 记 probe_zero_day + count=1，整子段区间入标。

    count=1 是诚实口径：只实证了中点 1 日（区别于 symbol_absent 的 count=段日数
    全段实证）；start/end 仍铺满子段——scan/repair 侧按区间扣减整段子段。
    """
    from data.tools import repair_gaps as rg

    # N5 · D④：市场级全零（盲区形态）标记用例迁 2001——现代交易日全零走守卫专测。
    gap = GapRange("000921.SZ", "2001-06-18", "2001-06-20",
                   ("2001-06-18", "2001-06-19", "2001-06-20"), suspend_justified=False)
    monkeypatch.setattr(rg, "_fetch_paged", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(rg, "REPAIR_DAY_SLEEP", 0.0)

    stats = rg.classify_gaps([gap], object(), lake_dir=str(tmp_path))

    side = json.loads((tmp_path / ".repair_unfillable.json").read_text(encoding="utf-8"))
    e = side["entries"][0]
    assert (e["symbol"], e["start"], e["end"]) == ("000921.SZ", "2001-06-18", "2001-06-20")
    assert e["reason"] == "probe_zero_day", "采样推定 reason（与实证 reason 分级）"
    assert e["count"] == 1, "count=1：只实证中点 1 日"
    assert stats["marked"] == 1 and stats["nonzero"] == 0


def test_classify_nonzero_midpoint_skips_marking(tmp_path, monkeypatch):
    """③ 中点【该标的】有行 → 不标（源有该标的行情，留给正规 --auto 补采）。

    探针只做「标的缺席推定」单向分类：中点有行即放弃标记权，防单日采样误杀
    可补段（非零跳过轮零副作用——不创建/不重写 sidecar 文件）。
    """
    from data.tools import repair_gaps as rg

    gap = GapRange("000921.SZ", "2024-09-03", "2024-09-05",
                   ("2024-09-03", "2024-09-04", "2024-09-05"), suspend_justified=False)

    def _fake(pro, api, tdc):
        # 中点 09-04：市场健康且 000921.SZ 自身有行（行情可补——正是 --auto 的活）
        return pd.DataFrame([{"ts_code": "000921.SZ", "trade_date": tdc,
                              "open": 1.0, "high": 1.0, "low": 1.0,
                              "close": 1.0, "vol": 100, "adj_factor": 1.0}])

    monkeypatch.setattr(rg, "_fetch_paged", _fake)
    monkeypatch.setattr(rg, "REPAIR_DAY_SLEEP", 0.0)

    stats = rg.classify_gaps([gap], object(), lake_dir=str(tmp_path))

    assert stats["marked"] == 0 and stats["nonzero"] == 1
    assert not (tmp_path / ".repair_unfillable.json").exists(), \
        "非零中点不落任何 sidecar 条目（留给 --auto）"


def test_classify_marks_symbol_absent_midpoint(tmp_path, monkeypatch):
    """标的级零行判定：中点市场健康而【该标的】daily 缺席 → probe_zero_day。

    Why symbol 级而非市场级：真缺段大头是停牌残留形态（suspend_d 漏记）——市场
    该日数千行、唯独该标的无行；只看市场级零行会把这批大头漏标，探针收敛目标
    （unjustified 骤降）落空。此即 symbol_absent 全段实证的「中点采样版」。

    Why 只认 daily：实证（2026-08-16，688646.SH 等 --auto 铁证 symbol_absent 三例）
    adj_factor 对停牌股连续发布——该标的 adj 在场而 daily 缺席时若按「任一接口
    在场即非零」处理，停牌残留全部漏标（v2 实弹 8108/8108 全非零的根因）。
    """
    from data.tools import repair_gaps as rg

    gap = GapRange("000921.SZ", "2024-09-03", "2024-09-05",
                   ("2024-09-03", "2024-09-04", "2024-09-05"), suspend_justified=False)

    def _fake(pro, api, tdc):
        # 市场健康（600000.SH 在场）；000921.SZ daily 缺席但 adj_factor 在场
        # （停牌日因子连续发布的真实形态）
        if api == "daily":
            return pd.DataFrame([{"ts_code": "600000.SH", "trade_date": tdc,
                                  "open": 1.0, "high": 1.0, "low": 1.0,
                                  "close": 1.0, "vol": 100}])
        return pd.DataFrame([{"ts_code": "600000.SH", "trade_date": tdc,
                              "adj_factor": 1.0},
                             {"ts_code": "000921.SZ", "trade_date": tdc,
                              "adj_factor": 1.0}])

    monkeypatch.setattr(rg, "_fetch_paged", _fake)
    monkeypatch.setattr(rg, "REPAIR_DAY_SLEEP", 0.0)

    stats = rg.classify_gaps([gap], object(), lake_dir=str(tmp_path))

    side = json.loads((tmp_path / ".repair_unfillable.json").read_text(encoding="utf-8"))
    e = side["entries"][0]
    assert e["symbol"] == "000921.SZ" and e["reason"] == "probe_zero_day"
    assert stats["marked"] == 1 and stats["nonzero"] == 0


def test_classify_shares_fetch_for_same_midpoint_day(tmp_path, monkeypatch):
    """同中点日只探一次：零行/非零是市场级属性（按 trade_date 拉全市场），

    不同标的的子段共享同一中点交易日时重复拉取是白耗配额——按日缓存一天一探。
    1.4 万子段 → 唯一中点日远少于子段数，实弹耗时按「日数」而非「段数」计。
    """
    from data.tools import repair_gaps as rg

    # N5 · D④：全零市场响应的标记用例迁 2001 盲区年代（现代交易日全零=守卫故障态）。
    g1 = GapRange("000921.SZ", "2001-06-18", "2001-06-22",
                  ("2001-06-18", "2001-06-19", "2001-06-20", "2001-06-21",
                   "2001-06-22"), suspend_justified=False)
    g2 = GapRange("000670.SZ", "2001-06-18", "2001-06-22",
                  ("2001-06-18", "2001-06-19", "2001-06-20", "2001-06-21",
                   "2001-06-22"), suspend_justified=False)
    fetched = []

    def _fake(pro, api, tdc):
        fetched.append((api, tdc))
        return pd.DataFrame()

    monkeypatch.setattr(rg, "_fetch_paged", _fake)
    monkeypatch.setattr(rg, "REPAIR_DAY_SLEEP", 0.0)

    stats = rg.classify_gaps([g1, g2], object(), lake_dir=str(tmp_path))

    assert fetched == [("daily", "20010620"), ("adj_factor", "20010620")], \
        "两子段共享中点日 06-20 → 只拉一对"
    assert stats["marked"] == 2, "两个子段各自入标（共享一次探针实证）"
    assert stats["probed_days"] == 1


def test_classify_skips_already_marked_and_resumes(tmp_path, monkeypatch):
    """sidecar 已标记段不再探（实证与推定条目一视同仁）——中断重跑续收的机制基础。"""
    from data.tools import repair_gaps as rg

    gap = GapRange("000921.SZ", "2024-09-03", "2024-09-05",
                   ("2024-09-03", "2024-09-04", "2024-09-05"), suspend_justified=False)
    # 预写 sidecar：该段已被 --auto 实证标记（market_empty）
    (tmp_path / ".repair_unfillable.json").write_text(json.dumps(
        {"entries": [{"symbol": "000921.SZ", "start": "2024-09-03",
                      "end": "2024-09-05", "reason": "market_empty", "count": 3}]},
        ensure_ascii=False), encoding="utf-8")

    calls = []

    def _fail_fetch(*a, **k):
        calls.append(a)
        raise AssertionError("已标记 unfillable 的段不应再发起探针拉取")

    monkeypatch.setattr(rg, "_fetch_paged", _fail_fetch)
    monkeypatch.setattr(rg, "REPAIR_DAY_SLEEP", 0.0)

    stats = rg.classify_gaps([gap], object(), lake_dir=str(tmp_path))

    assert calls == [], "已标记段零探针"
    assert stats["units"] == 0, "过滤后无可探子段"
    assert stats["marked"] == 0


def test_classify_cli_wiring_and_mutex(tmp_path, monkeypatch, capsys):
    """--classify CLI 接线：内部 scan → classify_gaps → stdout 摘要；与 --auto 互斥。

    main 分支在实弹前必须被mock 验证过——探针烧的是真实 Tushare 配额，接线 bug
    （漏传 lake_dir/摘要误读 stats）不该在 1 万段量级的实弹里才暴露。
    """
    from data.tools import repair_gaps as rg

    # mock 内部 scan（局部 import，patch 源模块属性；同 --auto 测试手法）
    monkeypatch.setattr(
        "data.tools.scan_integrity.scan",
        lambda *a, **k: {"gaps": [{"symbol": "000921.SZ", "start": "2024-09-03",
                                   "end": "2024-09-05",
                                   "missing_dates": ["2024-09-03", "2024-09-04",
                                                     "2024-09-05"],
                                   "suspend_justified": False}]},
    )
    monkeypatch.setattr("data._tushare_compat.get_pro", lambda: object())
    stats_calls = []

    def _fake_classify(gaps, pro, *, lake_dir):
        stats_calls.append(lake_dir)
        return {"units": 1, "marked": 1, "nonzero": 0, "probed_days": 1,
                "interrupted": False, "sidecar_entries": 1}

    monkeypatch.setattr(rg, "classify_gaps", _fake_classify)

    # 互斥红线：--classify 与 --auto/--report 组合直接报错退出
    for bad in (["--classify", "--auto"], ["--classify", "--report", "x.json"]):
        with pytest.raises(SystemExit):
            rg.main(bad + ["--lake-dir", str(tmp_path)])

    rc = rg.main(["--classify", "--lake-dir", str(tmp_path)])
    assert rc == 0
    assert stats_calls == [str(tmp_path)], "lake_dir 必须透传（sidecar 锚点）"
    out = capsys.readouterr().out
    assert "probe_zero_day" in out and "标记 1" in out, "stdout 摘要必须带标记数"


# ============================================================================
# N5 · D 批加固（2026-08-16）：sidecar 原子写 + 并发合并 + reason 定向清除
# + classify 中断非零退出 + 现代交易日市场全零守卫
# ============================================================================
# 物理意图：T1b 实弹后暴露的四个加固点——① classify 长跑（1h）期间 repair --auto
# 并发写 sidecar，直接覆盖写会抹掉并发方铁证条目（并发窗口）；② probe_zero_day
# 是采样推定，误标时需要「只清推定不动铁证」的定向逃生口；③ classify 中断返 0
# 会让 schtasks「上次运行结果」显示成功（半途被当完成）；④ 现代交易日（≥2005）
# 全市场零行只可能是源侧故障，无守卫会把当日全部待探标的批量误标（标记后双侧
# 跳过，误标段从此不再补采）。


def test_classify_final_save_merges_concurrent_entries(tmp_path, monkeypatch):
    """N5 D①：classify 落盘 re-load 合并——长跑期间并发落的铁证条目不被覆盖抹掉。

    场景：classify 探 000921.SZ 盲区中点（2001）期间，repair --auto 并发往 sidecar
    落了两条铁证（含同段不同 reason——验证四元组键的分级共存语义）。旧实现把启动
    时 load 的内存快照直接覆盖写回，并发条目静默消失（下轮 scan 重新进队烧配额）。
    """
    from data.tools import repair_gaps as rg

    gap = GapRange("000921.SZ", "2001-06-18", "2001-06-20",
                   ("2001-06-18", "2001-06-19", "2001-06-20"), suspend_justified=False)
    # 并发方（repair --auto）落的铁证：另一标的全段实证 + 同段不同 reason（分级共存）
    concurrent = [
        {"symbol": "600000.SH", "start": "2024-09-04", "end": "2024-09-05",
         "reason": "symbol_absent", "count": 2, "marked_at": "2026-08-16T00:00:00"},
        {"symbol": "000921.SZ", "start": "2001-06-18", "end": "2001-06-20",
         "reason": "market_empty", "count": 3, "marked_at": "2026-08-16T00:00:00"},
    ]

    def _fake(pro, api, tdc):
        # 模拟并发窗口：classify 拉取期间（cov 过滤之后）另一进程写 sidecar
        rg.save_unfillable_entries(str(tmp_path), list(concurrent))
        return pd.DataFrame()  # 盲区年代（2001）中点全零 → 正常标记 probe_zero_day

    monkeypatch.setattr(rg, "_fetch_paged", _fake)
    monkeypatch.setattr(rg, "REPAIR_DAY_SLEEP", 0.0)

    stats = rg.classify_gaps([gap], object(), lake_dir=str(tmp_path))
    assert stats["marked"] == 1

    side = json.loads((tmp_path / ".repair_unfillable.json").read_text(encoding="utf-8"))
    keys = {(e["symbol"], e["reason"]) for e in side["entries"]}
    # 并发铁证必须存活（覆盖写会抹掉）
    assert ("600000.SH", "symbol_absent") in keys, "并发铁证条目不得被 classify 内存快照覆盖"
    # 同段不同 reason 分级共存（四元组键含 reason，不互相顶掉）
    assert ("000921.SZ", "market_empty") in keys and ("000921.SZ", "probe_zero_day") in keys, \
        "同段实证/推定两级标记共存（键含 reason）"


def test_clear_unfillable_reason_scoped(tmp_path):
    """N5 D②：clear_unfillable(reason=) 定向清除——只删匹配 reason，铁证不动。

    三个分支：有匹配→重写保留余量；无匹配→零副作用（文件不动）；清完→删文件
    （不留空壳——空 sidecar 与无 sidecar 语义等同）。
    """
    from data.tools import repair_gaps as rg

    side = tmp_path / ".repair_unfillable.json"
    side.write_text(json.dumps({"entries": [
        {"symbol": "A.SZ", "start": "2001-06-18", "end": "2001-06-20",
         "reason": "probe_zero_day", "count": 1, "marked_at": "t1"},
        {"symbol": "B.SZ", "start": "2024-09-04", "end": "2024-09-05",
         "reason": "symbol_absent", "count": 2, "marked_at": "t2"},
        {"symbol": "C.SZ", "start": "2000-01-04", "end": "2000-01-05",
         "reason": "market_empty", "count": 2, "marked_at": "t3"},
    ]}, ensure_ascii=False), encoding="utf-8")

    # ① 只清 probe_zero_day：推定标记删，两条铁证原样保留
    assert rg.clear_unfillable(str(tmp_path), reason="probe_zero_day") is True
    left = json.loads(side.read_text(encoding="utf-8"))["entries"]
    assert {e["symbol"] for e in left} == {"B.SZ", "C.SZ"}, "铁证标记不得连坐清除"

    # ② 无匹配 reason：零副作用（不重写文件，marked_at 时间戳不动）
    assert rg.clear_unfillable(str(tmp_path), reason="nonexistent") is False
    assert len(json.loads(side.read_text(encoding="utf-8"))["entries"]) == 2

    # ③ 逐 reason 清空 → 文件删除（不留空壳）
    assert rg.clear_unfillable(str(tmp_path), reason="symbol_absent") is True
    assert rg.clear_unfillable(str(tmp_path), reason="market_empty") is True
    assert not side.exists()


def test_repair_main_clear_unfillable_reason(tmp_path, capsys):
    """N5 D② CLI：--clear-unfillable --reason probe_zero_day → 推定清、铁证留。"""
    from data.tools import repair_gaps as rg

    side = tmp_path / ".repair_unfillable.json"
    side.write_text(json.dumps({"entries": [
        {"symbol": "A.SZ", "start": "2001-06-18", "end": "2001-06-20",
         "reason": "probe_zero_day", "count": 1, "marked_at": "t1"},
        {"symbol": "A.SZ", "start": "2001-06-18", "end": "2001-06-20",
         "reason": "probe_zero_day", "count": 1, "marked_at": "t1b"},
        {"symbol": "B.SZ", "start": "2024-09-04", "end": "2024-09-05",
         "reason": "symbol_absent", "count": 2, "marked_at": "t2"},
    ]}, ensure_ascii=False), encoding="utf-8")

    rc = rg.main(["--clear-unfillable", "--reason", "probe_zero_day",
                  "--lake-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "probe_zero_day" in out and "保留 1" in out, "stdout 摘要带清除数与保留数"
    left = json.loads(side.read_text(encoding="utf-8"))["entries"]
    assert len(left) == 1 and left[0]["reason"] == "symbol_absent", "铁证留、推定清"

    # --reason 单独出现（无 --clear-unfillable）→ fail-fast（防「以为清了实则跑补采」）
    with pytest.raises(SystemExit):
        rg.main(["--reason", "probe_zero_day", "--lake-dir", str(tmp_path)])


def test_classify_cli_interrupted_returns_nonzero(tmp_path, monkeypatch, capsys):
    """N5 D③：classify 中断（已 checkpoint）→ main 返非零——半途≠完成。

    中断源四类：限频/异常/超时/市场全零守卫。返 0 会让 schtasks「上次运行结果」
    显示成功，半途而废的收敛被误当完成（daemon fail-closed 闸打不开的排查被掩盖）。
    """
    from data.tools import repair_gaps as rg

    monkeypatch.setattr("data.tools.scan_integrity.scan", lambda *a, **k: {"gaps": []})
    monkeypatch.setattr("data._tushare_compat.get_pro", lambda: object())
    monkeypatch.setattr(rg, "classify_gaps", lambda gaps, pro, *, lake_dir: {
        "units": 0, "marked": 0, "nonzero": 0, "probed_days": 0,
        "interrupted": True, "sidecar_entries": 0})
    rc = rg.main(["--classify", "--lake-dir", str(tmp_path)])
    assert rc == 1, "中断必须非零退出（重跑续收，已收标记经 checkpoint 不丢）"
    assert "中断" in capsys.readouterr().out


def test_classify_modern_day_market_all_zero_is_fault(tmp_path, monkeypatch, caplog):
    """N5 D④：现代交易日（≥2005）中点市场级全零 → 源侧故障判定：零标记 + warning + 中断。

    物理意图：2005 起全市场应数千行——该年代全零只能是 Tushare 单日故障性返空，
    绝非全部标的集体缺席。无守卫会把当日组内全部待探标的批量误标 probe_zero_day
    （sidecar 标记后 scan/repair 双侧跳过，误标段从此不再补采）。
    """
    import logging

    from data.tools import repair_gaps as rg

    gap = GapRange("000921.SZ", "2024-09-03", "2024-09-05",
                   ("2024-09-03", "2024-09-04", "2024-09-05"), suspend_justified=False)

    def _fake(pro, api, tdc):
        return pd.DataFrame()  # 两接口全零（故障性返空形态）

    monkeypatch.setattr(rg, "_fetch_paged", _fake)
    monkeypatch.setattr(rg, "REPAIR_DAY_SLEEP", 0.0)

    with caplog.at_level(logging.WARNING):
        stats = rg.classify_gaps([gap], object(), lake_dir=str(tmp_path))

    assert stats["marked"] == 0, "故障日不得批量误标"
    assert stats["interrupted"] is True, "守卫按中断处理（main 转非零退出供人盯）"
    assert stats["probed_days"] == 0, "故障日不计有效探针"
    assert not (tmp_path / ".repair_unfillable.json").exists(), "零标记不落 sidecar"
    assert "市场级全零" in caplog.text, "warning 必须留痕（人工排查锚点）"
