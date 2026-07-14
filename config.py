"""项目级配置文件

使用纯 Python 字典配置，避免复杂的 YAML/JSON 解析器。

凭证隔离策略：
- API Key / Token 通过 python-dotenv 从 .env 文件加载
- 绝对禁止将凭证硬编码在业务代码中
- 所有数据源模块通过 config 层统一获取凭证，实现单点管控
"""
import os
from datetime import datetime
from typing import Dict, Any, Optional

# 尝试从 .env 文件加载环境变量（开发环境）
# 生产环境应通过系统环境变量或容器 Secret 注入
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv 未安装时，回退到系统环境变量
    pass


# ============================================================
# 数据源凭证（从环境变量加载，绝不硬编码）
# ============================================================
DATA_SOURCE_CREDENTIALS = {
    "fred": {
        # 美联储经济数据 API Key
        "api_key": os.getenv("FRED_API_KEY", ""),
    },
    "tushare": {
        # Tushare Pro Token（A 股数据源）
        "token": os.getenv("TUSHARE_TOKEN", ""),
    },
}


def get_credential(source: str, key: str) -> str:
    """
    安全获取数据源凭证

    参数：
        source: 数据源名称（如 "fred", "tushare"）
        key: 凭证键名（如 "api_key", "token"）

    返回：
        凭证字符串

    异常：
        ValueError: 凭证未配置时抛出，强制开发者显式处理
    """
    cred = DATA_SOURCE_CREDENTIALS.get(source, {}).get(key, "")
    if not cred:
        raise ValueError(
            f"数据源凭证缺失：{source}.{key}。"
            f"请在 .env 文件或系统环境变量中配置 {source.upper()}_{key.upper()}"
        )
    return cred

# 交易时段配置（中国 A 股）
MARKET_HOURS = {
    "morning_start": "09:30",
    "morning_end": "11:30",
    "afternoon_start": "13:00",
    "afternoon_end": "15:00",
}

# 数据源配置
DATA_CONFIG = {
    "default_timezone": "Asia/Shanghai",
    "cache_dir": "data/cache",
    "max_missing_fill": 5,  # 最大前向填充天数（防范停牌期跨度过长）
}

# 宏观数据配置（示例）
MACRO_CONFIG = {
    "indicators": ["m2", "cpi", "ppi", "social_financing"],
    "thresholds": {
        "m2": 0.02,  # M2 增速 2% 阈值
        "cpi": 0.03,  # CPI 增速 3% 阈值
    },
    "check_window": 3,  # 连续几期超过阈值触发信号
}

# 可视化配置
VIZ_CONFIG = {
    "chart_theme": "plotly_white",
    "report_dir": "reports",
    "interactive": True,  # 是否生成交互式图表
    "export_formats": ["html"],  # 报告导出格式
}

# Mock 交易配置
MOCK_TRADING_CONFIG = {
    "order_timeout": 300,  # 订单超时时间（秒）
    "partial_fill_enabled": True,  # 是否允许部分成交
    "max_retries": 3,  # 最大重试次数
    "retry_delay": 1.0,  # 重试延迟（秒）
}

# ============================================================
# 工业级蜕变新增配置（纯字典，凭证仍走 .env）
# ============================================================
# 局部别名 _os：与文件顶部已 import 的 os 复用同一模块，此处仅为
# 保持新增配置块的视觉独立性；凭证一律通过 .env / 环境变量注入，
# 业务代码不得在此硬编码任何 Token / API Key。
import os as _os

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

# Celery 因子沙盒（Epic 3）
# cpu_gate_percent: CPU 占用闸门，超过该阈值则降级/排队，
# 防止因子全量重算压垮实时交易宿主机。
CELERY_CONFIG = {
    "broker_url": _os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    "queue": _os.getenv("CELERY_EXPLORER_QUEUE", "explorer"),
    "cpu_gate_percent": 80.0,
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
    "moneyflow_hsgt": "data_lake/moneyflow_hsgt.parquet",  # 北/南向资金市场级（by=single 扁平）
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
    # Plan C：宏观经济原始指标湖（8 湖独立新建，不复用现有 macro/moneyflow_hsgt 湖——语义不同）
    # shibor/shibor_quote 虽同源但粒度不同（全市场均值 vs 逐报价行），分湖避免覆盖。
    # szse_daily/sse_daily 的 LAKE_CONFIG key 用数据集名（与 TUSHARE_DATASETS key 一致，单一真相源），
    # 仅 lake 路径用 mkt_daily_* 前缀（语义：市场宽度统计，区别于个股 daily）。
    "cn_cpi": "data_lake/cn_cpi.parquet",           # CPI 月频原始（DatetimeIndex，tushare_sync 写）
    "cn_ppi": "data_lake/cn_ppi.parquet",           # PPI 月频原始
    "cn_gdp": "data_lake/cn_gdp.parquet",           # GDP 季频原始
    "cn_pmi": "data_lake/cn_pmi.parquet",           # PMI 月频原始
    "shibor": "data_lake/shibor.parquet",           # Shibor 日频均值（DatetimeIndex）
    "shibor_quote": "data_lake/shibor_quote.parquet",  # Shibor 逐报价行明细（DatetimeIndex + bank 数据列）
    "szse_daily": "data_lake/mkt_daily_szse.parquet",  # 深交所日级成交统计（by=date）
    "sse_daily": "data_lake/mkt_daily_sse.parquet",    # 上交所日级成交统计（by=date）
}
LAKE_CONFIG["default_lake"] = "daily"

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
    "macro":         {"source": "AKShare", "market": "宏观", "granularity": "月频→日频",
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
        "api": "cashflow", "by": "symbol",
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,net_profit_cash_flow,c_pay_acq_foroth_assets",
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
        "api": "top_list", "by": "date",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,name,close,pct_change,amount,net_amount,buy_amount,sell_amount",
        "lake": "data_lake/dragon_list.parquet",  # 复用 dragon_list 湖（切 Tushare）
    },
    "top_inst": {
        "api": "top_inst", "by": "date",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,name,close,pct_change,amount,net_amount,buy_amount,sell_amount",
        "lake": "data_lake/top_inst.parquet",
    },
    # —— 融资融券（margin/margin_detail/margin_secs）——
    # margin（市场汇总，symbol_col=exchange_id 交易所）/ margin_detail（逐标的 ts_code）：by=date。
    # margin_secs（标的列表快照）：by=single，落扁平 DataFrame（非时序 MultiIndex）。
    "margin": {
        "api": "margin", "by": "date",
        "date_col": "trade_date", "symbol_col": "exchange_id",
        "fields": "exchange_id,trade_date,rzye,rzmre,rqye,rqmcl,rzche,rqchl",
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
        "api": "margin_secs", "by": "single",
        "date_col": "start_date", "symbol_col": "ts_code",
        "fields": "ts_code,name,start_date",
        "lake": "data_lake/margin_secs.parquet",
    },
    # —— 北向资金（hsgt_top10/moneyflow_hsgt）：切 Tushare 替代 akshare sync_north_flow ——
    # hsgt_top10 当日十大成交股（有 ts_code，by=date），复用 north_flow 湖（切源）。
    "hsgt_top10": {
        "api": "hsgt_top10", "by": "date",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "trade_date,name,ts_code,vol,amount,north_direction",
        "lake": "data_lake/north_flow.parquet",  # 复用 north_flow 湖（切 Tushare）
    },
    # moneyflow_hsgt 市场级北/南向资金（无个股 symbol）→ single 扁平快照（非 MultiIndex）。
    "moneyflow_hsgt": {
        "api": "moneyflow_hsgt", "by": "single",
        "date_col": "trade_date", "symbol_col": "trade_date",
        "fields": "trade_date,ggt_ss,ggt_sz,sgt_ss,sgt_sz,north_money,south_money",
        "lake": "data_lake/moneyflow_hsgt.parquet",
    },
    # —— 板块/概念（Plan A Task 7）：补 sector 的 Tushare 维度 ——
    # concept（概念列表）：单次返回全量概念字典（code+name），无时间维度 → by=single 落扁平 df。
    # date_col 填 code 仅为占位（single 模式 _sync_single 不触碰索引，原样 to_parquet），
    # 概念字典本身是静态参照表，无时序索引语义。
    "concept": {
        "api": "concept", "by": "single",
        "date_col": "code", "symbol_col": "code",
        "fields": "code,name",
        "lake": "data_lake/concept.parquet",
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
    # index_member 指数成分股进出记录（纳入/剔除）：by=single 单次拉全量。
    # date_col=in_date（纳入日）作时间索引候选；symbol_col=con_code（成分股）。
    # ⚠️ 事实风险：tushare index_member 单次最多返 100 行（分批需循环），by=single 仅落首页 100 行，
    # 全量成分进出历史需逐 index_code 循环或 offset 分页（本 brief by=single 为最小可用口径，
    # 全量补数属后续优化）。当前 _sync_single 原样落盘（扁平 df，不重建时间索引）。
    "index_member": {
        "api": "index_member", "by": "single",
        "date_col": "in_date", "symbol_col": "con_code",
        "fields": "index_code,con_code,con_name,in_date,out_date",
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
        # 限售股解禁（share_float）：单日全市场一次返，按 date 分页。
        # 物理意图：解禁日前后存在系统性抛压（解禁股可流通），是事件驱动/回避策略关键信号。
        # date_col=ann_date（解禁公告日）—— float_date（实际解禁日）可能晚于公告，
        # 用 ann_date 索引保证回测只读到「市场已知」的解禁信息，无前视。
        "api": "share_float", "by": "date",
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,float_share,float_date,float_share_share",
        "lake": "data_lake/share_float.parquet",
    },
    "suspend_d": {
        # 每日停复牌（suspend_d）：单日全市场一次返，按 date 分页。
        # 物理意图：停牌期间无法交易，回测撮合层必须据此跳过；停牌原因（重大重组/
        # 核查）是事件因子。date_col=ann_date（公告日）而非 suspend_date（实际停牌日）——
        # 公告日是市场最早能预知停牌的时点，用 suspend_date 会把停牌信息「提前」落湖
        # 造成前视。⚠️ 字段名以 Tushare Pro 官方为准：ann_reason/reason_type，
        # brief 草稿的 suspend_reason/resume_reason 是旧版字段（已停用）。
        "api": "suspend_d", "by": "date",
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,suspend_date,resume_date,ann_reason,reason_type",
        "lake": "data_lake/suspend_d.parquet",
    },
    # ===== ETF 专题（Plan B Task 1-5）：fund_basic/fund_daily/fund_nav/fund_portfolio/fund_share =====
    # 物理意图：ETF 全景数据（列表/日线/净值/持仓/份额），用于 ETF 动量/折溢价/跟踪误差/份额变动分析。
    # fund_basic(market='EFT') 是其余 4 个 by=symbol 接口的标的池来源（_load_etf_universe 读它）。
    "fund_basic": {
        # ETF/基金基础信息（fund_basic market='EFT'）：单次拉全市场 ETF 列表，不分页 → single 模式。
        # Why single 而非 symbol：列表类接口本身无「标的维度分页」语义，一次性返全量更省配额；
        #   市场口径 market='EFT' 由调用方（_load_etf_universe）显式传，配置层 fields 不含 market 过滤（接口参数）。
        # Why date_col=found_date：single 模式当前不建时间索引（_sync_single 直接落扁平 df），
        #   date_col 仅为 schema 完备保留（Plan C 才给 single 加 index_mode=datetime）。
        "api": "fund_basic", "by": "single",
        "date_col": "found_date", "symbol_col": "ts_code",
        "fields": "ts_code,name,market,management,custodian,found_date,list_date,issue_date,delist_date",
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
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,open,high,low,close,vol,amount",
        "rename": {"vol": "volume"},  # vol→volume 列名归一（与股票日线湖对齐）
        "lake": "data_lake/etf_daily.parquet",
    },
    "fund_nav": {
        # ETF 净值（fund_nav）：单位净值/累计净值，单 ETF 全历史一次返，按 symbol 分页。
        # Why date_col=nav_date：净值披露日（nav_date）即市场可感知净值的确切时点，
        #   无公告滞后前视风险（净值本身是披露产物，nav_date 即公开日）。
        "api": "fund_nav", "by": "symbol",
        "date_col": "nav_date", "symbol_col": "ts_code",
        "fields": "ts_code,nav_date,unit_nav,accum_nav,accum_nav_rate",
        "lake": "data_lake/etf_nav.parquet",
    },
    "fund_portfolio": {
        # ETF 持仓（fund_portfolio）：前十大重仓股，按 symbol 分页。
        # ⚠️ 前视红线：date_col=ann_date（公告日），绝不用 end_date（报告期，如 20231231）。
        #   持仓数据公告滞后（季报/半年报披露窗口），end_date 早于 ann_date 数月，用 end_date 索引
        #   会在报告期内提前看到持仓构成，构成前视偏差。brief Step 2 曾误用 end_date，Step 3 修正为 ann_date。
        "api": "fund_portfolio", "by": "symbol",
        "date_col": "ann_date", "symbol_col": "ts_code",
        "fields": "ts_code,ann_date,end_date,symbol,name,amount,stk_value,stk_value_ratio",
        "lake": "data_lake/etf_portfolio.parquet",
    },
    "fund_share": {
        # ETF 份额变动（fund_share）：未流通/总份额/流通份额，按 symbol 分页。
        # Why date_col=trade_date：份额变动按交易日披露，trade_date 即公开可感知时点。
        "api": "fund_share", "by": "symbol",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,share_unissue,total_share,float_share",
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
        # CPI 月频：nt_yoy 全国同比（核心通胀口径）/ nt_mom 环比 / yty_yoy 城镇同比
        "api": "cn_cpi", "by": "single",
        "date_col": "month", "symbol_col": "month",
        "fields": "month,nt_yoy,nt_mom,yty_yoy",
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
        # PMI 月频：制造业采购经理指数（50 为荣枯线）+ business_index_pmi 生产经营预期
        "api": "cn_pmi", "by": "single",
        "date_col": "month", "symbol_col": "month",
        "fields": "month,manufacturing_pmi,business_index_pmi",
        "index_mode": "datetime",
        "lake": "data_lake/cn_pmi.parquet",
    },
    # —— C 组·银行间同业拆放（Plan C Task 4：shibor/shibor_quote）——
    # 物理意图：Shibor 是利率衍生品定价基准 + 流动性紧张先行指标（2007 起银行间报价）。
    # by=single index_mode=datetime：shibor 单一时间序列（1w..1y 各期限利率列），落 DatetimeIndex。
    # shibor_quote 含 bank（报价行）列，作数据列保留（不拆 MultiIndex，保持日期索引扁平）。
    "shibor": {
        "api": "shibor", "by": "single",
        "date_col": "date", "symbol_col": "date",
        "fields": "date,on,1w,2w,1m,3m,6m,9m,1y",
        "index_mode": "datetime",
        "lake": "data_lake/shibor.parquet",
    },
    "shibor_quote": {
        # shibor_quote 逐报价行明细：bank 列作数据保留，date_col=date 落 DatetimeIndex
        "api": "shibor_quote", "by": "single",
        "date_col": "date", "symbol_col": "date",
        "fields": "date,bank,on,1w,2w,1m,3m,6m,9m,1y",
        "index_mode": "datetime",
        "lake": "data_lake/shibor_quote.parquet",
    },
    # —— C 组·交易所成交统计（Plan C Task 5：szse_daily/sse_daily）——
    # 物理意图：交易所日级市值/PE/挂牌数是市场宽度（breadth）指标，宏观择时辅助。
    # by=date：单日全市场汇总（无个股 symbol），date_col/symbol_col 均为 trade_date
    # （市场级时序，落 MultiIndex(date, symbol) 但 symbol 层恒等于 trade_date，符合 by=date 契约）。
    "szse_daily": {
        "api": "szse_daily", "by": "date",
        "date_col": "trade_date", "symbol_col": "trade_date",
        "fields": "trade_date,issuer_num,sec_num,total_share,total_value,pe",
        "lake": "data_lake/mkt_daily_szse.parquet",
    },
    "sse_daily": {
        "api": "sse_daily", "by": "date",
        "date_col": "trade_date", "symbol_col": "trade_date",
        "fields": "trade_date,issuer_num,sec_num,total_share,total_value,pe",
        "lake": "data_lake/mkt_daily_sse.parquet",
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
        "api": "cyq_perf", "by": "symbol",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,his_low,his_high,cost_5,cost_15,cost_50,cost_85,cost_95,weight_avg,winner_rate",
        "lake": "data_lake/cyq_perf.parquet",
        "quota_type": "special",  # 特色数据：300/分独立通道（纯日志标记，限流仍走统一 rate_limiter）
    },
}

# 同步哨兵目录：POST /sync/{key} 触发时 touch {key}（=syncing）；成功删除，失败写 {key}.failed。
# 置于 data_lake/ 下便于与数据资产共同观测；运行时由 data_service 自动建目录。
SYNCING_DIR = os.path.join("data_lake", ".syncing")
