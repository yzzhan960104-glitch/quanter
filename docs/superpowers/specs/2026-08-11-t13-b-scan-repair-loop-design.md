---
title: T13-B Wave B——scan→repair 闭环（L2 scan gate + L3 自动补采，整体设计）
type: design/spec
status: draft
date: 2026-08-11
related: [plans/wayfinder/T13-blueprint.md, plans/wayfinder/T13.md, plans/wayfinder/T15.md]
governs: [T13-B]
---

# T13-B Wave B：scan→repair 闭环（整体设计）

## 1. 背景与缺陷模型

T13-A（Wave A · L1 写入守卫）已治理「写入侧静默抹除」（T12 闭环）。剩 **L2 检测侧 + L3 兜底侧** 仍静默（blueprint §1 三层级联）：

| 层 | 现状（破） | 灾难实例 |
|---|---|---|
| **L2 检测侧** | `scan_integrity` 孤立 CLI 无生产调度；`freshness` 只校验 max-date + 行数骤降，**不校验历史连续性** | 单日湖 max-date 合格但 300214 缺 07-14~07-21 不被发现 |
| **L3 兜底侧** | `repair_gaps` 裸调 `get_pro`（无限频）、`--auto` 10min 无输出、无 scan FAIL→repair 自动触发；`filter_universe_by_continuity` fail-open 静默跳过 | 缺口「被动跳过永不发现，且永不被补采」 |

**目标**：scan→repair 闭环跑通生产链——把三层「静默」逐一改成「有声」（blueprint §2.1）：L2 scan 升级生产 gate（发现缺口）→ L3 repair 自动补采（修复缺口），受限频 + 熔断保护。

## 2. 整体架构

```
L1 写入守卫（T13-A ✅ 已治理）────────────────────────────────
        │ 写入安全后，检测方有意义
        ▼
L2 检测侧（本工单）──────────────────────────────────────────
  ① scan 接 pipeline_then_eod 生产 gate（freshness 后），加连续性维度
  ② 每周独立全扫 cron（engine.sched，周期 backstop 防日级 gate 漏网）
  ▸ FAIL 语义 = 告警 + 入补采队列，不阻断当日交易（与 freshness FAIL 阻断不同）
        │ scan FAIL → 触发补采
        ▼
L3 自动补采（本工单）────────────────────────────────────────
  ③ repair_gaps 接 _fetch_paged 限频守卫（#5，repair + sync 共用）
  ④ scan FAIL → repair 自动触发，受 配额(N段/M行) + 熔断(K次失败暂停X小时)
  ⑤ repair 性能：分页进度上报 + 总超时（治 --auto 10min 无输出）
  ▸ FAIL 语义 = 熔断时降级告警（绝不把代理失败当成功吞掉，与 T15 解耦）
        │ 兜底
        ▼
交易侧 filter_universe_by_continuity ── 保留 fail-open 跳过缺标的 + 接告警通道去静默
```

## 3. 各子项设计

### 3.1 限频守卫（#5）—— `_fetch_paged` 每页 acquire basic 桶

**问题**：`_fetch_paged`（`data/tools/sync_daily_incremental.py:47`）分页裸调 `getattr(pro, api)` 无限频；`repair_gaps` 复用它补采多日，连续分页撞 Tushare 500/min 封禁。

**方案**：`_fetch_paged` 每页前 `tushare_rate_limiter_basic.acquire(1.0)`。调用方签名不变，`sync_daily_incremental` + `repair_gaps` 统一受限频保护（一处改两处受益）。**已实现**（2026-08-11）。

**接入点**：`data/tools/sync_daily_incremental.py:47`。

### 3.2 scan 生产 gate（#1）—— 接 `pipeline_then_eod` ③

**问题**：`scan_integrity.scan()`（`data/tools/scan_integrity.py:42`）是孤立 CLI，无生产调度。

**方案**：`scan` 接入 `pipeline_then_eod`（`trading/orchestrate/pipeline.py:60`）步骤③，紧跟 `check_freshness`（:125 `results = {k: check_freshness(...)}`）后：

```python
# pipeline_then_eod 步骤③ 后追加：连续性 scan（与 freshness 实时性互补）
from data.tools.scan_integrity import scan as _scan_integrity
scan_report = _scan_integrity(lake_dir=str(ROOT / "data_lake"), symbol=None,
                              since=..., end=expected)  # 近 N 日窗口
scan_gaps = scan_report.get("unjustified_gaps", 0)
if scan_gaps > 0:
    # L2 FAIL 语义：告警 + 入补采队列，不阻断 eod（历史缺口不硬阻断当日交易）
    _msg = f"scan 发现 {scan_gaps} 段漏采（{len(scan_report['unjustified_symbols'])} 标的），已入补采队列"
    logger.warning(_msg)
    # 触发 L3 自动补采（#2，见 3.4）
    ...repair_gaps 自动调用...
    # 不 return（不阻断 eod，与 freshness FAIL 的 return 不同）
```

**关键：scan FAIL 不阻断 eod**（blueprint §2.3）——历史缺口与当日交易无关，freshness FAIL（当日数据未到）才阻断 eod 产计划。

**scan 区间**：近 N 个交易日（建议 N=30，覆盖停牌复牌周期，与 `_backscan_recent` 同窗口）—— 全历史扫描由每周全扫（3.5）承担，日级 gate 只扫近期。

**接入点**：`trading/orchestrate/pipeline.py:125` 后；`data/tools/scan_integrity.py` scan 保持纯函数（pipeline 调用方接线）。

### 3.3 repair 性能（#4）—— 进度上报 + 总超时

**问题**：`repair_gaps --auto` 实测 10min 无输出（blueprint §0），根因：`_fetch_paged` 多日多页循环无进度 log + 无超时。

**方案**：
- `_fetch_paged` 每 N 页（如 10 页）log 已拉行数 + 当前 trade_date（进度可见）
- `repair_gaps.main` 加总超时（如 30min，env `REPAIR_TIMEOUT_SECONDS` 覆盖）—— 超时则停止拉新段，已拉部分 merge 落盘 + 告警（部分补采 > 完全不补）

**接入点**：`data/tools/sync_daily_incremental.py:_fetch_paged`（进度 log）+ `data/tools/repair_gaps.py:main`（超时）。

### 3.4 自动补采（#2）—— scan FAIL → repair，配额 + 熔断

**问题**：scan 发现缺口后无自动修复，缺口「永不补采」。

**方案**：pipeline scan FAIL（unjustified_gaps>0）→ 自动调 `repair_gaps`：

```python
# pipeline scan FAIL 后（3.2）
from data.tools.repair_gaps import repair_gaps as _do_repair, _load_gaps_from_report
# 配额：单次最多补 MAX_REPAIR_SEGMENTS 段（防过载）；熔断：连续 K 次失败暂停
gaps_to_repair = scan_report["gaps"][:MAX_REPAIR_SEGMENTS]  # 配额截断
try:
    new_lake = _do_repair(gaps_to_repair, lake_df, get_pro())
    safe_overwrite(lake_path, new_lake)  # T13-A 守卫保护补采写入
except Exception:
    # 熔断：连续失败计数，超阈值暂停自动补采 + 告警（绝不吞代理失败当成功）
    _record_repair_failure(); _alert_critical(...)
```

**配额/熔断参数**（blueprint §6 待定项落实）：
- `MAX_REPAIR_SEGMENTS = 50`（单次最多补 50 段，防单次过载）
- `MAX_REPAIR_ROWS = 100_000`（单次最多补 10 万行）
- `REPAIR_FAILURE_THRESHOLD = 3`（连续 3 次失败熔断）
- `REPAIR_RECOVERY_HOURS = 6`（熔断后暂停 6 小时）

熔断状态用 sidecar 文件（`data_lake/.repair_breaker.json`，记失败计数 + 熔断到期时间），与 freshness baseline sidecar 同模式。

**接入点**：`trading/orchestrate/pipeline.py`（scan FAIL 后调 repair_gaps）+ `data/tools/repair_gaps.py`（配额截断 + 熔断 sidecar）。

### 3.5 每周全扫（#5 调度）—— engine.sched cron

**问题**：日级 gate（pipeline ③）只扫近期 N 日，全历史缺口无周期 backstop。

**方案**：engine `__init__` add_job 段注册每周全扫：

```python
# engine.py __init__ add_job 段（与 pipeline_then_eod / pre_open / post_close 同处）
self.sched.add_job(
    self._weekly_scan, CronTrigger.from_crontab(
        os.getenv("ENGINE_WEEKLY_SCAN_CRON", "0 2 * * 6")),  # 周六 02:00（mon-sun 用数字 0-6，0=周日 APScheduler）
    id="weekly_scan",
)
```

`_weekly_scan`：全市场全历史 `scan_integrity.scan`（无 since/end 截断）→ FAIL 则告警 + 写报告 `logs/integrity_weekly.json`（人工/次日补采消费）。**不自动 repair**（全历史补采量大，日级 gate 的自动 repair 已覆盖近期；全历史缺口人工确认后补，避免周末大量补采撞限频）。

**接入点**：`trading/engine.py:__init__` add_job 段（:509 附近，与现有 5 job 并列）+ 新增 `_weekly_scan` method。

## 4. 降级语义（blueprint §2.3 已定）

| 层 | FAIL 处置 |
|---|---|
| L2 scan（日级 pipeline gate） | 告警 + 入补采队列，**不阻断当日交易** |
| L2 scan（每周全扫） | 告警 + 写报告，不自动 repair（人工确认） |
| L3 repair（自动补采） | 配额截断 + 熔断暂停；熔断时降级告警（**绝不把代理失败当成功吞掉**） |
| 交易侧 `filter_universe_by_continuity` | 保留 fail-open 跳过缺标的 + 接告警通道去静默 |

## 5. 依赖

- **T13-A ✅**（L1 写入守卫已合并 master，2026-08-11）—— repair 补采写入受 safe_overwrite 保护
- **T15 ✅**（代理根治 + 代码层防复发 NO_PROXY 已合并）—— 生产补采不再因代理失败

## 6. 接入点汇总

| 子项 | 文件:位置 |
|---|---|
| #5 限频（✅ 已做） | `data/tools/sync_daily_incremental.py:47` `_fetch_paged` |
| #1 scan gate | `trading/orchestrate/pipeline.py:125` 后（check_freshness 后） |
| #2 自动补采 | `trading/orchestrate/pipeline.py`（scan FAIL 后调 repair_gaps）+ `data/tools/repair_gaps.py`（配额+熔断 sidecar） |
| repair 性能 | `data/tools/sync_daily_incremental.py:_fetch_paged`（进度 log）+ `data/tools/repair_gaps.py:main`（超时） |
| 每周全扫 | `trading/engine.py:__init__` add_job 段 + 新增 `_weekly_scan` method |

## 7. 验收

1. **#5 限频**：`_fetch_paged` 每页 acquire basic 桶（mock 断言调用次数=页数）✅
2. **#1 scan gate**：pipeline_then_eod 在 freshness 后调 scan；scan FAIL 不阻断 eod（eod 仍跑）；scan PASS 无副作用
3. **#2 自动补采**：scan FAIL → repair_gaps 被调（配额截断 + 熔断 sidecar）；连续失败 K 次 → 熔断暂停 + 告警；熔断期内 repair 跳过
4. **repair 性能**：`_fetch_paged` 每 N 页 log 进度；main 超时停止 + 部分落盘
5. **每周全扫**：engine 注册 weekly_scan cron；触发全市场 scan + 报告
6. **全量回归**：1678+ 测试全绿（新增子项测试 + 现有无回归）

## 8. Out of scope

- Wave C（D5 复权 / D6 列完整 / D7 时区 / D9 行数阈值维度扩展）—— T13-C，依赖 #1 scan 落地后叠加
- 支柱 2（T16 拆 compute_unit + 结果回湖）—— 独立工单
