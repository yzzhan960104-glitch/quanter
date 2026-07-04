# 数据池架构与全量同步指南

## 架构

数据落 parquet 湖 → `DataLakeReader` 启动时 load 到内存 → `LakeDataFetcher` / 因子层直读。
回测主链路（`backtest_service` / `portfolio_service`）优先走真实湖，湖缺数据降级 `MockDataFetcher` 保离线可跑。

### 湖注册（`config.py` `LAKE_CONFIG['lakes']`）

| key | parquet | 内容 | sync 脚本 | 源 |
|-----|---------|------|-----------|-----|
| `daily` | a_shares_daily.parquet | 全市场日线 OHLCV | sync_data_lake.py | AKShare |
| `daily_active` | a_shares_active.parquet | 活跃池日线（~50只）| sync_sector_daily.py | AKShare |
| `minute` | a_shares_1min.parquet | 分钟 OHLCV | sync_jqdata_1min.py | JQData |
| `macro` | macro_credit.parquet | M2/M1/社融/DR007 | sync_macro_credit.py | AKShare |
| `sector` | sector.parquet | 板块资金流 | sync_sector_daily.py | AKShare |
| `fundamentals` | fundamentals.parquet | 估值面板 pe/pb/roe/市值 | sync_fundamentals.py | Tushare |
| `north_flow` | north_flow.parquet | 北向资金净流入 | sync_north_flow.py | AKShare |
| `dragon_list` | dragon_list.parquet | 龙虎榜上榜 | sync_dragon_list.py | AKShare |

## 全量同步命令（按需运行）

### 首次全量（建议按顺序）

```bash
# 1. 全市场日线（10年，AKShare，~1-2h；断点续传，失败重跑从断点继续）
python scripts/sync_data_lake.py --years 10

# 2. 活跃池日线（~5min，AKShare；写 daily_active 湖，不覆盖全市场 daily）
python scripts/sync_sector_daily.py

# 3. 宏观信贷（~10min，AKShare）
python scripts/sync_macro_credit.py

# 4. 基本面估值（10年，Tushare daily_basic 批量，需 2000+ 积分，~40min）
python scripts/sync_fundamentals.py --years 10

# 5. 北向资金（~10min，AKShare）
python scripts/sync_north_flow.py --years 2

# 6. 龙虎榜（~20min/月，AKShare）
python scripts/sync_dragon_list.py --days 30
```

### 小样本验证（快速跑通管道）

```bash
python scripts/sync_data_lake.py --years 2 --limit 10           # 10 只 2 年
python scripts/sync_fundamentals.py --years 2 --limit-dates 20  # 20 个交易日
```

### 增量更新（日常）

日线/活跃池/宏观/北向/龙虎榜日更（crontab）；基本面按财报季加更。

## 耗时估算

| 数据集 | 源 | 全量耗时 | 频率 |
|--------|----|---------|------|
| 全市场日线×10年 | AKShare | ~1-2h | 日更 |
| 活跃池日线 | AKShare | ~5min | 日更 |
| 宏观信贷 | AKShare | ~10min | 月更 |
| 基本面估值×10年 | Tushare | ~40min（批量按 trade_date）| 财报季 |
| 北向资金×2年 | AKShare | ~10min | 日更 |
| 龙虎榜×30天 | AKShare | ~20min | 日更 |

## 数据源约束（重要）

| 源 | 约束 | 应对 |
|----|------|------|
| **AKShare** | 无 Token，但限频 + 偶发网络瞬态（`RemoteDisconnected`）| `akshare_limiter` + `akshare_breaker` + 断点续传；失败重试可恢复 |
| **Tushare** | `stock_basic` / `daily_basic` 需 **2000+ 积分** | 积分不足时 `sync_fundamentals` 无法拉；`sync_data_lake` 已切 AKShare 规避 |
| **JQData** | 100 万条/日配额 | 分钟级专用；全市场日频基本面超配额（用 Tushare 批量按日期）|

### 实测约束（2026-07）

- 当前 Tushare Token 积分不足 `stock_basic` → `sync_data_lake` 切 AKShare（无门槛）。
- AKShare 偶发 `RemoteDisconnected`（网络瞬态）→ 熔断器 + 断点续传兜底；重跑可恢复。
- 基本面因子（`sync_fundamentals`）需 Tushare 积分；积分不足时建议先跑沪深300 子集或升级账户。

## 回测切真实数据湖

`server/services/backtest_service.py` / `portfolio_service.py` 优先 `LakeDataFetcher`（`data/lake_fetcher.py`）：

- **真实 symbol**（如 `600000.SH`）→ `daily` / `minute` 湖 `get_timeseries`
- **`dynamic_top50`**（前端 `ParamForm` 劫持的活跃池代号）→ `daily_active` 湖活跃池首只代表
- **湖缺数据** → 抛 `LookupError` → 降级 `MockDataFetcher`（`logger.warning` 留痕，保 CI/开发机可跑）

启动时 `server/main.py` lifespan 遍历 `LAKE_CONFIG['lakes']` 调 `reader.load(path, key)`，
parquet 缺失则 warning 跳过（离线降级，不阻断启动）。

## 因子层

| 因子模块 | 数据源湖 | 函数 |
|----------|----------|------|
| `factors/fundamental.py` | fundamentals | `valuation_cross_section`（横截面估值分位，value/growth 方向）|
| `factors/alternative.py` | north_flow / dragon_list | `north_flow_momentum`（连续净流入动量）、`dragon_signal`（当日上榜集合）|
| `factors/technical.py` | daily（透传）| MA/MACD/RSI/VPT |
| `factors/macro.py` / `macro_regime.py` | macro | 宏观锚点 / CreditRegime 状态机 |
