# -*- coding: utf-8 -*-
"""通用 Tushare 湖同步器测试：配置驱动 + 分页 + 断点续传 + 落湖。

设计意图（反黑盒测试）：
- fake_pro 替身 mock 掉 get_pro / tushare_rate_limiter / tushare_breaker，使测试
  **完全不依赖真 Tushare token / 网络环境**（开发机可能未配 TNSKHDATA_TOKEN），
  仅验证同步器的分页/断点/落湖逻辑正确性。
- 通过 TUSHARE_DATASETS[key] 临时覆盖落湖路径到 tmp_path，保证测试隔离无副作用。
"""
import copy
import os
import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def _isolate_tushare_registry():
    """深拷贝 TUSHARE_DATASETS + LAKE_CONFIG['lakes']，测试后还原原对象。

    Why autouse 深拷贝：两个测试就地覆盖全局 TUSHARE_DATASETS['fina_income'] 和
    LAKE_CONFIG['lakes']['fina_income']，若不还原会污染后续测试（测试顺序依赖、
    隔离性破坏）。深拷贝保证测试内 mutate 不影响原注册表，yield 后还原引用即可
    让其他模块看到原始未被改动的配置。copy.deepcopy 处理嵌套 dict（lakes 子键）。
    """
    from config import TUSHARE_DATASETS, LAKE_CONFIG
    saved_datasets = copy.deepcopy(TUSHARE_DATASETS)
    saved_lakes = copy.deepcopy(LAKE_CONFIG["lakes"])
    yield
    TUSHARE_DATASETS.clear()
    TUSHARE_DATASETS.update(saved_datasets)
    LAKE_CONFIG["lakes"].clear()
    LAKE_CONFIG["lakes"].update(saved_lakes)


class _FakePro:
    """tushare pro 替身：按 api_name 返回可控 DataFrame。

    Why __getattr__：pro 接口方法（pro.income / pro.stock_basic ...）在运行时由
    tushare DataApi 动态分发，测试替身用 __getattr__ 一次性兜底所有 api_name，
    避免逐方法硬编码；同时记录调用序列供断言「shard 已存在即跳过」等行为。
    """
    def __init__(self):
        self.calls = []  # 记录 (api_name, kwargs)
        self._data = {
            "income": pd.DataFrame({
                "ts_code": ["000001.SZ"] * 3,
                "ann_date": ["20240101", "20240401", "20240701"],
                "end_date": ["20231231", "20240331", "20240630"],
                "total_revenue": [1e9, 1.1e9, 1.2e9],
                "n_income": [1e8, 1.1e8, 1.2e8],
            }),
        }

    def __getattr__(self, api_name):
        def _call(**kwargs):
            self.calls.append((api_name, kwargs))
            return self._data.get(api_name, pd.DataFrame())
        return _call


@pytest.fixture
def fake_pro(monkeypatch):
    """mock pro 接口 + 限频/熔断器（acquire 直通、breaker 永远放行）。

    Why 同时 mock 三个：sync_dataset 经 _fetch_with_guard 串联 rate_limiter → breaker
    → get_pro，三道闸门任一未被 mock 都会触达真实 tushare/网络。fixture 一次性
    把数据路径短路，让测试聚焦分页/落湖逻辑本身。

    Why 双重 patch get_pro（Task 2 修复 Task 1 隔离漏洞）：data/tushare_sync.py 顶部
    `from data._tushare_compat import get_pro` 把函数对象绑到 tushare_sync 模块全局
    命名空间。_fetch_with_guard 体内的 `pro = get_pro()` 解析的是 tushare_sync 模块的
    get_pro（导入时绑定），而非 _tushare_compat 模块的 get_pro。仅 patch
    `data._tushare_compat.get_pro` **不会** 改变 tushare_sync.get_pro 的绑定——旧实现下
    测试静默穿透到真实 Tushare（.env 有 token 时命中真 API，无 token 时返空→shard 为空
    →_build_multiindex 抛错或读到上次残留 shard），导致跨文件测试套件污染（test_tushare_datasets_stock
    先运行时本测试读到残留 shard），断言 flaky。本 fixture 同时 patch 两处绑定，保证替身真正短路。
    """
    fake = _FakePro()
    monkeypatch.setattr("data._tushare_compat.get_pro", lambda: fake)
    monkeypatch.setattr("data.tushare_sync.get_pro", lambda: fake)
    monkeypatch.setattr("data.tushare_sync.tushare_rate_limiter",
                        type("L", (), {"acquire": lambda self, n: None})())
    monkeypatch.setattr("data.tushare_sync.tushare_breaker",
                        type("B", (), {"allow_request": lambda self: True,
                                       "record_success": lambda self: None,
                                       "record_failure": lambda self: None})())
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
    }
    LAKE_CONFIG["lakes"]["fina_income"] = TUSHARE_DATASETS["fina_income"]["lake"]
    from data.tushare_sync import sync_dataset
    sync_dataset("fina_income", "2024-01-01", "2024-12-31",
                 symbols=["000001.SZ"], resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["fina_income"]["lake"])
    assert df.index.names == ["date", "symbol"]
    assert "total_revenue" in df.columns
    assert len(df) == 3


def test_sync_dataset_resume_skips_existing_shard(tmp_path, fake_pro, monkeypatch):
    """断点续传：shard 已存在即跳过（省配额）。"""
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
    # 预置 shard（模拟已拉过 000001.SZ）
    pd.DataFrame({"total_revenue": [1e9]},
                 index=pd.DatetimeIndex(["2024-01-01"], name="ann_date")
                 ).to_parquet(os.path.join(shard_dir, "000001.SZ.parquet"))
    from data.tushare_sync import sync_dataset
    sync_dataset("fina_income", "2024-01-01", "2024-12-31",
                 symbols=["000001.SZ"], resume=True)
    # fake_pro 未被调（shard 已存在跳过）
    assert fake_pro.calls == []


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

