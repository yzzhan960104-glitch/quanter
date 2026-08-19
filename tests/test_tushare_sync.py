# -*- coding: utf-8 -*-
"""通用 Tushare 湖同步器测试：配置驱动 + 分页 + 断点续传 + 落湖。

设计意图（反黑盒测试）：
- fake_pro 替身 mock 掉 get_pro / tushare_rate_limiter / tushare_breaker，使测试
  **完全不依赖真 Tushare token / 网络环境**（开发机可能未配 TUSHARE_TOKEN），
  仅验证同步器的分页/断点/落湖逻辑正确性。
- 通过 TUSHARE_DATASETS[key] 临时覆盖落湖路径到 tmp_path，保证测试隔离无副作用。
"""
import os
import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def _isolate_tushare_registry(tushare_registry_isolated):
    """薄壳（原 ~40 行块收口 tests/conftest.py::tushare_registry_isolated，2026-08-19 W2）。"""

@pytest.fixture
def fake_pro(monkeypatch):
    """薄壳（原 ~60 行块收口 tests/_tushare_stub.py，2026-08-19 W2）。"""
    from tests._tushare_stub import FakePro, install_fake_pro
    fake = FakePro(data={"income": pd.DataFrame({
                "ts_code": ["000001.SZ"] * 3,
                "ann_date": ["20240101", "20240401", "20240701"],
                "end_date": ["20231231", "20240331", "20240630"],
                "total_revenue": [1e9, 1.1e9, 1.2e9],
                "n_income": [1e8, 1.1e8, 1.2e8],
            })})
    install_fake_pro(monkeypatch, fake)
    return fake


def test_sync_dataset_by_symbol_multiindex(tmp_path, fake_pro, monkeypatch):
    """by=symbol 分页：逐标的拉取 → MultiIndex(date,symbol) 落湖。"""
    from config import TUSHARE_DATASETS, LAKE_CONFIG
    # 注册一个测试数据集
    TUSHARE_DATASETS["fina_income"] = {
        "api": "income", "by": "symbol",  # 按标的分页
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,total_revenue,n_income",
        "lake": str(tmp_path / "income.parquet"),
        "shard_dir": str(tmp_path / "shards"),  # 隔离 shard_dir（避免读到全局 data_lake/shards 的真实 shard）
    }
    LAKE_CONFIG["lakes"]["fina_income"] = TUSHARE_DATASETS["fina_income"]["lake"]
    from data.tushare_sync import sync_dataset
    sync_dataset("fina_income", "2024-01-01", "2024-12-31",
                 symbols=["000001.SZ"], resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["fina_income"]["lake"])
    assert df.index.names == ["date", "symbol"]
    assert "total_revenue" in df.columns
    assert len(df) == 3


def test_sync_dataset_resume_skips_up_to_date_shard(tmp_path, fake_pro, monkeypatch):
    """断点续传：shard 已最新（max >= end）→ 跳过，不浪费配额。"""
    from config import TUSHARE_DATASETS, LAKE_CONFIG
    shard_dir = str(tmp_path / "shards")
    TUSHARE_DATASETS["fina_income"] = {
        "api": "income", "by": "symbol",
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,total_revenue",
        "lake": str(tmp_path / "income.parquet"),
        "shard_dir": shard_dir,
    }
    os.makedirs(shard_dir)
    # 预置 shard：已覆盖到 2024-12-31（== end）→ 应跳过
    pd.DataFrame({"total_revenue": [1e9]},
                 index=pd.DatetimeIndex(["2024-12-31"], name="ann_date")
                 ).to_parquet(os.path.join(shard_dir, "000001.SZ.parquet"))
    from data.tushare_sync import sync_dataset
    sync_dataset("fina_income", "2024-01-01", "2024-12-31",
                 symbols=["000001.SZ"], resume=True)
    # fake_pro 未被调（shard 已最新，跳过）
    assert fake_pro.calls == []


def test_sync_dataset_resume_incremental_gap_fetch_merges(tmp_path, fake_pro):
    """增量续传：shard 落后 → 只拉缺口（start=shard_max+1）→ 合并去重落 shard/湖。"""
    from config import TUSHARE_DATASETS, LAKE_CONFIG
    shard_dir = str(tmp_path / "shards")
    TUSHARE_DATASETS["fina_income"] = {
        "api": "income", "by": "symbol",
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,total_revenue",
        "lake": str(tmp_path / "income.parquet"),
        "shard_dir": shard_dir,
    }
    os.makedirs(shard_dir)
    # 旧 shard：只到 2024-01-01
    pd.DataFrame({"total_revenue": [1e9]},
                 index=pd.DatetimeIndex(["2024-01-01"], name="ann_date")
                 ).to_parquet(os.path.join(shard_dir, "000001.SZ.parquet"))
    # 新拉数据：2024-03-01（缺口内）
    fake_pro._data["income"] = pd.DataFrame({
        "ts_code": ["000001.SZ"],
        "ann_date": ["20240301"],
        "end_date": ["20240331"],
        "total_revenue": [2e9],
    })
    from data.tushare_sync import sync_dataset
    sync_dataset("fina_income", "2024-01-01", "2024-12-31",
                 symbols=["000001.SZ"], resume=True)
    # API 只拉缺口：start_date = shard 最新日次日（20240102），不是全窗口起点
    income_calls = [kw for api, kw in fake_pro.calls if api == "income"]
    assert len(income_calls) == 1
    assert income_calls[0]["start_date"] == "20240102"
    assert income_calls[0]["end_date"] == "20241231"
    # shard 合并：旧 1 行 + 新 1 行
    shard_df = pd.read_parquet(os.path.join(shard_dir, "000001.SZ.parquet"))
    assert len(shard_df) == 2
    assert sorted(shard_df.index.astype(str)) == ["2024-01-01", "2024-03-01"]
    # 湖同样合并（旧数据不丢）
    lake_df = pd.read_parquet(TUSHARE_DATASETS["fina_income"]["lake"])
    assert len(lake_df) == 2


def test_sync_dataset_resume_empty_fetch_keeps_shard(tmp_path, fake_pro):
    """增量续传拉空（节假日/接口故障）→ 旧 shard 完整保留（关键防线）。"""
    from config import TUSHARE_DATASETS, LAKE_CONFIG
    shard_dir = str(tmp_path / "shards")
    TUSHARE_DATASETS["fina_income"] = {
        "api": "income", "by": "symbol",
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,total_revenue",
        "lake": str(tmp_path / "income.parquet"),
        "shard_dir": shard_dir,
    }
    os.makedirs(shard_dir)
    old = pd.DataFrame({"total_revenue": [1e9]},
                       index=pd.DatetimeIndex(["2024-01-01"], name="ann_date"))
    old.to_parquet(os.path.join(shard_dir, "000001.SZ.parquet"))
    # 接口返空（列齐全但零行）
    fake_pro._data["income"] = pd.DataFrame(
        columns=["ts_code", "ann_date", "end_date", "total_revenue"])
    from data.tushare_sync import sync_dataset
    sync_dataset("fina_income", "2024-01-01", "2024-12-31",
                 symbols=["000001.SZ"], resume=True)
    shard_df = pd.read_parquet(os.path.join(shard_dir, "000001.SZ.parquet"))
    assert len(shard_df) == 1
    assert shard_df["total_revenue"].iloc[0] == 1e9


def test_sync_dataset_resume_adj_refetches_full_range(tmp_path, fake_pro):
    """复权数据集（adj_api）增量：落后时重拉 shard 起始日..end，重建前复权基线。"""
    from config import TUSHARE_DATASETS, LAKE_CONFIG
    shard_dir = str(tmp_path / "shards")
    TUSHARE_DATASETS["daily_test"] = {
        "api": "daily", "by": "symbol", "adj_api": "adj_factor",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,open,high,low,close",
        "lake": str(tmp_path / "daily.parquet"),
        "shard_dir": shard_dir,
    }
    os.makedirs(shard_dir)
    # 旧 shard：只有 2023-12-29（旧基线 close=10.0）
    pd.DataFrame({"ts_code": ["000001.SZ"], "open": [9.8], "high": [10.2],
                  "low": [9.7], "close": [10.0]},
                 index=pd.DatetimeIndex(["2023-12-29"], name="trade_date")
                 ).to_parquet(os.path.join(shard_dir, "000001.SZ.parquet"))
    # 原始行情 + 复权因子（12-29=0.9 → 01-03=1.0，最新日基准）
    fake_pro._data["daily"] = pd.DataFrame({
        "ts_code": ["000001.SZ"] * 3,
        "trade_date": ["20231229", "20240102", "20240103"],
        "open": [9.8, 10.5, 11.0],
        "high": [10.2, 10.8, 11.3],
        "low": [9.7, 10.2, 10.8],
        "close": [10.0, 10.6, 11.2],
    })
    fake_pro._data["adj_factor"] = pd.DataFrame({
        "ts_code": ["000001.SZ"] * 3,
        "trade_date": ["20231229", "20240102", "20240103"],
        "adj_factor": [0.9, 0.95, 1.0],
    })
    from data.tushare_sync import sync_dataset
    sync_dataset("daily_test", "2024-01-01", "2024-01-03",
                 symbols=["000001.SZ"], resume=True)
    # 复权数据集：start = shard 起始日（20231229），而非缺口次日（20231230）
    daily_calls = [kw for api, kw in fake_pro.calls if api == "daily"]
    adj_calls = [kw for api, kw in fake_pro.calls if api == "adj_factor"]
    assert len(daily_calls) == 1 and len(adj_calls) == 1
    assert daily_calls[0]["start_date"] == "20231229"
    assert daily_calls[0]["end_date"] == "20240103"
    assert adj_calls[0]["start_date"] == "20231229"
    # 3 行全部保留，且旧行被新基线重建
    shard_df = pd.read_parquet(os.path.join(shard_dir, "000001.SZ.parquet"))
    assert len(shard_df) == 3
    closes = shard_df.sort_index()["close"].astype(float)
    assert closes.iloc[-1] == pytest.approx(11.2)  # 最新日 adj/latest=1 → 原始价
    assert closes.iloc[0] == pytest.approx(9.0)    # 12-29: 10.0 * 0.9 / 1.0


def test_build_multiindex_by_date_symbol_from_column(tmp_path):
    """by=date 模式：symbol 必须来自 shard 内 ts_code 列，而非交易日文件名。

    Why 此测试：by=date shard 是「单日全市场」（文件名=交易日如 20240105.parquet，
    shard 内含多标的的 ts_code 列）。早期实现一律从文件名取 symbol，导致每行被标
    symbol='20240105'，symbol 级全错。本测试直接构造单日全市场 shard，验证合并后
    MultiIndex 的 symbol 来自真实标的码（000001.SZ / 600000.SH），不是交易日串。
    """
    from data.tushare_sync import _build_multiindex
    shard_dir = tmp_path / "shards"
    shard_dir.mkdir()
    # 构造单日全市场 shard：DatetimeIndex(ann_date) + ts_code 列 + 数据列
    # 两个真实标的，同一天 ann_date=2024-01-05
    shard_df = pd.DataFrame({
        "ts_code": ["000001.SZ", "600000.SH"],
        "end_date": ["20231231", "20231231"],
        "total_revenue": [1e9, 2e9],
    }, index=pd.DatetimeIndex(["2024-01-05", "2024-01-05"], name="ann_date"))
    # 文件名是交易日（by=date 模式的 shard 命名约定）
    shard_df.to_parquet(shard_dir / "20240105.parquet")

    out = str(tmp_path / "moneyflow.parquet")
    _build_multiindex(str(shard_dir), date_col="ann_date",
                      symbol_col="ts_code", out=out, by="date")

    df = pd.read_parquet(out)
    # MultiIndex 名
    assert df.index.names == ["date", "symbol"]
    # symbol 必须是真实标的码，不是交易日 '20240105'（防退化核心断言）
    symbols = set(df.index.get_level_values("symbol"))
    assert symbols == {"000001.SZ", "600000.SH"}, (
        f"by=date symbol 应来自 ts_code 列，实际：{symbols}")
    # 行数 + 数据列保持
    assert len(df) == 2
    assert "total_revenue" in df.columns


def test_sync_by_date_skips_only_valid_shard(tmp_path, fake_pro):
    """by=date：空 shard（0 行）不得永久跳过——视为缺失重拉。

    Why 此测试：_sync_by_date 在 resume=True 时原实现只判 os.path.exists(shard) 即
    continue，不校验 shard 是否空/损坏。历史中断写坏的空 shard（0 行）会被永久跳过，
    该日数据永远拉不到。本测试预置一个 0 行 shard（模拟历史中断落盘的坏文件），
    断言 resume=True 同步会重拉并落 1 行有效数据，而非继续跳过留下空湖。
    """
    from config import TUSHARE_DATASETS
    shard_dir = tmp_path / "shards"
    shard_dir.mkdir()
    TUSHARE_DATASETS["moneyflow_test"] = {
        "api": "moneyflow", "by": "date",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,buy_lg_amount",
        "lake": str(tmp_path / "moneyflow.parquet"),
        "shard_dir": str(shard_dir),
    }
    # 空 shard（历史中断写坏：列齐全但零行）
    pd.DataFrame(columns=["ts_code", "trade_date", "buy_lg_amount"]) \
        .to_parquet(shard_dir / "20240105.parquet")
    # trade_cal 交易日历（_trade_days 经 fake_pro 拉取，须 mock 2024-01-05 为交易日）
    fake_pro._data["trade_cal"] = pd.DataFrame(
        {"cal_date": ["20240105"], "is_open": ["1"]})
    # fake_pro 替身返该日 1 行有效数据
    fake_pro._data["moneyflow"] = pd.DataFrame({
        "ts_code": ["000001.SZ"], "trade_date": ["20240105"],
        "buy_lg_amount": [1.0],
    })
    from data.tushare_sync import sync_dataset
    sync_dataset("moneyflow_test", "2024-01-05", "2024-01-05", resume=True)
    df = pd.read_parquet(TUSHARE_DATASETS["moneyflow_test"]["lake"])
    assert len(df) == 1  # 空 shard 被重拉（否则湖空 len==0）


def test_sync_by_date_corrupt_shard_refetches(tmp_path, fake_pro):
    """by=date：损坏 shard（非 parquet 字节）重拉不中断循环（final review I-1）。

    Why 此测试：_sync_by_date 在 resume=True 时校验既有 shard，损坏（parquet 解析失败）
    要删盘重拉。原实现把 os.remove 放在 except 块内裸调，Windows 文件占用/权限会从
    except 再抛 → 中断 _sync_by_date 循环 → 后续交易日 shard 不再拉取。本测试预置
    「非 parquet 字节」损坏 shard，断言重拉成功（len==1）而非中断循环留下空湖。

    覆盖：read_parquet 抛 Exception → old=None → 单独 try os.remove（失败容忍）
          → _fetch_with_guard 重拉 → to_parquet 覆盖落盘 → 湖 1 行有效数据。
    """
    from config import TUSHARE_DATASETS
    shard_dir = tmp_path / "shards"
    shard_dir.mkdir()
    TUSHARE_DATASETS["moneyflow_test"] = {
        "api": "moneyflow", "by": "date",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,buy_lg_amount",
        "lake": str(tmp_path / "moneyflow.parquet"),
        "shard_dir": str(shard_dir),
    }
    # 损坏 shard：写入非 parquet 字节（read_parquet 会抛异常）
    (shard_dir / "20240105.parquet").write_bytes(b"not a parquet")
    # trade_cal 交易日历（_trade_days 经 fake_pro 拉取，须 mock 2024-01-05 为交易日）
    fake_pro._data["trade_cal"] = pd.DataFrame(
        {"cal_date": ["20240105"], "is_open": ["1"]})
    # fake_pro 替身返该日 1 行有效数据
    fake_pro._data["moneyflow"] = pd.DataFrame({
        "ts_code": ["000001.SZ"], "trade_date": ["20240105"],
        "buy_lg_amount": [1.0],
    })
    from data.tushare_sync import sync_dataset
    sync_dataset("moneyflow_test", "2024-01-05", "2024-01-05", resume=True)
    df = pd.read_parquet(TUSHARE_DATASETS["moneyflow_test"]["lake"])
    assert len(df) == 1  # 损坏 shard 被重拉（否则中断循环湖空 len==0）


# ============ _fetch_with_guard 限频退避重试测试 ============
# Why 独立测试组：限频退避是全量下载（by=date 全市场逐日）的关键修复，原实现直接
# record_failure 返空导致整数据集卡死。此处逐态覆盖（瞬时态退避成功 / 退避耗尽失败 /
# 持久态不重试 / 熔断 OPEN 冷却重试），mock time.sleep 避免真睡拖慢测试。

class _FlakyPro:
    """可控失败序列的 pro 替身：按预设异常序列抛错，之后返回正常 DataFrame。

    Why 不复用 _FakePro：_FakePro 的 __getattr__ 永远返静态数据，无法模拟「前 N 次
    抛限频异常、第 N+1 次成功」的退避场景。_FlakyPro 按调用序号消费 failures 队列，
    队列空后返回 success_df，精确刻画瞬时态限频的恢复过程。
    """
    def __init__(self, failures: list[Exception], success_df: pd.DataFrame):
        self._failures = list(failures)  # 按序抛出的异常队列
        self._success_df = success_df
        self.call_count = 0  # 总调用次数（含抛异常 + 成功）

    def __getattr__(self, api_name):
        def _call(**kwargs):
            self.call_count += 1
            if self._failures:
                raise self._failures.pop(0)
            return self._success_df
        return _call


def _mock_sleep(monkeypatch):
    """mock time.sleep 为 no-op，记录 sleep 时长序列供断言退避是否指数增长。

    Why 单独 helper：退避重试路径里 time.sleep 会真睡（2/4/8/16/32s 累计 ~62s），
    拖慢测试套件。patch data.tushare_sync.time.sleep 为记录器，既不睡又能断言退避序列。
    """
    sleeps = []
    monkeypatch.setattr("data.tushare_sync.time.sleep", lambda s: sleeps.append(s))
    return sleeps


def test_fetch_guard_transient_retry_then_success(monkeypatch):
    """瞬时态限频 → 指数退避重试 → 恢复成功（验证不直接返空、不 record_failure）。

    场景：pro 前 2 次抛限频异常，第 3 次返正常 df。期望 _fetch_with_guard 退避后重试成功
    返回数据，且重试期间不 record_failure（限频是瞬时态，不应污染熔断计数）。
    """
    from data import tushare_sync
    sleeps = _mock_sleep(monkeypatch)

    # 真实 breaker + rate_limiter：验证 record_failure 未被调用（熔断不跳闸）
    real_breaker_calls = {"fail": 0, "succ": 0}
    monkeypatch.setattr(tushare_sync.tushare_breaker, "allow_request", lambda: True)
    monkeypatch.setattr(tushare_sync.tushare_breaker, "record_success",
                        lambda: real_breaker_calls.__setitem__("succ", real_breaker_calls["succ"] + 1))
    monkeypatch.setattr(tushare_sync.tushare_breaker, "record_failure",
                        lambda: real_breaker_calls.__setitem__("fail", real_breaker_calls["fail"] + 1))
    monkeypatch.setattr(tushare_sync.tushare_rate_limiter, "acquire", lambda n=1.0, timeout=None: None)

    success_df = pd.DataFrame({"ts_code": ["000001.SZ"], "v": [1]})
    flaky = _FlakyPro(
        failures=[Exception("rate limit temporarily busy"),
                  Exception("Sorry, 频率过快")],
        success_df=success_df,
    )
    monkeypatch.setattr(tushare_sync, "get_pro", lambda: flaky)

    df = tushare_sync._fetch_with_guard("moneyflow", trade_date="20240105")
    # 返回成功数据（不是空）
    assert not df.empty
    assert len(df) == 1
    # pro 被调用 3 次（2 次失败 + 1 次成功）
    assert flaky.call_count == 3
    # 退避了 2 次（首次不退避，2 次失败各退避一次），序列 2s, 4s（指数）
    assert sleeps == [2.0, 4.0], f"退避序列应为 [2,4]，实际 {sleeps}"
    # 重试期间不 record_failure；成功后 record_success 一次
    assert real_breaker_calls["fail"] == 0, "瞬时态退避期间不应 record_failure"
    assert real_breaker_calls["succ"] == 1


def test_fetch_guard_transient_exhaust_then_record_failure(monkeypatch):
    """连续瞬时态限频超 max_retries → 最终 record_failure 一次 + 返空。

    场景：pro 连续抛限频异常（> _BACKOFF_MAX_RETRIES 次）。期望退避耗尽后才
    record_failure 一次（而非每次失败都计），并返回空 DF。
    """
    from data import tushare_sync
    sleeps = _mock_sleep(monkeypatch)

    real_breaker_calls = {"fail": 0, "succ": 0}
    monkeypatch.setattr(tushare_sync.tushare_breaker, "allow_request", lambda: True)
    monkeypatch.setattr(tushare_sync.tushare_breaker, "record_success",
                        lambda: real_breaker_calls.__setitem__("succ", real_breaker_calls["succ"] + 1))
    monkeypatch.setattr(tushare_sync.tushare_breaker, "record_failure",
                        lambda: real_breaker_calls.__setitem__("fail", real_breaker_calls["fail"] + 1))
    monkeypatch.setattr(tushare_sync.tushare_rate_limiter, "acquire", lambda n=1.0, timeout=None: None)

    # 抛 max_retries+1 次异常（首次 + max_retries 次退避重试全失败）
    n_fail = tushare_sync._BACKOFF_MAX_RETRIES + 1
    flaky = _FlakyPro(
        failures=[Exception("rate limit 429 too many requests")] * n_fail,
        success_df=pd.DataFrame(),
    )
    monkeypatch.setattr(tushare_sync, "get_pro", lambda: flaky)

    df = tushare_sync._fetch_with_guard("moneyflow", trade_date="20240105")
    # 退避耗尽返空
    assert df.empty
    # 调用 max_retries+1 次（首次 + max_retries 次重试）
    assert flaky.call_count == n_fail
    # 退避 max_retries 次（序列 2,4,8,16,32s）
    assert len(sleeps) == tushare_sync._BACKOFF_MAX_RETRIES, (
        f"退避次数应为 {_BACKOFF_MAX_RETRIES}，实际 {len(sleeps)}")
    assert sleeps == [2.0, 4.0, 8.0, 16.0, 32.0], f"退避序列应指数增长，实际 {sleeps}"
    # 最终只 record_failure 一次（不是每次失败都计）
    assert real_breaker_calls["fail"] == 1, "退避耗尽应只 record_failure 一次"
    assert real_breaker_calls["succ"] == 0


def test_fetch_guard_persistent_no_retry(monkeypatch):
    """持久态（积分/权限）→ 不重试直接返空，不 record_failure。

    场景：pro 抛「积分不足」异常。期望不退避、不重试、不 record_failure，直接返空。
    Why 不 record_failure：积分不足与接口健康无关，计熔断会误 OPEN 拖累其他正常接口。
    """
    from data import tushare_sync
    sleeps = _mock_sleep(monkeypatch)

    real_breaker_calls = {"fail": 0, "succ": 0}
    monkeypatch.setattr(tushare_sync.tushare_breaker, "allow_request", lambda: True)
    monkeypatch.setattr(tushare_sync.tushare_breaker, "record_success",
                        lambda: real_breaker_calls.__setitem__("succ", real_breaker_calls["succ"] + 1))
    monkeypatch.setattr(tushare_sync.tushare_breaker, "record_failure",
                        lambda: real_breaker_calls.__setitem__("fail", real_breaker_calls["fail"] + 1))
    monkeypatch.setattr(tushare_sync.tushare_rate_limiter, "acquire", lambda n=1.0, timeout=None: None)

    flaky = _FlakyPro(
        failures=[Exception("抱歉，您积分不足 permission denied")],
        success_df=pd.DataFrame({"v": [1]}),
    )
    monkeypatch.setattr(tushare_sync, "get_pro", lambda: flaky)

    df = tushare_sync._fetch_with_guard("income", ts_code="000001.SZ")
    assert df.empty
    # 只调用 1 次（不重试）
    assert flaky.call_count == 1
    # 不退避
    assert sleeps == []
    # 不 record_failure（持久态与接口健康无关）
    assert real_breaker_calls["fail"] == 0
    assert real_breaker_calls["succ"] == 0


def test_fetch_guard_unknown_exception_records_failure(monkeypatch):
    """未知异常 → 保守 record_failure 一次 + 返空（宁可误 OPEN 也不漏防线）。

    场景：pro 抛非限频非积分的未知异常（如 JSON 解析错）。期望不重试、直接
    record_failure + 返空。
    """
    from data import tushare_sync
    _mock_sleep(monkeypatch)

    real_breaker_calls = {"fail": 0, "succ": 0}
    monkeypatch.setattr(tushare_sync.tushare_breaker, "allow_request", lambda: True)
    monkeypatch.setattr(tushare_sync.tushare_breaker, "record_success",
                        lambda: real_breaker_calls.__setitem__("succ", real_breaker_calls["succ"] + 1))
    monkeypatch.setattr(tushare_sync.tushare_breaker, "record_failure",
                        lambda: real_breaker_calls.__setitem__("fail", real_breaker_calls["fail"] + 1))
    monkeypatch.setattr(tushare_sync.tushare_rate_limiter, "acquire", lambda n=1.0, timeout=None: None)

    flaky = _FlakyPro(
        failures=[ValueError("unexpected JSON parse error")],
        success_df=pd.DataFrame(),
    )
    monkeypatch.setattr(tushare_sync, "get_pro", lambda: flaky)

    df = tushare_sync._fetch_with_guard("income", ts_code="000001.SZ")
    assert df.empty
    assert flaky.call_count == 1
    assert real_breaker_calls["fail"] == 1


def test_fetch_guard_breaker_open_cooldown_retry(monkeypatch):
    """熔断 OPEN → sleep recovery_timeout → HALF_OPEN 放行 → 重试成功（不直接返空）。

    场景：allow_request 首次 False（OPEN），sleep recovery_timeout 后第二次 True。
    期望不直接返空，而是冷却后重走重试链。这是 by=date 全历史不卡死的关键。
    """
    from data import tushare_sync
    sleeps = _mock_sleep(monkeypatch)

    # allow_request 序列：False, True（首次 OPEN，冷却后 HALF_OPEN 放行）
    allow_seq = [False, True]
    monkeypatch.setattr(tushare_sync.tushare_breaker, "allow_request",
                        lambda: allow_seq.pop(0) if allow_seq else True)
    monkeypatch.setattr(tushare_sync.tushare_breaker, "record_success", lambda: None)
    monkeypatch.setattr(tushare_sync.tushare_breaker, "record_failure", lambda: None)
    monkeypatch.setattr(tushare_sync.tushare_breaker, "recovery_timeout", 60.0)
    monkeypatch.setattr(tushare_sync.tushare_rate_limiter, "acquire", lambda n=1.0, timeout=None: None)

    success_df = pd.DataFrame({"ts_code": ["000001.SZ"], "v": [1]})
    flaky = _FlakyPro(failures=[], success_df=success_df)
    monkeypatch.setattr(tushare_sync, "get_pro", lambda: flaky)

    df = tushare_sync._fetch_with_guard("moneyflow", trade_date="20240105")
    # 冷却后重试成功，返回数据（不因首次 OPEN 直接返空）
    assert not df.empty
    assert flaky.call_count == 1
    # sleep 了一次 recovery_timeout（60s）等待 HALF_OPEN
    assert 60.0 in sleeps, f"应 sleep recovery_timeout=60s 等冷却，实际 sleeps={sleeps}"


def test_fetch_guard_empty_data_no_failure(monkeypatch):
    """空数据（df.empty）→ 不 record_failure（正常无数据语义，不污染熔断）。

    场景：pro 返回空 df（如节假日无数据）。期望返空且不 record_failure
    （空数据是正常无数据，非接口异常，不应拖累熔断器）。
    """
    from data import tushare_sync
    _mock_sleep(monkeypatch)

    real_breaker_calls = {"fail": 0, "succ": 0}
    monkeypatch.setattr(tushare_sync.tushare_breaker, "allow_request", lambda: True)
    monkeypatch.setattr(tushare_sync.tushare_breaker, "record_success",
                        lambda: real_breaker_calls.__setitem__("succ", real_breaker_calls["succ"] + 1))
    monkeypatch.setattr(tushare_sync.tushare_breaker, "record_failure",
                        lambda: real_breaker_calls.__setitem__("fail", real_breaker_calls["fail"] + 1))
    monkeypatch.setattr(tushare_sync.tushare_rate_limiter, "acquire", lambda n=1.0, timeout=None: None)

    flaky = _FlakyPro(failures=[], success_df=pd.DataFrame())
    monkeypatch.setattr(tushare_sync, "get_pro", lambda: flaky)

    df = tushare_sync._fetch_with_guard("moneyflow", trade_date="20240105")
    assert df.empty
    assert real_breaker_calls["fail"] == 0
    # 空数据不计熔断但仍维持健康度（原逻辑：return 前未显式 record_success，
    # 此处验证至少不 record_failure —— 关键是不污染熔断）


# ============ resolve_symbols 标的池自动路由测试 ============
# Why 独立测试组：by=symbol 数据集标的来源有三类（股票/ETF/指数），resolve_symbols 按
# cfg['universe'] 字段路由到正确 loader。核心防退化：旧逻辑统一喂股票列表，导致 ETF/指数
# 类数据集（fund_*/index_*）在 slow 批静默落空（df.empty 直接 continue，不报错不落盘）。


def test_resolve_symbols_stock(monkeypatch):
    """universe=stock → 调 _load_universe（全市场股票列表）。"""
    from config import TUSHARE_DATASETS
    from data import tushare_sync
    monkeypatch.setattr(tushare_sync, "_load_universe", lambda: ["000001.SZ", "600000.SH"])
    monkeypatch.setattr(tushare_sync, "_load_etf_universe", lambda: ["159919.SZ"])
    TUSHARE_DATASETS["_test_stock"] = {"by": "symbol", "universe": "stock"}
    assert tushare_sync.resolve_symbols("_test_stock") == ["000001.SZ", "600000.SH"]


def test_resolve_symbols_etf(monkeypatch):
    """universe=etf → 调 _load_etf_universe（基金代码），绝不调股票 loader。

    防退化核心：ETF 类若误用 _load_universe（股票），fund_daily 等会被喂股票代码
    → 接口返空 → 静默落空。本测试钉死 ETF 必须走基金标的池。
    """
    from config import TUSHARE_DATASETS
    from data import tushare_sync
    monkeypatch.setattr(tushare_sync, "_load_universe", lambda: ["000001.SZ"])
    monkeypatch.setattr(tushare_sync, "_load_etf_universe", lambda: ["159919.SZ", "510300.SH"])
    TUSHARE_DATASETS["_test_etf"] = {"by": "symbol", "universe": "etf"}
    syms = tushare_sync.resolve_symbols("_test_etf")
    assert syms == ["159919.SZ", "510300.SH"]
    assert "000001.SZ" not in syms, "ETF 数据集不能喂股票代码"


def test_resolve_symbols_index(monkeypatch):
    """universe=index → 返回核心宽基指数常量（不发任何标的池请求）。

    防退化核心：指数类若误用 _load_universe（股票），index_daily 会被喂股票代码
    → 静默落空。指数代码是固定核心宽基，无需也不应从股票/基金接口拉。
    """
    from config import TUSHARE_DATASETS
    from data import tushare_sync
    # 确保两个网络 loader 都不被调（指数代码是常量，零网络依赖）
    monkeypatch.setattr(tushare_sync, "_load_universe", lambda: ["000001.SZ"])
    monkeypatch.setattr(tushare_sync, "_load_etf_universe", lambda: ["159919.SZ"])
    TUSHARE_DATASETS["_test_index"] = {"by": "symbol", "universe": "index"}
    syms = tushare_sync.resolve_symbols("_test_index")
    assert "000300.SH" in syms, "沪深300（核心宽基）必须在指数池"
    # 指数池里不应混入股票/基金代码
    assert "000001.SZ" not in syms and "159919.SZ" not in syms


def test_resolve_symbols_default_stock(monkeypatch):
    """无 universe 字段 → 缺省 stock（向后兼容未显式声明的既有数据集）。"""
    from config import TUSHARE_DATASETS
    from data import tushare_sync
    monkeypatch.setattr(tushare_sync, "_load_universe", lambda: ["600000.SH"])
    TUSHARE_DATASETS["_test_default"] = {"by": "symbol"}  # 故意不写 universe
    assert tushare_sync.resolve_symbols("_test_default") == ["600000.SH"]


def test_resolve_symbols_limit(monkeypatch):
    """limit 切片：编排脚本子集验证（如先跑沪深300 子集）用。"""
    from config import TUSHARE_DATASETS
    from data import tushare_sync
    monkeypatch.setattr(tushare_sync, "_load_universe",
                        lambda: ["a.SZ", "b.SZ", "c.SZ", "d.SZ"])
    TUSHARE_DATASETS["_test_limit"] = {"by": "symbol", "universe": "stock"}
    assert tushare_sync.resolve_symbols("_test_limit", limit=2) == ["a.SZ", "b.SZ"]


def test_sync_by_symbol_uses_resolver_when_symbols_none(tmp_path, fake_pro, monkeypatch):
    """_sync_by_symbol：symbols=None 时经 resolve_symbols(key) 路由，不硬调 _load_universe。

    Why 此集成测：旧实现 symbols=None 时硬调 _load_universe()（股票），是 ETF/指数类静默
    落空的根因。改造后应经 resolve_symbols 按 universe 字段路由。spy 替换 resolve_symbols，
    断言它被以正确 key 调用——这一行改动是整个修复的落点。
    """
    from config import TUSHARE_DATASETS, LAKE_CONFIG
    TUSHARE_DATASETS["_test_route"] = {
        "api": "income", "by": "symbol", "universe": "etf",
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,total_revenue",
        "lake": str(tmp_path / "routed.parquet"),
    }
    LAKE_CONFIG["lakes"]["_test_route"] = TUSHARE_DATASETS["_test_route"]["lake"]
    spy = {"key": None}
    def _spy(key, limit=None):
        spy["key"] = key
        return ["159919.SZ"]
    monkeypatch.setattr("data.tushare_sync.resolve_symbols", _spy)
    from data.tushare_sync import sync_dataset
    sync_dataset("_test_route", "2024-01-01", "2024-12-31", symbols=None, resume=False)
    assert spy["key"] == "_test_route", "symbols=None 时必须经 resolve_symbols 路由"


def test_by_symbol_datasets_universe_correctly_declared():
    """守卫：by=symbol 数据集的 universe 字段必须与标的语义一致（防漏配导致静默落空）。

    Why 此守卫：resolve_symbols 按 universe 路由，若 fund_* 误标 stock（或漏标走 default），
    fund_daily 会被喂股票代码静默落空。本测试按数据集名前缀钉死三类映射，未来新增/改名
    数据集时若漏配 universe 会立即在 CI 失败，而非跑到 slow 批才发现空湖。
    """
    from config import TUSHARE_DATASETS
    for key, cfg in TUSHARE_DATASETS.items():
        if cfg.get("by") != "symbol" or cfg.get("_unavailable"):
            continue
        uni = cfg.get("universe", "stock")  # 缺省视为 stock（向后兼容）
        if key.startswith("fund_"):
            assert uni == "etf", f"{key} 应 universe=etf（基金代码），实际 {uni!r}"
        elif key.startswith("index_"):
            assert uni == "index", f"{key} 应 universe=index（指数代码），实际 {uni!r}"
        else:
            assert uni == "stock", f"{key} 应 universe=stock（股票），实际 {uni!r}"


def test_sync_single_filters_to_cfg_fields(tmp_path, fake_pro, monkeypatch):
    """_sync_single: 接口忽略 fields 返多余列时，落湖前按 cfg.fields 筛选保留列。

    Why: cn_pmi 等宏观接口忽略 fields 参数——请求 fields='MONTH,PMI010000' 仍返回全部 64 列
    （含 UPDATE_BY/CREATE_BY 等 100% NaN 垃圾元字段）。落湖前按 cfg.fields 筛选，确保湖只含
    声明列，防湖膨胀 + 防垃圾列污染下游。对尊重 fields 的接口（落湖列本就 ⊆ fields）筛选无变化。
    """
    from config import TUSHARE_DATASETS, LAKE_CONFIG
    import pandas as pd
    # 模拟 cn_pmi 接口忽略 fields：请求 2 列但返回 5 列（含 100% NaN 垃圾元字段 + 多余列）
    fake_pro._data["cn_pmi_test"] = pd.DataFrame({
        "MONTH": ["202401", "202402"],
        "PMI010000": [50.1, 50.2],
        "UPDATE_BY": [None, None],   # 100% NaN 垃圾元字段
        "CREATE_BY": [None, None],
        "PMI010500": [49.8, 49.9],   # fields 外多余列
    })
    TUSHARE_DATASETS["cn_pmi_test"] = {
        "api": "cn_pmi_test", "by": "single",
        "date_col": "MONTH", "symbol_col": "MONTH",
        "fields": "MONTH,PMI010000",   # 只声明 2 列
        "index_mode": "datetime",
        "lake": str(tmp_path / "pmi.parquet"),
    }
    LAKE_CONFIG["lakes"]["cn_pmi_test"] = TUSHARE_DATASETS["cn_pmi_test"]["lake"]
    from data.tushare_sync import sync_dataset
    sync_dataset("cn_pmi_test", "2024-01-01", "2024-12-31", resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["cn_pmi_test"]["lake"])
    # 只保留 fields 声明列：MONTH 进 index，PMI010000 为唯一数据列
    assert "PMI010000" in df.columns, "声明列 PMI010000 应保留"
    assert "UPDATE_BY" not in df.columns, "100% NaN 垃圾元字段应在落湖前删除"
    assert "CREATE_BY" not in df.columns, "100% NaN 垃圾元字段应在落湖前删除"
    assert "PMI010500" not in df.columns, "fields 外多余列应删除"


def test_sync_by_symbol_no_date_filter_skips_date(tmp_path, fake_pro, monkeypatch):
    """_sync_by_symbol: cfg['no_date_filter']=True 时不传 start_date/end_date。

    Why: 事件类接口（如 dividend 分红）按标的返全历史，不认 start_date/end_date——实测
    dividend 传日期参数直接返空（shard 全空 → _build_multiindex 抛 RuntimeError）。
    no_date_filter 让此类接口拉全历史（落湖后按区间自然覆盖），避免日期参数致空。
    对认日期的接口（fina_* 财报）不加该标记，行为不变。
    """
    from config import TUSHARE_DATASETS, LAKE_CONFIG
    import pandas as pd
    fake_pro._data["dividend"] = pd.DataFrame({
        "ts_code": ["600000.SH"] * 2,
        "ann_date": ["20240627", "20250627"],
        "div_proc": ["实施", "实施"],
        "stk_div": [0.0, 0.0], "cash_div": [0.42, 0.40],
        "record_date": ["20240715", "20250715"], "ex_date": ["20240716", "20250716"],
    })
    TUSHARE_DATASETS["_test_div"] = {
        "api": "dividend", "by": "symbol", "no_date_filter": True,
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,div_proc,stk_div,cash_div,record_date,ex_date",
        "lake": str(tmp_path / "div.parquet"),
    }
    LAKE_CONFIG["lakes"]["_test_div"] = TUSHARE_DATASETS["_test_div"]["lake"]
    from data.tushare_sync import sync_dataset
    sync_dataset("_test_div", "2024-01-01", "2024-12-31", symbols=["600000.SH"], resume=False)
    # no_date_filter=True → 请求 kwargs 不应含 start_date/end_date（防 dividend 日期致空）
    div_calls = [kw for api, kw in fake_pro.calls if api == "dividend"]
    assert div_calls, "dividend 应被调用"
    assert "start_date" not in div_calls[0], "no_date_filter 时不应传 start_date（dividend 不认日期）"
    assert "end_date" not in div_calls[0], "no_date_filter 时不应传 end_date"


# ============ 写入前历史行数守卫接入（T13-A · Task 3） ============
# 物理意图：_sync_single / _build_multiindex 做全量覆盖写（非 append），正是 T12 抹除
# 路径（1020万→3200）。接入 assert_safe_overwrite 后，落盘前若新行数相对现有骤降 → 拒写，
# 原湖完好。本测试重演 T12：mock _fetch_with_guard 回残片 → _sync_single 拒写。


def test_sync_single_refuses_crater_overwrite(tmp_path, monkeypatch):
    """重演 T12：现有大湖，_fetch_with_guard 回残片 → _sync_single 拒写，文件不变。"""
    from data.integrity import WriteGuardError, existing_row_count
    from data import tushare_sync
    out = str(tmp_path / "lake.parquet")

    # 现有大湖（10000 行 MultiIndex(date, symbol)）
    pd.DataFrame({"date": pd.date_range("2024-01-01", periods=10000),
                  "symbol": ["000001.SZ"] * 10000,
                  "close": range(10000)}).set_index(["date", "symbol"]).to_parquet(out)

    # mock 通用同步器的限频拉取，返回残片（模拟 SOCKS 异常只拿到单日）
    crater = pd.DataFrame({"trade_date": ["20260724"], "ts_code": ["000001.SZ"],
                           "close": [10.0]})
    monkeypatch.setattr(tushare_sync, "_fetch_with_guard",
                        lambda api, **kw: crater)
    # _sync_single 签名：(key, api, fields, date_col, out, cfg=None, start=None, end=None)
    with pytest.raises(WriteGuardError):
        tushare_sync._sync_single("fakekey", "daily", None, "trade_date", out,
                                  cfg={"api": "daily", "by": "single",
                                       "date_col": "trade_date",
                                       "symbol_col": "ts_code"})
    # 原湖未被覆盖（守卫拒写，行数不变）
    assert existing_row_count(out) == 10000


def test_sync_single_date_range_bypasses_guard(tmp_path, monkeypatch):
    """date_range 数据集（shibor 范式）_sync_single 窗口写放行（避免误伤增量致断更）。

    物理意图：sync_incremental 对 date_range 数据集做窗口增量（_sync_single 覆盖写只含
    新窗口，[6] merge 还原）；_sync_single 守卫若触发会拒写致每日断更。date_range 旁路
    让 _sync_single 放行，最终落盘由 sync_incremental [6] old_df 基线守卫负责。
    """
    from data.integrity import existing_row_count
    from data import tushare_sync
    out = str(tmp_path / "shibor.parquet")
    # 现有大湖（模拟 shibor 近 3 年 249 行，DatetimeIndex）
    pd.DataFrame({"rate": range(249)},
                 index=pd.DatetimeIndex(pd.date_range("2023-01-01", periods=249))).to_parquet(out)
    # 窗口写：_fetch_with_guard 回 1 行（增量窗口），cfg date_range=True
    window = pd.DataFrame({"trade_date": ["20260811"], "rate": [1.5]})
    monkeypatch.setattr(tushare_sync, "_fetch_with_guard", lambda api, **kw: window)
    # date_range=True → 守卫旁路，不抛 WriteGuardError（窗口中间写由 sync_incremental [6] 守卫）
    tushare_sync._sync_single("shibor", "shibor", "rate", "trade_date", out,
                              cfg={"api": "shibor", "by": "single", "date_range": True,
                                   "date_col": "trade_date"})
    # 窗口写覆盖成功（_sync_single 中间态，由调用方 merge 还原）
    assert existing_row_count(out) == 1
