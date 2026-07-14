# -*- coding: utf-8 -*-
"""数据集资产注册表对齐测试（Plan A Task 11：注册表对齐 + 端到端验证）。

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
"""
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
    """STOCK_TUSHARE_KEYS 与 TUSHARE_DATASETS 的股票类子集双向交叉（Finding 2 修复）。

    Why 不再只遍历硬编码列表：原 test_stock_lakes_count_matches_tushare_datasets 只遍历
    STOCK_TUSHARE_KEYS 自身，未来在 config.py 加股票类 Tushare 数据集却忘更新本文件硬编码列表，
    测试仍会绿（自映射盲点）。本断言从 TUSHARE_DATASETS 推导实际股票类 key 集，反向钉死：
    凡 DATASET_REGISTRY 标 source=Tushare 的，必须都在 TUSHARE_DATASETS（防注册了无法同步的 key）；
    反之 STOCK_TUSHARE_KEYS 必须 ⊆ TUSHARE_DATASETS（防硬编码列表混入不存在的 key）。

    股票 vs ETF/宏观 区分：ETF(fund_*)/宏观(cn_*/shibor*/szse_daily/sse_daily) 当前未注册到
    DATASET_REGISTRY（Plan B/C 后续 Task），故「DATASET_REGISTRY 里 source=Tushare 的 key」
    自然等价于股票类。用此实际集合做交叉，不依赖硬编码。
    """
    # 实际股票类 = DATASET_REGISTRY 中 source=Tushare 的 key（配置层真相，非硬编码列表）
    actual_stock_in_registry = {
        k for k, spec in DATASET_REGISTRY.items() if spec.get("source") == "Tushare"
    }
    # 反向断言 1：硬编码列表 ⊆ 实际（防硬编码列表多写）
    assert set(STOCK_TUSHARE_KEYS) <= actual_stock_in_registry, \
        f"STOCK_TUSHARE_KEYS 含未注册 source=Tushare 的 key：" \
        f"{set(STOCK_TUSHARE_KEYS) - actual_stock_in_registry}"
    # 反向断言 2：硬编码列表的每个 key 都在 TUSHARE_DATASETS（防硬编码列表混入不存在的数据集）
    stale_keys = set(STOCK_TUSHARE_KEYS) - set(TUSHARE_DATASETS)
    assert not stale_keys, \
        f"STOCK_TUSHARE_KEYS 含 TUSHARE_DATASETS 里不存在的 key（硬编码列表失同步）：{stale_keys}"
    # 反向断言 3：所有 source=Tushare 的 DATASET_REGISTRY key 都在 TUSHARE_DATASETS
    # （防 DATASET_REGISTRY 注册了一个 TUSHARE_DATASETS 没有的数据集 → sync_dataset 会 KeyError）
    orphan_in_registry = actual_stock_in_registry - set(TUSHARE_DATASETS)
    assert not orphan_in_registry, \
        f"DATASET_REGISTRY 标 source=Tushare 但 TUSHARE_DATASETS 无此 key（无法同步）：{orphan_in_registry}"


def test_all_tushare_datasets_lakes_registered():
    """TUSHARE_DATASETS 每个 key 的 lake 路径必须在 LAKE_CONFIG 注册（防漏湖寻址）。

    Why 全量交叉（不限股票类）：DataLakeReader 按 LAKE_CONFIG["lakes"][key] 寻址，sync_dataset
    按 TUSHARE_DATASETS[key]["lake"] 落盘。若任一 lake 路径未在 LAKE_CONFIG 注册，会出现
    「写了但读不到」的静默故障。ETF/宏观虽未进 DATASET_REGISTRY，但其湖路径必须在
    LAKE_CONFIG 注册（通用同步器 + reader 都靠它）。本断言一次性钉死全部 37 个数据集。
    """
    missing_lakes = {
        k: spec["lake"]
        for k, spec in TUSHARE_DATASETS.items()
        if spec["lake"] not in LAKE_CONFIG["lakes"].values()
    }
    assert not missing_lakes, \
        f"TUSHARE_DATASETS 的 lake 路径未注册到 LAKE_CONFIG（reader 无法寻址）：{missing_lakes}"


def test_parquet_path_resolves_reused_lake():
    """复用湖数据集的 _parquet_path 必须返回物理湖路径（Finding 1 根治验证）。

    What：top_list 复用 dragon_list 湖、hsgt_top10 复用 north_flow 湖。前端 list_datasets
    经 _parquet_path(key) 寻址 parquet，若用数据集 key 直接索引 LAKE_CONFIG 会返 None →
    status=missing（即便物理湖已同步）。本断言钉死 _parquet_path 经 lake_key fallback 后
    返回物理湖路径（非 None）。

    Why 在配置层测试而非端到端：_parquet_path 是 list_datasets 的热路径，单测覆盖最稳。
    端到端需先同步 parquet（依赖 token/网络/配额），单测只验配置映射逻辑，零外部依赖。
    """
    # top_list → dragon_list 湖（复用，切 Tushare 替代 akshare）
    assert _parquet_path("top_list") == LAKE_CONFIG["lakes"]["dragon_list"], \
        f"_parquet_path('top_list') 应返回 dragon_list 湖路径，" \
        f"实际 {_parquet_path('top_list')!r}"
    assert _parquet_path("top_list") is not None, \
        "_parquet_path('top_list') 返 None（lake_key fallback 失效，前端会误报 missing）"
    # hsgt_top10 → north_flow 湖（复用，切 Tushare 替代 akshare）
    assert _parquet_path("hsgt_top10") == LAKE_CONFIG["lakes"]["north_flow"], \
        f"_parquet_path('hsgt_top10') 应返回 north_flow 湖路径，" \
        f"实际 {_parquet_path('hsgt_top10')!r}"
    assert _parquet_path("hsgt_top10") is not None, \
        "_parquet_path('hsgt_top10') 返 None（lake_key fallback 失效，前端会误报 missing）"


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
    # daily：AKShare 旧湖（非 Tushare），同样应零回归
    assert _parquet_path("daily") == LAKE_CONFIG["lakes"]["daily"], \
        f"_parquet_path('daily') 应返回 daily 自己的湖路径，实际 {_parquet_path('daily')!r}"
    # 守卫：lake_key 字段只应出现在复用湖数据集上（top_list/hsgt_top10），其余都不该有
    # （防误加 lake_key 导致非复用湖被错误重定向）
    reused = {k for k, s in DATASET_REGISTRY.items() if "lake_key" in s}
    assert reused == {"top_list", "hsgt_top10"}, \
        f"仅 top_list/hsgt_top10 应声明 lake_key，实际声明者：{reused}"
