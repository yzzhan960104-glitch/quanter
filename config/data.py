# -*- coding: utf-8 -*-
"""数据层核心配置（数据湖/JQData/AKShare/数据源）—— 从 config.py 拆出（归属：数据层）。

本模块含数据层最关键的配置：
    - DATA_CONFIG：数据源通用配置（时区/缓存/前向填充天数）
    - LAKE_CONFIG：数据湖配置（**跨段拼接**：base 段先定义，再追加 lakes/default_lake）
    - MACRO_CLIENT_CONFIG：宏观另类数据客户端（Yahoo/Alpha Vantage）
    - JQDATA_CONFIG：JQData 分钟级客户端（含配额闸门三道防线，**命门参数勿动**）
    - AKSHARE_CONFIG：AKShare 数据流（替代 Tushare）

**LAKE_CONFIG 保序红线**：base 段（default_path/shard_dir/years_default）必须先定义，
随后才能追加 LAKE_CONFIG["lakes"] / LAKE_CONFIG["default_lake"]——顺序颠倒会 NameError。
base 段引用 _os.getenv，故本模块顶部 import os as _os（从原 config.py 行 108 带过来的别名，
让搬运代码零改动）。

dotenv 副作用由 config/__init__.py 包入口最早执行，保证本模块 _os.getenv 读到 .env 注入值。
"""
import os as _os
from typing import Dict, Any


# 数据源配置
DATA_CONFIG = {
    "default_timezone": "Asia/Shanghai",
    "cache_dir": "data/cache",
    "max_missing_fill": 5,  # 最大前向填充天数（防范停牌期跨度过长）
}

# 数据湖（Epic 1）
# Parquet 作为 A 股日线落盘格式，列式存储兼顾读写吞吐与内存友好；
# shard_dir 用于按年/月分片，避免单文件膨胀导致读取 OOM。
LAKE_CONFIG = {
    "default_path": _os.getenv("DATA_LAKE_PATH", "data_lake/a_shares_daily.parquet"),
    "shard_dir": "data_lake/shards",
    "years_default": 10,
}

# 宏观另类数据客户端（Epic 5）
# yfinance_symbols: 标普/原油/黄金/VIX 的 Yahoo Finance 标准代号；
# av_treasury_maturities: Alpha Vantage 美债收益率关键期限，覆盖短端与长端。
MACRO_CLIENT_CONFIG = {
    "yfinance_symbols": {"SPX": "^GSPC", "CL": "CL=F", "GC": "GC=F", "VIX": "^VIX"},
    "av_treasury_maturities": ["3MO", "2Y", "10Y", "30Y"],
}

# ============================================================
# 宏观 CTA 究极重构：JQData / AKShare / 多湖注册
# （数据流从 Tushare 切轨到 AKShare，并引入 JQData 分钟级）
# ============================================================

# JQData 分钟级客户端（Epic 1）
# 设计意图：JQData 按「调用次数」计费，必须在客户端侧建立配额闸门，
# 任何一次越界调用都可能触发超额扣费，故三道防线缺一不可：
#   - quota_manual_limit：本地手动计数到 95 万即硬停，留 5 万余量；
#   - quota_warn_spare：与 get_query_count 差值低于 5 万即告警；
#   - calibrate_every：每 10 次本地计数用服务端计数校准，防本地漂移。
JQDATA_CONFIG: Dict[str, Any] = {
    "freq_default": "5m",
    "quota_warn_spare": 50_000,      # spare<5万 即停
    "quota_manual_limit": 950_000,   # 手动计数 95万 即停
    "calibrate_every": 10,           # 每 10 次用 get_query_count 校准
}

# AKShare 数据流（替代 Tushare）
# 设计意图：AKShare 为开源数据源，无 Token 与配额限制，但接口字段
# 存在上游漂移风险，故锁定 active_pool_size / top_sectors / momentum_window
# 等业务阈值于 config，避免散落在调用点难以维护。
AKSHARE_CONFIG: Dict[str, Any] = {
    "qfq": "qfq",
    "active_pool_size": 50,
    "top_sectors": 3,
    "momentum_window": 20,
}

# 多湖路径注册（DataLakeReader 按 key 缓存）
# 设计意图：从单湖（仅日线）扩展到 macro/sector/daily/minute/crypto 五湖，
# DataLakeReader 通过此 dict 按 key 寻址并缓存已打开的 Parquet 句柄，
# 避免重复 IO 打开造成的句柄泄漏与内存膨胀。
# 注意：此处仅「追加」lakes / default_lake 两个键到既有 LAKE_CONFIG，
# 不重定义整个字典，保持 default_path / shard_dir / years_default 不变。
LAKE_CONFIG["lakes"] = {
    "macro": "data_lake/macro_credit.parquet",
    "sector": "data_lake/sector.parquet",
    "daily": "data_lake/a_shares_daily.parquet",          # 全市场日线（sync_data_lake 写）
    "daily_active": "data_lake/a_shares_active.parquet",   # 活跃池日线（sync_sector_daily 写，与 daily 分流防互覆盖）
    "minute": "data_lake/a_shares_1min.parquet",
    "crypto": "data_lake/crypto_btc_1m.parquet",
    # P1 新增湖：parquet 缺失时 reader.load 离线降级（warning 不阻断启动）；sync 脚本就绪后落盘
    "fundamentals": "data_lake/fundamentals.parquet",      # 基本面因子面板 pe/pb/roe...（sync_fundamentals 写）
    "north_flow": "data_lake/north_flow.parquet",          # 北向资金日频净流入（sync_north_flow 写）
    "dragon_list": "data_lake/dragon_list.parquet",        # 龙虎榜明细（sync_dragon_list 写）
    # 通用 Tushare 湖同步器（Plan A/B/C）落湖：key 与 TUSHARE_DATASETS 一一对应
    "fina_income": "data_lake/fina_income.parquet",        # 利润表（tushare_sync 写，MultiIndex date/symbol）
    "fina_balance": "data_lake/fina_balance.parquet",      # 资产负债表
    "fina_cashflow": "data_lake/fina_cashflow.parquet",    # 现金流量表
    "forecast": "data_lake/forecast.parquet",              # 业绩预告
    "express": "data_lake/express.parquet",                # 业绩快报
    "dividend": "data_lake/dividend.parquet",              # 分红送股
    # Plan A Task 3-5：资金流/龙虎榜/融资融券（tushare_sync 写）
    "moneyflow": "data_lake/moneyflow.parquet",            # 个股资金流（by=date）
    "top_inst": "data_lake/top_inst.parquet",              # 龙虎榜机构席位（by=date）
    "margin": "data_lake/margin.parquet",                  # 融资融券市场汇总（by=date, exchange_id 作 symbol）
    "margin_detail": "data_lake/margin_detail.parquet",    # 融资融券逐标的（by=date）
    "margin_secs": "data_lake/margin_secs.parquet",        # 融资融券标的列表（by=single 快照，扁平 df）
    "moneyflow_hsgt": "data_lake/moneyflow_hsgt.parquet",  # 北/南向资金市场级（by=date 逐日，MultiIndex）
    # Plan A Task 7：板块/概念（ths_daily 不复用 sector 湖——同花顺概念板块 vs 申万行业，分类口径与 ts_code 空间不同）
    "concept": "data_lake/concept.parquet",            # 概念字典（concept 接口，by=single 扁平）
    "ths_daily": "data_lake/ths_daily.parquet",        # 同花顺板块指数日线（ths_daily，by=date）
    # Plan A Task 8：指数（三数据集新建独立湖，不复用现有湖）
    "index_daily": "data_lake/index_daily.parquet",       # 指数日线（tushare_sync 写，MultiIndex date/ts_code）
    "index_weight": "data_lake/index_weight.parquet",     # 指数成分权重（by=date, con_code 作 symbol）
    "index_member": "data_lake/index_member.parquet",     # 指数成分股进出（by=single 扁平，全量需循环补）
    # Plan A Task 10：股东/解禁/停牌（四湖独立新增，不复用 dragon_list/north_flow/sector）
    "top10_holders": "data_lake/top10_holders.parquet",      # 前十大股东（tushare_sync 写，by=symbol，ann_date 防前视）
    "top10_floatholders": "data_lake/top10_floatholders.parquet",  # 前十大流通股东（by=symbol，ann_date 防前视）
    "share_float": "data_lake/share_float.parquet",          # 限售股解禁（by=date，ann_date 防前视）
    "suspend_d": "data_lake/suspend_d.parquet",              # 每日停复牌（by=date，ann_date 防前视）
    # Plan A Task 9：特色筹码 cyq_perf（300/分独立通道，tushare_sync 写）
    # ⚠️ 此前 LAKE_CONFIG 遗漏 cyq_perf lake 注册（TUSHARE_DATASETS 已有 lake 字段但 lakes dict 缺 key），
    # Task 11 注册表对齐时补齐——DataLakeReader 按 lakes[key] 寻址，缺 key 会导致筹码湖读不到。
    "cyq_perf": "data_lake/cyq_perf.parquet",                # 筹码分布及胜率（by=symbol，tushare_sync 写）
    # Plan B Task 6：ETF 专题湖（key 与 TUSHARE_DATASETS 一一对应）
    "fund_basic": "data_lake/fund_basic.parquet",        # ETF/基金基础信息（tushare_sync single 写，扁平快照）
    "fund_daily": "data_lake/etf_daily.parquet",         # ETF 日线（tushare_sync by=symbol 写，MultiIndex date/symbol；vol→volume）
    "fund_nav": "data_lake/etf_nav.parquet",             # ETF 净值（by=symbol，MultiIndex date/symbol）
    "fund_portfolio": "data_lake/etf_portfolio.parquet", # ETF 持仓（by=symbol，date_col=ann_date 防前视）
    "fund_share": "data_lake/etf_share.parquet",         # ETF 份额变动（by=symbol，MultiIndex date/symbol）
    # Plan C：宏观经济原始指标湖（独立新建，不复用现有 macro/moneyflow_hsgt 湖——语义不同）
    # shibor/shibor_quote 虽同源但粒度不同（全市场均值 vs 逐报价行），分湖避免覆盖。
    # mkt_daily（B 类合并）：原 szse_daily/sse_daily 两湖合并为一个 mkt_daily
    # （daily_info 接口返沪深两市，exchange 列区分），单一真相源 LAKE_CONFIG[key]==TUSHARE_DATASETS[key]['lake']。
    "cn_cpi": "data_lake/cn_cpi.parquet",           # CPI 月频原始（DatetimeIndex，tushare_sync 写）
    "cn_ppi": "data_lake/cn_ppi.parquet",           # PPI 月频原始
    "cn_gdp": "data_lake/cn_gdp.parquet",           # GDP 季频原始
    "cn_pmi": "data_lake/cn_pmi.parquet",           # PMI 月频原始
    "shibor": "data_lake/shibor.parquet",           # Shibor 日频均值（DatetimeIndex）
    "shibor_quote": "data_lake/shibor_quote.parquet",  # Shibor 逐报价行明细（DatetimeIndex + bank 数据列）
    "mkt_daily": "data_lake/mkt_daily.parquet",  # 交易所日级成交统计（daily_info，沪深合并，by=date）
}
LAKE_CONFIG["default_lake"] = "daily"
