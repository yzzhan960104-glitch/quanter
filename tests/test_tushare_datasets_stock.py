# -*- coding: utf-8 -*-
"""股票类各 Tushare 数据集配置 + 落湖契约测试（Plan A Task 2）。

设计意图（反黑盒 + 反前视偏差）：
- **前视红线**：财报类（income/balancesheet/cashflow/forecast/express）必须以 ann_date
  （公告日）为时间索引，绝不能用 end_date（报告期）—— 报告期早于公告日，用 end_date
  索引等于在公告前就知道业绩，回测出现前视偏差。test_fina_datasets_use_ann_date_not_end_date
  是这条红线的机器化守卫，配置任何一处回退到 end_date 立即红。
- **配置完备性**：fina_balance/fina_cashflow/forecast/express/dividend 五个数据集必须在
  TUSHARE_DATASETS 注册且 fields/落湖路径声明完整，否则 sync_dataset 直接 KeyError。
- **落湖契约**：三大报表经 sync_dataset 后产出 MultiIndex(date, symbol) parquet，这是
  DataLakeReader 双向切片的硬契约，test_fina_three_statements_lake 守卫。
"""
import copy
import pandas as pd
import pytest

from config import TUSHARE_DATASETS, LAKE_CONFIG


@pytest.fixture(autouse=True)
def _isolate_tushare_registry():
    """深拷贝 TUSHARE_DATASETS + LAKE_CONFIG['lakes']，测试后还原原对象引用。

    Why autouse 深拷贝（Task 1 review 教训）：本文件的测试会就地覆盖全局
    TUSHARE_DATASETS[key]['lake']（重定向到 tmp_path）与 LAKE_CONFIG['lakes'][key]。
    若不还原，全局注册表会被污染——后续测试拿到指向 tmp_path 的 lake 路径（tmp_path
    测试结束即销毁），导致跨测试顺序依赖 + 真实 sync 脚本写到错误路径。
    手法与 tests/test_tushare_sync.py::_isolate_tushare_registry 完全一致：
    clear()+update(saved) 保留原 dict 对象身份（其他模块 from config import 的引用不变），
    嵌套 lakes 子 dict 由 deepcopy 兜底。
    """
    saved_datasets = copy.deepcopy(TUSHARE_DATASETS)
    saved_lakes = copy.deepcopy(LAKE_CONFIG["lakes"])
    yield
    TUSHARE_DATASETS.clear()
    TUSHARE_DATASETS.update(saved_datasets)
    LAKE_CONFIG["lakes"].clear()
    LAKE_CONFIG["lakes"].update(saved_lakes)


class _FakePro:
    """tushare pro 替身：按 api_name 返回可控 DataFrame。

    Why __getattr__：pro 接口方法（pro.income / pro.balancesheet ...）在运行时由
    tushare DataApi 动态分发，测试替身用 __getattr__ 一次性兜底所有 api_name，
    避免逐方法硬编码。set(api, df) 注入可控数据。
    """
    def __init__(self):
        self._data = {}

    def set(self, api, df):
        self._data[api] = df

    def __getattr__(self, api):
        def _c(**kw):
            return self._data.get(api, pd.DataFrame())
        return _c


@pytest.fixture
def fake_pro(monkeypatch):
    """mock pro 接口 + 限频/熔断器（acquire 直通、breaker 永远放行）。

    Why 同时 mock 三个：sync_dataset 经 _fetch_with_guard 串联 rate_limiter → breaker
    → get_pro，三道闸门任一未被 mock 都会触达真实 tushare/网络。fixture 一次性
    把数据路径短路，让测试聚焦分页/落湖逻辑本身。

    Why 双重 patch get_pro（关键防漏网）：data/tushare_sync.py 顶部用
    `from data._tushare_compat import get_pro` 把函数对象绑到 tushare_sync 模块的
    全局命名空间。`_fetch_with_guard` 体内 `pro = get_pro()` 解析的是 tushare_sync
    模块的 get_pro（导入时绑定），而非 _tushare_compat 模块的 get_pro。
    仅 patch `data._tushare_compat.get_pro` **不会** 改变 tushare_sync.get_pro 的绑定
    ——旧实现下测试会穿透到真实 Tushare（.env 有 token 时静默命中真 API，
    无 token 时返回空 DataFrame 导致 _build_multiindex 抛「shard 目录无数据」），
    测试既不隔离也不稳定。本 fixture 同时 patch 两处绑定，保证替身真正短路。
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


def test_fina_datasets_use_ann_date_not_end_date():
    """财报类前视红线：date_col 必须是 ann_date，绝不能是 end_date。

    Why 机器化守卫：财报的 end_date 是「报告期」（如 20231231），ann_date 才是「公告日」
    （如 20240330）。报告期早于公告日数月，若用 end_date 索引湖，回测在 2024-01-01
    就能读到 2023 年报数据，构成前视偏差。本测试把红线钉死在配置层，PR review 都
    可能漏，机器不会。dividend 用 ann_date（分红方案公告日）同理——绝不用 div_proc
    （分红进度文本字段，非日期）或 record_date（除权登记日，晚于公告日，也偏）。
    """
    for key in ("fina_income", "fina_balance", "fina_cashflow", "forecast", "express"):
        assert TUSHARE_DATASETS[key]["date_col"] == "ann_date", \
            f"{key} 必须用 ann_date（公告日）索引，禁用 end_date（报告期，前视偏差）"


def test_dividend_uses_ann_date():
    """dividend 前视红线：date_col 必须是 ann_date，禁用 div_proc（非日期文本）。

    Why 独立守卫：brief Step 3 曾误写 date_col=div_proc（div_proc 是「预案/实施」
    文本进度字段，非日期），随后注释修正为 ann_date。本测试把修正钉死，防止回退。
    """
    assert TUSHARE_DATASETS["dividend"]["date_col"] == "ann_date", \
        "dividend 必须用 ann_date（分红方案公告日）索引，禁用 div_proc（文本进度，非日期）"


def test_all_five_datasets_registered():
    """五个新数据集必须在 TUSHARE_DATASETS 注册且关键字段完备。

    Why 守卫完备性：sync_dataset 直接 cfg = TUSHARE_DATASETS[key]，缺任一 key 立即
    KeyError；缺 api/by/date_col/symbol_col/fields/lake 任一字段，运行时崩在更深处。
    配置层把契约钉死，PR review 漏一眼也守得住。
    """
    required_keys = ("fina_balance", "fina_cashflow", "forecast", "express", "dividend")
    required_fields = ("api", "by", "date_col", "symbol_col", "fields", "lake")
    for key in required_keys:
        assert key in TUSHARE_DATASETS, f"{key} 未在 TUSHARE_DATASETS 注册"
        cfg = TUSHARE_DATASETS[key]
        for f in required_fields:
            assert f in cfg, f"{key} 配置缺字段 {f}"
        # by=symbol：财报/分红均逐标的拉取（单标的全历史一次返）
        assert cfg["by"] == "symbol", f"{key} 分页模式应为 symbol（逐标的）"


def test_five_datasets_lake_registered():
    """五个新数据集必须在 LAKE_CONFIG['lakes'] 注册（DataLakeReader 寻址依赖）。"""
    for key in ("fina_balance", "fina_cashflow", "forecast", "express", "dividend"):
        assert key in LAKE_CONFIG["lakes"], \
            f"{key} 未在 LAKE_CONFIG['lakes'] 注册，DataLakeReader 无法寻址"
        # 落湖路径与 TUSHARE_DATASETS 声明必须一致（单一真相源，避免两处分叉）
        assert LAKE_CONFIG["lakes"][key] == TUSHARE_DATASETS[key]["lake"], \
            f"{key} 的 LAKE_CONFIG 路径与 TUSHARE_DATASETS 不一致"


def test_fina_three_statements_lake(tmp_path, fake_pro, monkeypatch):
    """三大报表落 MultiIndex(date, symbol)。

    Why 端到端契约：income/balancesheet/cashflow 经 sync_dataset 后必须产出
    MultiIndex(date, symbol) parquet，这是 DataLakeReader 按日期区间 + 标的列表
    双向切片的硬契约。索引名错（如缺 'date' 或 'symbol'）→ reader 切片 KeyError。

    Why 同时重定向 shard_dir（关键防污染）：sync_dataset 的 shard 默认落
    data_lake/shards/<key>/（共享文件系统），仅重定向 lake 输出会留下 shard 残留，
    污染后续测试（如 test_tushare_sync.py 读到本测试的假 shard）。shard_dir 一并
    指向 tmp_path 子目录，测试结束自动销毁，零残留。
    """
    fake_pro.set("income", pd.DataFrame({
        "ts_code": ["000001.SZ"], "ann_date": ["20240101"], "end_date": ["20231231"],
        "total_revenue": [1e9], "n_income": [1e8]}))
    fake_pro.set("balancesheet", pd.DataFrame({
        "ts_code": ["000001.SZ"], "ann_date": ["20240101"], "end_date": ["20231231"],
        "total_assets": [1e10]}))
    fake_pro.set("cashflow", pd.DataFrame({
        "ts_code": ["000001.SZ"], "ann_date": ["20240101"], "end_date": ["20231231"],
        "net_profit_cash_flow": [9e7]}))
    from data.tushare_sync import sync_dataset
    for key in ("fina_income", "fina_balance", "fina_cashflow"):
        monkeypatch.setitem(TUSHARE_DATASETS[key], "lake", str(tmp_path / f"{key}.parquet"))
        monkeypatch.setitem(TUSHARE_DATASETS[key], "shard_dir", str(tmp_path / f"shards_{key}"))
        sync_dataset(key, "2024-01-01", "2024-12-31", symbols=["000001.SZ"], resume=False)
        df = pd.read_parquet(TUSHARE_DATASETS[key]["lake"])
        assert df.index.names == ["date", "symbol"]


def test_forecast_express_dividend_lake(tmp_path, fake_pro, monkeypatch):
    """预告/快报/分红 同样落 MultiIndex(date, symbol)。

    Why 补全端到端：brief 只示范了三大报表，但 forecast/express/dividend 走同一
    sync_dataset 管道，索引契约应一致。任一接口字段差异（如 forecast 的 type/
    p_change_min/max）不应影响索引结构——索引由 date_col + symbol_col 驱动，与数据列无关。

    Why 同时重定向 shard_dir：与 test_fina_three_statements_lake 同理，避免假 shard
    污染共享 data_lake/shards/，污染后续 test_tushare_sync.py 等测试。
    """
    fake_pro.set("forecast", pd.DataFrame({
        "ts_code": ["000001.SZ"], "ann_date": ["20240115"], "end_date": ["20231231"],
        "type": ["预增"], "p_change_min": [40], "p_change_max": [60],
        "min_range": [4e7], "max_range": [6e7]}))
    fake_pro.set("express", pd.DataFrame({
        "ts_code": ["000001.SZ"], "ann_date": ["20240220"], "end_date": ["20231231"],
        "revenue": [1e9], "n_income": [1e8], "total_profit": [1.2e8]}))
    fake_pro.set("dividend", pd.DataFrame({
        "ts_code": ["000001.SZ"], "ann_date": ["20240330"],
        "div_proc": ["预案"], "stk_div": [0.0], "cash_div": [1.5],
        "record_date": [""], "ex_date": [""]}))
    from data.tushare_sync import sync_dataset
    for key in ("forecast", "express", "dividend"):
        monkeypatch.setitem(TUSHARE_DATASETS[key], "lake", str(tmp_path / f"{key}.parquet"))
        monkeypatch.setitem(TUSHARE_DATASETS[key], "shard_dir", str(tmp_path / f"shards_{key}"))
        sync_dataset(key, "2024-01-01", "2024-12-31", symbols=["000001.SZ"], resume=False)
        df = pd.read_parquet(TUSHARE_DATASETS[key]["lake"])
        assert df.index.names == ["date", "symbol"], f"{key} 索引名错误"
        assert len(df) == 1, f"{key} 行数错误"
