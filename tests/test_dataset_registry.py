# -*- coding: utf-8 -*-
"""数据集资产注册表对齐测试（Plan A Task 11 + Plan C Task 6：注册表对齐 + 端到端验证）。

设计意图（元数据层单一真相源）：
- **前端 DataLakeView 反射契约**：前端表格/下拉框经 /api/v1/data/datasets 反射 DATASET_REGISTRY，
  绝不在前端硬编码数据集名。本测试把「Tushare 新湖必须注册到 DATASET_REGISTRY 且 source=Tushare」
  钉死在配置层——任一新湖漏注册，前端就看不到该资产，宏观切源/数据湖可视直接断档。
- **退役清理（2026-08-05）**：top_list / hsgt_top10 / concept / concept_detail 因 Tushare
  无接口权限长期 _unavailable，已整体删除（注册表/TUSHARE_DATASETS/LAKE_CONFIG/探针/测试），
  本文件不再断言这四个 key。
- **Plan C Task 6 新增（宏观收尾）**：DATASET_REGISTRY["macro"]（macro_credit 湖，CreditRegime 输入）
  source 从 AKShare 切 Tushare（主源 cn_m + akshare 社融 fallback）；并新增 8 个原始宏观指标数据集
  （cn_cpi/cn_ppi/cn_gdp/cn_pmi/shibor/shibor_quote/szse_daily/sse_daily）的 DATASET_REGISTRY 条目，
  让前端 DataLakeView 可反射这些 Tushare 宏观资产。端到端测试验证 sync_macro 落湖 →
  CreditRegime.compute 返 1/0/-1（不抛、列名契约成立）。
"""
import pytest

from config import LAKE_CONFIG, DATASET_REGISTRY, TUSHARE_DATASETS

# data_service._parquet_path 是前端 list_datasets 的湖寻址热路径（Finding 1 修复对象）。
# 用函数真实返回值断言（非 monkeypatch），覆盖复用湖 fallback 落点。
from presentation.server.services.data_service import _parquet_path


# ============================================================================
# 股票类 Tushare 新湖清单（concept/concept_detail 已整体退役删除，2026-08-05）
# ============================================================================
# 单一真相 = config.py 里实际的 TUSHARE_DATASETS 的股票类 key。
# brief 给的列表含 concept_detail（错误）且不完整，此处以实际 TUSHARE_DATASETS 为准。
STOCK_TUSHARE_KEYS = [
    # 财报 6（fina_income/balance/cashflow/forecast/express/dividend）
    "fina_income", "fina_balance", "fina_cashflow", "forecast", "express", "dividend",
    # 资金流 / 龙虎榜 2（top_list 已退役删除）
    "moneyflow", "top_inst",
    # 融资融券 3
    "margin", "margin_detail", "margin_secs",
    # 北向资金 1（hsgt_top10 已退役删除）
    "moneyflow_hsgt",
    # 板块 1（concept/concept_detail 已退役删除）
    "ths_daily",
    # 指数 3
    "index_daily", "index_weight", "index_member",
    # 股东 / 解禁 / 停牌 4
    "top10_holders", "top10_floatholders", "share_float", "suspend_d",
    # 特色筹码 1（300/分独立通道）
    "cyq_perf",
]


def test_new_stock_lakes_registered():
    """Plan A 股票类新湖全部注册到 LAKE_CONFIG + DATASET_REGISTRY（前端可视反射契约）。

    What：每个股票类 Tushare 数据集的「落湖 key」必须在 LAKE_CONFIG["lakes"] 注册
    （DataLakeReader 寻址靠它），且在 DATASET_REGISTRY 注册（前端 DataLakeView 反射靠它）。

    Why 机器化守卫：DATASET_REGISTRY 是前端数据湖可视的单一真相源，任一新湖漏注册，
    前端表格就缺这一行，宏观切源/资产盘点直接断档。PR review 漏一眼也守得住。

    Why 统一注册：每个数据集在 DATASET_REGISTRY 补 source=Tushare 元信息，前端 DataLakeView
    才能反射「该资产由 Tushare 生产」。退役的 top_list/hsgt_top10/concept/concept_detail
    已从清单剔除（2026-08-05）。
    """
    for ds_key in STOCK_TUSHARE_KEYS:
        # 1) 数据集必须在 TUSHARE_DATASETS 注册（配置完备性，sync_dataset 依赖）
        assert ds_key in TUSHARE_DATASETS, \
            f"{ds_key} 未在 TUSHARE_DATASETS 注册（配置层缺失）"

        # _unavailable 数据集不落湖，跳过 lake 注册检查（当前注册表已无此类，保留机制防回归）
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
    凡 DATASET_REGISTRY 走 data/tools/sync_tushare.py 的，必须都在 TUSHARE_DATASETS
    （防注册了无法同步的 key）；反之 STOCK_TUSHARE_KEYS 必须 ⊆ TUSHARE_DATASETS
    （防硬编码列表混入不存在的 key）。

    ⚠️ Plan C Task 6 后界定变更（source → script）：原断言用 source=="Tushare" 推导「股票类」，
    但 Task 6 把 macro（macro_credit 湖，script=sync_macro_credit.py）也标 source=Tushare
    （主源 cn_m + akshare 社融 fallback），而 macro 不在 TUSHARE_DATASETS（它走独立脚本，不经
    通用同步器）。若仍用 source 界定，macro 会被误判为 orphan。故改用 script=="data/tools/sync_tushare.py"
    精确界定「通用同步器数据集」——这才是 sync_dataset(key) 能消费的集合（ETF/宏观原始指标也走
    通用同步器，都在 TUSHARE_DATASETS，自然通过；macro 走独立脚本，不在此集合，不误判）。
    """
    # 实际通用同步器数据集 = DATASET_REGISTRY 中 script=sync_tushare.py 的 key
    # （配置层真相，非硬编码列表；精确界定 sync_dataset 可消费的集合，排除 macro 等独立脚本数据集）
    actual_sync_tushare_in_registry = {
        k for k, spec in DATASET_REGISTRY.items()
        if spec.get("script") == "data/tools/sync_tushare.py"
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
    # _unavailable 数据集不落湖，豁免 lake 注册检查（当前注册表已无此类，保留机制防回归）
    missing_lakes = {
        k: spec["lake"]
        for k, spec in TUSHARE_DATASETS.items()
        if not spec.get("_unavailable")
        and spec["lake"] not in LAKE_CONFIG["lakes"].values()
    }
    assert not missing_lakes, \
        f"TUSHARE_DATASETS 的 lake 路径未注册到 LAKE_CONFIG（reader 无法寻址）：{missing_lakes}"


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
    # 守卫：lake_key 复用机制已废弃（2026-07-19 盘点：代理不可用，复用无意义；
    # 相关数据集 top_list/hsgt_top10 已整体删除，2026-08-05），
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


@pytest.fixture
def fake_pro(monkeypatch):
    """薄壳（原 ~60 行块收口 tests/_tushare_stub.py，2026-08-19 W2）。"""
    from tests._tushare_stub import FakePro, install_fake_pro
    fake = FakePro(data=None)
    install_fake_pro(monkeypatch, fake, module_targets=("data.tools.sync_macro_credit",))
    return fake


MACRO_RAW_KEYS = ("cn_cpi", "cn_ppi", "cn_gdp", "cn_pmi", "shibor", "shibor_quote",
                  "mkt_daily")


def test_macro_source_changed_to_tushare():
    """macro（macro_credit 湖）source 必须切到 Tushare（主源 cn_m + akshare 社融 fallback）。

    Why 钉死切源：DATASET_REGISTRY["macro"] 对应 macro_credit.parquet（CreditRegime 输入湖），
    由 data/tools/sync_macro_credit.py 产出。Plan C Task 2 已把 sync_macro_credit 重写为
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


# ============================================================================
# daily 双轨收口（T13-A · Task 5）
# ============================================================================
# 物理意图：删 TUSHARE_DATASETS["daily"]（通用同步器不再认 daily → 全量重建路径不可达，
# 防 T12 式无守卫覆盖）+ DATASET_REGISTRY["daily"]["script"] 指向增量脚本（sweep /
# POST /sync/daily 走增量）。weekly/monthly 保留不动（仍走通用同步器）。


def test_daily_removed_from_tushare_datasets():
    """daily 双轨收口：通用同步器不再认 daily（全量重建路径不可达）。"""
    assert "daily" not in TUSHARE_DATASETS


def test_weekly_monthly_still_in_tushare_datasets():
    """周/月线仍走通用同步器（只收口 daily，不动周月）。"""
    assert "weekly" in TUSHARE_DATASETS
    assert "monthly" in TUSHARE_DATASETS


def test_daily_registry_script_points_to_incremental():
    """sweep/POST /sync/daily 唯一入口 = 增量同步脚本。"""
    assert DATASET_REGISTRY["daily"]["script"] == "data/tools/sync_daily_incremental.py"


def test_sync_tushare_cli_no_longer_accepts_daily():
    """sync_tushare.py argparse choices 不再含 daily。"""
    assert "daily" not in list(TUSHARE_DATASETS.keys())

# ============================================================================
# TUSHARE_DATASETS 全量声明表（W3 表驱动收口 · 2026-08-19）
# ============================================================================
# 原 stock/etf/macro 三个 datasets 文件里 13 个 per-family「注册守卫」用例同模板
# （fields 完备 + by/date_col/symbol_col/api 声明 + 湖注册），收口为一张全量声明表：
# 每个数据集一行，四键即配置契约快照（新增数据集必须登记，双向对账兜底漏登/多登）。
# 湖注册+路径一致性由 test_all_tushare_datasets_lakes_registered 全量交叉钉死，不再
# 按 family 重复。行内注释承载各键的事故语境（前视红线/B 类订正/退役合并）。
_DATASET_SPECS = [
    ("cn_cpi", "cn_cpi", "single", "month", "month"),  # 宏观 single：index_mode=datetime（见 index_mode 专项例）
    ("cn_gdp", "cn_gdp", "single", "quarter", "quarter"),
    ("cn_pmi", "cn_pmi", "single", "MONTH", "MONTH"),
    ("cn_ppi", "cn_ppi", "single", "month", "month"),
    ("cyq_chips", "cyq_chips", "symbol", "trade_date", "ts_code"),
    ("cyq_perf", "cyq_perf", "symbol", "trade_date", "ts_code"),
    ("daily_basic", "daily_basic", "date", "trade_date", "ts_code"),
    ("dividend", "dividend", "symbol", "ann_date", "ts_code"),  # 分红：ann_date 前视红线
    ("express", "express", "symbol", "ann_date", "ts_code"),  # 快报：ann_date 前视红线
    ("fina_balance", "balancesheet", "symbol", "ann_date", "ts_code"),  # 同上：ann_date 前视红线
    ("fina_cashflow", "cashflow", "symbol", "ann_date", "ts_code"),  # 同上：ann_date 前视红线
    ("fina_income", "income", "symbol", "ann_date", "ts_code"),  # 财报三表+预分配：by=symbol 逐标的全历史；date_col=ann_date 前视红线（公告日非报告期）
    ("forecast", "forecast", "symbol", "ann_date", "ts_code"),  # 预告：ann_date 前视红线
    ("fund_basic", "fund_basic", "single", "found_date", "ts_code"),
    ("fund_daily", "fund_daily", "symbol", "trade_date", "ts_code"),
    ("fund_nav", "fund_nav", "symbol", "nav_date", "ts_code"),
    ("fund_portfolio", "fund_portfolio", "symbol", "ann_date", "ts_code"),  # ETF 前视红线：必须 ann_date（禁 end_date 报告期）
    ("fund_share", "fund_share", "symbol", "trade_date", "ts_code"),
    ("hs_const_sh", "hs_const", "single", "in_date", "ts_code"),
    ("hs_const_sz", "hs_const", "single", "in_date", "ts_code"),
    ("index_daily", "index_daily", "symbol", "trade_date", "ts_code"),
    ("index_member", "index_weight", "symbol", "trade_date", "con_code"),  # B 类订正：api 复用 index_weight（成分股，code_param=index_code）
    ("index_weight", "index_weight", "date", "trade_date", "con_code"),
    ("margin", "margin", "date", "trade_date", "exchange_id"),  # symbol_col=exchange_id（市场级，非 ts_code）
    ("margin_detail", "margin_detail", "date", "trade_date", "ts_code"),
    ("margin_secs", "margin_secs", "single", "trade_date", "ts_code"),
    ("mkt_daily", "daily_info", "date", "trade_date", "trade_date"),  # B 类合并：szse/sse→daily_info 沪深两市；旧 szse_daily/sse_daily 已退役
    ("moneyflow", "moneyflow", "date", "trade_date", "ts_code"),
    ("moneyflow_hsgt", "moneyflow_hsgt", "date", "trade_date", "trade_date"),
    ("monthly", "monthly", "symbol", "trade_date", "ts_code"),
    ("share_float", "share_float", "single", "ann_date", "ts_code"),  # A10：ann_date 前视红线
    ("shibor", "shibor", "single", "date", "date"),
    ("shibor_quote", "shibor_quote", "single", "date", "date"),
    ("stk_factor_pro", "stk_factor_pro", "date", "trade_date", "ts_code"),
    ("stock_basic", "stock_basic", "single", "list_date", "ts_code"),
    ("suspend_d", "suspend_d", "date", "trade_date", "ts_code"),  # A10 停牌真值（4 真实列，旧幻觉字段已删——见 suspend_d_uses_official_fields）
    ("ths_daily", "ths_daily", "date", "trade_date", "ts_code"),  # 不复用 sector 湖（akshare 申万 vs 同花顺概念，ts_code 空间不同）
    ("top10_floatholders", "top10_floatholders", "symbol", "ann_date", "ts_code"),  # A10：ann_date 前视红线
    ("top10_holders", "top10_holders", "symbol", "ann_date", "ts_code"),  # A10：ann_date 前视红线
    ("top_inst", "top_inst", "date", "trade_date", "ts_code"),
    ("weekly", "weekly", "symbol", "trade_date", "ts_code"),
]


@pytest.mark.parametrize("key, api, by, date_col, symbol_col", _DATASET_SPECS)
def test_tushare_datasets_declared(key, api, by, date_col, symbol_col):
    """每个数据集的 (api, by, date_col, symbol_col) 与声明表逐键一致 + fields/lake 完备。

    Why 逐键参数化（而非 13 个 family 用例）：sync_dataset 直接 cfg = TUSHARE_DATASETS[key]，
    缺任一字段运行时崩在深处；表驱动让失败信息精确到键，且新增数据集漏登记即红。
    """
    cfg = TUSHARE_DATASETS.get(key)
    assert cfg is not None, f"{key} 未在 TUSHARE_DATASETS 注册"
    for f in ("api", "by", "date_col", "symbol_col", "fields", "lake"):
        assert f in cfg, f"{key} 配置缺字段 {f}"
    assert cfg["api"] == api, f"{key} api 应为 {api}"
    assert cfg["by"] == by, f"{key} by 应为 {by}"
    assert cfg["date_col"] == date_col, f"{key} date_col 应为 {date_col}"
    assert cfg["symbol_col"] == symbol_col, f"{key} symbol_col 应为 {symbol_col}"


def test_dataset_declaration_table_reconciles():
    """声明表 ↔ 注册表双向对账 + 退役键禁令。

    表漏登（新增数据集没进表）/ 表多登（删数据集没删行）都失败——表即契约快照。
    退役键（2026-08-05 直连无权限 / B 类合并）必须保持缺席，防复活。
    """
    declared = {row[0] for row in _DATASET_SPECS}
    actual = set(TUSHARE_DATASETS)
    assert declared == actual, (
        f"声明表与注册表不一致：漏登 {sorted(actual - declared)} / 多登 {sorted(declared - actual)}")
    for retired in ("concept", "concept_detail", "top_list", "szse_daily", "sse_daily"):
        assert retired not in TUSHARE_DATASETS, f"{retired} 已退役，不应再注册"


def test_macro_single_datasets_index_mode_datetime():
    """宏观 single 数据集 index_mode=datetime（DatetimeIndex 湖契约，W3 收口保留的独有断言）。

    Why：宏观湖无 symbol 层（全市场单序列），_sync_single 依据 index_mode='datetime'
    分支重建时间索引；漏配则落扁平 df，DataLakeReader 按日期切片直接 KeyError。
    """
    for key in ("cn_cpi", "cn_ppi", "cn_gdp", "cn_pmi", "shibor", "shibor_quote"):
        assert TUSHARE_DATASETS[key]["index_mode"] == "datetime",             f"{key} index_mode 应为 datetime（DatetimeIndex 宏观湖）"
