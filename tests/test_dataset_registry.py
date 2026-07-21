# -*- coding: utf-8 -*-
"""数据集资产注册表对齐测试（Plan A Task 11 + Plan C Task 6：注册表对齐 + 端到端验证）。

设计意图（元数据层单一真相源）：
- **前端 DataLakeView 反射契约**：前端表格/下拉框经 /api/v1/data/datasets 反射 DATASET_REGISTRY，
  绝不在前端硬编码数据集名。本测试把「Tushare 新湖必须注册到 DATASET_REGISTRY 且 source=Tushare」
  钉死在配置层——任一新湖漏注册，前端就看不到该资产，宏观切源/数据湖可视直接断档。
- **剔除 concept_detail**：brief 草稿 new_lakes 含 concept_detail，但该数据集按「概念 id」分页
  （pro.concept_detail(id=...)），通用同步器只支持 symbol/date/single 三种 by，无 by=concept 模式，
  Plan A Task 7 已决策跳过。TUSHARE_DATASETS 实际不含 concept_detail，本测试同样不断言它。
- **复用湖判断**：top_list 复用 dragon_list 湖、hsgt_top10 复用 north_flow 湖（切 Tushare 替代 akshare），
  这两个 LAKE_CONFIG key 已存在（不重复加），但 DATASET_REGISTRY 仍需补 source=Tushare 元信息
  （前端要能看到「龙虎榜/北向资金」现在由 Tushare 生产，而非仍标 AKShare）。
- **Plan C Task 6 新增（宏观收尾）**：DATASET_REGISTRY["macro"]（macro_credit 湖，CreditRegime 输入）
  source 从 AKShare 切 Tushare（主源 cn_m + akshare 社融 fallback）；并新增 8 个原始宏观指标数据集
  （cn_cpi/cn_ppi/cn_gdp/cn_pmi/shibor/shibor_quote/szse_daily/sse_daily）的 DATASET_REGISTRY 条目，
  让前端 DataLakeView 可反射这些 Tushare 宏观资产。端到端测试验证 sync_macro 落湖 →
  CreditRegime.compute 返 1/0/-1（不抛、列名契约成立）。
"""
import pandas as pd
import pytest

from config import LAKE_CONFIG, DATASET_REGISTRY, TUSHARE_DATASETS

# data_service._parquet_path 是前端 list_datasets 的湖寻址热路径（Finding 1 修复对象）。
# 用函数真实返回值断言（非 monkeypatch），覆盖复用湖 fallback 落点。
from server.services.data_service import _parquet_path


# ============================================================================
# 股票类 Tushare 新湖清单（剔除 concept_detail——按概念 id 分页不可行，Task 7 已跳过）
# ============================================================================
# 单一真相 = config.py 里实际的 TUSHARE_DATASETS 的股票类 key。
# brief 给的列表含 concept_detail（错误）且不完整，此处以实际 TUSHARE_DATASETS 为准。
STOCK_TUSHARE_KEYS = [
    # 财报 6（fina_income/balance/cashflow/forecast/express/dividend）
    "fina_income", "fina_balance", "fina_cashflow", "forecast", "express", "dividend",
    # 资金流 / 龙虎榜 3（top_list 复用 dragon_list 湖）
    "moneyflow", "top_list", "top_inst",
    # 融资融券 3
    "margin", "margin_detail", "margin_secs",
    # 北向资金 2（hsgt_top10 复用 north_flow 湖）
    "hsgt_top10", "moneyflow_hsgt",
    # 板块 / 概念 2（concept_detail 跳过：按概念 id 分页，通用同步器不支持）
    "concept", "ths_daily",
    # 指数 3
    "index_daily", "index_weight", "index_member",
    # 股东 / 解禁 / 停牌 4
    "top10_holders", "top10_floatholders", "share_float", "suspend_d",
    # 特色筹码 1（300/分独立通道）
    "cyq_perf",
]


def test_concept_detail_not_in_tushare_datasets():
    """concept_detail 必须不在 TUSHARE_DATASETS（按概念 id 分页，本 task 跳过）。

    Why 钉死：brief 草稿 new_lakes 曾含 concept_detail，但该接口需 pro.concept_detail(id=...)
    逐概念分页，通用同步器只支持 symbol/date/single 三种 by，无 by=concept 模式。Task 7 已决策
    跳过，TUSHARE_DATASETS 实际不含此 key。本测试守卫「不误注册一个无法同步的数据集」。
    """
    assert "concept_detail" not in TUSHARE_DATASETS, \
        "concept_detail 应跳过（按概念 id 分页，通用同步器不支持 by=concept）"


def test_new_stock_lakes_registered():
    """Plan A 股票类新湖全部注册到 LAKE_CONFIG + DATASET_REGISTRY（前端可视反射契约）。

    What：每个股票类 Tushare 数据集的「落湖 key」必须在 LAKE_CONFIG["lakes"] 注册
    （DataLakeReader 寻址靠它），且在 DATASET_REGISTRY 注册（前端 DataLakeView 反射靠它）。

    Why 机器化守卫：DATASET_REGISTRY 是前端数据湖可视的单一真相源，任一新湖漏注册，
    前端表格就缺这一行，宏观切源/资产盘点直接断档。PR review 漏一眼也守得住。

    Why 复用湖也要注册 DATASET_REGISTRY：top_list 复用 dragon_list 湖、hsgt_top10 复用
    north_flow 湖，LAKE_CONFIG 的 lake key 已存在（不重复加），但 DATASET_REGISTRY 必须补
    source=Tushare 元信息——前端要能区分「龙虎榜现在由 Tushare 生产」与「仍标 AKShare」。
    复用湖的 DATASET_REGISTRY key 用数据集名（top_list/hsgt_top10），与 LAKE_CONFIG 的
    湖 key（dragon_list/north_flow）解耦，两个注册表语义不同（资产 vs 寻址）。
    """
    for ds_key in STOCK_TUSHARE_KEYS:
        # 1) 数据集必须在 TUSHARE_DATASETS 注册（配置完备性，sync_dataset 依赖）
        assert ds_key in TUSHARE_DATASETS, \
            f"{ds_key} 未在 TUSHARE_DATASETS 注册（配置层缺失）"

        # _unavailable 数据集（代理无接口，如 top_list/hsgt_top10）不落湖，跳过 lake 注册检查
        if TUSHARE_DATASETS[ds_key].get("_unavailable"):
            continue

        # 2) 落湖路径必须在 LAKE_CONFIG["lakes"] 注册（DataLakeReader 寻址依赖）
        #    反查：TUSHARE_DATASETS[ds_key]["lake"] 路径应能在 LAKE_CONFIG["lakes"].values() 找到
        lake_path = TUSHARE_DATASETS[ds_key]["lake"]
        assert lake_path in LAKE_CONFIG["lakes"].values(), \
            f"{ds_key} 的 lake 路径 {lake_path} 未注册到 LAKE_CONFIG['lakes']（DataLakeReader 无法寻址）"

        # 3) DATASET_REGISTRY 必须有该数据集的元信息（前端 DataLakeView 反射依赖）
        assert ds_key in DATASET_REGISTRY, \
            f"{ds_key} 未在 DATASET_REGISTRY 注册（前端 DataLakeView 看不到该资产）"

        # 4) source 必须标 Tushare（区分新旧源，前端 macro 切源靠此字段）
        assert DATASET_REGISTRY[ds_key].get("source") == "Tushare", \
            f"{ds_key} source 应为 Tushare，实际 {DATASET_REGISTRY[ds_key].get('source')!r}"


def test_fina_income_source_is_tushare():
    """fina_income source 必须 == Tushare（brief 显式断言，宏指标切源守卫）。

    Why 独立钉死：fina_income 是利润表（财报核心），前端 macro/credit 切源到 Tushare 时
    依赖此字段判断数据源。brief Step 1 显式要求 DATASET_REGISTRY.get("fina_income", {}).get("source") == "Tushare"。
    """
    assert DATASET_REGISTRY.get("fina_income", {}).get("source") == "Tushare", \
        "fina_income source 必须为 Tushare（财报利润表切源守卫）"


def test_dataset_registry_has_required_fields():
    """DATASET_REGISTRY 每条记录字段完备（前端反射契约：source/market/granularity/script/freshness_hours）。

    Why 守卫完备性：前端 DataLakeView 表格的每一列都反射 DATASET_REGISTRY 的字段，缺任一字段
    前端渲染 KeyError。data_service 推断「健康/过期」状态靠 freshness_hours + parquet mtime，
    缺 freshness_hours 则状态判定失效。
    """
    required_fields = ("source", "market", "granularity", "script", "freshness_hours")
    for ds_key in STOCK_TUSHARE_KEYS:
        entry = DATASET_REGISTRY.get(ds_key, {})
        for f in required_fields:
            assert f in entry, f"{ds_key} DATASET_REGISTRY 缺字段 {f}"


def test_lake_config_and_tushare_datasets_path_consistent():
    """LAKE_CONFIG lake 路径与 TUSHARE_DATASETS lake 路径一致（单一真相源，避免两处分叉）。

    Why 钉死：DataLakeReader 按 LAKE_CONFIG["lakes"][key] 寻址，sync_dataset 按
    TUSHARE_DATASETS[key]["lake"] 落盘。若两处分叉，reader 读到的是旧湖/空湖，sync 写到
    新湖，数据「写了但读不到」的静默故障。本测试把路径一致性钉死。
    """
    # 反查：lake_path → LAKE_CONFIG key（用于复用湖场景下也能断言路径匹配）
    for ds_key in STOCK_TUSHARE_KEYS:
        # _unavailable 数据集不落湖，跳过路径一致性检查
        if TUSHARE_DATASETS[ds_key].get("_unavailable"):
            continue
        tushare_lake = TUSHARE_DATASETS[ds_key]["lake"]
        assert tushare_lake in LAKE_CONFIG["lakes"].values(), \
            f"{ds_key}: TUSHARE_DATASETS lake {tushare_lake} 不在 LAKE_CONFIG['lakes'].values()"


def test_stock_lakes_count_matches_tushare_datasets():
    """股票类 DATASET_REGISTRY 条数 == 股票类 TUSHARE_DATASETS 条数（无漏注册/误注册）。

    Why 数量守卫：逐条断言可能漏一个，用总数兜底——股票类 24 个数据集必须全部在 DATASET_REGISTRY。
    """
    registered_stock = [k for k in STOCK_TUSHARE_KEYS if k in DATASET_REGISTRY]
    assert len(registered_stock) == len(STOCK_TUSHARE_KEYS), \
        f"股票类应注册 {len(STOCK_TUSHARE_KEYS)} 个，实际注册 {len(registered_stock)} 个，" \
        f"缺失：{set(STOCK_TUSHARE_KEYS) - set(registered_stock)}"


def test_stock_keys_cross_check_with_tushare_datasets():
    """STOCK_TUSHARE_KEYS 与 TUSHARE_DATASETS 的通用同步器子集双向交叉（Finding 2 修复）。

    Why 不再只遍历硬编码列表：原 test_stock_lakes_count_matches_tushare_datasets 只遍历
    STOCK_TUSHARE_KEYS 自身，未来在 config.py 加股票类 Tushare 数据集却忘更新本文件硬编码列表，
    测试仍会绿（自映射盲点）。本断言从 TUSHARE_DATASETS 推导实际通用同步器 key 集，反向钉死：
    凡 DATASET_REGISTRY 走 scripts/sync_tushare.py 的，必须都在 TUSHARE_DATASETS
    （防注册了无法同步的 key）；反之 STOCK_TUSHARE_KEYS 必须 ⊆ TUSHARE_DATASETS
    （防硬编码列表混入不存在的 key）。

    ⚠️ Plan C Task 6 后界定变更（source → script）：原断言用 source=="Tushare" 推导「股票类」，
    但 Task 6 把 macro（macro_credit 湖，script=sync_macro_credit.py）也标 source=Tushare
    （主源 cn_m + akshare 社融 fallback），而 macro 不在 TUSHARE_DATASETS（它走独立脚本，不经
    通用同步器）。若仍用 source 界定，macro 会被误判为 orphan。故改用 script=="scripts/sync_tushare.py"
    精确界定「通用同步器数据集」——这才是 sync_dataset(key) 能消费的集合（ETF/宏观原始指标也走
    通用同步器，都在 TUSHARE_DATASETS，自然通过；macro 走独立脚本，不在此集合，不误判）。
    """
    # 实际通用同步器数据集 = DATASET_REGISTRY 中 script=sync_tushare.py 的 key
    # （配置层真相，非硬编码列表；精确界定 sync_dataset 可消费的集合，排除 macro 等独立脚本数据集）
    actual_sync_tushare_in_registry = {
        k for k, spec in DATASET_REGISTRY.items()
        if spec.get("script") == "scripts/sync_tushare.py"
    }
    # 反向断言 1：硬编码股票列表 ⊆ 实际通用同步器集合（防硬编码列表多写）
    assert set(STOCK_TUSHARE_KEYS) <= actual_sync_tushare_in_registry, \
        f"STOCK_TUSHARE_KEYS 含未注册 script=sync_tushare.py 的 key：" \
        f"{set(STOCK_TUSHARE_KEYS) - actual_sync_tushare_in_registry}"
    # 反向断言 2：硬编码列表的每个 key 都在 TUSHARE_DATASETS（防硬编码列表混入不存在的数据集）
    stale_keys = set(STOCK_TUSHARE_KEYS) - set(TUSHARE_DATASETS)
    assert not stale_keys, \
        f"STOCK_TUSHARE_KEYS 含 TUSHARE_DATASETS 里不存在的 key（硬编码列表失同步）：{stale_keys}"
    # 反向断言 3：所有走通用同步器的 DATASET_REGISTRY key 都在 TUSHARE_DATASETS
    # （防 DATASET_REGISTRY 注册了一个 TUSHARE_DATASETS 没有的数据集 → sync_dataset 会 KeyError）
    orphan_in_registry = actual_sync_tushare_in_registry - set(TUSHARE_DATASETS)
    assert not orphan_in_registry, \
        f"DATASET_REGISTRY 标 script=sync_tushare.py 但 TUSHARE_DATASETS 无此 key（无法同步）：{orphan_in_registry}"


def test_all_tushare_datasets_lakes_registered():
    """TUSHARE_DATASETS 每个 key 的 lake 路径必须在 LAKE_CONFIG 注册（防漏湖寻址）。

    Why 全量交叉（不限股票类）：DataLakeReader 按 LAKE_CONFIG["lakes"][key] 寻址，sync_dataset
    按 TUSHARE_DATASETS[key]["lake"] 落盘。若任一 lake 路径未在 LAKE_CONFIG 注册，会出现
    「写了但读不到」的静默故障。ETF/宏观虽未进 DATASET_REGISTRY，但其湖路径必须在
    LAKE_CONFIG 注册（通用同步器 + reader 都靠它）。本断言一次性钉死全部 37 个数据集。
    """
    # _unavailable 数据集（代理无接口，如 concept/top_list/hsgt_top10）不落湖，豁免 lake 注册检查
    missing_lakes = {
        k: spec["lake"]
        for k, spec in TUSHARE_DATASETS.items()
        if not spec.get("_unavailable")
        and spec["lake"] not in LAKE_CONFIG["lakes"].values()
    }
    assert not missing_lakes, \
        f"TUSHARE_DATASETS 的 lake 路径未注册到 LAKE_CONFIG（reader 无法寻址）：{missing_lakes}"


def test_parquet_path_unavailable_datasets_return_none():
    """代理不可用数据集（top_list/hsgt_top10）_parquet_path 返 None（2026-07-19 盘点订正）。

    What：top_list/hsgt_top10 代理 tnskhdata 无方法（probe 实测 DataApi has no attribute），
    原 lake_key 复用设计（top_list→dragon_list、hsgt_top10→north_flow）废弃——复用建立在
    代理能跑通的假设上，实际跑不通。删 lake_key 后 _parquet_path 经 _lake_key fallback 到
    数据集 key 自身，LAKE_CONFIG 无 top_list/hsgt_top10 → 返 None（诚实反映无物理湖）。

    Why 返 None 是对的：代理不可用 = 永远无数据，前端 list_datasets 标 missing 比假装读到
    akshare 老数据（dragon_list hit / north_flow 24 行）更诚实。龙虎榜单用 dragon_list，
    北向总量用 moneyflow_hsgt.north_money，均独立湖。
    """
    # top_list/hsgt_top10：删 lake_key 后无 LAKE_CONFIG 映射 → None（代理不可用，无物理湖）
    assert _parquet_path("top_list") is None, \
        f"_parquet_path('top_list') 应返 None（代理不可用，无物理湖），实际 {_parquet_path('top_list')!r}"
    assert _parquet_path("hsgt_top10") is None, \
        f"_parquet_path('hsgt_top10') 应返 None（代理不可用，无物理湖），实际 {_parquet_path('hsgt_top10')!r}"


def test_parquet_path_non_reused_lake_unchanged():
    """非复用湖的 _parquet_path 行为零回归（Finding 1 fallback 不破坏既有数据集）。

    Why 回归保护：_lake_key 缺省 fallback 到数据集 key 自身，必须保证既有 daily/macro/fina_income
    等 key=lake_key 的数据集行为不变。fina_income 是 Task 11 端到端已验证落盘的数据集，
    断言它返回自己的湖（非复用、非 None）。
    """
    # fina_income：非复用湖，key=lake_key，应返回自己的湖路径
    assert _parquet_path("fina_income") == LAKE_CONFIG["lakes"]["fina_income"], \
        f"_parquet_path('fina_income') 应返回 fina_income 自己的湖路径，" \
        f"实际 {_parquet_path('fina_income')!r}"
    # daily：tushare 前复权日线（2026-07-19 订正 source：AKShare→Tushare），路径零回归
    assert _parquet_path("daily") == LAKE_CONFIG["lakes"]["daily"], \
        f"_parquet_path('daily') 应返回 daily 自己的湖路径，实际 {_parquet_path('daily')!r}"
    # 守卫：lake_key 复用机制已废弃（2026-07-19 盘点：top_list/hsgt_top10 代理不可用，复用无意义），
    # DATASET_REGISTRY 不应再有任何 lake_key 字段（防误加导致非复用湖被错误重定向）
    reused = {k for k, s in DATASET_REGISTRY.items() if "lake_key" in s}
    assert reused == set(), \
        f"lake_key 复用已废弃，不应有任何数据集声明 lake_key，实际声明者：{reused}"


# ============================================================================
# Plan C Task 6：宏观湖注册 + macro 切源 Tushare + CreditRegime 端到端
# ============================================================================
# Why 本组测试（宏观收尾，与 Task 11 股票类对等）：
#   - macro（macro_credit 湖）是 CreditRegime 的输入湖，源从 AKShare 切到 Tushare
#     （主源 Tushare cn_m(M1/M2) + akshare 社融/DR007 fallback），前端 DataLakeView 要能看到
#     「宏观信贷状态」现在主源是 Tushare 而非仍标 AKShare。
#   - 8 个原始宏观指标（cn_cpi/.../sse_daily）虽已在 TUSHARE_DATASETS + LAKE_CONFIG 注册（Task 3-5），
#     但 DATASET_REGISTRY 缺元信息 → 前端表格看不到这些资产。本组补注册并守卫。
#   - 端到端：fake_pro mock Tushare cn_m + monkeypatch akshare fetch_macro_raw，跑 sync_macro
#     落 tmp_path → CreditRegime.compute 不抛、返 ∈{+1,0,-1}（列名契约 + 无前视 ffill 不破）。


# —— fake_pro fixture（与 test_tushare_datasets_macro.py 同实现，文件级作用域）——
# Why 复制而非 conftest 抽取：与 stock/etf/macro 三个文件保持一致手法，避免改动 conftest
# 影响其它测试文件。mock pro 接口 + 限频/熔断器双 patch get_pro。

class _FakePro:
    """tushare pro 替身：按 api_name 返回可控 DataFrame（与 stock/etf/macro 文件同实现）。"""
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
    """mock pro 接口 + 限频/熔断器。

    单 patch get_pro：sync_macro_credit.py:39 已 `from data._tushare_compat import get_pro`
    把 get_pro 绑定到自身模块命名空间（line 128 `pro = get_pro()` 走模块绑定），
    故只需 patch `scripts.sync_macro_credit.get_pro` 即可劫持调用（与 test_sync_macro_credit.py:38
    单 patch 同手法）。无需再 patch `data._tushare_compat.get_pro`——那是对源模块的冗余 patch，
    此处不触发源模块的 get_pro 调用路径。
    """
    fake = _FakePro()
    monkeypatch.setattr("scripts.sync_macro_credit.get_pro", lambda: fake)
    # 限频/熔断器 patch：sync_macro_credit._fetch_with_guard 内部调用，不 patch 会
    # 触达真实限流器（阻塞）或熔断器（OPEN 返空 DF → 宏观湖为空）。与 test_sync_macro_credit 同手法。
    monkeypatch.setattr("scripts.sync_macro_credit.tushare_rate_limiter",
                        type("L", (), {"acquire": lambda self, n: None})())
    monkeypatch.setattr("scripts.sync_macro_credit.tushare_breaker",
                        type("B", (), {"allow_request": lambda self: True,
                                       "record_success": lambda self: None,
                                       "record_failure": lambda self: None})())
    return fake


# 原始宏观指标 7 数据集 key（B 类合并后：szse/sse → mkt_daily 一个）
MACRO_RAW_KEYS = ("cn_cpi", "cn_ppi", "cn_gdp", "cn_pmi", "shibor", "shibor_quote",
                  "mkt_daily")


def test_macro_source_changed_to_tushare():
    """macro（macro_credit 湖）source 必须切到 Tushare（主源 cn_m + akshare 社融 fallback）。

    Why 钉死切源：DATASET_REGISTRY["macro"] 对应 macro_credit.parquet（CreditRegime 输入湖），
    由 scripts/sync_macro_credit.py 产出。Plan C Task 2 已把 sync_macro_credit 重写为
    Tushare cn_m(M0/M1/M2) 主源 + akshare 社融/DR007 fallback（混合源语义，plan 既定决策非 bug）。
    此处 source 标 Tushare（主源），让前端 DataLakeView 反射出「宏观信贷现已切 Tushare」，
    而非仍停留在 AKShare 标签。
    """
    assert DATASET_REGISTRY["macro"]["source"] == "Tushare", \
        "macro source 应切到 Tushare（cn_m 主源 + akshare 社融 fallback）"


def test_macro_lakes_registered():
    """7 个原始宏观指标湖必须在 LAKE_CONFIG['lakes'] 注册（前端可视 + reader 寻址）。

    What：cn_cpi/cn_ppi/cn_gdp/cn_pmi/shibor/shibor_quote 走 single+datetime 落 DatetimeIndex；
          mkt_daily 走 by=date 落 MultiIndex。B 类合并后（szse/sse → daily_info 合为 mkt_daily）
          共 7 湖，本测试守卫「不漏湖 + 旧 szse/sse 已删」。

    Why 同时补 DATASET_REGISTRY 守卫：Task 3-5 只注册了 TUSHARE_DATASETS + LAKE_CONFIG（同步器 +
    reader 用），DATASET_REGISTRY 缺元信息 → 前端表格看不到这些资产。本测试一并钉死 DATASET_REGISTRY
    注册（与 Task 11 股票类对等：source=Tushare + market=宏观 + granularity + freshness_hours）。
    """
    for key in MACRO_RAW_KEYS:
        # 1) LAKE_CONFIG["lakes"] 必须有该湖 key（reader 寻址依赖）
        assert key in LAKE_CONFIG["lakes"], f"{key} 未注册到 LAKE_CONFIG['lakes']"
        # 2) LAKE_CONFIG 路径与 TUSHARE_DATASETS 一致（单一真相源，防两处分叉）
        assert LAKE_CONFIG["lakes"][key] == TUSHARE_DATASETS[key]["lake"], \
            f"{key} LAKE_CONFIG 路径与 TUSHARE_DATASETS 不一致"
        # 3) DATASET_REGISTRY 必须有该数据集元信息（前端 DataLakeView 反射依赖）
        assert key in DATASET_REGISTRY, \
            f"{key} 未在 DATASET_REGISTRY 注册（前端 DataLakeView 看不到该宏观资产）"
        # 4) source 标 Tushare（区分源，前端 macro 切源靠此字段）
        assert DATASET_REGISTRY[key].get("source") == "Tushare", \
            f"{key} source 应为 Tushare，实际 {DATASET_REGISTRY[key].get('source')!r}"
        # 5) 必备字段完备（前端表格每列反射）
        for f in ("source", "market", "granularity", "script", "freshness_hours"):
            assert f in DATASET_REGISTRY[key], f"{key} DATASET_REGISTRY 缺字段 {f}"
    # 交易所统计湖路径文件名必须含 mkt_daily（B 类合并后沪深合一，区别于个股 daily 湖）
    assert "mkt_daily" in LAKE_CONFIG["lakes"]["mkt_daily"], \
        "mkt_daily 湖路径文件名应含 mkt_daily（市场宽度统计语义）"
    # 旧 szse_daily/sse_daily 湖必须已删（合并入 mkt_daily）
    assert "szse_daily" not in LAKE_CONFIG["lakes"], "szse_daily 湖应删（合并入 mkt_daily）"
    assert "sse_daily" not in LAKE_CONFIG["lakes"], "sse_daily 湖应删（合并入 mkt_daily）"
    assert "szse_daily" not in DATASET_REGISTRY, "szse_daily 资产应删（合并入 mkt_daily）"
    assert "sse_daily" not in DATASET_REGISTRY, "sse_daily 资产应删（合并入 mkt_daily）"


# —— CreditRegime.compute 返值规则核实（core/macro_regime.py:133-171，构造方向性数据依据）——
# 取 tail(_MIN_LOOKBACK=20) 工作日窗口（≈ 4 周，必然跨月），首尾比较判趋势：
#   +1（扩张）：credit_up（shrzgm 尾>首）AND gap_pos（M1M2_gap 末值>0）AND rate_down（无 dr007 视作 True）
#   -1（收缩）：credit_down（shrzgm 尾<首）AND not gap_pos（M1M2_gap 末值≤0）AND rate_up（无 dr007 视作 True）
#   0（中性）：  其余（credit_up/credit_down 都 False 即 shrzgm 首尾相等，或信号矛盾）
# M1M2_gap = m1_yoy - m2_yoy（sync_macro_credit.py:192，M1同比 - M2同比）。
# 下方三组参数化数据严格对齐上述规则，分别压到 +1 / -1 / 0 三个分支（不再用宽松 in (1,0,-1)）。
@pytest.mark.parametrize(
    "m1_yoy,m2_yoy,shrzgm_values,expected",
    [
        # 扩张：shrzgm 月度递增（40000→65000，步长 1000）→ 跨月窗口首尾递增 credit_up=True；
        #       m1_yoy=8 > m2_yoy=5 → M1M2_gap=+3>0 → gap_pos=True；无 dr007 → rate_down=True ⇒ +1。
        pytest.param(8.0, 5.0, [40000 + i * 1000 for i in range(26)], 1, id="expansion"),
        # 收缩：shrzgm 月度递减（60000→35000，步长 -1000）→ credit_down=True；
        #       m1_yoy=5 < m2_yoy=8 → M1M2_gap=-3≤0 → not gap_pos=True；无 dr007 → rate_up=True ⇒ -1。
        pytest.param(5.0, 8.0, [60000 - i * 1000 for i in range(26)], -1, id="contraction"),
        # 中性：shrzgm 常数 50000 → credit_up=False / credit_down=False → 两个方向分支都不进 ⇒ 0。
        pytest.param(5.0, 8.0, [50000] * 26, 0, id="neutral"),
    ],
)
def test_credit_regime_end_to_end_with_tushare_macro(
    m1_yoy, m2_yoy, shrzgm_values, expected, tmp_path, fake_pro, monkeypatch
):
    """端到端：sync_macro 落湖 → CreditRegime.compute 返预期方向值（+1 扩张 / -1 收缩 / 0 中性）。

    What：mock Tushare cn_m（M1/M2 同比，月频）+ monkeypatch akshare fetch_macro_raw（社融 shrzgm），
          跑 sync_macro(start, end, out=tmp_path) 落盘 macro_credit.parquet，再用 CreditRegime
          读湖 compute(末日) → 必须返参数化预期值（1/-1/0，精确断言，不再用宽松 in 集合）。

    Why 三组方向性守卫（宏观 CTA 顶层最致命回归）：
      - 早期版本用全常数 mock（m1_yoy=5/m2_yoy=8→gap=-3；shrzgm=50000 常数），导致
        credit_up=credit_down=False 且 gap_pos=False，三分支都不触发 → 必然返 0（中性兜底）。
        旧的宽松断言 `r in (1,0,-1)` 虽过，但**只证明「不抛+中性分支」，未验证 +1/-1 方向性路径**——
        CreditRegime 是宏观 CTA 顶层锚，扩张/收缩方向性是最关键回归路径（网关宏观否决、前端红绿灯）。
      - 本参数化三组分别稳定压到 +1/-1/0 三个分支：shrzgm 月度单调（递增/递减/常数）保证 20 工作日
        跨月窗口首尾可比较；M1/M2 同比组合控制 gap_pos 正负；精确断言 expected 不留宽松余地。

    Why 端到端守卫（源切换不破坏消费者）：sync_macro_credit 切源 Tushare 后，落盘湖必须
    仍含 CreditRegime core 字段 shrzgm + M1M2_gap（core/macro_regime.py:154），否则 compute 走
    「缺列防御」分支强制返 0（宏观否决/绿灯双双失效）。本测试是「源切换不破坏消费者」的最强守卫。

    无前视红线（align_to_daily ffill-only 不破）：sync_macro 内部 align_to_daily 严格仅向前 ffill，
    CreditRegime._series 用 .loc[:date] 时间门控，本端到端验证整条链路无 bfill 回填。
    """
    # Tushare cn_m：M1/M2 同比（月频，month=YYYYMM）。26 个【不同】月份保证 > _MIN_LOOKBACK(20)。
    # ⚠️ brief 草稿用 ["202301"]*25（同月份），set_index 后重复索引会让 align_to_daily 的
    # reindex 抛 "duplicate labels"；此处修正为 26 个连续不同月份（brief 意图不变：足量样本）。
    # 月份须覆盖 sync 区间 [start, end]：从 2022-12 起生成 26 个月（至 2025-01），确保 align 的
    # bdate_range(2023-01..2025-01) 内有月值可 ffill（否则月初都早于 cal 首日 → 全 NaN）。
    n_months = 26
    months_ym = [d.strftime("%Y%m") for d in pd.date_range("2022-12-01", periods=n_months, freq="MS")]
    fake_pro.set("cn_m", pd.DataFrame({
        "month": months_ym,
        "m1_yoy": [m1_yoy] * n_months, "m2_yoy": [m2_yoy] * n_months,
        "m0_yoy": [m2_yoy] * n_months}))
    # akshare 社融 fallback：monkeypatch fetch_macro_raw，shrzgm 按 scenario 返 26 个月度值
    # （递增/递减/常数三态），dr007 返空（可选列，compute 缺 dr007 时 rate_down/rate_up 视作 True）。
    # ⚠️ 月份列日期用【BMS 每月首个工作日】（非 1 号）：align_to_daily 把月度日期 reindex 到工作日
    # 日历，若月度日期落在非工作日（如 12-01 周日）会被 reindex 丢弃 → 整月值缺失 → ffill 用上月
    # 值一路填到下次月初，导致 compute tail(20) 窗口首尾同值（credit_up/down 都 False）。用 BMS
    # 保证每月值都落在工作日，跨月窗口首尾能稳定体现月度增减趋势 → 方向性分支被真压到。
    bms_days = pd.bdate_range("2022-12-01", periods=n_months, freq="BMS")
    months_iso = [d.strftime("%Y-%m-%d") for d in bms_days]
    import data.clients.akshare_client as akc
    monkeypatch.setattr(akc.AKShareClient, "fetch_macro_raw",
                        lambda self, kind: pd.DataFrame({"月份": months_iso,
                                                         "社融增量": shrzgm_values})
                        if kind == "shrzgm" else pd.DataFrame())
    out = str(tmp_path / "macro.parquet")
    from scripts.sync_macro_credit import sync_macro
    sync_macro("2023-01-01", "2025-01-01", out=out)
    # 落盘湖必须含 CreditRegime core 字段（列名契约）
    df = pd.read_parquet(out)
    assert "shrzgm" in df.columns, "CreditRegime core 字段 shrzgm 缺失（源切换破坏消费者）"
    assert "M1M2_gap" in df.columns, "CreditRegime core 字段 M1M2_gap 缺失（源切换破坏消费者）"
    # CreditRegime 读湖 compute(末日) → 返参数化预期值（精确断言 1/-1/0，验证方向性分支被真压到）
    from core.macro_regime import CreditRegime
    r = CreditRegime(macro_df=df).compute(df.index[-1])
    assert r == expected, (
        f"CreditRegime.compute 应返 {expected}，实际 {r} "
        f"(窗口首尾 shrzgm={df['shrzgm'].dropna().iloc[-21]:.0f}→{df['shrzgm'].dropna().iloc[-1]:.0f}, "
        f"M1M2_gap 末值={df['M1M2_gap'].dropna().iloc[-1]})"
    )
