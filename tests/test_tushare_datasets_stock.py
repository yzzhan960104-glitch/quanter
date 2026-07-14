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


def test_task3to5_datasets_registered():
    """Task 3-5 数据集配置完备性 + 落湖注册契约。

    top_list 复用 dragon_list 湖（切 Tushare 替代 akshare sync_dragon_list），
    不在 LAKE_CONFIG 新增 key；其余 5 个各自独立湖。
    """
    by_date_specs = {
        "moneyflow": ("trade_date", "ts_code"),
        "top_list": ("trade_date", "ts_code"),
        "top_inst": ("trade_date", "ts_code"),
        "margin": ("trade_date", "exchange_id"),
        "margin_detail": ("trade_date", "ts_code"),
    }
    for key, (date_col, sym) in by_date_specs.items():
        assert key in TUSHARE_DATASETS, f"{key} 未注册"
        cfg = TUSHARE_DATASETS[key]
        assert cfg["by"] == "date", f"{key} by 应为 date"
        assert cfg["date_col"] == date_col, f"{key} date_col 应为 {date_col}"
        assert cfg["symbol_col"] == sym, f"{key} symbol_col 应为 {sym}"
    assert TUSHARE_DATASETS["margin_secs"]["by"] == "single"
    # 各自独立湖的 5 个：注册到 LAKE_CONFIG 且路径与 TUSHARE_DATASETS 一致
    for key in ("moneyflow", "top_inst", "margin", "margin_detail", "margin_secs"):
        assert key in LAKE_CONFIG["lakes"], f"{key} 未注册到 LAKE_CONFIG"
        assert LAKE_CONFIG["lakes"][key] == TUSHARE_DATASETS[key]["lake"], \
            f"{key} LAKE_CONFIG 与 TUSHARE_DATASETS 路径不一致"
    # top_list 复用 dragon_list 湖（切源，不新增 key）
    assert TUSHARE_DATASETS["top_list"]["lake"] == LAKE_CONFIG["lakes"]["dragon_list"], \
        "top_list 应复用 dragon_list 湖（切 Tushare 替代 akshare sync_dragon_list）"


def test_moneyflow_top_list_by_date(tmp_path, fake_pro, monkeypatch):
    """by=date 数据集（moneyflow/top_list）落 MultiIndex，symbol 从 symbol_col 列取（非文件名）。

    Why 守卫 Task 1 fix：by=date 的 shard 文件名是交易日（20240105.parquet），
    _build_multiindex 必须从 df[symbol_col] 取 symbol——若误从文件名取，symbol 全错成交易日。
    """
    import data.tushare_sync as ts
    monkeypatch.setattr(ts, "_trade_days", lambda s, e: ["20240105"])
    cases = {
        "moneyflow": ("moneyflow", pd.DataFrame({
            "ts_code": ["000001.SZ", "600000.SH"], "trade_date": ["20240105", "20240105"],
            "buy_sm_amount": [1e8, 2e8], "sell_sm_amount": [9e7, 1.5e8],
            "buy_elg_amount": [3e8, 4e8], "sell_elg_amount": [2e8, 3e8],
            "net_mf_amount": [1e7, 5e7]})),
        "top_list": ("top_list", pd.DataFrame({
            "ts_code": ["000001.SZ"], "trade_date": ["20240105"],
            "name": ["平安银行"], "close": [10.5], "pct_change": [9.9],
            "amount": [5e8], "net_amount": [1e8], "buy_amount": [3e8], "sell_amount": [2e8]})),
    }
    for key, (api, data) in cases.items():
        fake_pro.set(api, data)
        monkeypatch.setitem(TUSHARE_DATASETS[key], "lake", str(tmp_path / f"{key}.parquet"))
        monkeypatch.setitem(TUSHARE_DATASETS[key], "shard_dir", str(tmp_path / f"shards_{key}"))
        ts.sync_dataset(key, "2024-01-05", "2024-01-05", resume=False)
        df = pd.read_parquet(TUSHARE_DATASETS[key]["lake"])
        assert df.index.names == ["date", "symbol"], f"{key} 索引名错"
        syms = set(df.index.get_level_values("symbol"))
        assert "20240105" not in syms, f"{key} symbol 误取自文件名（交易日）"
        # symbol 必须来自 ts_code 列（真实标的码）
        assert syms.issubset(set(data["ts_code"].tolist())), f"{key} symbol 不在 ts_code 列"


def test_margin_by_date_and_secs_single(tmp_path, fake_pro, monkeypatch):
    """margin by=date（市场汇总，symbol_col=exchange_id）+ margin_secs by=single（扁平快照）。"""
    import data.tushare_sync as ts
    monkeypatch.setattr(ts, "_trade_days", lambda s, e: ["20240105"])
    # margin 市场汇总（exchange_id 作 symbol）
    fake_pro.set("margin", pd.DataFrame({
        "exchange_id": ["SSE"], "trade_date": ["20240105"],
        "rzye": [1e10], "rzmre": [1e9], "rqye": [1e8], "rqmcl": [1e7],
        "rzche": [5e8], "rqchl": [5e6]}))
    monkeypatch.setitem(TUSHARE_DATASETS["margin"], "lake", str(tmp_path / "margin.parquet"))
    monkeypatch.setitem(TUSHARE_DATASETS["margin"], "shard_dir", str(tmp_path / "shards_margin"))
    ts.sync_dataset("margin", "2024-01-05", "2024-01-05", resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["margin"]["lake"])
    assert df.index.names == ["date", "symbol"]
    assert "SSE" in df.index.get_level_values("symbol")  # exchange_id 作 symbol
    # margin_secs single（扁平快照，非 MultiIndex）
    fake_pro.set("margin_secs", pd.DataFrame({
        "ts_code": ["000001.SZ"], "name": ["平安银行"], "start_date": ["20100301"]}))
    monkeypatch.setitem(TUSHARE_DATASETS["margin_secs"], "lake", str(tmp_path / "margin_secs.parquet"))
    ts.sync_dataset("margin_secs", "2024-01-05", "2024-01-05", resume=False)
    secs = pd.read_parquet(TUSHARE_DATASETS["margin_secs"]["lake"])
    assert len(secs) == 1 and "ts_code" in secs.columns  # 扁平 df（single 模式）


def test_hsgt_top10_by_date_reuse_north_flow(tmp_path, fake_pro, monkeypatch):
    """hsgt_top10 by=date 复用 north_flow 湖（切 Tushare 替代 akshare）。"""
    import data.tushare_sync as ts
    monkeypatch.setattr(ts, "_trade_days", lambda s, e: ["20240105"])
    fake_pro.set("hsgt_top10", pd.DataFrame({
        "trade_date": ["20240105", "20240105"], "name": ["贵州茅台", "招商银行"],
        "ts_code": ["600519.SH", "600036.SH"], "vol": [1e6, 9e5],
        "amount": [1.8e9, 4e8], "north_direction": ["北向", "北向"]}))
    monkeypatch.setitem(TUSHARE_DATASETS["hsgt_top10"], "lake", str(tmp_path / "north.parquet"))
    monkeypatch.setitem(TUSHARE_DATASETS["hsgt_top10"], "shard_dir", str(tmp_path / "shards_hsgt"))
    ts.sync_dataset("hsgt_top10", "2024-01-05", "2024-01-05", resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["hsgt_top10"]["lake"])
    assert df.index.names == ["date", "symbol"]
    assert {"600519.SH", "600036.SH"} == set(df.index.get_level_values("symbol"))


def test_hsgt_top10_reuses_north_flow_config():
    """hsgt_top10 配置层复用 north_flow 湖（切 Tushare 替代 akshare，不新增 LAKE_CONFIG key）。

    Why 独立配置测试：端到端测试会 monkeypatch lake 到 tmp_path，无法断言原始配置契约。
    复用关系是配置层不变量（hsgt_top10 是 north_flow 湖的 Tushare 生产者），单独钉死。
    """
    assert TUSHARE_DATASETS["hsgt_top10"]["lake"] == LAKE_CONFIG["lakes"]["north_flow"]


def test_moneyflow_hsgt_single(tmp_path, fake_pro, monkeypatch):
    """moneyflow_hsgt 市场级（single 扁平，非 MultiIndex）。"""
    import data.tushare_sync as ts
    fake_pro.set("moneyflow_hsgt", pd.DataFrame({
        "trade_date": ["20240105"], "ggt_ss": [1e9], "ggt_sz": [8e8],
        "sgt_ss": [5e9], "sgt_sz": [4e9], "north_money": [9e9], "south_money": [1.8e9]}))
    monkeypatch.setitem(TUSHARE_DATASETS["moneyflow_hsgt"], "lake", str(tmp_path / "mf_hsgt.parquet"))
    ts.sync_dataset("moneyflow_hsgt", "2024-01-05", "2024-01-05", resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["moneyflow_hsgt"]["lake"])
    assert len(df) == 1 and "north_money" in df.columns  # 扁平 df（single）


# ====================================================================
# Plan A Task 7/8/10：板块概念 / 指数 / 股东解禁停牌
# ====================================================================

def test_concept_ths_daily_registered():
    """Task 7 板块概念数据集配置完备性 + 落湖注册契约。

    Why 守卫完备性：sync_dataset 直接 cfg = TUSHARE_DATASETS[key]，缺任一字段运行时崩在深处。
    concept 是静态概念字典（by=single 扁平），ths_daily 是同花顺板块指数日线（by=date，单日全市场）。
    concept_detail 按「概念 id」分页（pro.concept_detail(id=...)），通用同步器只支持
    symbol/date/single 三种 by，无 by=concept 模式 → 本 task 跳过 concept_detail，
    待框架扩展 by=concept 后再接入（见 notes）。
    """
    # concept：静态字典，single 模式
    assert "concept" in TUSHARE_DATASETS, "concept 未注册"
    cfg_concept = TUSHARE_DATASETS["concept"]
    for f in ("api", "by", "date_col", "symbol_col", "fields", "lake"):
        assert f in cfg_concept, f"concept 配置缺字段 {f}"
    assert cfg_concept["by"] == "single", "concept 应为 single（静态字典，单次拉全量）"
    assert cfg_concept["api"] == "concept"
    # ths_daily：板块指数日线，date 模式（单日全市场板块行情一次返）
    assert "ths_daily" in TUSHARE_DATASETS, "ths_daily 未注册"
    cfg_ths = TUSHARE_DATASETS["ths_daily"]
    for f in ("api", "by", "date_col", "symbol_col", "fields", "lake"):
        assert f in cfg_ths, f"ths_daily 配置缺字段 {f}"
    assert cfg_ths["by"] == "date", "ths_daily 应为 date（单日全市场板块行情）"
    assert cfg_ths["date_col"] == "trade_date", "ths_daily date_col 应为 trade_date"
    assert cfg_ths["symbol_col"] == "ts_code", "ths_daily symbol_col 应为 ts_code（板块指数代码）"
    # concept_detail 必须不在注册表（按概念 id 分页，通用同步器不支持，本 task 跳过）
    assert "concept_detail" not in TUSHARE_DATASETS, \
        "concept_detail 应跳过（按概念 id 分页，需扩展 by=concept，本 task 不接入）"


def test_concept_ths_daily_lake_registered():
    """concept/ths_daily 必须在 LAKE_CONFIG['lakes'] 注册（DataLakeReader 寻址依赖）。

    Why 不复用 sector 湖：sector 湖由 akshare 写申万行业日线（sync_sector_daily），
    ths_daily 是同花顺概念板块指数日线（不同分类口径 + 不同 ts_code 空间），
    混写会互相覆盖。两者独立湖，各走各的 ts_code 空间。
    """
    for key in ("concept", "ths_daily"):
        assert key in LAKE_CONFIG["lakes"], f"{key} 未在 LAKE_CONFIG['lakes'] 注册"
        assert LAKE_CONFIG["lakes"][key] == TUSHARE_DATASETS[key]["lake"], \
            f"{key} LAKE_CONFIG 路径与 TUSHARE_DATASETS 不一致"


def test_ths_daily_by_date(tmp_path, fake_pro, monkeypatch):
    """ths_daily by=date 落 MultiIndex(date, symbol)，symbol 从 ts_code 列取（非文件名）。

    Why 守卫 Task 1 fix：by=date 的 shard 文件名是交易日（20240105.parquet），
    _build_multiindex 必须从 df[symbol_col] 取 symbol——若误从文件名取，symbol 全错成交易日。
    ths_daily 的 ts_code 是板块指数代码（如 885572.TI），非个股代码，但仍走同一 symbol_col 管道。
    """
    import data.tushare_sync as ts
    monkeypatch.setattr(ts, "_trade_days", lambda s, e: ["20240105"])
    fake_pro.set("ths_daily", pd.DataFrame({
        "ts_code": ["885572.TI", "885538.TI"], "trade_date": ["20240105", "20240105"],
        "open": [1000.0, 980.0], "high": [1010.0, 995.0], "low": [998.0, 975.0],
        "close": [1005.0, 990.0], "pre_close": [1000.0, 985.0],
        "vol": [1e6, 8e5], "amount": [1e8, 7e7], "pct_change": [0.5, 0.51]}))
    monkeypatch.setitem(TUSHARE_DATASETS["ths_daily"], "lake",
                        str(tmp_path / "ths_daily.parquet"))
    monkeypatch.setitem(TUSHARE_DATASETS["ths_daily"], "shard_dir",
                        str(tmp_path / "shards_ths_daily"))
    ts.sync_dataset("ths_daily", "2024-01-05", "2024-01-05", resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["ths_daily"]["lake"])
    # 索引契约：MultiIndex(date, symbol)
    assert df.index.names == ["date", "symbol"], "ths_daily 索引名错"
    syms = set(df.index.get_level_values("symbol"))
    # symbol 必须来自 ts_code 列（板块指数代码），绝不能是文件名里的交易日
    assert "20240105" not in syms, "ths_daily symbol 误取自文件名（交易日）"
    assert syms == {"885572.TI", "885538.TI"}, "ths_daily symbol 不在 ts_code 列"


def test_concept_single_flat(tmp_path, fake_pro, monkeypatch):
    """concept by=single 落扁平 DataFrame（静态字典，非时序 MultiIndex）。

    Why 扁平而非 MultiIndex：concept 接口返回概念列表（code+name），无时间维度，
    _sync_single 原样 to_parquet，不重建索引。守卫 single 模式不误走 MultiIndex 管道。
    """
    import data.tushare_sync as ts
    fake_pro.set("concept", pd.DataFrame({
        "code": ["TS2", "TS3"], "name": ["新能源汽车", "锂电池"]}))
    monkeypatch.setitem(TUSHARE_DATASETS["concept"], "lake",
                        str(tmp_path / "concept.parquet"))
    ts.sync_dataset("concept", "2024-01-05", "2024-12-31", resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["concept"]["lake"])
    # 扁平 df：非 MultiIndex，code/name 为普通列
    assert len(df) == 2
    assert "code" in df.columns and "name" in df.columns
    assert df.index.names != ["date", "symbol"], "concept 应为扁平 df，不应是 MultiIndex"


def test_index_datasets_registered():
    """指数三数据集配置完备性 + 落湖注册契约。

    Why 配置层守卫：sync_dataset 直接 cfg = TUSHARE_DATASETS[key]，缺 key/字段立即 KeyError。
    index_daily by=symbol（symbols=指数代码由调用方传，不复用股票 _load_universe）；
    index_weight by=date（symbol_col=con_code 成分股，非指数代码）；
    index_member by=single（单次拉全量，date_col=in_date 纳入日）。
    """
    specs = {
        "index_daily":  ("symbol", "trade_date", "ts_code"),
        "index_weight": ("date",   "trade_date", "con_code"),
        "index_member": ("single", "in_date",    "con_code"),
    }
    for key, (by, date_col, sym) in specs.items():
        assert key in TUSHARE_DATASETS, f"{key} 未注册"
        cfg = TUSHARE_DATASETS[key]
        for f in ("api", "by", "date_col", "symbol_col", "fields", "lake"):
            assert f in cfg, f"{key} 配置缺字段 {f}"
        assert cfg["by"] == by, f"{key} by 应为 {by}"
        assert cfg["date_col"] == date_col, f"{key} date_col 应为 {date_col}"
        assert cfg["symbol_col"] == sym, f"{key} symbol_col 应为 {sym}"
        # lake 路径在 LAKE_CONFIG 注册（DataLakeReader 寻址依赖，单一真相源）
        assert key in LAKE_CONFIG["lakes"], f"{key} 未注册到 LAKE_CONFIG"
        assert LAKE_CONFIG["lakes"][key] == cfg["lake"], \
            f"{key} LAKE_CONFIG 与 TUSHARE_DATASETS lake 路径不一致"


def test_index_daily_by_symbol(tmp_path, fake_pro, monkeypatch):
    """index_daily by=symbol：逐指数代码拉日线，落 MultiIndex(date, ts_code)。

    Why 关键守卫——symbols 来源：index_daily 的 symbols 是指数代码（000300.SH 等），
    必须由调用方显式传，绝不能 fallback 到 _load_universe（那是 A 股股票列表，会把
    000001.SZ 当指数查，全返空）。本测试显式传 symbols=["000300.SH"]，验证不触达
    _load_universe（mock 它抛错以反向证伪）。

    Why 反向证伪 _load_universe：若 sync_dataset 误用 symbols=None 走 _load_universe，
    本测试注入的 _load_universe 会抛 RuntimeError，测试立即红，堵死股票/指数混用污染。
    """
    import data.tushare_sync as ts
    fake_pro.set("index_daily", pd.DataFrame({
        "ts_code": ["000300.SH", "000300.SH"], "trade_date": ["20240105", "20240108"],
        "open": [3800.0, 3850.0], "high": [3850.0, 3880.0], "low": [3780.0, 3820.0],
        "close": [3840.0, 3870.0], "vol": [5e8, 6e8], "amount": [1e10, 1.2e10]}))
    # 反向证伪：若 symbols 误 fallback 到 _load_universe，此 mock 抛错
    def _boom():
        raise AssertionError("index_daily 不应 fallback 到 _load_universe（股票列表）")
    monkeypatch.setattr(ts, "_load_universe", _boom)
    monkeypatch.setitem(TUSHARE_DATASETS["index_daily"], "lake",
                        str(tmp_path / "index_daily.parquet"))
    monkeypatch.setitem(TUSHARE_DATASETS["index_daily"], "shard_dir",
                        str(tmp_path / "shards_index_daily"))
    ts.sync_dataset("index_daily", "2024-01-05", "2024-01-31",
                    symbols=["000300.SH"], resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["index_daily"]["lake"])
    # 硬契约：MultiIndex(date, symbol)，DataLakeReader 双向切片依赖
    assert df.index.names == ["date", "symbol"]
    assert set(df.index.get_level_values("symbol")) == {"000300.SH"}, \
        "index_daily symbol 必须是指数代码，不能是股票代码或交易日"


def test_index_weight_by_date(tmp_path, fake_pro, monkeypatch):
    """index_weight by=date：逐交易日拉成分权重，symbol 从 con_code 列取（非文件名）。

    Why 守卫 Task 1 fix（by=date symbol 来源）：shard 文件名是交易日（20240105.parquet），
    _build_multiindex 必须从 df[con_code] 取 symbol。若误从文件名取，symbol 全错成交易日，
    污染指数成分权重的跨标的切片语义。
    """
    import data.tushare_sync as ts
    monkeypatch.setattr(ts, "_trade_days", lambda s, e: ["20240131"])
    fake_pro.set("index_weight", pd.DataFrame({
        "index_code": ["000300.SH", "000300.SH"], "con_code": ["000001.SZ", "600519.SH"],
        "trade_date": ["20240131", "20240131"], "weight": [0.85, 5.21]}))
    monkeypatch.setitem(TUSHARE_DATASETS["index_weight"], "lake",
                        str(tmp_path / "index_weight.parquet"))
    monkeypatch.setitem(TUSHARE_DATASETS["index_weight"], "shard_dir",
                        str(tmp_path / "shards_index_weight"))
    ts.sync_dataset("index_weight", "2024-01-31", "2024-01-31", resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["index_weight"]["lake"])
    assert df.index.names == ["date", "symbol"]
    syms = set(df.index.get_level_values("symbol"))
    assert "20240131" not in syms, "index_weight symbol 误取自文件名（交易日）"
    # symbol 必须来自 con_code 列（成分股代码），非 index_code（指数代码）
    assert syms == {"000001.SZ", "600519.SH"}, f"index_weight symbol 应为成分股代码，实际 {syms}"


def test_index_member_single(tmp_path, fake_pro, monkeypatch):
    """index_member by=single：单次拉成分进出记录，落扁平 DataFrame（非 MultiIndex）。

    Why 单次模式契约：by=single 走 _sync_single，原样落盘不重建时间索引。
    ⚠️ index_member 接口单次最多返 100 行，全量成分历史需逐 index_code 循环补——
    本测试验证最小可用口径（首页 100 行扁平落盘），全量补数属后续优化。
    """
    import data.tushare_sync as ts
    fake_pro.set("index_member", pd.DataFrame({
        "index_code": ["000300.SH", "000300.SH"], "con_code": ["000001.SZ", "600519.SH"],
        "con_name": ["平安银行", "贵州茅台"], "in_date": ["20230612", "20190617"],
        "out_date": ["", ""]}))
    monkeypatch.setitem(TUSHARE_DATASETS["index_member"], "lake",
                        str(tmp_path / "index_member.parquet"))
    ts.sync_dataset("index_member", "2024-01-01", "2024-12-31", resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["index_member"]["lake"])
    # 扁平 df（single 模式不重建 MultiIndex），关键字段在列里
    assert len(df) == 2
    assert {"index_code", "con_code", "con_name", "in_date", "out_date"}.issubset(df.columns)
    assert "平安银行" in df["con_name"].tolist()


def test_a10_holders_use_ann_date_not_end_date():
    """A10 股东类前视红线：top10_holders/top10_floatholders 必须用 ann_date 索引。

    Why 机器化守卫（brief Step 1 草稿曾误写 date_col=end_date）：
      end_date 是「报告期末」（如 20231231），而前十大股东名单要等到季报/年报
      实际公告日（ann_date，如 20240430）市场才能感知。用 end_date 索引等于在
      公告前数月就已知股东筹码结构，回测出现前视偏差。本测试把红线钉死，PR review
      漏一眼也守得住。
    """
    for key in ("top10_holders", "top10_floatholders"):
        assert key in TUSHARE_DATASETS, f"{key} 未在 TUSHARE_DATASETS 注册"
        assert TUSHARE_DATASETS[key]["date_col"] == "ann_date", \
            f"{key} 必须用 ann_date（公告日）索引，禁用 end_date（报告期末，前视偏差）"
        assert TUSHARE_DATASETS[key]["by"] == "symbol", \
            f"{key} 分页模式应为 symbol（逐标的拉全历史）"


def test_a10_share_float_suspend_d_use_ann_date():
    """A10 解禁/停牌前视红线：share_float/suspend_d 均用 ann_date 索引（by=date）。

    Why 钉死 ann_date：
      - share_float 的 float_date（实际解禁日）可能晚于公告日 ann_date，用 ann_date
        索引保证回测只读到「市场已知」的解禁信息；
      - suspend_d 的 suspend_date（实际停牌日）同理，公告日 ann_date 是市场最早能
        预知停牌的时点。两者都用 ann_date 与财报/股东保持同一前视防线，时间轴统一。
    """
    for key in ("share_float", "suspend_d"):
        assert key in TUSHARE_DATASETS, f"{key} 未在 TUSHARE_DATASETS 注册"
        assert TUSHARE_DATASETS[key]["by"] == "date", f"{key} 分页模式应为 date"
        assert TUSHARE_DATASETS[key]["date_col"] == "ann_date", \
            f"{key} 必须用 ann_date（公告日）索引，禁用 float_date/suspend_date（前视偏差）"
        assert TUSHARE_DATASETS[key]["symbol_col"] == "ts_code", \
            f"{key} symbol_col 应为 ts_code（by=date 从该列取 symbol，非文件名）"


def test_a10_all_four_datasets_registered():
    """A10 四个数据集配置完备性 + 落湖注册契约（单一真相源）。

    Why 守卫完备性：sync_dataset 直接 cfg = TUSHARE_DATASETS[key]，缺任一 key 立即
    KeyError；缺 api/by/date_col/symbol_col/fields/lake 任一字段，运行时崩在更深处。
    配置层把契约钉死。落湖路径必须在 LAKE_CONFIG['lakes'] 注册且与 TUSHARE_DATASETS
    一致（DataLakeReader 按 lakes[key] 寻址，两处分叉会导致寻址错湖）。
    """
    required_fields = ("api", "by", "date_col", "symbol_col", "fields", "lake")
    for key in ("top10_holders", "top10_floatholders", "share_float", "suspend_d"):
        assert key in TUSHARE_DATASETS, f"{key} 未在 TUSHARE_DATASETS 注册"
        cfg = TUSHARE_DATASETS[key]
        for f in required_fields:
            assert f in cfg, f"{key} 配置缺字段 {f}"
        # 四个湖均独立新增（不复用 dragon_list/north_flow/sector 等）
        assert key in LAKE_CONFIG["lakes"], f"{key} 未在 LAKE_CONFIG['lakes'] 注册"
        assert LAKE_CONFIG["lakes"][key] == cfg["lake"], \
            f"{key} 的 LAKE_CONFIG 路径与 TUSHARE_DATASETS 不一致"


def test_a10_suspend_d_uses_official_fields():
    """suspend_d 字段名以 Tushare Pro 官方为准：ann_reason/reason_type。

    Why 钉死：brief Step 1 草稿写的 suspend_reason/resume_reason 是旧版（已停用）字段，
    Pro 版官方字段是 ann_reason（停复牌原因说明）+ reason_type（原因类别）。配置误用
    旧字段名会导致 _fetch_with_guard 拉回空列，落湖后原因列全 NaN。本测试机器化守卫。
    """
    fields = TUSHARE_DATASETS["suspend_d"]["fields"].split(",")
    fields = [f.strip() for f in fields]
    assert "ann_reason" in fields, "suspend_d 应使用 Pro 官方字段 ann_reason（非旧版 suspend_reason）"
    assert "reason_type" in fields, "suspend_d 应使用 Pro 官方字段 reason_type"
    # 同时保留 suspend_date/resume_date（实际停/复牌日，作为数据列落湖，仅不作时间索引）
    assert "suspend_date" in fields and "resume_date" in fields


def test_a10_top10_holders_by_symbol(tmp_path, fake_pro, monkeypatch):
    """top10_holders by=symbol 端到端：落 MultiIndex(date, symbol)，date 来自 ann_date。

    Why 端到端契约：经 sync_dataset 后必须产出 MultiIndex(date, symbol) parquet，
    这是 DataLakeReader 按日期区间 + 标的列表双向切片的硬契约。date 必须来自
    ann_date（公告日）而非 end_date——若 _cleanse/date_col 配置回退到 end_date，
    本测试的 ann_date=20240430 会被 end_date=20231231 覆盖，索引日期错位即暴露。

    Why 同时重定向 shard_dir：sync_dataset 的 shard 默认落共享 data_lake/shards/<key>/，
    仅重定向 lake 输出会留下 shard 残留污染后续测试。shard_dir 一并指向 tmp_path 子目录，
    测试结束自动销毁，零残留。
    """
    fake_pro.set("top10_holders", pd.DataFrame({
        "ts_code": ["000001.SZ", "000001.SZ"],
        "ann_date": ["20240430", "20240430"],
        "end_date": ["20231231", "20231231"],
        "holder_name": ["中国平安保险", "香港中央结算"],
        "hold_amount": [1.8e10, 5e9],
        "hold_ratio": [49.5, 13.8]}))
    from data.tushare_sync import sync_dataset
    key = "top10_holders"
    monkeypatch.setitem(TUSHARE_DATASETS[key], "lake", str(tmp_path / f"{key}.parquet"))
    monkeypatch.setitem(TUSHARE_DATASETS[key], "shard_dir", str(tmp_path / f"shards_{key}"))
    sync_dataset(key, "2024-01-01", "2024-12-31", symbols=["000001.SZ"], resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS[key]["lake"])
    # 索引契约
    assert df.index.names == ["date", "symbol"], f"{key} 索引名错误"
    # date 必须来自 ann_date（20240430），证明未回退到 end_date（20231231）
    dates = set(str(d.date()) for d in df.index.get_level_values("date"))
    assert "2024-04-30" in dates, f"{key} date 应来自 ann_date=20240430（防前视），实际 {dates}"
    assert "2023-12-31" not in dates, f"{key} date 不应来自 end_date（前视偏差）"
    # symbol 来自文件名（by=symbol shard），保留真实标的码
    assert set(df.index.get_level_values("symbol")) == {"000001.SZ"}
    assert len(df) == 2  # 两条股东记录


def test_a10_share_float_suspend_d_by_date(tmp_path, fake_pro, monkeypatch):
    """share_float/suspend_d by=date 端到端：落 MultiIndex，symbol 从 ts_code 列取。

    Why 守卫 Task 1 fix：by=date 的 shard 文件名是交易日（20240105.parquet），
    _build_multiindex 必须从 df[ts_code] 取 symbol——若误从文件名取，symbol 全错成交易日。
    同时验证 date_col=ann_date 生效：share_float 的 ann_date=20240120（公告）而非
    float_date=20240201（实际解禁），suspend_d 的 ann_date=20240115（公告）而非
    suspend_date=20240120（实际停牌），证明前视防线在 by=date 路径同样生效。
    """
    import data.tushare_sync as ts
    monkeypatch.setattr(ts, "_trade_days", lambda s, e: ["20240105"])

    # share_float：单日全市场解禁公告，ann_date 公告日 ≠ float_date 解禁日
    fake_pro.set("share_float", pd.DataFrame({
        "ts_code": ["000001.SZ", "600000.SH"],
        "ann_date": ["20240120", "20240120"],
        "float_share": [5e8, 3e8],
        "float_date": ["20240201", "20240205"],
        "float_share_share": [5e8, 3e8]}))
    monkeypatch.setitem(TUSHARE_DATASETS["share_float"], "lake", str(tmp_path / "share_float.parquet"))
    monkeypatch.setitem(TUSHARE_DATASETS["share_float"], "shard_dir", str(tmp_path / "shards_sf"))
    ts.sync_dataset("share_float", "2024-01-05", "2024-01-05", resume=False)
    df_sf = pd.read_parquet(TUSHARE_DATASETS["share_float"]["lake"])
    assert df_sf.index.names == ["date", "symbol"], "share_float 索引名错"
    syms = set(df_sf.index.get_level_values("symbol"))
    assert syms == {"000001.SZ", "600000.SH"}, f"share_float symbol 应来自 ts_code 列，实际 {syms}"
    assert "20240105" not in syms, "share_float symbol 误取自文件名（交易日）"
    # date 来自 ann_date（20240120 公告），证明未用 float_date（20240201 实际解禁）
    sf_dates = set(str(d.date()) for d in df_sf.index.get_level_values("date"))
    assert "2024-01-20" in sf_dates, f"share_float date 应来自 ann_date=20240120，实际 {sf_dates}"
    assert "2024-02-01" not in sf_dates, "share_float date 不应来自 float_date（前视偏差）"

    # suspend_d：单日全市场停复牌，ann_date 公告日 ≠ suspend_date 实际停牌日
    fake_pro.set("suspend_d", pd.DataFrame({
        "ts_code": ["000002.SZ"],
        "ann_date": ["20240115"],
        "suspend_date": ["20240120"],
        "resume_date": ["20240125"],
        "ann_reason": ["重大资产重组"],
        "reason_type": ["S"]}))
    monkeypatch.setitem(TUSHARE_DATASETS["suspend_d"], "lake", str(tmp_path / "suspend_d.parquet"))
    monkeypatch.setitem(TUSHARE_DATASETS["suspend_d"], "shard_dir", str(tmp_path / "shards_sd"))
    ts.sync_dataset("suspend_d", "2024-01-05", "2024-01-05", resume=False)
    df_sd = pd.read_parquet(TUSHARE_DATASETS["suspend_d"]["lake"])
    assert df_sd.index.names == ["date", "symbol"], "suspend_d 索引名错"
    assert set(df_sd.index.get_level_values("symbol")) == {"000002.SZ"}, \
        "suspend_d symbol 应来自 ts_code 列"
    # date 来自 ann_date（20240115 公告），证明未用 suspend_date（20240120 实际停牌）
    sd_dates = set(str(d.date()) for d in df_sd.index.get_level_values("date"))
    assert "2024-01-15" in sd_dates, f"suspend_d date 应来自 ann_date=20240115，实际 {sd_dates}"
    assert "2024-01-20" not in sd_dates, "suspend_d date 不应来自 suspend_date（前视偏差）"
