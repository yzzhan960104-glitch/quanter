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
    # mock repair_gaps 返回异常收缩的湖（模拟 dedup/recompute bug 致 5000→100）
    monkeypatch.setattr(rg, "repair_gaps", lambda gaps, lake_df, pro: _big_lake(100))

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
