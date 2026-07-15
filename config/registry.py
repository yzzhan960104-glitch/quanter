# -*- coding: utf-8 -*-
"""数据集资产注册表 + 通用 Tushare 湖同步器注册表（数据层·单一真相源）—— 从 config.py 拆出（归属：数据层）。

本模块是「数据湖有哪些资产、各自怎么同步、多新鲜算健康」的**单一真相源**：
    - DATASET_REGISTRY：前端 DataLakeView 反射的资产元信息（source/market/granularity/...）
    - TUSHARE_DATASETS：通用 Tushare 同步器的声明式配置（api/by/date_col/fields/lake/...）
    - SYNCING_DIR：同步哨兵目录（POST /sync/{key} 触发时 touch {key}）

**原样搬运红线**：registry.py 最大（约 600 行），整段剪切不省略任何字段/注释。
所有字段契约注释（前视红线 ann_date、字段名订正、实测局限、配额策略等）必须原样保留，
这是后续接手者理解为何 date_col=ann_date 而非 end_date、为何 by=single 而非 by=date 的唯一线索。
"""
import os
from typing import Dict, Any


# ============================================================
# 数据集资产注册表（层级一·数据湖可视）—— 决策点① = 方案 B（不引 Celery Beat）
# ============================================================
# 这是「数据湖有哪些资产、各自怎么同步、多新鲜算健康」的**单一真相源**。
# 前端 DataLakeView 的表格、下拉框全部经 /api/v1/data/datasets 反射本表，
# 绝不在前端硬编码数据集名。状态判定由 data_service 联合 parquet mtime +
# data_lake/.syncing/{key} 哨兵文件推导，不依赖任何调度器，零新增守护进程。
#
# 字段契约：
#   source:          数据源（与 data/clients 实际对接的源对齐）
#   market:          市场口径（仅展示）
#   granularity:     粒度（仅展示）
#   script:          同步脚本相对路径（POST /sync/{key} 以 sys.executable 子进程拉起）
#   args:            同步脚本额外 argv（缺省 []，走脚本 __main__ 默认参数）
#   schedule:        计划节奏（**仅元信息展示**，无 Beat 守护，不做强约束）
#   freshness_hours: 「健康」新鲜度阈值（小时）；parquet mtime 距今 ≤ 此值 = healthy，否则 stale
# key 与 LAKE_CONFIG["lakes"] 的 key 通常一一对应（路径不重复定义，只在此声明资产语义）。
# 例外——复用湖场景：数据集名作 key，但物理 parquet 落在既有湖（见 lake_key 字段）。
#   lake_key: 物理湖在 LAKE_CONFIG["lakes"] 的 key；缺省 = 数据集 key 自身（一一对应）。
#   仅复用湖需显式声明：top_list → dragon_list、hsgt_top10 → north_flow（切 Tushare 替代
#   akshare，parquet 落同一文件，避免双湖分叉）。data_service 的 _parquet_path /
#   _loaded_data_span 优先读 lake_key 作湖索引，fallback 到数据集 key（零回归保护既有湖）。
DATASET_REGISTRY: Dict[str, Dict[str, Any]] = {
    # macro（macro_credit 湖）：CreditRegime 的输入湖，由 sync_macro_credit.py 产出。
    # Plan C Task 6 源切换：主源 Tushare cn_m(M0/M1/M2) + akshare 社融(shrzgm)/DR007 fallback。
    # Why source 标 Tushare（主源）而非 AKShare：sync_macro_credit 已重写为 Tushare cn_m 为主，
    # akshare 仅作社融/DR007 的 fallback（Tushare 无专门接口）。前端 DataLakeView 据此反射
    # 「宏观信贷现已切 Tushare」，而非仍停留在 AKShare 标签（混合源语义，plan 既定决策非 bug）。
    "macro":         {"source": "Tushare", "market": "宏观", "granularity": "月频→日频",
                      "script": "scripts/sync_macro_credit.py", "schedule": "每月初",   "freshness_hours": 720},
    "sector":        {"source": "AKShare", "market": "板块", "granularity": "1d",
                      "script": "scripts/sync_sector_daily.py", "schedule": "每日18:00", "freshness_hours": 24},
    "daily":         {"source": "AKShare", "market": "A股",  "granularity": "1d",
                      "script": "scripts/sync_data_lake.py",   "schedule": "每日18:00", "freshness_hours": 24},
    "daily_active":  {"source": "AKShare", "market": "A股",  "granularity": "1d",
                      "script": "scripts/sync_sector_daily.py", "schedule": "每日18:00", "freshness_hours": 24},
    "minute":        {"source": "JQData",  "market": "A股",  "granularity": "1m",
                      "script": "scripts/sync_jqdata_1min.py", "schedule": "每日18:00", "freshness_hours": 24},
    "crypto":        {"source": "Binance", "market": "加密", "granularity": "1m",
                      "script": "scripts/sync_binance_vision.py", "schedule": "每日",   "freshness_hours": 24},
    "fundamentals":  {"source": "AKShare", "market": "A股",  "granularity": "日频",
                      "script": "scripts/sync_fundamentals.py", "schedule": "每周",     "freshness_hours": 168},
    "north_flow":    {"source": "AKShare", "market": "A股",  "granularity": "1d",
                      "script": "scripts/sync_north_flow.py",  "schedule": "每日18:00", "freshness_hours": 24},
    "dragon_list":   {"source": "AKShare", "market": "A股",  "granularity": "1d",
                      "script": "scripts/sync_dragon_list.py", "schedule": "每日18:00", "freshness_hours": 24},
    # ============================================================================
    # Tushare 数据中心新湖（Plan A Task 11 注册表对齐）
    # ============================================================================
    # 设计意图（元数据层单一真相源）：以下 24 个股票类数据集全部走通用 Tushare 同步器
    # scripts/sync_tushare.py（data/tushare_sync.py 的 sync_dataset），source 统一标 Tushare，
    # 让前端 DataLakeView 能看到这些新资产、macro 切源时能区分新旧。
    #
    # Why 按数据集粒度而非湖类别粒度：每个 Tushare 数据集有独立的接口/分页/字段配置
    # （见 TUSHARE_DATASETS），同步节奏（schedule）与新鲜度（freshness_hours）也因披露频率
    # 而异——财报季频 vs 资金流日频 vs 指数权重月频。按数据集粒度注册才能精确表达各自节奏，
    # 前端表格也能逐行展示「哪个数据集何时同步、是否过期」。
    #
    # Why 复用湖仍单独注册数据集：top_list 复用 dragon_list 湖、hsgt_top10 复用 north_flow 湖
    # （TUSHARE_DATASETS lake 路径相同），但 DATASET_REGISTRY 用数据集名（top_list/hsgt_top10）
    # 作 key——前端要能区分「龙虎榜现在由 Tushare 生产」vs「dragon_list 仍标 AKShare」，
    # 两个注册表语义不同（资产元信息 vs 寻址路径），key 解耦。
    #
    # 字段口径：
    #   script=scripts/sync_tushare.py —— 通用同步器（POST /sync/{key} 子进程拉起）
    #   freshness_hours —— 「健康」新鲜度阈值：日频=24h、季频=2190h（90天*24.3）、月频=730h、
    #                       年频=8760h、静态快照=8760h（标的不常变动）。财报类按季报披露窗口设 2190h。
    #   schedule —— 仅元信息展示，无 Beat 守护（决策点①=方案B，不引 Celery）。
    # —— 财报 6（季频，ann_date 公告日索引，防前视）——
    "fina_income":     {"source": "Tushare", "market": "A股", "granularity": "季频",
                        "script": "scripts/sync_tushare.py", "schedule": "每季报披露窗口", "freshness_hours": 2190},
    "fina_balance":    {"source": "Tushare", "market": "A股", "granularity": "季频",
                        "script": "scripts/sync_tushare.py", "schedule": "每季报披露窗口", "freshness_hours": 2190},
    "fina_cashflow":   {"source": "Tushare", "market": "A股", "granularity": "季频",
                        "script": "scripts/sync_tushare.py", "schedule": "每季报披露窗口", "freshness_hours": 2190},
    "forecast":        {"source": "Tushare", "market": "A股", "granularity": "不定期",
                        "script": "scripts/sync_tushare.py", "schedule": "每季报披露窗口", "freshness_hours": 2190},
    "express":         {"source": "Tushare", "market": "A股", "granularity": "不定期",
                        "script": "scripts/sync_tushare.py", "schedule": "每季报披露窗口", "freshness_hours": 2190},
    "dividend":        {"source": "Tushare", "market": "A股", "granularity": "不定期",
                        "script": "scripts/sync_tushare.py", "schedule": "每年预案公告季", "freshness_hours": 2190},
    # —— 资金流 / 龙虎榜 3（日频，trade_date 索引）——
    # top_list 复用 dragon_list 湖（切 Tushare 替代 akshare sync_dragon_list）。
    "moneyflow":       {"source": "Tushare", "market": "A股", "granularity": "1d",
                        "script": "scripts/sync_tushare.py", "schedule": "每日18:00", "freshness_hours": 24},
    "top_list":        {"source": "Tushare", "market": "A股", "granularity": "1d",
                        "script": "scripts/sync_tushare.py", "schedule": "每日18:00", "freshness_hours": 24,
                        "lake_key": "dragon_list"},  # 复用 dragon_list 湖（切 Tushare 替代 akshare）
    "top_inst":        {"source": "Tushare", "market": "A股", "granularity": "1d",
                        "script": "scripts/sync_tushare.py", "schedule": "每日18:00", "freshness_hours": 24},
    # —— 融资融券 3（margin/margin_detail 日频，margin_secs 静态快照）——
    "margin":          {"source": "Tushare", "market": "A股", "granularity": "1d",
                        "script": "scripts/sync_tushare.py", "schedule": "每日18:00", "freshness_hours": 24},
    "margin_detail":   {"source": "Tushare", "market": "A股", "granularity": "1d",
                        "script": "scripts/sync_tushare.py", "schedule": "每日18:00", "freshness_hours": 24},
    "margin_secs":     {"source": "Tushare", "market": "A股", "granularity": "快照",
                        "script": "scripts/sync_tushare.py", "schedule": "每月", "freshness_hours": 730},
    # —— 北向资金 2（hsgt_top10 复用 north_flow 湖，切 Tushare 替代 akshare）——
    "hsgt_top10":      {"source": "Tushare", "market": "A股", "granularity": "1d",
                        "script": "scripts/sync_tushare.py", "schedule": "每日18:00", "freshness_hours": 24,
                        "lake_key": "north_flow"},  # 复用 north_flow 湖（切 Tushare 替代 akshare）
    "moneyflow_hsgt":  {"source": "Tushare", "market": "A股", "granularity": "1d",
                        "script": "scripts/sync_tushare.py", "schedule": "每日18:00", "freshness_hours": 24},
    # —— 板块 / 概念 2（concept 静态字典，ths_daily 板块指数日频）——
    # concept_detail 按概念 id 分页（pro.concept_detail(id=...)），通用同步器不支持 by=concept，
    # Plan A Task 7 已决策跳过，此处不注册 concept_detail。
    "concept":         {"source": "Tushare", "market": "板块", "granularity": "快照",
                        "script": "scripts/sync_tushare.py", "schedule": "每月", "freshness_hours": 730},
    "ths_daily":       {"source": "Tushare", "market": "板块", "granularity": "1d",
                        "script": "scripts/sync_tushare.py", "schedule": "每日18:00", "freshness_hours": 24},
    # —— 指数 3（index_daily 日频，index_weight 月频，index_member 快照）——
    "index_daily":     {"source": "Tushare", "market": "指数", "granularity": "1d",
                        "script": "scripts/sync_tushare.py", "schedule": "每日18:00", "freshness_hours": 24},
    "index_weight":    {"source": "Tushare", "market": "指数", "granularity": "月频",
                        "script": "scripts/sync_tushare.py", "schedule": "每月初", "freshness_hours": 730},
    "index_member":    {"source": "Tushare", "market": "指数", "granularity": "快照",
                        "script": "scripts/sync_tushare.py", "schedule": "每月", "freshness_hours": 730},
    # —— 股东 / 解禁 / 停牌 4（季频/日频，ann_date 公告日索引防前视）——
    "top10_holders":      {"source": "Tushare", "market": "A股", "granularity": "季频",
                           "script": "scripts/sync_tushare.py", "schedule": "每季报披露窗口", "freshness_hours": 2190},
    "top10_floatholders": {"source": "Tushare", "market": "A股", "granularity": "季频",
                           "script": "scripts/sync_tushare.py", "schedule": "每季报披露窗口", "freshness_hours": 2190},
    "share_float":     {"source": "Tushare", "market": "A股", "granularity": "1d",
                        "script": "scripts/sync_tushare.py", "schedule": "每日18:00", "freshness_hours": 24},
    "suspend_d":       {"source": "Tushare", "market": "A股", "granularity": "1d",
                        "script": "scripts/sync_tushare.py", "schedule": "每日18:00", "freshness_hours": 24},
    # —— 特色筹码 1（cyq_perf，300/分独立通道，日频）——
    "cyq_perf":        {"source": "Tushare", "market": "A股", "granularity": "1d",
                        "script": "scripts/sync_tushare.py", "schedule": "每日18:00", "freshness_hours": 24},
    # ============================================================================
    # Plan C Task 6：宏观经济原始指标数据集注册（8 湖，与 Task 11 股票类对等）
    # ============================================================================
    # 设计意图：这 8 个宏观原始指标湖已在 TUSHARE_DATASETS + LAKE_CONFIG 注册（Task 3-5 同步器 +
    # reader 用），但 DATASET_REGISTRY 缺元信息 → 前端 DataLakeView 表格看不到这些资产。
    # 本组补 source=Tushare + market=宏观 + granularity + freshness_hours，让前端可反射。
    #
    # Why 按数据集粒度注册（与股票类同范式）：每个宏观指标的披露频率不同（CPI/PPI/PMI 月频、
    # GDP 季频、Shibor 日频），freshness_hours 因频率而异，按数据集粒度才能精确表达。
    #
    # 字段口径（freshness_hours）：
    #   月频 = 730h（30.4天*24，自然月阈值，过此即标 stale）
    #   季频 = 2190h（与财报同口径，90天*24.3）
    #   日频 = 24h（Shibor/交易所统计，每日披露）
    # —— C 组·宏观原始指标 6（CPI/PPI/GDP/PMI 月季频 + Shibor 日频，tushare_sync 写）——
    "cn_cpi":          {"source": "Tushare", "market": "宏观", "granularity": "月频",
                        "script": "scripts/sync_tushare.py", "schedule": "每月中旬",  "freshness_hours": 730},
    "cn_ppi":          {"source": "Tushare", "market": "宏观", "granularity": "月频",
                        "script": "scripts/sync_tushare.py", "schedule": "每月中旬",  "freshness_hours": 730},
    "cn_gdp":          {"source": "Tushare", "market": "宏观", "granularity": "季频",
                        "script": "scripts/sync_tushare.py", "schedule": "每季发布",  "freshness_hours": 2190},
    "cn_pmi":          {"source": "Tushare", "market": "宏观", "granularity": "月频",
                        "script": "scripts/sync_tushare.py", "schedule": "月末",      "freshness_hours": 730},
    "shibor":          {"source": "Tushare", "market": "宏观", "granularity": "1d",
                        "script": "scripts/sync_tushare.py", "schedule": "每日11:00", "freshness_hours": 24},
    "shibor_quote":    {"source": "Tushare", "market": "宏观", "granularity": "1d",
                        "script": "scripts/sync_tushare.py", "schedule": "每日11:00", "freshness_hours": 24},
    # —— C 组·交易所成交统计 1（市场宽度，by=date，tushare_sync 写）——
    # B 类合并：原 szse_daily/sse_daily 两数据集合为 mkt_daily（daily_info 接口返沪深两市，
    # exchange 列区分）。LAKE_CONFIG key=mkt_daily（与 TUSHARE_DATASETS 一致，单一真相源）。
    "mkt_daily":       {"source": "Tushare", "market": "宏观", "granularity": "1d",
                        "script": "scripts/sync_tushare.py", "schedule": "每日18:00", "freshness_hours": 24},
}

# ============================================================
# 通用 Tushare 湖同步器注册表（Plan A/B/C 三大类采集共用框架）
# ============================================================
# 设计意图（配置驱动 / 显式至上）：每个数据集 = 一份声明式配置（接口/分页/字段/落湖），
# data/tushare_sync.py 的 sync_dataset(key) 统一执行，新增数据集只需在此注册一行，
# 不再为每个接口写一份同步脚本。三份 plan（股票/ETF/宏观）均复用本框架。
#
# 字段契约：
#   api:        tushare pro 接口名（pro.<api>(...)），如 income / moneyflow / index_daily
#   by:         分页模式 —— symbol（逐标的）/ date（逐交易日）/ single（单次不分页）
#   date_col:   前视红线 —— 用作时间索引的列。财报类必须用 ann_date（公告日），
#               绝不用 end_date（报告期）—— 报告期早于实际公告日，会导致前视偏差。
#   symbol_col: 标的列名（多数 ts_code，指数类为 ts_code；落湖 MultiIndex 第二级）
#   fields:     逗号分隔字段串（省配额：只拉所需列，避免全字段回传 + 落盘膨胀）
#   lake:       落湖 parquet 路径（与 LAKE_CONFIG["lakes"][key] 保持一致）
#   shard_dir:  可选，分片目录（断点续传，缺省 data_lake/shards/<key>）
TUSHARE_DATASETS: Dict[str, Dict[str, Any]] = {
    # —— 股票类（Plan A 各 Task 逐步填充）——
    "fina_income": {
        "api": "income", "by": "symbol",
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,total_revenue,n_income,n_income_attr_p",
        "lake": "data_lake/fina_income.parquet",
    },
    "fina_balance": {
        # 资产负债表（balancesheet）：单标的全历史一次返，按 symbol 分页
        "api": "balancesheet", "by": "symbol",
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,total_assets,total_liab,total_hldr_eqy_exc_min_int",
        "lake": "data_lake/fina_balance.parquet",
    },
    "fina_cashflow": {
        # 现金流量表（cashflow）：单标的全历史一次返，按 symbol 分页
        # ⚠️ 事实订正（真 token 探测）：旧 fields 含幻觉列 net_profit_cash_flow /
        # c_pay_acq_foroth_assets（API 不返回）→ 落湖后全 NaN。真实列见下
        # （net_profit=净利润现金流影响 / finan_exp=财务费用 / c_fr_sale_sg=销售商品收到的现金）。
        "api": "cashflow", "by": "symbol",
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,net_profit,finan_exp,c_fr_sale_sg",
        "lake": "data_lake/fina_cashflow.parquet",
    },
    "forecast": {
        # 业绩预告（forecast）：披露窗口通常 1月（年报预告）/4月（一季报）/7月（中报）/10月（三季报），
        # 按 symbol 分页拉全历史再按 ann_date 切区间，避免逐日拉取空窗期浪费配额。
        "api": "forecast", "by": "symbol",
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,type,p_change_min,p_change_max,min_range,max_range",
        "lake": "data_lake/forecast.parquet",
    },
    "express": {
        # 业绩快报（express）：披露窗口与 forecast 类似，按 symbol 分页。
        "api": "express", "by": "symbol",
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,revenue,n_income,total_profit",
        "lake": "data_lake/express.parquet",
    },
    "dividend": {
        # 分红送股（dividend）：date_col=ann_date（分红方案公告日）。
        # ⚠️ 前视红线：绝不用 end_date（接口无此列）/ record_date（除权登记日，晚于公告日）
        # / div_proc（预案/实施等文本进度字段，非日期）。ann_date 是市场最早能感知分红的时点。
        "api": "dividend", "by": "symbol",
        "no_date_filter": True,  # 分红事件类：不认 start/end_date（实测传了返空），拉全历史
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,div_proc,stk_div,cash_div,record_date,ex_date",
        "lake": "data_lake/dividend.parquet",
    },
    # —— 个股资金流（moneyflow）：单日全市场，按 date 分页 ——
    # 物理意图：主力资金（大单/特大单）流向是动量/反转因子核心。单次请求返全市场当日，
    # 请求数=交易日数（效率高）。by=date 时 symbol 从 ts_code 列取（不从文件名）。
    "moneyflow": {
        "api": "moneyflow", "by": "date",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,buy_sm_amount,sell_sm_amount,buy_elg_amount,sell_elg_amount,net_mf_amount",
        "lake": "data_lake/moneyflow.parquet",
    },
    # —— 龙虎榜（top_list/top_inst）：单日全市场，按 date 分页 ——
    # dragon_list 湖切 Tushare（原 akshare 源退役）；top_inst 机构席位单独湖。
    "top_list": {
        # ⚠️ 事实订正：龙虎榜个股买卖额真实列名是 l_buy / l_sell（非 buy_amount/sell_amount）。
        # l_amount=龙虎榜成交总额 / net_amount=净额 / amount=全市场成交额。
        "api": "top_list", "by": "date",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,name,close,pct_change,amount,net_amount,l_buy,l_sell",
        "lake": "data_lake/dragon_list.parquet",  # 复用 dragon_list 湖（切 Tushare）
    },
    "top_inst": {
        # ⚠️ 事实订正（结构重写）：config 原误配成 top_list 同款字段，实际 top_inst 是
        # 「龙虎榜机构席位」明细——exalter=营业部/机构名 / buy,buy_rate=买入额及占比 /
        # sell,sell_rate=卖出额及占比 / net_buy=净买入 / side=业务侧(E买F卖) / reason=上榜原因。
        # 无 name/close/pct_change（那些是个股字段，营业部席位明细不返）。
        "api": "top_inst", "by": "date",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "trade_date,ts_code,exalter,buy,buy_rate,sell,sell_rate,net_buy,side",
        "lake": "data_lake/top_inst.parquet",
    },
    # —— 融资融券（margin/margin_detail/margin_secs）——
    # margin（市场汇总，symbol_col=exchange_id 交易所）/ margin_detail（逐标的 ts_code）：by=date。
    # margin_secs（标的列表快照）：by=single，落扁平 DataFrame（非时序 MultiIndex）。
    "margin": {
        # ⚠️ 事实订正：删幻觉列 rqchl（市场汇总接口不返回，仅 margin_detail 返），
        # 新增 rzrqye(融资融券余额)/rqyl(融券余量)——真 token 探测确认的市场级列。
        "api": "margin", "by": "date",
        "date_col": "trade_date", "symbol_col": "exchange_id",
        "fields": "exchange_id,trade_date,rzye,rzmre,rzche,rqye,rqmcl,rzrqye,rqyl",
        "lake": "data_lake/margin.parquet",
    },
    "margin_detail": {
        "api": "margin_detail", "by": "date",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,rzye,rzmre,rqye,rqmcl,rzche,rqchl",
        "lake": "data_lake/margin_detail.parquet",
    },
    "margin_secs": {
        # 标的列表快照（单次拉全市场不分页）→ single 模式落扁平 DataFrame。
        # ⚠️ 事实订正：删幻觉列 start_date（API 不返回），真实列为 trade_date+exchange。
        "api": "margin_secs", "by": "single",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,name,exchange",
        "lake": "data_lake/margin_secs.parquet",
    },
    # —— 北向资金（hsgt_top10/moneyflow_hsgt）：切 Tushare 替代 akshare sync_north_flow ——
    # hsgt_top10 当日十大成交股（有 ts_code，by=date），复用 north_flow 湖（切源）。
    "hsgt_top10": {
        # ⚠️ 事实订正：删幻觉列 vol / north_direction（API 不返回）。真实列为
        # close/change/rank/market_type/amount/net_amount/buy/sell。north_money 在
        # moneyflow_hsgt 市场级接口里，hsgt_top10 只返个股十大成交明细。
        # ⚠️ 实测局限（2026-07 probe）：buy/sell/net_amount 为代理近期降级字段——2023 历史
        #   段全有值，2024+ tnskhdata 代理不再返（None 或缺列），落湖约 63% NaN。核心 amount
        #   全期 0% NaN 可用。保留三字段不丢 2023 历史，下游北向明细分析须知近期缺值。
        "api": "hsgt_top10", "by": "date",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "trade_date,ts_code,name,close,rank,amount,net_amount,buy,sell",
        "lake": "data_lake/north_flow.parquet",  # 复用 north_flow 湖（切 Tushare）
    },
    # moneyflow_hsgt 市场级北/南向资金（无个股 symbol）→ by=date 逐日拉（市场级时序）。
    # ⚠️ quick 批订正（by=single → by=date）：原 single 模式 _sync_single 只传 fields 不传
    # 任何日期参数，实测 moneyflow_hsgt() 无参抛 Invalid request parameters（接口硬性要求
    # 日期参数）。实测 moneyflow_hsgt(trade_date='20240105')=1 行（接受单日 trade_date 参数，
    # 返回当日沪深港通合计资金流）。改 by=date 后 _sync_by_date 逐日传 trade_date，每日返
    # 1 行市场级时序，落 MultiIndex(date, symbol)——symbol 层恒等于 trade_date（市场级无个股，
    # 与 mkt_daily 同构，_build_multiindex 走 symbol_col==date_col 的冗余 symbol 分支）。
    # ⚠️ 事实订正：删幻觉列 sgt_ss / sgt_sz（API 只返沪/深股通合计 hgt/sgt，不分沪深细分）。
    "moneyflow_hsgt": {
        "api": "moneyflow_hsgt", "by": "date",
        "date_col": "trade_date", "symbol_col": "trade_date",
        "fields": "trade_date,ggt_ss,ggt_sz,hgt,sgt,north_money,south_money",
        "lake": "data_lake/moneyflow_hsgt.parquet",
    },
    # —— 板块/概念（Plan A Task 7）：补 sector 的 Tushare 维度 ——
    # concept（概念列表）：单次返回全量概念字典（code+name），无时间维度 → by=single 落扁平 df。
    # date_col 填 code 仅为占位（single 模式 _sync_single 不触碰索引，原样 to_parquet），
    # 概念字典本身是静态参照表，无时序索引语义。
    "concept": {
        # ⚠️ 事实订正（B 类·方法名错）：tnskhdata 代理无任何概念接口
        # （concept / stock_concept / concept_detail 均 No such method），本数据集不可下载。
        # 标 _unavailable 由通用同步器 sync_dataset 检测后跳过并打印提示，不下载/不报错。
        # 待 akshare 换源（akshare 有概念板块接口）后恢复。
        "api": "concept", "by": "single",
        "date_col": "code", "symbol_col": "code",
        "fields": "code,name",
        "lake": "data_lake/concept.parquet",
        "_unavailable": "tnskhdata 无概念接口（concept/stock_concept/concept_detail 均 No such method），待 akshare 换源",
    },
    # ths_daily（同花顺板块指数日线）：单日全市场板块行情一次返（ts_code 为板块指数代码如 885572.TI，
    # 非个股）→ by=date 分页。symbol 从 ts_code 列取（Task 1 fix 已保证 by=date 不从文件名取 symbol）。
    # 物理意图：板块指数动量/轮动因子核心，与 sector 湖（akshare 申万行业日线）互补——
    # ths_daily 是同花顺概念板块维度，sector 是申万行业维度，两湖不混写。
    "ths_daily": {
        "api": "ths_daily", "by": "date",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,open,high,low,close,pre_close,vol,amount,pct_change",
        "lake": "data_lake/ths_daily.parquet",
    },
    # —— 指数（index_daily/index_weight/index_member）：Plan A Task 8 ——
    # index_daily 指数日线行情：单指数全历史一次返（接口支持 ts_code + start/end_date），
    # 按 symbol 分页；symbols=指数代码列表（由调用方显式传，如 000300.SH/000905.SH/000016.SH，
    # 不能复用 _load_universe 的股票列表）。date_col=trade_date（指数日线只有交易日，无公告概念，
    # 无前视风险）。fields 对齐 tushare index_daily 输出（vol=成交量手数，amount=成交额千元）。
    "index_daily": {
        "api": "index_daily", "by": "symbol",
        "universe": "index",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,open,high,low,close,vol,amount",
        "lake": "data_lake/index_daily.parquet",
    },
    # index_weight 指数成分权重：by=date，逐交易日拉全市场所有指数当日成分权重，
    # symbol_col=con_code（成分股代码，如 000001.SZ）作 MultiIndex 第二级。
    # ⚠️ 事实风险：tushare 官方标注 index_weight 为「月度数据」（建议 start/end 输入当月首末日），
    # 但历史数据中混有日频权重（见 waditu/tushare#1825）。by=date 逐日拉取会在月度日返空，
    # 浪费配额但逻辑正确；若配额紧张可改 by=symbol + index_code 逐指数拉全历史更省。
    # 现按 brief 声明 by=date，保留 con_code 作 symbol 的跨指数复用语义。
    "index_weight": {
        "api": "index_weight", "by": "date",
        "date_col": "trade_date", "symbol_col": "con_code",
        "fields": "index_code,con_code,trade_date,weight",
        "lake": "data_lake/index_weight.parquet",
    },
    # index_member 指数成分权重（B 类·方法名错订正）：tnskhdata 无 index_member 方法
    # （No such method），但 index_weight 方法可用且返回成分股权重。本数据集复用 index_weight
    # 接口按 symbol（逐指数代码）拉全历史成分权重，date_col=trade_date（权重日，无公告概念，
    # 无前视风险）。注意：此数据集不再是「成分股进出记录」（in_date/out_date 字段 index_weight
    # 不返回），而是「成分权重时序」——业务上等价于逐指数的权重历史，与 index_weight
    # （by=date 全市场当日）区别仅在分页口径（逐指数 vs 逐交易日）。
    "index_member": {
        "api": "index_weight", "by": "symbol",
        "universe": "index",
        "date_col": "trade_date", "symbol_col": "con_code",
        "fields": "index_code,con_code,trade_date,weight",
        # code_param=index_code：index_weight 接口按指数代码拉取，参数名是 index_code
        # （非通用 ts_code），在 _sync_by_symbol 用此参数名传指数代码。
        "code_param": "index_code",
        "lake": "data_lake/index_member.parquet",
    },
    # —— 股东/解禁/停牌（Plan A Task 10）——
    # ⚠️ 前视红线（brief Step 1 草稿曾误写 end_date，此处钉死 ann_date）：
    # top10_holders/floatholders 的 end_date 是「报告期末」（如 20231231），而股东名单
    # 要等到季报/年报实际公告日（ann_date，如 20240430）市场才能感知。用 end_date 索引
    # 等于在公告前数月就已知前十大股东，回测出现前视偏差，故 date_col 必须用 ann_date。
    "top10_holders": {
        # 前十大流通股东（top10_holders）：单标的全历史一次返，按 symbol 分页。
        # 物理意图：股东集中度/筹码结构是中线选股与回避庄股的核心因子；hold_ratio
        # （持股比例）+ holder_name 用于识别机构/产业资本动向。
        "api": "top10_holders", "by": "symbol",
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,holder_name,hold_amount,hold_ratio",
        "lake": "data_lake/top10_holders.parquet",
    },
    "top10_floatholders": {
        # 前十大流通股东（top10_floatholders）：与 top10_holders 同结构，仅统计口径
        # 切到「流通股」（剔除限售股）。date_col 同样钉死 ann_date（同一前视红线）。
        "api": "top10_floatholders", "by": "symbol",
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,holder_name,hold_amount,hold_ratio",
        "lake": "data_lake/top10_floatholders.parquet",
    },
    "share_float": {
        # 限售股解禁（share_float）：单次拉全量解禁记录，by=single（不再逐日分页）。
        # 物理意图：解禁日前后存在系统性抛压（解禁股可流通），是事件驱动/回避策略关键信号。
        # date_col=ann_date（解禁公告日）—— float_date（实际解禁日）可能晚于公告，
        # 用 ann_date 索引保证回测只读到「市场已知」的解禁信息，无前视。
        # ⚠️ quick 批订正（by=date → by=single，积分红线）：实测 share_float 服务端单次硬上限
        # 6000 行，且 trade_date 参数无效（share_float 用 ann_date/float_date，非 trade_date）。
        # 原 by=date 逐日传 trade_date → 每日返全量第一页 6000 行 → 730 日重复拉 ~435 万行
        # 白烧积分。改 by=single 一次拉最新 6000 条解禁记录（ann_date 跨近 1-2 年），覆盖近期
        # 解禁事件足够（事件驱动策略关注近期解禁压力），远期历史解禁已过期无信号价值。
        # ⚠️ 事实订正：删幻觉列 float_share_share（API 不返回），真实列含 float_ratio
        # （解禁比例）/ holder_name（持有人）/ share_type（解禁类型）。
        "api": "share_float", "by": "single",
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,float_date,float_share,float_ratio,holder_name,share_type",
        "lake": "data_lake/share_float.parquet",
    },
    "suspend_d": {
        # 每日停复牌（suspend_d）：单日全市场一次返，按 date 分页。
        # 物理意图：停牌期间无法交易，回测撮合层必须据此跳过；停牌时点（早盘/午盘）/
        # 类型是事件因子。
        # ⚠️ 事实订正（结构重写）：真 token 探测确认 API 仅返回 4 列
        # （ts_code, trade_date, suspend_timing, suspend_type），不返回 ann_date /
        # suspend_date / resume_date / ann_reason / reason_type（旧版字段已停用）。
        # 前视防护降级：date_col 改 trade_date（停牌日）。理想应用 ann_date（公告日，
        # 市场最早能预知停牌的时点），但 API 不返回 ann_date，只能用 trade_date（停牌
        # 当日）——回测在停牌当日开盘前可能尚未感知，存在轻微前视残留，但停牌信息通常
        # 盘前/盘中即时公告，用 trade_date 作降级索引可接受（停牌当日不撮合即可）。
        # ⚠️ 实测局限（2026-07 probe）：suspend_timing 99% NaN（API 返该列但绝大多数停牌无
        #   午盘/全天时点标注），suspend_type 为主要有效字段（0% NaN）。保留 timing 因 API 结构
        #   返该列且 test_a10 钉死 4 列契约；下游停牌过滤应以 suspend_type 为准。
        "api": "suspend_d", "by": "date",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,suspend_timing,suspend_type",
        "lake": "data_lake/suspend_d.parquet",
    },
    # ===== ETF 专题（Plan B Task 1-5）：fund_basic/fund_daily/fund_nav/fund_portfolio/fund_share =====
    # 物理意图：ETF 全景数据（列表/日线/净值/持仓/份额），用于 ETF 动量/折溢价/跟踪误差/份额变动分析。
    # fund_basic(market='E') 是其余 4 个 by=symbol 接口的标的池来源（_load_etf_universe 读它）。
    "fund_basic": {
        # ETF/基金基础信息（fund_basic market='E'）：单次拉全市场场内基金列表，不分页 → single 模式。
        # Why single 而非 symbol：列表类接口本身无「标的维度分页」语义，一次性返全量更省配额。
        # Why params market='E'（quick 批订正，事实修正）：fund_basic 全量返 15000 行（含 13827
        #   场外基金 O + 1173 场内基金 E）。实测 market='EFT' 返 **0 行**（EFT 是错误码，非 Tushare
        #   真实 market 值），market='E' 才返场内基金。params market='E' 由 _sync_single 合并进
        #   kwargs 传给 API，在服务端过滤场内基金，避免拉到全量 15000 污染标的池。
        # Why date_col=found_date：single 模式当前不建时间索引（_sync_single 直接落扁平 df），
        #   date_col 仅为 schema 完备保留（Plan C 才给 single 加 index_mode=datetime）。
        "api": "fund_basic", "by": "single",
        "date_col": "found_date", "symbol_col": "ts_code",
        "fields": "ts_code,name,market,management,custodian,found_date,list_date,issue_date,delist_date",
        "params": {"market": "E"},  # 服务端过滤场内基金（实测 market='EFT'=0 行，E 才正确）
        "lake": "data_lake/fund_basic.parquet",
    },
    "fund_daily": {
        # ETF 日线（fund_daily）：单 ETF 全历史一次返，按 symbol 分页。
        # ⚠️ 字段名陷阱：fund_daily 返回 vol（非 volume），与股票 daily/pro_bar 的 volume 不一致。
        #   为对齐 DataLakeReader 双湖（daily 湖列名 volume）切片语义，配置 rename={'vol':'volume'}，
        #   通用同步器在 _cleanse 后、落 shard 前应用 rename（见 data/tushare_sync.py）。
        #   Why 对齐：etf_daily 湖若保留 vol 列名，与 a_shares_daily 的 volume 列名分叉，跨湖因子
        #   计算需写两套列名分支，违反单一真相原则。
        "api": "fund_daily", "by": "symbol",
        "universe": "etf",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,open,high,low,close,vol,amount",
        "rename": {"vol": "volume"},  # vol→volume 列名归一（与股票日线湖对齐）
        "lake": "data_lake/etf_daily.parquet",
    },
    "fund_nav": {
        # ETF 净值（fund_nav）：单位净值/累计净值，单 ETF 全历史一次返，按 symbol 分页。
        # Why date_col=nav_date：净值披露日（nav_date）即市场可感知净值的确切时点，
        #   无公告滞后前视风险（净值本身是披露产物，nav_date 即公开日）。
        # ⚠️ 事实订正：删幻觉列 accum_nav_rate（API 不返回）。真实列含 ann_date（公告日）/
        #   accum_div（累计分红）/ net_asset, total_netasset（净资产）/ adj_nav（复权净值）。
        "api": "fund_nav", "by": "symbol",
        "universe": "etf",
        "date_col": "nav_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,nav_date,unit_nav,accum_nav,accum_div,adj_nav",
        "lake": "data_lake/etf_nav.parquet",
    },
    "fund_portfolio": {
        # ETF 持仓（fund_portfolio）：前十大重仓股，按 symbol 分页。
        # ⚠️ 前视红线：date_col=ann_date（公告日），绝不用 end_date（报告期，如 20231231）。
        #   持仓数据公告滞后（季报/半年报披露窗口），end_date 早于 ann_date 数月，用 end_date 索引
        #   会在报告期内提前看到持仓构成，构成前视偏差。
        # ⚠️ 事实订正（结构重写）：删幻觉列 name / stk_value / stk_value_ratio（API 不返回）。
        #   真实列：symbol（重仓股代码）/ mkv（市值）/ stk_mkv_ratio（占股票市值比）/
        #   stk_float_ratio（占流通股比）。原 stk_value 真实名为 mkv。
        "api": "fund_portfolio", "by": "symbol",
        "universe": "etf",
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,symbol,mkv,amount,stk_mkv_ratio,stk_float_ratio",
        "lake": "data_lake/etf_portfolio.parquet",
    },
    "fund_share": {
        # ETF 份额变动（fund_share）：按 symbol 分页。
        # Why date_col=trade_date：份额变动按交易日披露，trade_date 即公开可感知时点。
        # ⚠️ 事实订正（结构重写）：删幻觉列 share_unissue / total_share / float_share
        #   （API 不返回）。真实列为 fd_share（基金份额）/ fund_type（基金类型）/
        #   market（市场）。fd_share 即当日总份额。
        "api": "fund_share", "by": "symbol",
        "universe": "etf",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,fd_share,fund_type,market",
        "lake": "data_lake/etf_share.parquet",
    },
    # —— C 组·宏观经济原始指标（Plan C Task 3：cn_cpi/ppi/gdp/pmi）——
    # 物理意图：CPI/PPI/PMI 月频 + GDP 季频是宏观择时（利率/库存/景气周期）的根因指标。
    # by=single：宏观接口单次按区间拉全量（cn_cpi 参数 start_m/end_m 为 YYYYMM 月串，
    # cn_gdp 无区间参数一次返全历史），无标的/交易日分页，故走 single 管道。
    # index_mode=datetime：宏观湖落 DatetimeIndex（无 symbol 层，区别于股票 MultiIndex）。
    # ⚠️ 事实审查（fields/参数名待探测，见 plan notes）：
    #   cn_cpi/cn_ppi/cn_pmi 月频 → date_col=month（YYYYMM），format 自动推断见 _sync_single
    #   cn_gdp 季频 → date_col=quarter（YYYYQ1），format 走 %YQ 季度解析分支
    "cn_cpi": {
        # CPI 月频：nt_yoy 全国同比（核心通胀口径）/ nt_mom 环比 / nt_val 全国当月值。
        # ⚠️ 事实订正：删幻觉列 yty_yoy（API 不返回，城镇同比真实名为 town_yoy）。
        "api": "cn_cpi", "by": "single",
        "date_col": "month", "symbol_col": "month",
        "fields": "month,nt_val,nt_yoy,nt_mom,town_yoy",
        "index_mode": "datetime",
        "lake": "data_lake/cn_cpi.parquet",
    },
    "cn_ppi": {
        # PPI 月频：ppi_yoy 工业生产者出厂价格同比（工业品通缩/通胀先行指标）
        "api": "cn_ppi", "by": "single",
        "date_col": "month", "symbol_col": "month",
        "fields": "month,ppi_yoy,ppi_mom",
        "index_mode": "datetime",
        "lake": "data_lake/cn_ppi.parquet",
    },
    "cn_gdp": {
        # GDP 季频：gdp 不变价绝对额 / gdp_yoy 同比 / pi+si+ti 三次产业贡献
        # date_col=quarter（YYYYQ1），前视红线无 end_date 概念（宏观指标当日发布即生效）
        "api": "cn_gdp", "by": "single",
        "date_col": "quarter", "symbol_col": "quarter",
        "fields": "quarter,gdp,gdp_yoy,pi,si,ti",
        "index_mode": "datetime",
        "lake": "data_lake/cn_gdp.parquet",
    },
    "cn_pmi": {
        # PMI 月频：制造业采购经理指数（50 为荣枯线）。
        # ⚠️ 事实订正（结构重写）：cn_pmi 接口返回的不是中文指标名，而是 PMI 分项编码列。
        # 旧 fields（month/manufacturing_pmi/business_index_pmi）全是幻觉——API 返回大写
        # MONTH 列 + PMI010000（制造业 PMI 主指数）等编码列。date_col=MONTH（注意大写，
        # API 列名如此，format 推断走 6 位月频分支）。fields 仅取核心主指数 PMI010000，
        # 其余 60+ 分项编码（生产/新订单/库存/从业人员等）按需在下游补取，避免湖膨胀。
        # 接口参数（start_m/end_m）仍用小写 month 语义（见探测脚本 PARAMS）。
        "api": "cn_pmi", "by": "single",
        "date_col": "MONTH", "symbol_col": "MONTH",
        "fields": "MONTH,PMI010000",
        "index_mode": "datetime",
        "lake": "data_lake/cn_pmi.parquet",
    },
    # —— C 组·银行间同业拆放（Plan C Task 4：shibor/shibor_quote）——
    # 物理意图：Shibor 是利率衍生品定价基准 + 流动性紧张先行指标（2007 起银行间报价）。
    # by=single index_mode=datetime：shibor 单一时间序列（1w..1y 各期限利率列），落 DatetimeIndex。
    # shibor_quote 含 bank（报价行）列，作数据列保留（不拆 MultiIndex，保持日期索引扁平）。
    "shibor": {
        # date_range=true（quick 批订正）：shibor 无参返最近 2000 行（2018 起全历史分页上限），
        # 加 start_date/end_date 区间精确返近 3 年，避免落盘膨胀 + 烧配额拉无用远期历史。
        # _sync_single 检测 date_range=true 后把 sync_dataset 的 start/end 转 start_date/end_date 传 API。
        "api": "shibor", "by": "single",
        "date_col": "date", "symbol_col": "date",
        "fields": "date,on,1w,2w,1m,3m,6m,9m,1y",
        "index_mode": "datetime",
        "date_range": True,  # 区间拉取（避免无参返 2000 行全历史）
        "lake": "data_lake/shibor.parquet",
    },
    "shibor_quote": {
        # shibor_quote 逐报价行明细：bank 列作数据保留，date_col=date 落 DatetimeIndex
        # ⚠️ 事实订正（结构重写）：shibor_quote 是双边报价（每个期限拆入 b / 拆出 a 两列），
        # 非均值。旧 fields（on/1w/...单列）是 shibor 均值湖的字段，误抄过来。真实列为
        # 各期限的 _b（拆入）/ _a（拆出）双列。date_col=date 落 DatetimeIndex。
        # date_range=true（quick 批订正）：与 shibor 同理，加区间精确返近 3 年。
        # ⚠️ 实测局限（2026-07 probe）：接口约 4000 行服务端上限（17 银行 × ~235 日 ≈ 1 年），
        #   date_range 3 年区间实际仅落近 1 年（2025-08 起）。全历史需按区间分批拉（follow-up）。
        "api": "shibor_quote", "by": "single",
        "date_col": "date", "symbol_col": "date",
        "fields": "date,bank,on_b,on_a,1w_b,1w_a,2w_b,2w_a,1m_b,1m_a,3m_b,3m_a,6m_b,6m_a,9m_b,9m_a,1y_b,1y_a",
        "index_mode": "datetime",
        "date_range": True,  # 区间拉取（避免无参返近期分页上限，精确近 3 年）
        "lake": "data_lake/shibor_quote.parquet",
    },
    # —— C 组·交易所成交统计（Plan C Task 5，B 类合并订正：szse_daily/sse_daily → mkt_daily）——
    # 物理意图：交易所日级市值/PE/挂牌数是市场宽度（breadth）指标，宏观择时辅助。
    # ⚠️ 事实订正（B 类·方法名错 + 合并）：tnskhdata 无 szse_daily / sse_daily 方法
    # （均 No such method），Tushare 真实接口是 daily_info（全市场交易所日级统计，含沪深两市，
    # 由 exchange 列区分）。原两个数据集合并为一个 mkt_daily（api=daily_info），按 date 分页。
    # daily_info 单日全市场汇总（ts_code=交易所代码如 SSE/SZSE），date_col/symbol_col 均
    # trade_date（市场级时序，落 MultiIndex(date, symbol) 但 symbol 层恒等于 trade_date）。
    # 真实列见 fields（com_count=上市公司数 / total_mv=总市值 / float_mv=流通市值 /
    # trans_count=成交笔数 / pe=市盈率 / tr=换手率 / exchange=交易所）。
    "mkt_daily": {
        # ⚠️ 实测局限（2026-07 probe）：total_share/float_share 约 75% NaN（daily_info 对多数
        #   市场分层如沪 A/B 股不返总股本，仅个别分层有值），属接口正常稀疏非 bug；
        #   com_count/total_mv/float_mv/pe 为有效字段，下游取市值宽度用 total_mv/float_mv。
        "api": "daily_info", "by": "date",
        "date_col": "trade_date", "symbol_col": "trade_date",
        "fields": "trade_date,ts_code,ts_name,com_count,total_share,float_share,total_mv,float_mv,pe,exchange",
        "lake": "data_lake/mkt_daily.parquet",
    },
    # —— D 组·特色筹码（Plan A Task 9：cyq_perf，300/分独立通道）——
    # 物理意图：cyq_perf 是筹码分布及胜率（cost_5/15/50/85/95 五档成本 + weight_avg 平均成本
    # + winner_rate 获利盘比例 + his_low/his_high 历史高低价），用于判断个股筹码集中度与
    # 盈亏结构，是主力动向与支撑压力分析的特色数据源。
    # by=symbol：逐标的拉全历史筹码分布（单标的一次返，无分页），与财报/股东同管道。
    # date_col=trade_date：交易日（非报告期，无前视风险），与日线对齐。
    # quota_type=special：特色数据按 300 次/分单独计频通道。
    #   限流仍走统一 tushare_rate_limiter（refill_rate=1 token/s + 突发桶 capacity=5，
    #   持续 ~60/分，远严于特色数据 300/分配额，故实际消耗 ≤ 配额上限，不会触发
    #   Tushare 端 300/分限频）；quota_type 仅作日志层标记，便于限频问题时快速定位
    #   特色数据通道，不新增单独限流器（极简，拒绝过度设计）。
    #   Why 不按 300/分放宽：特色数据为低频批量任务，统一限流器已足够且与常规数据集
    #   共享桶，避免按通道各建一桶导致的阈值失真与复杂度膨胀。
    "cyq_perf": {
        # ⚠️ 事实订正：五档成本列真实名为 cost_5pct/cost_15pct/cost_50pct/cost_85pct/cost_95pct
        # （非 cost_5/cost_15/...，旧名是缩写幻觉，API 返回全名带 pct 后缀）。
        "api": "cyq_perf", "by": "symbol",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,his_low,his_high,cost_5pct,cost_15pct,cost_50pct,cost_85pct,cost_95pct,weight_avg,winner_rate",
        "lake": "data_lake/cyq_perf.parquet",
        "quota_type": "special",  # 特色数据：300/分独立通道（纯日志标记，限流仍走统一 rate_limiter）
    },
}

# 同步哨兵目录：POST /sync/{key} 触发时 touch {key}（=syncing）；成功删除，失败写 {key}.failed。
# 置于 data_lake/ 下便于与数据资产共同观测；运行时由 data_service 自动建目录。
SYNCING_DIR = os.path.join("data_lake", ".syncing")
