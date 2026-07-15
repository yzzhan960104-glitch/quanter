# -*- coding: utf-8 -*-
"""ETF 专题类 Tushare 数据集配置 + 落湖契约测试（Plan B Task 1-6）。

设计意图（反黑盒 + 反前视偏差 + 列名归一）：
- **前视红线**：fund_portfolio（ETF 持仓）必须用 ann_date（公告日）索引，绝不用
  end_date（报告期）—— 持仓数据公告滞后，end_date 早于 ann_date 数月，用 end_date
  索引会在报告期内提前看到持仓构成。test_fund_portfolio_by_symbol_ann_date 守卫。
- **列名归一（rename 机制）**：fund_daily 原始返 vol 列（与股票日线 volume 分叉），
  配置 rename={'vol':'volume'} 由通用同步器在落 shard 前应用，确保 etf_daily 湖与
  a_shares_daily 湖列名一致，跨湖因子计算免分支。test_fund_daily_by_symbol_vol_to_volume 守卫。
- **标的池纯净度**：_load_etf_universe 必须用 market='E' 过滤场内基金（排除场外 O），
  否则 fund_daily 等会拉到场外基金污染 ETF 专题湖。test_load_etf_universe_filters_market_e 守卫。

fixture 复制说明：本文件顶部完整复制 test_tushare_datasets_stock.py 的 autouse
_isolate_tushare_registry fixture（深拷贝还原全局注册表）+ fake_pro fixture
（双 patch get_pro + rate_limiter/breaker 短路），因 pytest fixture 是文件级
作用域，不能跨文件直接复用（conftest 未抽取）。
"""
import copy
import pandas as pd
import pytest

from config import TUSHARE_DATASETS, LAKE_CONFIG


@pytest.fixture(autouse=True)
def _isolate_tushare_registry():
    """深拷贝 TUSHARE_DATASETS + LAKE_CONFIG['lakes']，测试后还原原对象引用。

    Why autouse 深拷贝（与 test_tushare_datasets_stock.py 同手法）：本文件的测试会
    就地覆盖全局 TUSHARE_DATASETS[key]['lake']（重定向到 tmp_path）。若不还原，全局
    注册表会被污染——后续测试拿到指向 tmp_path 的 lake 路径（tmp_path 测试结束即
    销毁），导致跨测试顺序依赖 + 真实 sync 脚本写到错误路径。
    手法：clear()+update(saved) 保留原 dict 对象身份（其他模块 from config import 的
    引用不变），嵌套 lakes 子 dict 由 deepcopy 兜底。
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

    Why __getattr__：pro 接口方法（pro.fund_basic / pro.fund_daily ...）在运行时由
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

    Why 双重 patch get_pro（关键防漏网，与 stock 文件同手法）：data/tushare_sync.py
    顶部 `from data._tushare_compat import get_pro` 把函数对象绑到 tushare_sync 模块的
    全局命名空间，仅 patch _tushare_compat.get_pro 不改变 tushare_sync.get_pro 的绑定。
    本 fixture 同时 patch 两处绑定，保证替身真正短路。
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


def test_etf_datasets_registered():
    """Plan B 五个 ETF 数据集配置完备性 + 前视红线守卫。

    Why 机器化守卫：sync_dataset 直接 cfg = TUSHARE_DATASETS[key]，缺任一 key/字段立即 KeyError。
    前视红线（fund_portfolio 必须 ann_date）单独钉死——PR review 漏一眼也守得住。
    """
    required = ("fund_basic", "fund_daily", "fund_nav", "fund_portfolio", "fund_share")
    required_fields = ("api", "by", "date_col", "symbol_col", "fields", "lake")
    by_symbol_keys = ("fund_daily", "fund_nav", "fund_portfolio", "fund_share")
    for key in required:
        assert key in TUSHARE_DATASETS, f"{key} 未在 TUSHARE_DATASETS 注册"
        cfg = TUSHARE_DATASETS[key]
        for f in required_fields:
            assert f in cfg, f"{key} 配置缺字段 {f}"
    # fund_basic single（列表快照，不分页）；其余 4 个 by=symbol（逐标的）
    assert TUSHARE_DATASETS["fund_basic"]["by"] == "single", "fund_basic 应为 single 模式"
    for key in by_symbol_keys:
        assert TUSHARE_DATASETS[key]["by"] == "symbol", f"{key} 应为 symbol 模式"
    # 前视红线：fund_portfolio 必须 ann_date（禁 end_date）
    assert TUSHARE_DATASETS["fund_portfolio"]["date_col"] == "ann_date", \
        "fund_portfolio 必须用 ann_date（公告日）索引，禁用 end_date（报告期，前视偏差）"


def test_etf_lakes_registered():
    """五个 ETF 湖在 LAKE_CONFIG['lakes'] 注册，且路径与 TUSHARE_DATASETS 一致（单一真相源）。

    Why 守卫：DataLakeReader 按 LAKE_CONFIG['lakes'][key] 寻址 parquet，路径分叉会导致
    reader 读空文件而 sync 写到另一处。两处路径必须钉死一致。
    """
    for key in ("fund_basic", "fund_daily", "fund_nav", "fund_portfolio", "fund_share"):
        assert key in LAKE_CONFIG["lakes"], f"{key} 未在 LAKE_CONFIG['lakes'] 注册"
        assert LAKE_CONFIG["lakes"][key] == TUSHARE_DATASETS[key]["lake"], \
            f"{key} 的 LAKE_CONFIG 路径与 TUSHARE_DATASETS 不一致"


def test_fund_basic_single(tmp_path, fake_pro, monkeypatch):
    """fund_basic single 模式落扁平 df（场内基金列表快照，非 MultiIndex）。

    Why 端到端 single 契约：_sync_single 直接 to_parquet 原样落盘，不重建时间索引，
    落湖是扁平 DataFrame（ts_code/name/market 列保留），区别于 by=symbol/date 的 MultiIndex。

    ⚠️ quick 批订正：fund_basic 配置加 params={'market':'E'}（实测 market='EFT'=0 行，E 才正确），
    _sync_single 把 params 合并进 kwargs 传 API。fake_pro 忽略 kwargs 返固定 df，故测试数据
    market 列用 'E'（与新 params 语义一致），验证落盘扁平结构不受 params 影响。
    """
    import data.tushare_sync as ts
    fake_pro.set("fund_basic", pd.DataFrame({
        "ts_code": ["510300.SH", "510050.SH"],
        "name": ["沪深300ETF", "50ETF"],
        "market": ["E", "E"],
        "management": ["华泰柏瑞", "华夏"],
        "custodian": ["招商银行", "工商银行"],
        "found_date": ["20120504", "20110509"],
        "list_date": ["20120528", "20110601"],
        "issue_date": ["", ""], "delist_date": ["", ""]}))
    monkeypatch.setitem(TUSHARE_DATASETS["fund_basic"], "lake",
                        str(tmp_path / "fund_basic.parquet"))
    ts.sync_dataset("fund_basic", "2024-01-01", "2024-12-31", resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["fund_basic"]["lake"])
    assert len(df) == 2, "fund_basic 行数错误"
    assert "ts_code" in df.columns and "name" in df.columns, "fund_basic 扁平列缺失"
    # params market='E' 守卫：配置层必须带 params（实测 market='EFT'=0 行污染标的池）
    assert TUSHARE_DATASETS["fund_basic"].get("params") == {"market": "E"}, \
        "fund_basic 必须配 params market='E'（market='EFT' 返 0 行，E 才是场内基金真实码）"


def test_fund_daily_by_symbol_vol_to_volume(tmp_path, fake_pro, monkeypatch):
    """fund_daily by=symbol 落 MultiIndex(date, symbol)，且 vol 列 rename 为 volume（与股票日线湖对齐）。

    Why 端到端 + rename 守卫：fund_daily 原始返回 vol 列，配置 rename={'vol':'volume'} 后
    通用同步器在落 shard 前应用 rename，落湖列名为 volume。若框架漏接 rename，本测试断言
    'volume' in df.columns 立即红——确保 ETF 日线与 a_shares_daily 列名一致，跨湖因子计算免分支。
    """
    import data.tushare_sync as ts
    fake_pro.set("fund_daily", pd.DataFrame({
        "ts_code": ["510300.SH", "510300.SH"],
        "trade_date": ["20240105", "20240108"],
        "open": [4.1, 4.2], "high": [4.2, 4.3], "low": [4.0, 4.1],
        "close": [4.15, 4.25], "vol": [1e7, 1.1e7], "amount": [4.2e7, 4.6e7]}))
    monkeypatch.setitem(TUSHARE_DATASETS["fund_daily"], "lake",
                        str(tmp_path / "etf_daily.parquet"))
    monkeypatch.setitem(TUSHARE_DATASETS["fund_daily"], "shard_dir",
                        str(tmp_path / "shards_fund_daily"))
    ts.sync_dataset("fund_daily", "2024-01-05", "2024-01-10",
                    symbols=["510300.SH"], resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["fund_daily"]["lake"])
    assert df.index.names == ["date", "symbol"], "fund_daily 索引名错误"
    assert len(df) == 2, "fund_daily 行数错误"
    # vol→volume rename 守卫（关键列名归一）
    assert "volume" in df.columns, "fund_daily vol 未 rename 为 volume"
    assert "vol" not in df.columns, "fund_daily 仍残留 vol 列（rename 未生效）"
    assert "close" in df.columns


def test_fund_nav_by_symbol(tmp_path, fake_pro, monkeypatch):
    """fund_nav by=symbol 落 MultiIndex(date, symbol)，date_col=nav_date。

    ⚠️ 真实列对齐：删幻觉 accum_nav_rate，真实列含 ann_date/accum_div/adj_nav。
    """
    import data.tushare_sync as ts
    fake_pro.set("fund_nav", pd.DataFrame({
        "ts_code": ["510300.SH", "510300.SH"],
        "ann_date": ["20240105", "20240108"],
        "nav_date": ["20240105", "20240108"],
        "unit_nav": [4.15, 4.25], "accum_nav": [4.15, 4.25],
        "accum_div": [0.0, 0.024], "adj_nav": [4.15, 4.27]}))
    monkeypatch.setitem(TUSHARE_DATASETS["fund_nav"], "lake",
                        str(tmp_path / "etf_nav.parquet"))
    monkeypatch.setitem(TUSHARE_DATASETS["fund_nav"], "shard_dir",
                        str(tmp_path / "shards_fund_nav"))
    ts.sync_dataset("fund_nav", "2024-01-05", "2024-01-10",
                    symbols=["510300.SH"], resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["fund_nav"]["lake"])
    assert df.index.names == ["date", "symbol"], "fund_nav 索引名错误"
    assert len(df) == 2 and "unit_nav" in df.columns


def test_fund_portfolio_by_symbol_ann_date(tmp_path, fake_pro, monkeypatch):
    """fund_portfolio by=symbol 落 MultiIndex(date, symbol)，date_col=ann_date（前视红线）。

    Why 端到端守卫 ann_date：落湖后 date 索引必须来自 ann_date（公告日），绝不能来自
    end_date（报告期）。本测试注入 ann_date=20240330/end_date=20231231 两列，落湖 date
    索引应为 2024-03-30（公告日）而非 2023-12-31（报告期）——若误用 end_date 索引立即红。

    ⚠️ 真实列对齐：删幻觉 name/stk_value/stk_value_ratio，真实列含 mkv/stk_mkv_ratio/
    stk_float_ratio（symbol 是重仓股代码）。
    """
    import data.tushare_sync as ts
    fake_pro.set("fund_portfolio", pd.DataFrame({
        "ts_code": ["510300.SH"], "ann_date": ["20240330"], "end_date": ["20231231"],
        "symbol": ["600519.SH"], "mkv": [1.8e9], "amount": [1e6],
        "stk_mkv_ratio": [6.5], "stk_float_ratio": [3.2]}))
    monkeypatch.setitem(TUSHARE_DATASETS["fund_portfolio"], "lake",
                        str(tmp_path / "etf_portfolio.parquet"))
    monkeypatch.setitem(TUSHARE_DATASETS["fund_portfolio"], "shard_dir",
                        str(tmp_path / "shards_fund_portfolio"))
    ts.sync_dataset("fund_portfolio", "2024-01-01", "2024-12-31",
                    symbols=["510300.SH"], resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["fund_portfolio"]["lake"])
    assert df.index.names == ["date", "symbol"], "fund_portfolio 索引名错误"
    # date 索引取自 ann_date（公告日 20240330），非 end_date（报告期 20231231）
    dates = df.index.get_level_values("date")
    assert pd.Timestamp("2024-03-30") in dates, "fund_portfolio date 索引未取自 ann_date（前视偏差风险）"
    assert pd.Timestamp("2023-12-31") not in dates, "fund_portfolio date 误用 end_date（前视偏差）"


def test_fund_share_by_symbol(tmp_path, fake_pro, monkeypatch):
    """fund_share by=symbol 落 MultiIndex(date, symbol)，date_col=trade_date。

    ⚠️ 真实列对齐（结构重写）：删幻觉 share_unissue/total_share/float_share，真实列为
    fd_share（基金份额）/ fund_type / market。
    """
    import data.tushare_sync as ts
    fake_pro.set("fund_share", pd.DataFrame({
        "ts_code": ["510300.SH", "510300.SH"],
        "trade_date": ["20240105", "20240108"],
        "fd_share": [5e9, 5.1e9], "fund_type": ["ETF", "ETF"], "market": ["E", "E"]}))
    monkeypatch.setitem(TUSHARE_DATASETS["fund_share"], "lake",
                        str(tmp_path / "etf_share.parquet"))
    monkeypatch.setitem(TUSHARE_DATASETS["fund_share"], "shard_dir",
                        str(tmp_path / "shards_fund_share"))
    ts.sync_dataset("fund_share", "2024-01-05", "2024-01-10",
                    symbols=["510300.SH"], resume=False)
    df = pd.read_parquet(TUSHARE_DATASETS["fund_share"]["lake"])
    assert df.index.names == ["date", "symbol"], "fund_share 索引名错误"
    assert len(df) == 2 and "fd_share" in df.columns


def test_load_etf_universe_filters_market_e(fake_pro, monkeypatch):
    """_load_etf_universe 仅返回 market='E' 的场内基金（排除场外基金 O）。

    Why 守卫标的池纯净度：fund_basic 同时含场内基金（market=E）与场外基金（market=O，13827 只），
    _load_etf_universe 必须用 market='E' 过滤，否则后续 fund_daily/fund_nav 会拉到场外
    基金（无日线行情或字段不符），污染 ETF 专题湖。

    ⚠️ quick 批订正（EFT→E 事实修正）：实测 fund_basic(market='EFT') 返 **0 行**（EFT 不是
    Tushare 真实 market 码），market='E' 才返场内基金。_load_etf_universe 改用 market='E'。
    """
    from data.tushare_sync import _load_etf_universe
    fake_pro.set("fund_basic", pd.DataFrame({
        "ts_code": ["510300.SH", "510050.SH", "000001.OF"],
        "name": ["沪深300ETF", "50ETF", "华夏成长"],
        "market": ["E", "E", "O"],  # 第三只是场外基金，须排除
        "management": ["华泰柏瑞", "华夏", "华夏"]}))
    codes = _load_etf_universe()
    assert "510300.SH" in codes and "510050.SH" in codes
    assert "000001.OF" not in codes, "场外基金（market=O）应被 _load_etf_universe 排除"
