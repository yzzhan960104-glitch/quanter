# 数据单一真相源清单（Data Source of Truth）

> 适用版本：SSoT Phase A + Phase B 收口（2026-08-05）+ Phase C 完成（trade_event(SIGNAL).meta
> 升格真相源、save_plan 删除）+ 巡检调度实况。**本文数字/实况截至 2026-08-15（CR-11 刷新）**，
> 会随治理推进漂移，以 `scripts/audit_ssot.py` 当日实现为准。
> 依据：`docs/superpowers/specs/2026-08-05-ssot-final-hardening-design.md` §3.1。
> 目的：消灭「同一逻辑数据存在多份来源、读写各指各的」历史债务；为引擎/服务/
> 播报/复盘/巡检提供**唯一读入口**，为新代码提供**唯一写入口**。

---

## 总原则

1. **每个数据域只有一个写入口（单一真相源，SSoT）** —— 即下方表格的「唯一真相源」列。
   其余路径只允许「只读归档」「审计镜像」「按需导出产物」三种身份之一。
2. **新代码一律读写 SSoT**；发现新双源立即在本清单登记并按 SSoT spec 治理。
3. **允许的双写模式仅限：DB 真相源 + 可重建镜像**（如 `account` 同步镜像、按需导出
   CSV/JSON）。镜像必须可从真相源重建，且由同一事务/同一调用点写。
4. **遗留文件统一归档到 `logs/archive/`**（只移动不删除，可恢复）。
5. **写入口被静态护栏 + 巡检双保险守卫**：
   - 静态护栏：`tests/test_ssot_static_guard.py`（CI / pytest 时炸）
   - 运维巡检：`scripts/audit_ssot.py`——**已挂 Windows 计划任务 `QuanterAudit`，每日 16:05
     自动跑（CR-7 · 2026-08-15 起）**：post_close 后核当日账；经 `ops/run_audit.bat`
     落盘 `logs/audit_schtask.log`，errs 传 exit 1 供任务计划程序「上次运行结果」外显。
     挂载/管理入口：`ops/manage_ops_schtasks.py`（`QuanterAudit` 受『绝不加入
     RETIRED/LEGACY 清退名单』红线保护（见 register_audit docstring），禁止误清）。
   - BANNED pattern 同口径（精确正则，跳注释），命中即 FAIL。

---

## 数据域最终表（9 域，spec §3.1 目标态）

| # | 数据域 | 唯一真相源 | 导出产物（可重建） |
|---|--------|-----------|-------------------|
| 1 | **成交流水** | `state_store.fill`（`logs/trading_state.db`，UNIQUE(order_id, traded_time) 幂等） | 导出接口（`export_trades`）按需生成 CSV；镜像不入库 |
| 2 | **订单/委托** | `state_store.order`（order_id PK，状态机 SUBMITTED/FILLED/CANCELED 等） | — |
| 3 | **持仓** | `state_store.position`（含归因列 `strategy` + `entry_rationale`，Phase B2） | 券商 QMT 真实持仓（对账 reconcile 用，by design 外部参照，非内部真相源） |
| 4 | **交易生命周期** | `state_store.trade_event`（`UNIQUE(account_id, trade_id, action)` 幂等；动作枚举 SIGNAL / CONFIRMED / VETOED / BLOCKED / ORDERED / SUBMITTED / REJECTED / DRY_RUN / FILLED / CLOSED 等） | — |
| 5 | **日权益/熔断基线** | `state_store.account_daily`（PRIMARY KEY (account_id, date)；start_total_asset 是 C-1 熔断 -3% 读口基线，close_total_asset 是日终闭合校验） | `daily_equity` 表已退役（Phase B5，死表清理） |
| 6 | **交易计划** | `state_store.trade_event(SIGNAL).meta`（**Phase C 已完成升格**：`save_plan/load_plan` 已删并进 BANNED，DB 是唯一生产写入口） | `plan_<date>.json` 仅为按需导出/审计归档产物，可由 trade_event(SIGNAL) 重建，**不再是写入口** |
| 7 | **数据就绪** | `state_store.data_ready` + `job_ledger`（`get_ready` 单口读，PRIMARY KEY (date, dataset)） | — |
| 8 | **播报幂等** | `job_ledger`（`logs/trading_job_run.db`，brief_<bot> 行 begin/finish 成对，独立库不与 trading_state 混） | — |
| 9 | **参数迭代/实验** | `experiment/experiments.db`（ACTIVE 表，Phase B3 收口） | — |

> 注 1（CR-11 · 2026-08-15 订正，Phase C 完成语义）：第 6 域过渡态**已收口**——
> `save_plan`/`load_plan` 已随 Phase C（C3）删除，`logs/trading_plans/plan_<date>.json`
> 降级为按需导出/审计归档产物，任何代码再写它即违规双源。
> 注 2：`save_plan` 调用/定义/模块属性访问**已在 `scripts/audit_ssot.py` BANNED 集合内**
> （如 `\bsave_plan\s*\(`、`\bdef\s+save_plan\b`、`trading_plan\.save_plan\b`），命中即 FAIL——
> 巡检与静态护栏双守卫，防 JSON 落盘路径复活。

---

## 读写拓扑（spec §3.2）

```
引擎/服务 ──写──▶ state_store（logs/trading_state.db 唯一写入口）
                    │
                    ├─▶ 导出接口（CSV/JSON 按需生成，不落盘）
                    ├─▶ 播报/复盘/digest（唯一读入口）
                    └─▶ 巡检 audit_ssot.py（一致性校验 + 护栏）
```

**禁止模式**：
- 任何模块读 `logs/live_trades.csv` / `logs/param_iter_state.json` 回退（已删，命中护栏）。
- 任何模块写 `logs/live_trades.csv`（Phase A 已删 record_live_trade / LIVE_TRADE_LOG）。
- 任何模块写 `logs/param_iter_state.json`（Phase B3 已切 experiment.db ACTIVE）。
- 任何模块读 `replay_runs/*.json` 旧归档作「近期回测」真相（已迁 `data/replay_tasks.db`）。

---

## 守卫机制（防回归）

### 静态护栏（CI 时炸）

`tests/test_ssot_static_guard.py`：精确正则扫生产目录 .py 文件（跳 archive/tests），
BANNED pattern 命中即 FAIL：
- `record_live_trade\(` / `LIVE_TRADE_LOG\s*=` / `LIVE_TRADE_COLUMNS\s*=`
- `os\.getenv.*LIVE_TRADE_READ_SOURCE` / `["']live_trades\.csv`
- `\bimport\b.*\brecord_live_trade\b`
- `["']param_iter_state\.json`

精确 pattern 跳注释：A4 删除时按 CLAUDE.md「注释说明为什么」保留 11 行审计追溯注释
（如「原 record_live_trade CSV 审计块已删除」），注释里只出现【名字】无括号/等号/import/
引号/csv 字面，不构成代码引用，pattern 不命中。

### 运维巡检（已挂调度，每日自动跑）

`scripts/audit_ssot.py`：跑 **7 项检查**（A6 起 5→7，CR-11 · 2026-08-15 订正），任一 FAIL 退出码 1：
- `check_fill_position`：fill 流水 ↔ position 持仓一致（BUY+ / SELL- 累加 = position.qty，容差 1e-6；
  CR-5 起含**漏挂方向反向扫描**——fill 净额≠0 而 position 缺行/为 0 即告警）
- `check_account_daily_closed`：每交易日 start+close 非空（熔断基线闭合）
- `check_trade_event_chain`：孤儿 SIGNAL（>7 日无后续合法 action 即告警；合法集 = CONFIRMED/VETOED/ORDERED/SUBMITTED/TP1_FILLED/TP2_FILLED/STOP_TRIGGERED/FILLED/CLOSED + 下单审计四态 DRY_RUN/BLOCKED/REJECTED/DIRECTION_UNKNOWN，T17 补全；OPEN 从未实写已删）
- `check_engine_process_count`：引擎进程数 ≤1（C-5 单例，PowerShell Get-CimInstance / pgrep 跨平台）
- `check_client_process`（A6 新增）：miniQMT 客户端进程数 == 1（0=未起，>1=多实例，探测失败亦有声）
- `check_port_owner_consistency`（A6 新增）：端口属主 == pid 文件 PID（不一致=旧链/非法链，与 supervisor 三合同口径）
- `check_guard_ripgrep`：复用 A5 BANNED（同口径 pattern，运维侧镜像，含 save_plan 系列）

退出码语义：0=全绿，1=有不一致。**调度实况：已挂 schtasks `QuanterAudit` 每日 16:05
（CR-7 · 2026-08-15 起，post_close 后核当日账）**，日志落 `logs/audit_schtask.log`；
也可手动 `python scripts/audit_ssot.py` 或 cron 定期跑。

---

## 历史 / 已治理动作

### Phase A（2026-08-05，CSV 镜像彻底退役）

- 删除 `record_live_trade` / `LIVE_TRADE_LOG` / `LIVE_TRADE_COLUMNS`（CSV 写盘三件套）。
- 删除 `LIVE_TRADE_READ_SOURCE` env 回退分支（`aggregate_fills_by_symbol` / `export_trades` /
  `query_trades` 三处读口）—— 只读 `state_store.query_fills`。
- `submit_order` 审计平移 `trade_event`（BLOCKED / ORDERED / REJECTED / DRY_RUN），
  `trade_event UNIQUE(account_id,trade_id,action)` 幂等双写（engine 路径 + server 手动路径）。
- `logs/live_trades.csv` 归档到 `logs/archive/`。

### Phase B（2026-08-05，DB schema 硬化 + 收口）

- **B1**：expired 改 pre_open 现算（previous_trading_day 基准日，holding_days 边界断言 ==不平/>平）。
- **B2**：归因落 DB 列（`position.strategy` + `position.entry_rationale`），engine 成交路径接线。
- **B3**：`param_iter_state.json` 全切 `experiment.db` ACTIVE（`--legacy` fail-closed 入口）。
- **B4**：播报幂等迁 `job_ledger`（brief_<bot> 行 begin/finish 成对，删 `logs/.last_<bot>_brief`）。
- **B5**：`daily_equity` 死表退役 + `position_book` 清理（`account_daily` 是日权益唯一真相源）。
- **B6**：`scripts/audit_ssot.py` 精确巡检（本文件配套）+ 本清单重写。

### 历史（2026-08-03，CSV → SQLite 迁移）

- 回测结果迁 `data/replay_tasks.db`（`replay_runs/*.json` 只读归档）。
- `logs/quanter.log` 路径统一（旧 `presentation/logs/` 归档）。

---

## 遗留风险与下一步

- ~~**第 6 域过渡**~~ **已收口（Phase C 完成）**：`trade_event(SIGNAL).meta` 是交易计划唯一
  真相源，`save_plan/load_plan` 已删且进 BANNED pattern（命中即 FAIL）。
- **account_daily start_total_asset 漏采**：模拟盘 / 非盘前启动场景下 start 可能 NULL
  （`audit_ssot.check_account_daily_closed` 会告警；DG-G3 起 T-1 close 兜底回填 +
  熔断缺基线 fail-closed）。生产 live 前需保证 pre_open 窗口 `[09:22, 10:00)` 内 engine
  起来并写了 start snap_at；否则当日熔断走 fail-closed 停调度（不再裸奔）。
- **第 12 域 symbol→名称映射**：双源同源（Tushare），`data_lake/stock_basic.parquet`
  （落盘）与 `data/symbol_names`（实时拉取）内容可能漂移；建议统一以 parquet 为 SSoT。
- ~~**前端 caisen 死视图**~~ **已清理（CR-11 · 2026-08-15 核实）**：
  `presentation/web/src/api/caisen.ts` 及 `/caisen` 死视图已随 caisen 前端整体退役删除，
  不再存在 SSoT 层面的悬挂引用。
