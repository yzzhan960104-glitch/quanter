# Tushare 数据快照扩容 · 完成报告

- **日期**：2026-07-25
- **分支**：`feat/discovery-l0-l1`
- **执行模式**：全自主（用户授权，无人工确认）
- **状态**：✅ 全部完成

---

## 1. 任务完成总览

| 阶段 | Task | 状态 |
|---|---|---|
| 设计 | spec + plan 文档 | ✅ commit |
| 代码 | Task 1-12（双桶限频/路由/前复权/注册表/CLI/scripts收敛/dry-run/一致性） | ✅ 全 commit，**112 测试全绿零回归** |
| 回填 | Task 13 全量回填（近5年，cyq_chips 近3月） | ✅ 8 数据集全成功，0 失败 |

## 2. 数据集回填汇总（2026-07-25 02:00~02:56 + cyq_chips 02:04~02:22）

| 数据集 | quota | 模式 | 行数 | 标的 | 耗时 | 落湖 |
|---|---|---|---|---|---|---|
| stock_basic | basic | single | 5,531 | - | 0s | data_lake/stock_basic.parquet |
| hs_const_sh | basic | single | 581 | - | 0s | data_lake/hs_const_sh.parquet |
| hs_const_sz | basic | single | 242 | - | 0s | data_lake/hs_const_sz.parquet |
| daily_basic | special | date | 6,835,729 | 5,742 | 356s | data_lake/daily_basic.parquet |
| stk_factor_pro | special | date | 7,077,576 | 5,989 | 417s | data_lake/stk_factor_pro.parquet |
| weekly | basic | symbol+adj | 1,349,665 | 5,319 | 1,297s | data_lake/a_shares_weekly.parquet |
| monthly | basic | symbol+adj | 315,394 | 5,312 | 1,296s | data_lake/a_shares_monthly.parquet |
| cyq_chips | special | symbol | 28,412,319 | 5,322 | 1,092s | data_lake/cyq_chips.parquet |

**新增数据量**：~4490 万行，8 个新湖。

## 3. 数据健康验证（抽样）

- **cyq_chips**：MultiIndex(date,symbol)，逐价位分布正确（000007.SZ 2026-04-01 price=3.0/9.8 percent=0.01/0.05）
- **daily_basic**：PE/PB/换手率/市值字段齐全（000001.SZ 2021-01-04 close=18.60 pe=12.80 pb=1.25）
- **stk_factor_pro**：`_bfq` 后缀订正生效（macd_bfq/kdj_k_bfq/cci_bfq 无 NaN 幻觉列）
- **weekly 前复权**：000001.SZ **2026-07-03 close=10.29 与既有 daily 同日完全一致** → 新管道 latest_adj 基准与旧 fetch_qfq 对齐
- **monthly 前复权**：月末 close 合理（000001.SZ 2026-06-30 close=10.05）

## 4. 关键架构改动（Task 1-12）

1. **`data/resilience.py`**：拆双桶 `tushare_rate_limiter_basic`(498/min) + `tushare_rate_limiter_special`(300/min)，旧名别名零改
2. **`data/tushare_sync.py`**：`_fetch_with_guard` 按 `quota_type` 路由双桶；`_sync_by_symbol` 加 `adj_api` 前复权增强（照搬 fetch_qfq: `price=raw×adj/latest`）；`resolve_symbols` 加 concept universe
3. **`config/registry.py`**：补 10 新数据集 + moneyflow 归 special；DATASET_REGISTRY 补元信息；daily.script 切 sync_tushare.py
4. **`config/data.py`**：LAKE_CONFIG 补 9 新湖
5. **`data/sync_cli.py`+`data/sync.py`**：统一 CLI `python -m data.sync`（--all/--keys/--since/--quota/--incremental/--dry-run）
6. **`scripts/sync_data_lake.py`**：__main__ 转薄壳 + DeprecationWarning

## 5. dry-run 订正的关键事实（Task 11，防幻觉列）

- `stk_factor_pro`：列全带 `_bfq/_hfq/_qfq` 后缀，订正 fields（原 macd/rsi_6/cci 是幻觉列）
- `concept`/`concept_detail`：直连 tushare 仍返"无正确的接口名"（积分不足），标回 `_unavailable`
- `cyq_chips`：接口硬性要求 ts_code（纯 trade_date 报错），by=date → by=symbol

## 6. 已知限制与后续建议

| 限制 | 影响 | 后续 |
|---|---|---|
| `concept`/`concept_detail` 不可用 | 用户清单"概念板块和成分"未达成 | Tushare 积分提升后删 `_unavailable` 恢复（concept_detail 自动随 concept 恢复，resolve_symbols universe=concept 已就绪） |
| `daily` 保留既有全历史（2016-起，1009万行） | 新管道 adj_api 已验证等价（Task 12）但未替换既有湖 | 需切换时跑 `python -m data.sync --keys daily --since 2016-01-01`（备份后） |
| `opt_basic` 跳过 | 用户清单"期权列表"未做（YAGNI，无期权策略） | 需要时加回注册表 |
| `cyq_chips` 仅近3月 | 筹码动态转移，历史价值低，全量会爆炸（~6亿行） | 需要历史时按区间分批拉 |
| `sync_all_tushare`/`sync_incremental`/`sync_daily_incremental` 未转薄壳 | 其编排逻辑（致命错误停批/by=date 增量 _merge_dedup）比 CLI fail-soft 成熟 | CLI --incremental 增强（吸收 by=date 增量）后再收敛 |

## 7. 统一入口用法（后续日常同步）

```bash
# 全量回填所有数据集
python -m data.sync --all --since 2021-01-01

# 增量（读湖最新日 → 今天）
python -m data.sync --all --incremental

# 仅基础桶 / 特色桶
python -m data.sync --quota basic --since 2021-01-01
python -m data.sync --quota special --since 2021-01-01

# 指定数据集
python -m data.sync --keys daily_basic,stk_factor_pro --since 2021-01-01

# 小样例验证（by=symbol 限2标的 / by=date 限1日）
python -m data.sync --keys cyq_chips --since 2026-07-01 --dry-run
```

## 8. 交付物清单

- **代码**：12 commits on `feat/discovery-l0-l1`（Task 1-12 各一）
- **文档**：`docs/superpowers/specs/2026-07-25-tushare-data-snapshot-design.md`（spec）+ `docs/superpowers/plans/2026-07-25-tushare-data-snapshot.md`（plan）+ 本报告
- **测试**：112 passed（含 8 个新测试文件）
- **数据**：8 新湖 ~4490 万行
- **日志**：`data_lake/.syncing/snapshot-2026-07-25.*.log`
