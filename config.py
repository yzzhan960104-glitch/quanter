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
# key 与 LAKE_CONFIG["lakes"] 的 key 一一对应（路径不重复定义，只在此声明资产语义）。
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
}

# 同步哨兵目录：POST /sync/{key} 触发时 touch {key}（=syncing）；成功删除，失败写 {key}.failed。
# 置于 data_lake/ 下便于与数据资产共同观测；运行时由 data_service 自动建目录。
SYNCING_DIR = os.path.join("data_lake", ".syncing")
