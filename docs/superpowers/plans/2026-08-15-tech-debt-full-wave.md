# 技术债全量清偿实施计划（2026-08-15 · 一次性完成）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **用户已预授权**：完成 plan 后按 subagent 方式执行到底（用户原话：「完成plan后，按照sub agent的方式去执行。我去睡觉了，希望你可以完成所有工作！」2026-08-15）。执行中不再向用户提问；遇阻塞按「诚实 BLOCKED 记录 + 继续其余任务」处理。

**Goal:** 一次性清偿 `docs/architecture/06-tech-debt.md` 全部存活技术债（CR-1..11 + Q/TB(W2) + TD + SS(T6/M2) + CN(T10/T11) + DOC/DC + 测试 follow-up），全量验证门绿后合入 master 并 push。

**Architecture:** 六波次依赖排序（止血观测 → 策略数学单源 → 风控语义 → 结构收尾 → broker 适配层 → 文档对账收官），每 task 独立提交、每波次跑验证门；W1-B/W2 大件遵守治理 spec 原文约束（「逻辑只搬位置不改行为」+ re-export 兼容 + L3/L4 双跑）。

**Tech Stack:** Python 3.10（`.venv310`）+ pytest / Vue3 + vitest + vue-tsc / FastAPI / sqlite3 裸 SQL / GitHub Actions。

## Global Constraints（每个 task 隐含遵守）

1. **环境**：一切 Python 命令用 `E:/quanter/.venv310/Scripts/python.exe`，注入 `PYTHONUTF8=1`（GBK 管道会级联 UnicodeEncodeError）。前端 `npm --prefix presentation/web run test|typecheck`。
2. **live 引擎红线**：引擎进程 PID 27960（uvicorn :8000）运行中——**禁止 kill/重启/`python -m trading`/uvicorn/任何 QMT connect**；只改代码不碰进程。合并后生效需用户重启（需 QUANTER_API_TOKEN），此为用户决策。
3. **测试基线**：collect ~1870 项；**已知 2 个存量红**（tests/e2e_long_cycle/test_probabilistic_broker.py 两个 resting TP 测试，日期敏感，master 同挂）。任何 task 不得新增红；红则当场修复或 revert 该 task。
4. **提交纪律**：每 task 一个 commit（中文 conventional：`fix(debt)/feat(debt)/docs(debt)/test(debt)` + CR 编号）；分支 `debt/full-wave-0815`（从 master `3a476dc5` 切出）；终态 ff 合回 master 并 push origin。
5. **TDD**：每 task 先写失败测试再实现（纯搬移型 task 例外：以「行为等价守卫」——搬移前后的定向测试全绿为准）。
6. **注释**：新增/修改代码配中文注释，说明 What+Why（物理意图）。
7. **不动 .env / schtasks 现有任务**；新增 schtasks 注册只做代码+注册函数（注册函数提供，实际 `_schtasks` 执行仅限新增 QuanterAudit 一项，幂等 /F）。
8. **文档同步**：销账时 `docs/architecture/06-tech-debt.md` 与 `docs/architecture/deep-dives/2026-08-14-critical-review.md` 同步改（防自造漂移）；收官统一在 Task 18 做，中途不反复改。

## 关键勘探结论（执行者必读的事实基线）

- **HEAD** `3a476dc5`（master=origin，工作区净）。G 波/A1+A2 已入；06-tech-debt 的行号引用经三路勘探逐一核实**与现状吻合**，可直接按行号动工。
- CR-1 根因：`client.ts:60-62` 拦截器剥壳 + `discovery.ts:64-86` 四函数 `const { data } = await apiClient.get<T>` 二次解构 → undefined；`DiscoveryLabView.vue:58-80` 空 catch 吞掉。
- CR-4 落点：`trading/phases/post_close.py:300-314`（curr=None → 仅 warning，live 无告警无 halt）；先例同文件 :214-223（live CRITICAL）+ :360-365（`except _CriticalHalt: raise` 通道已打通）；关联悬崖 :431-445（收盘快照失败静默）。
- CR-5 落点：`scripts/audit_ssot.py:89-116`（fill↔position 只扫 `position WHERE qty!=0` 方向）+ :141-166（孤儿集合含幽灵 `OPEN`、漏 `ORDERED/TP1_FILLED/TP2_FILLED/STOP_TRIGGERED`）；`trading/state_store.py:350/:364` direction 无 CHECK；G5 迁移先例 `_migrate_with_backup`（:168-250）+ sidecar（:146-165）+ fill 范例（:325-357）。
- CR-7 落点：`infra/notifier.py`（DingTalk/Telegram/WeCom 三网络通道，无本地通道；`build_default_manager` :203-230 装配口；`_broadcast` :128-152 并发软降级）；schtasks 注册模板 `ops/manage_ops_schtasks.py:167-182` `register_guard()`；红线：`register()` 只清退不创建。
- CR-6 根因：`data/tools/repair_gaps.py` 单日拉取异常直接 raise → **已拉的 230/350 日全部丢弃**（与超时分支「部分 merge 落盘」:134-144 语义不一致）；Tushare 服务端 500/min 限频与客户端令牌桶错位。存量 16371 段（418 标的）且熔断循环中。
- CR-2 落点：`strategies/neckline/backtest.py:149-151`（参数 `id_cfg`/`exec`）⇄ `trading/compute/plan.py:141-146`（参数 `stop_cfg`）；`plan.py:98` `stop_cfg.get("stop_atr_mult", 2.0)` 兜底与 EXEC_DEFAULTS 1.0 不一致；`diag/diag_2026_cases.py:39-69` 另有诊断副本。
- CR-3 素材：stop_loss_monitor 每 30s 一轮（`stop_loss.py:145-540`，⑤后:536 前为接入点）；`query_asset`（qmt.py:852-915）锁定返 `{}`；blackout 节流范式 `trading/alerting.py` QuoteBlackoutThrottle + ports 注入（**禁止模块级 global**）；emergency_halt 在 `trading/gateway_service.py:433-470`（粘滞锁拒新单，**不是**停调度）；`_cancel_all_open_orders` 在 `trading/io/breaker.py:48`。
- W1-B 地图：engine.py 1693 行，re-export 块 L73-263 + 迁出注释 L283-470；内部实际使用 ~28 符号（见 Task 10 表）；trading 内 7 处外部引用；tests 触点 ~275（真需迁约 40-50）；gateway lazy 8 处（顶部化须换模块对象风格保 patch 命中）。
- W2 spec 原文（master design §5.1/§5.2/§5.3 + §9）：H1=BrokerProtocol + qmt.py 按连接/IO/业务分文件「**逻辑只搬+接缝注释**」+ re-export 兼容块 + L4 双跑；H2=回调体 `order_state.handle_order_update(engine, update)` 依赖经 Ports（state_store/notifier 显式注入）；M2=actual_sid 单 SSoT + StopLossContext + `is_vetoed()` 单点。**「串通挂撤/拒涨停」不在 broker**（在 phases/critical/compute），勿错搬。
- CR-8 隐藏消费方：`infra/tools/dingtalk_review_bridge.py:13/:40` 在跑消费 `POST /training/review` 与 `/research/review`——**这两个 router 不能删**。
- 工单回填目标文本：T0.1.md:6 `status: open`；T2.md:6-7；T13.md:6-7；MAP.md:52-62 frontier；sdd ledger `.superpowers/sdd/2026-08-13-g-wave-p0-guards/progress.md`（35 行止于 G6，缺 G7/G8；`task-G7-report.md` 缺、`task-G7-brief.md` 在）。
- CI：仅 ci.yml；repo=`yzzhan960104-glitch/quanter`；`docs/data_pool.md`/`caisen-methodology-summary.md` 已不存在（T0 丙删实际已执行，文档需销账）。

---

## File Structure（新增文件总览）

| 文件 | 职责 | Task |
|---|---|---|
| `presentation/web/src/api/discovery.spec.ts` | facade 解包语义回归 | T1 |
| `.github/workflows/ci-heartbeat.yml` | CI 心跳元守卫 | T5 |
| `ops/run_audit.bat` | 巡检调度入口（chcp+cd+PYTHONUTF8+重定向） | T4 |
| `trading/compute/price_levels.py` | 入场价位三件套单源 | T7 |
| `tests/trading/test_price_levels_golden.py` | 回测⇄实盘等价 golden | T7 |
| `trading/broker_ports.py`（或入 protocols.py） | BrokerProtocol + 回调 Ports | T12 |
| `broker/qmt_connection.py` / `qmt_io.py` / `qmt_business.py` | H1 分层落点（qmt.py 留兼容 re-export） | T13 |
| `tests/trading/test_stop_loss_portfolio_breaker.py` | CR-3 盘中熔断 | T8 |
| `ops/run_data_check.py`（自 data/tools 迁入） | TD 断边 | T9 |

---

# Wave A — 止血与观测（T1-T6，相互独立，可并行 subagent）

### Task 1: CR-1 discovery 死页修复 + 形状契约守卫

**Files:**
- Modify: `presentation/web/src/api/discovery.ts:64-86`
- Create: `presentation/web/src/api/discovery.spec.ts`
- Modify: `ops/check_contracts.py`（新增静态守卫规则）
- Modify: `tests/test_check_contracts.py`（喂假 ts 钉死新规则）
- Modify: `presentation/web/package.json:7`（predev 死路径顺带修）

**Interfaces:** Produces: check_contracts 新函数 `check_no_double_unwrap(ts_paths) -> list[str]`（供 main 调用）；discovery.ts 四函数签名不变（`getSensitivity/getHeatmap/getParams/getDiscoveryStatus`，返回 `Promise<T>` 直返 payload）。

- [ ] **Step 1: 写失败测试**（vitest）`discovery.spec.ts`：

```ts
import { describe, it, expect, vi } from 'vitest'

// 对齐 client.ts 拦截器运行时语义：apiClient.get 已直接 resolve 业务 payload
const payload = { n_trials: 12, marginals: [], ranking: [], dead_params: [], blind_spots: [] }
vi.mock('./client', () => ({ apiClient: { get: vi.fn(async () => payload) } }))

import { getSensitivity } from './discovery'

describe('discovery facade 解包语义（CR-1 回归钉）', () => {
  it('getSensitivity 直返 payload，不做二次解构', async () => {
    expect(await getSensitivity()).toBe(payload) // 二次解包会得 undefined → 红
  })
})
```

- [ ] **Step 2: 跑测确认红**：`npm --prefix presentation/web run test -- src/api/discovery.spec.ts` → FAIL（undefined ≠ payload）。
- [ ] **Step 3: 修 discovery.ts** 四函数改 trading.ts 姿势（去 `<>` 泛型与解构）：

```ts
export function getSensitivity(): Promise<SensitivityResponse> {
  return apiClient.get('/api/v1/research/discovery/sensitivity')
}
export function getHeatmap(x: string, y: string, metric = 'calmar'): Promise<HeatmapResponse> {
  return apiClient.get('/api/v1/research/discovery/heatmap', { params: { x, y, metric } })
}
export function getParams(): Promise<ParamsResponse> {
  return apiClient.get('/api/v1/research/discovery/params')
}
export function getDiscoveryStatus(): Promise<DiscoveryStatus> {
  return apiClient.get('/api/v1/research/discovery/status')
}
```

- [ ] **Step 4: 跑测绿** + 全量 `npm --prefix presentation/web run test` + `run typecheck`。
- [ ] **Step 5: check_contracts 加静态守卫**：新增 `check_no_double_unwrap`（扫描 `presentation/web/src/api/*.ts`，命中 `const { data } = await apiClient.` 即报错——对已剥壳实例二次解构），在 `main()` 里与 URL 对账并列调用；`tests/test_check_contracts.py` 加两例（含违规 ts → 报错；正常直返 ts → 通过）。中文注释说明根因（CR-1）。
- [ ] **Step 6: 修 package.json:7** predev `../scripts/ops/check_ports.py` → `../../ops/check_ports.py`；顺带 `vitest.config.ts:8` 注释路径订正。
- [ ] **Step 7: 验证**：`python ops/run_checks.py`（gate② 应绿且新守卫 0 命中）。
- [ ] **Step 8: Commit** `fix(debt): CR-1 discovery 死页修复——剥壳语义直返 + 形状契约静态守卫入 gate②`

### Task 2: CR-4 curr_equity 缺失 fail-closed + 收盘快照失败有声

**Files:**
- Modify: `trading/phases/post_close.py:300-314`（curr 分支）、`:431-445`（快照段）
- Test: `tests/trading/test_breaker_fail_closed.py`（追加两测）

- [ ] **Step 1: 写失败测试**：① `test_post_close_curr_equity_missing_live_halts`：live + query_asset 返 `{}` → 断言 `pytest.raises(_CriticalHalt)` 且 `_alert_critical` 被调；② `test_post_close_curr_equity_missing_dry_skips`：dry_run → `breaker_skipped=True` 不 raise。③ `test_snapshot_close_failure_live_alerts`：snapshot_close_equity 抛异常 → live 推 `_alert_critical`（不 raise）。
- [ ] **Step 2: 跑红**：`.venv310/Scripts/python.exe -m pytest tests/trading/test_breaker_fail_closed.py -x -q`（PYTHONUTF8=1）。
- [ ] **Step 3: 实现** `:307` 分支改造（对齐 DG-G3 裁决「不选仅告警不动作」；异常路径 :305-306 并入同语义——curr_equity 保持 None 落入同一分支）：

```python
if curr_equity is None or float(curr_equity) <= 0:
    # CR-4（DG-G3 对称收口）：当前权益缺失=熔断最该在岗的断线场景，
    # live 必须保守停调度并推 CRITICAL；dry_run 保留 skipped 语义（无真实资金敞口）。
    breaker_skipped = True
    msg = (f"post_close 熔断评估失效：query_asset 无有效当前权益 date={today_eq} "
           f"curr={curr_equity}（断线/锁定/查询失败）——按 fail-closed 停调度")
    logger.critical(msg)
    if _mode() == "live":
        _alert_critical(msg)
        raise _CriticalHalt(msg)
    logger.warning("dry_run 跳过日内熔断：curr=None（无真实资金敞口，保守停手当日）")
```

`:431-445` 快照段：except 内追加 `if _mode() == "live": _alert_critical(...)`（**不 raise**——快照失败不阻断 post_close 其余闭合段，但必须有声，防静默掏空次日 T-1 兜底）。docstring `:157-160` 同步 breaker_skipped 新语义。
- [ ] **Step 4: 跑绿** + 全量 `tests/trading/` 定向。
- [ ] **Step 5: Commit** `fix(debt): CR-4 curr_equity 缺失 live fail-closed + 收盘快照失败有声`

### Task 3: CR-5 漏挂方向观测三件套

**Files:**
- Modify: `scripts/audit_ssot.py`（check_fill_position 反向扫描 + 孤儿 action 集合订正）
- Modify: `trading/state_store.py`（fill 表 CHECK 迁移 + insert_fill 校验）
- Test: `tests/test_audit_ssot.py`（反向扫描用例）、`tests/test_position_book.py`（迁移用例）

- [ ] **Step 1: 写失败测试**：① 反向扫描：构造 fill 净额=100 而 position 无行 → `check_fill_position` 报 mismatch（现静默 PASS → 红）；② 孤儿集合：SIGNAL 后仅 `ORDERED` → 不误报；仅 `TP1_FILLED` → 不误报。③ 迁移：旧库含 `direction='buy'`（小写脏值）→ 迁移后行被跳入 sidecar、新表有 CHECK。
- [ ] **Step 2: 实现反向扫描**（:108 后，沿用显式 Python 累加风格避开 SUM NULL 陷阱）：

```python
# CR-5：漏挂方向（fill 净额≠0 而 position 缺行/为 0）——旧扫描集只扫 position≠0，
# 真实持仓漏记（→止损/止盈漏挂、敞口裸奔）方向符号根本不进循环，静默 PASS。
for sym, net in fills.items():
    if abs(net) > 1e-6:
        row = con.execute("SELECT qty FROM position WHERE symbol=?", (sym,)).fetchone()
        if row is None or abs(row["qty"]) <= 1e-6:
            mismatches.append(f"{sym}: fill净额={net} 但 position 缺行/为0（漏挂向）")
```

孤儿集合 `('CONFIRMED','VETOED','OPEN','FILLED','CLOSED')` → 订正为 `('CONFIRMED','VETOED','ORDERED','SUBMITTED','TP1_FILLED','TP2_FILLED','STOP_TRIGGERED','FILLED','CLOSED')`（删从未写入的 `OPEN`）。
- [ ] **Step 3: fill CHECK 迁移**：复用 `_migrate_with_backup`（G5 范式，fill 范例 :325-357），迁移条件「现表无 CHECK 时」；迁移前先 `SELECT DISTINCT direction` 实证历史值仅 BUY/SELL（勘探证实写入侧按 `=="BUY"` 判定、大写）；DDL 两处（:350/:364）加 `CHECK(direction IN ('BUY','SELL'))`；`insert_fill`(:837-867) 入口加 `if direction not in ("BUY","SELL"): raise ValueError(...)`。
- [ ] **Step 4: 跑绿**：新测试 + `tests/test_audit_ssot.py` + `tests/test_position_book.py` + `tests/trading/test_order_state*.py`。
- [ ] **Step 5: Commit** `fix(debt): CR-5 漏挂方向观测——audit 反向扫描 + 孤儿口径 + fill.direction CHECK`

### Task 4: CR-7 告警双通道 + 巡检调度挂载

**Files:**
- Modify: `infra/notifier.py`（新增 LocalFileChannel + 无条件装配）
- Create: `ops/run_audit.bat`
- Modify: `ops/manage_ops_schtasks.py`（新增 `register_audit()` + main flag）
- Test: `tests/test_notifier.py`（追加）、`tests/test_manage_ops_schtasks.py`（追加）

- [ ] **Step 1: 失败测试**：① `test_local_file_channel_appends`：send → `logs/alerts.log` 追加含时间戳/级别/文本行；② `test_default_manager_includes_local_file`：build_default_manager 通道列表含 LocalFileChannel（**无条件**——钉钉挂了本地仍有痕）；③ schtasks：`register_audit` 构造的 /TR 指向 run_audit.bat、/SC DAILY。
- [ ] **Step 2: 实现** LocalFileChannel（实现 NotificationChannel 接口；`logs/` 目录 makedirs；异常自吞仅 log——本地通道自身不抛）。`register_audit()` 照抄 register_guard 模板（/SC DAILY /MO 1，时间 16:05——post_close 15:30 之后；**不进 RETIRED/LEGACY 清退清单**）；`ops/run_audit.bat`：

```bat
@echo off
chcp 65001 >nul
cd /d E:\quanter
set PYTHONUTF8=1
".venv310\Scripts\python.exe" "scripts\audit_ssot.py" >> logs\audit_schtask.log 2>&1
```

- [ ] **Step 3: 执行注册**：`python ops/manage_ops_schtasks.py --register-audit`（幂等 /F；这是本计划唯一一次 schtasks 写操作）。`schtasks /Query /TN QuanterAudit` 验证。
- [ ] **Step 4: 跑绿** + Commit `feat(debt): CR-7 告警双通道（LocalFileChannel）+ audit_ssot 每日 schtasks 挂载`

### Task 5: CR-10/CR-11 CI 心跳元守卫 + 权威文档刷新

**Files:**
- Create: `.github/workflows/ci-heartbeat.yml`
- Modify: `.github/workflows/ci.yml`（加 weekly schedule——CI 自身心跳）
- Modify: `docs/guardrails.md`（重写过期段）、`docs/data-source-of-truth.md`（7 项检查/调度实况/Phase C 完成语义）
- Modify: `presentation/web/.env.example`（死引用注释订正）

- [ ] **Step 1: ci.yml 加心跳**：`on:` 增加 `schedule: [{cron: "17 1 * * 1"}]`（周一 UTC 01:17，避开整点）。
- [ ] **Step 2: 新建 ci-heartbeat.yml**（独立 concurrency 组 `ci-heartbeat`，防被 push run 取消；`permissions: actions: read`）：

```yaml
name: CI 心跳元守卫
on:
  schedule:
    - cron: "23 2 * * *"   # 每日 UTC 02:23
  workflow_dispatch: {}
permissions:
  actions: read
concurrency:
  group: ci-heartbeat
  cancel-in-progress: false
jobs:
  heartbeat:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - name: 断言 ci.yml 最近成功 run 距今 <= 7 天
        run: |
          set -euo pipefail
          latest=$(curl -sf -H "Authorization: Bearer ${{ secrets.GITHUB_TOKEN }}" \
            -H "Accept: application/vnd.github+json" \
            "https://api.github.com/repos/yzzhan960104-glitch/quanter/actions/workflows/ci.yml/runs?branch=master&status=success&per_page=1" \
            | jq -r '.workflow_runs[0].created_at')
          if [ -z "$latest" ] || [ "$latest" = "null" ]; then
            echo "ci.yml 无任何成功 run——保护链死亡"; exit 1; fi
          age_days=$(( ( $(date +%s) - $(date -d "$latest" +%s) ) / 86400 ))
          echo "最近成功 run: $latest（${age_days} 天前）"
          [ "$age_days" -le 7 ]
```

中文注释说明：CI 死亡=静默失败（07-25→08-13 教训 19 天），元守卫把「已 N 天没跑」从无人知晓变 workflow 红+邮件；另注「GitHub 对 60 天无活动 repo 自动停 schedule」的边界。
- [ ] **Step 3: guardrails.md 刷新**：:14 测试数 533→「~1870（截至 2026-08-15 collect）」；:39-40/§五 路径 `web`→`presentation/web`；§四 E2E 段整段重写为「tests/e2e/ 已随 caisen 退役，E2E 现由 e2e_long_cycle（pytest）+ vitest 组件测承担」；**新增一节「熔断/鉴权 fail-closed 语义」**（DG-G2 live 无 token 拒起、DG-G3 基线缺失停调度、CR-4 curr_equity 同口径）+ **风险取向显式声明**：「本系统在超卖与漏挂之间系统性选择防超卖（stop_loss.py `_tp_ok=True` / order_state.py `_tp_already=True` / audit 旧版单向扫描），漏挂方向靠 CR-5 反向扫描+人工补挂兜底——这是有意的取向，不是遗漏」；§七 follow-up 更新。
- [ ] **Step 4: data-source-of-truth.md 刷新**：:82「5 项」→ 7 项（补 check_client_process / check_port_owner_consistency）；:20-21/:89 调度描述 → 「已挂 schtasks QuanterAudit 每日 16:05（CR-7 · 2026-08-15 起）」；:34/:42-43 Phase C 完成语义订正（save_plan 已删且在 BANNED）；:130-131 caisen 已删。**加「截至日期」标注**（测试数等会漂）。
- [ ] **Step 5: Commit** `feat(debt): CR-10 CI 心跳元守卫 + CR-11 guardrails/data-source-of-truth 权威刷新`

### Task 6: CR-6 补采回路复活（部分落盘 + 限频降速）+ 实弹验证

**Files:**
- Modify: `data/tools/repair_gaps.py`（单日异常→部分落盘；降速参数）
- Test: `tests/test_repair_gaps.py`（追加部分落盘用例）

- [ ] **Step 1: 失败测试**：`test_partial_persist_on_fetch_error`——第 N 日 `_fetch_paged` 抛「频率超限」→ 已拉 1..N-1 日**仍 merge 落盘**、熔断计数+1、进程不 raise（exit 0 带 partial 计数）。
- [ ] **Step 2: 实现**：把逐段拉取循环体内 `_fetch_paged` 调用包 try/except——异常时 `logger.warning` + `break` 出拉新段循环，**继续走既有 merge 落盘路径**（与 :134-144 超时分支同语义）；新增 env `REPAIR_DAY_SLEEP`（默认 1.5s，日间隔 sleep，给 Tushare 服务端窗口留余量——客户端 500/min 桶与服务端计数窗口错位是实锤根因）；返回值/日志带 `partial=True` 标记。中文注释写明「部分补采 > 完全不补」的物理意图与 230/350 白拉教训。
- [ ] **Step 3: 跑绿**（既有两测 + 新测）。
- [ ] **Step 4: 实弹一轮**：后台 `Popen` 同款命令 `python -m data.tools.repair_gaps --auto --lake-dir data_lake`（日志重定向 logs/repair_auto.log 追加；**熔断窗内会自动跳过——若跳过则改 `REPAIR_RECOVERY_HOURS=0` 环境变量跑一轮再复原**）。验证 log 出现「部分落盘/进度净增长」，记录补成段数到 commit message。
- [ ] **Step 5: Commit** `fix(debt): CR-6 补采回路复活——异常部分落盘 + 限频降速（16371 段开始净收敛）`

**—— Wave A 门：`python ops/run_checks.py` 全绿 + 全量 pytest 无新增红——**

---

# Wave B — 策略数学单源（T7）

### Task 7: CR-2 compute_price_levels 单源 + C1 参数口收口 + golden 等价

**Files:**
- Create: `trading/compute/price_levels.py`
- Modify: `strategies/neckline/backtest.py:137-160`（改调单源）、`trading/compute/plan.py:95-166`（改调单源 + :98 兜底 2.0→1.0 对齐）、`diag/diag_2026_cases.py:39-69`（改调单源）
- Test: `tests/trading/test_price_levels_golden.py`（新）

**Interfaces:** Produces:

```python
@dataclass(frozen=True)
class PriceLevels:
    buy_limit: float | None      # 回踩买入限价（颈线 + buy_limit_atr_mult×ATR；仅回测挂单语义用）
    stop: float                  # 初始止损 = 颈线 − stop_atr_mult×ATR
    tp1: float | None            # 分级止盈一档 = 颈线 + tp1_h_mult×H（None=未配置→全量 tp2）
    tp2: float                   # 止盈 = 颈线 + tp_h_mult×H
    cancel_on: float             # 撤单阈值 = 颈线 + cancel_thresh_mult×H

def compute_price_levels(
    *, c_star: float, high: float, atr: float,
    stop_atr_mult: float, buy_limit_atr_mult: float,
    tp1_h_mult: float | None, tp_h_mult: float, cancel_thresh_mult: float,
) -> PriceLevels: ...
```

- [ ] **Step 1: 失败测试（golden）**：① 单源函数纯数学断言（手算几组）；② **等价 golden**：同一组 (c_star, H, ATR, 六参数) 下，`backtest.simulate_exit` 旧路径产出与新路径产出的 stop/tp1/tp2/cancel_on 逐位相等（迁移前以旧实现快照值钉死，防迁移变形）；③ `plan.build_orders_from_signals` 同输入同价位；④ C1：`stop_cfg` 缺 `stop_atr_mult` 时兜底=1.0（对齐 EXEC_DEFAULTS，修 plan.py:98 的 2.0 幽灵默认——**先查生产 stop_cfg 是否总显式给值**，确认 2.0 从未生效后改）。
- [ ] **Step 2: 实现** price_levels.py（纯函数零 IO；中文 docstring 写明「回测⇄实盘等价性是本系统头号资产，此函数是其数学单一归宿（CR-2/审计 spec A5+C1）」）。
- [ ] **Step 3: 两侧改调**：backtest.py/plan.py/diag 各删本地公式改 import（顶部直 import 物理真身）；参数取值仍从各自配置口读（id_cfg/exec/stop_cfg），但**默认值常量收敛到一处**（price_levels.py 定义 `PRICE_LEVEL_DEFAULTS`，三口引用）。
- [ ] **Step 4: 跑绿**：golden + `tests/trading/` + `tests/strategies/`（backtest 相关）+ `tests/test_engine.py` eod 路径定向。
- [ ] **Step 5: Commit** `feat(debt): CR-2 入场价位三件套单源 compute_price_levels + C1 参数默认收敛（回测⇄实盘 golden 钉死）`

---

# Wave C — 风控语义（T8）

### Task 8: CR-3 盘中组合级熔断评估点（5min 节流 + emergency_halt 不停调度）

**Files:**
- Modify: `trading/alerting.py`（新增 PortfolioBreakerThrottle）、`trading/ports.py`（EnginePorts 加字段，default_factory 保旧构造）
- Modify: `trading/phases/stop_loss.py`（⑤后接入 `_check_portfolio_loss_limit`）
- Test: `tests/trading/test_stop_loss_portfolio_breaker.py`（新）

**设计要点（执行者必读）**：
- **触发动作 = `emergency_halt()`（gateway_service）+ `_cancel_all_open_orders(gw)`（io.breaker）+ CRITICAL**——粘滞锁拒新单，**绝不 raise _CriticalHalt**（停调度会连带杀死止损监控本身，盘中不可接受）。
- **评估失败（query_asset 返空/异常）= 连续计数 ≥3 → `_alert_critical`（节流），不停调度**——断线场景 monitor 在 :271 已有 L1 halt 兜底，这里只补观测。
- **基线缺失（start=None）live = emergency_halt + CRITICAL**（对齐 DG-G3「不选仅告警不动作」，但转换 halt 形态保监控存活）；dry_run = warning。
- 顶部直 import：`from trading.compute.breaker import check_daily_loss_limit`、`from trading.gateway_service import emergency_halt`、`from trading.io.breaker import cancel_all_open_orders`（W1-A 红线：禁 lazy engine 反查）。

- [ ] **Step 1: 失败测试**：① 节流：同 5min 窗内第二轮 `should_check` 返 False；② tripped：start=100万 curr=95万 → emergency_halt 被调 + cancel_all 被调 + 不抛 _CriticalHalt + monitor 正常返回；③ curr 缺失 ×3 → `_alert_critical` 恰一次（节流）+ 不 halt；④ 基线 None + live → emergency_halt + CRITICAL；⑤ dry_run gw=None → no-op。测试构造照抄 `tests/trading/test_stop_loss_monitor_decide_exit.py` 的 `_make_ports_with_fresh_blackout` 先例。
- [ ] **Step 2: 实现** PortfolioBreakerThrottle（QuoteBlackoutThrottle 同范式：`_lock` + `last_check_ts` + `interval: float = 300.0` + `miss_streak: int`，原子方法 `should_check(now) -> bool` / `record_miss()` / `reset()`——reset() 供 M4 式 conftest autouse 复用既有 `_reset_resilience_singletons` 挂点，**把新单例加进该 fixture 清单**）。EnginePorts 加 `breaker_throttle: PortfolioBreakerThrottle = field(default_factory=PortfolioBreakerThrottle)`。
- [ ] **Step 3: stop_loss.py 接入**（⑤ pending 撤单后、⑥ 聚合告警前）：

```python
# CR-3：盘中组合级 -3% 评估点前移（旧态唯一判定点在 15:30 post_close=盘后闸）。
# 每 5min 节流读 query_asset；触发走 emergency_halt 粘滞锁（拒新单）而非 _CriticalHalt
# （停调度会杀死止损监控自身——盘中绝对不可接受）；评估失败只观测不动作。
if ports.breaker_throttle.should_check(now):
    await _check_portfolio_loss_limit(gw, ports.breaker_throttle)
```

`_check_portfolio_loss_limit` 内按设计要点三分支实现（复用 post_close.py:282 的 `get_start_equity` 读口 + `_resolve_account_id`）。
- [ ] **Step 4: 跑绿**：新测 + `tests/trading/test_stop_loss*.py` 全家 + `test_circuit_breaker.py`。
- [ ] **Step 5: Commit** `feat(debt): CR-3 盘中组合级熔断——5min 节流评估点前移，emergency_halt 不停调度`

**—— Wave C 门：全量 pytest + run_checks——**

---

# Wave D — 结构收尾（T9-T11）

### Task 9: TD 断边（run_data_check 迁移 + 日历函数下沉）

**Files:**
- Move: `data/tools/run_data_check.py` → `ops/run_data_check.py`（data→trading 边归零——ops 合法依赖 trading）
- Modify: `data/calendar.py`（收编 `expected_latest_trade_day`）、`trading/calendar.py`（改为 re-export 保 engine.py:724 不动）
- Test: 迁移 tests patch 锚 `data.tools.run_data_check.*` → `ops.run_data_check.*`

- [ ] **Step 1: grep 全部引用**（bat/schtasks/PIPELINE_TASKS/docs/tests）列清单——**若有 schtasks TR 指向旧路径，同步更新 PIPELINE_TASKS 元数据并在 commit 注明**（勘探：活跃 schtasks 只有 Server/Guard，此文件为 bat/手动入口）。
- [ ] **Step 2: 迁移 + `expected_latest_trade_day` 下沉 data/calendar.py**（交易日历纯域函数；trading/calendar.py `from data.calendar import expected_latest_trade_day` re-export，engine 不动）。
- [ ] **Step 3: 复跑 #2 扫描脚本**验证 `data -> trading` 边 = 0（写进 commit message）。
- [ ] **Step 4: 跑绿**（迁移的测试 + tests/data 定向）+ Commit `refactor(debt): TD data→trading 边清零——run_data_check 迁 ops + 日历函数下沉`

### Task 10: W1-B engine re-export 块删除 + gateway lazy 顶部化

**Files:**
- Modify: `trading/engine.py`（删 L73-263 re-export 块 + L283-470 迁出注释块；~28 个内部使用符号改顶部直 import 物理真身）
- Modify: `trading/catchup.py:138`、`trading/orchestrate/__init__.py:44-50`、`trading/orchestrate/pipeline.py:129`、`trading/__main__.py:125`、`trading/tools/trigger_pre_open_once.py:78`、`trading/tools/trigger_eod_once.py:75`（改 import 物理真身；`__main__` 的 lazy 保 apscheduler 理由若在则改为 lazy 物理路径而非 engine）
- Modify: gateway lazy 8 处顶部化（engine.py:374/392/1344、eod_plan.py:225、order_state.py:489、io/orders.py:43、post_close.py:237/348）——**换模块对象风格** `from trading import gateway_service` + 属性访问（防 from-import 本地绑定冻结 patch 命中），同步迁对应 patch；gateway_service.py 自身 8 处 state_store/job_ledger lazy 顶部化（模块对象风格，qmt_gateway 的 xtquant 容错 lazy **不动**）
- Modify: tests ~40-50 处 patch/import 迁物理路径

**执行程序（先审计后动刀）：**
- [ ] **Step 1: 审计脚本先行**——临时脚本列 engine re-export 符号 × 内部 Load 计数 × tests 触点（勘探表为基线，动刀前重跑核实）；产出清单贴进 commit body。
- [ ] **Step 2: 零引用符号直接删**（勘探清单「零内部引用」列：_CriticalHalt re-export、ExitAction/ExitReason、_cancel_all_open_orders、_check_daily_loss_limit、build_orders_from_signals、decide_exit、get_data_ready、place_take_profit、should_trigger_stop、7 个 data_ctx 新名、各旧 `_` 别名等）。
- [ ] **Step 3: 内部使用符号改直 import**（勘探「实际内部使用」表：_mode×10、_alert_critical×9、_critical_guard×5、clock×6、calendar×4、_state_store×4 等 28 个——engine 顶部加一段直 import 物理真身，中文注释注明 W1-B 销账）。
- [ ] **Step 4: 7 处外部引用迁移** + gateway lazy 顶部化（模块对象风格）。
- [ ] **Step 5: 测试迁移**——`grep -rn "trading\.engine\." tests/ | grep -v get_gateway\|TradingEngine\|trading_plan\|load_plan\|calendar\|clock\|_state_store\|_submit"` 逐个迁物理路径（这些例外是 engine 自有/自用 import，patch 点天然保留）；`from trading.engine import` 的集群符号（_CriticalHalt/place_take_profit/pre_open/_trade_cfg/_close_expired_positions/_critical_guard 等 ~15 处）迁物理真身。
- [ ] **Step 6: 验收**：① `grep -n "^from trading\.\|^import trading\." trading/engine.py` 无集群模块 re-export（直 import 除外）；② 全量 pytest 绿；③ `python ops/run_checks.py` 绿。
- [ ] **Step 7: Commit** `refactor(debt): W1-B engine re-export 块删除 + gateway lazy 顶部化（模块对象风格保 patch）`

### Task 11: T10 嵌套父子实证关闭 + T11 connect 留痕 + M2 非broker件（StopLossContext/is_vetoed）

**Files:**
- Modify: `ops/process_topology.py`（视实证结果）、`broker/qmt.py connect`（-1 逐次 status_msg 留痕）
- Create: `trading/stop_loss_context.py`（StopLossContext dataclass）
- Modify: `trading/engine.py:1489-1581`（_stoploss 三 map 收口进 StopLossContext）、`trading/phases/stop_loss.py`（签名收参）、`trading/state_store.py`（新增 `is_vetoed(trade_id)`）、`trading/eod_plan.py:212`（改调单点）
- Test: `tests/trading/test_stop_loss_context.py`（新）、state_store is_vetoed 用例

- [ ] **Step 1: T10 实证**：读当前进程树（`wmic process where "name='python.exe'" get ProcessId,ParentProcessId,CommandLine` 只读）——若引擎进程无嵌套 python 子进程（或子进程 ppid 已被 process_topology:82-121 去重覆盖），在 T10.md 写 Resolution「已实证不发生（2026-08-15 进程快照）+ 拓扑已有 ppid 去重，降级关闭」；若发现嵌套，补递归祖先链去重 + 测试。
- [ ] **Step 2: T11 留痕**：connect 每次 -1 返回时 `logger.warning("connect -1 sid=%s status=%s msg=%s", ...)`（用 `_map_qmt_status` 结果），**不改重试次数/顺序**（G8 刚重构过，周一实战前不动行为）；T11.md Resolution 记「观测补齐完成；重试语义保持 G8 形态」。
- [ ] **Step 3: StopLossContext**：frozen dataclass 持 `stop_prices/monitor_ctx/pending_ctx` 三 dict + 类型注释；engine `_stoploss` 构造点收口、`stop_loss_monitor` 签名以单参收三 map（~5 处测试 patch 同步）；**若迁移中任一测试暴露状态机语义变形，立即降级**为「命名注释 + docstring 锁」（spec M2 原文允许），并在 commit 注明降级理由。
- [ ] **Step 4: is_vetoed 单点**：`state_store.is_vetoed(trade_id) -> bool`（`get_latest_action(trade_id) == "VETOED"` 封装）+ eod_plan.py:212 改调 + 用例。
- [ ] **Step 5: 跑绿 + Commit** `feat(debt): T10 实证关闭 + T11 connect 留痕 + M2 StopLossContext/is_vetoed 单点`

**—— Wave D 门：全量 pytest + run_checks + `grep _eng_mod` = 0 复核——**

---

# Wave E — broker 适配层 W2（T12-T15，最高风险波，逐 task 门禁）

### Task 12: W2-H2 回调体 Ports 化

**Files:**
- Modify: `trading/ports.py`（EnginePorts 扩 `state_store` / `notifier` 显式依赖字段——dataclass default_factory 指向现有单例模块）
- Modify: `trading/order_state.py`（`handle_order_update(engine, update)` → `handle_order_update(ports, update)`；内部 `_state_store`/钉钉通知改经 ports 属性访问；engine 实例依赖逐个替换）
- Modify: `trading/engine.py:1628`（回调注册处传 ports）
- Test: 既有 order_state 测试迁移 + 新契约测试（fake ports 注入断言副作用走 ports）

**边界**：只抽象**副作用依赖**（state_store 写/钉钉通知）；不动三分支业务逻辑（async_response/order/trade 语义逐行保形——这是 08-04 幂等红线的载体）。
- [ ] **Step 1: 失败测试**（fake ports 断言 insert_fill/apply_fill_to_position 经 ports.state_store）→ Step 2 实现 → Step 3 全量 order_state 测试绿 → Step 4 Commit `refactor(debt): W2-H2 回调体 Ports 化——order_state 副作用依赖显式注入`

### Task 13: W2-H1 BrokerProtocol + qmt.py 四文件分层（逻辑只搬）

**Files:**
- Create: `broker/qmt_connection.py`（连接生命周期 + 辅助函数 + C++ 回调骨架）、`broker/qmt_io.py`（查询/IO 六方法）、`broker/qmt_business.py`（_confirm_cancelled/_sync_orders_if_stale/submit_order/撤单三方法/cleanup_orders/锁风控状态机）
- Modify: `broker/qmt.py` → 收缩为「契约 + re-export 兼容块」（同 T1 范式：`from broker.qmt_connection import *` 显式列名 re-export，**外部 import 面零变化**）
- Create: `trading/broker_ports.py`（BrokerProtocol：submit/cancel/query_asset/query_orders/query_trades/sync_positions/probe + 钩子；`isinstance` 运行时契约测试）

**红线（spec §5.1 原文）**：逻辑只搬位置 + 接缝注释，**不改行为**；`set_order_update_callback` 保留为契约一部分；分层后 `tests/test_layer_contract.py` 必须绿（broker 不 import trading）。
- [ ] **Step 1: 搬移前快照**——`tests/trading/test_qmt_gateway.py`（897 行）+ `test_qmt_health_guard.py`（721 行）+ `test_qmt_cancel_confirm.py` 全绿基线记录。
- [ ] **Step 2: 按 agent 勘探的集群表逐块搬**（连接层 L121-406/425-786/1516-1661、IO L789-1082、业务 L1085-1508）——每搬一块跑对应测试；qmt.py re-export 保 `QmtExecutionGateway` 类名与模块级符号面不变（类本体留在 connection，io/business 方法经 mixin 或组合——**选 mixin**（`class QmtExecutionGateway(QmtBusinessMixin, QmtIoMixin, QmtConnectionBase)`），零调用点改动）。
- [ ] **Step 3: BrokerProtocol** + 契约测试（`isinstance(QmtExecutionGateway(), BrokerProtocol)` 结构化断言）。
- [ ] **Step 4: 门**：三套 qmt 测试全绿 + `tests/test_layer_contract.py` 绿 + `python ops/run_checks.py` 绿。
- [ ] **Step 5: Commit** `refactor(debt): W2-H1 broker 四文件分层 + BrokerProtocol（逻辑只搬，mixin 保类面）`

### Task 14: M2 actual_sid 单 SSoT

**Files:**
- Modify: `broker/qmt.py`（`_write_runtime_session` 降运行态快照，docstring 标「非真相源」）、消费方改读 DB `account.session_id`
- Test: 对应用例

- [ ] 先 grep `engine_session.json` 全部读者（trading_supervisor/守护脚本）→ 改读 `state_store` 查询口（无则加 `get_session_id(account_id)`）→ json 保留写入但标注降级 → 测试绿 → Commit `feat(debt): M2 actual_sid 单 SSoT——DB account.session_id 唯一真相源`

### Task 15: W2 波次门——L3 + L4 双跑

- [ ] **L3**：`.venv310/Scripts/python.exe -m pytest tests/e2e_long_cycle -q`（基线：24 passed + 2 存量红）。
- [ ] **L4 近似双跑**：master（合并前）与 branch 各跑一遍 `tests/e2e_long_cycle` 的 `table_snapshot` 产出，diff 行为输出（工具已存在 `tests/e2e_long_cycle/table_snapshot.py`）；任何 diff 逐条解释（预期内：时间戳/新 CR-4 告警文案）。
- [ ] 性能冒烟：`strategies/neckline` 扫描热路径计时 vs 基线（35.5s 量级，<10% 退化）。
- [ ] 任一门红 → 回滚 Wave E 对应 task（`git revert`），BLOCKED 记录。

---

# Wave F — 文档对账收官（T16-T18）

### Task 16: CR-9 工单回填 + CR-8 孤儿处置记录

**Files:** `plans/wayfinder/T0.1.md`（status: closed + closed_at + Resolution 链 deep-dives）、`T2.md`（status: done + completed: 2026-08-12 + Resolution 记 W1-A/t2-keystone，诚实注明「完整三维适配层余项归 W2/W3 继续」）、`T13.md`（status: done + completed: 2026-08-11 + Resolution 记 A/B 合并 + CR-6 运行态尾巴转出）、`T9.md`（status 补 done + 299ab2de）、`T10.md`/`T11.md`（Task 11 的 Resolution）、`T6.md`（Resolution：①不重启 DG-5 ②W3 划线 ③schema 契约测试已落 ④键对齐已落，status: closed）、`MAP.md:52-62` frontier 重写（删 T0.1/T9/T13，阻塞链改「W2→T3」，注明 T2 keystone done）、`.superpowers/sdd/2026-08-13-g-wave-p0-guards/progress.md`（补 G7/G8 complete 条目 + **G8 撞号消歧注**：sdd Task G8=删 caisen ≠ master commit G8=sid 闸）、Create `task-G7-report.md`（由 brief + commit 4a29362f 内容补写，诚实标注「事后补记 2026-08-15」）。
**CR-8 处置记录**：在 06-tech-debt CR-8 条目销账处写明——training/research router **保留**（dingtalk_review_bridge 在跑消费 /training/review 与 /research/review）；ops/processes 标注「内部观测端点」；review/diagnose 标注「CLI 写面」；sector/flow 已删（Task 17 前置：本 task 内完成 macro 三处删除——macro.py sector 腿 + macro.ts getSectorFlow + DashboardView 板块块 + 对应测试改）。
- [ ] 全部回填 → Commit `docs(debt): CR-9 工单状态全量回填 + CR-8 孤儿路由显式处置 + sdd G7/G8 补账`

### Task 17: 06-tech-debt 全量销账对账 + 架构视图收官

**Files:** `docs/architecture/06-tech-debt.md`（逐条销账：CR-1..11、Q/TB(W2)、TD、SS、CN、DOC、DC、测试 follow-up——热力图与四级表重写为终态）、`deep-dives/2026-08-14-critical-review.md`（文头加「2026-08-15 清偿后记」段，逐 CR 标注处置）、`roadmap.md`（波次状态刷新：W1-B done、W2 done、G 波含尾巴、A 波仍待）、`02-module-dependencies.md`（复跑扫描脚本刷新边权/行数）、`README.md`（分层行数）。
- [ ] **DC 死代码顺手清**：grep `消息重复|pro 死参`（P3 follow-ups 线索）+ `vitest.config.ts:8`/`.env.example` 注释——找到即删，找不到则在销账处记「线索已穷尽，未复现」。
- [ ] **silently orphaned patch 审计**：临时脚本扫 tests/ 中 `patch("trading.engine.X")`/`setattr(engine, "X", ...)` 的 X 不再是 engine 属性的孤儿（W1-B 后残留）——产出报告贴 commit body；明确失效的（符号已物理迁移且断言不再守护任何行为）迁物理路径或删除，拿不准的保留并列表（诚实标注）。
- [ ] Commit `docs(debt): 06-tech-debt 全量销账对账 + 架构视图收官刷新`

### Task 18: 终验 + 合并 + push + 记忆更新

- [ ] **全量门**：`python ops/run_checks.py` 五 gate 全绿；全量 pytest（.venv310，PYTHONUTF8=1）无新增红（基线 2 存量）；e2e_long_cycle L3 绿。
- [ ] **merge**：master ff 合入 `debt/full-wave-0815`；`git push origin master`（含分支）。
- [ ] **CI 实证**：push 后确认 ci run 触发（无法本机看则注明待验；heartbeat workflow 落盘即生效）。
- [ ] **补采进度复核**：读 logs/repair_auto.log 确认 Task 6 后净收敛进行中。
- [ ] **记忆更新**：MEMORY.md 各条目（frontend/risk/ci/governance/w0/price-level）刷成清偿后状态；新增本波次记忆。
- [ ] **最终报告**：向用户输出完成清单（每 task 状态 + 验证证据 + BLOCKED 项及原因 + 周一 9:22 前用户需做的事：重启引擎使新代码生效——需 QUANTER_API_TOKEN）。

## Self-Review 结论

- **Spec 覆盖**：06-tech-debt 活债逐条 → T1(CR-1)/T2(CR-4)/T3(CR-5)/T4(CR-7)/T5(CR-10+11)/T6(CR-6)/T7(CR-2)/T8(CR-3)/T9(TD)/T10(W1-B)/T11(CN-T10/T11+M2 部分)/T12-15(W2 Q/TB + M2)/T16(CR-8/9+DOC)/T17(销账+DC+orphan patch)。✅ 无遗漏。
- **已知不做（诚实边界）**：A 波 A3/A4（策略可信——非 #6 债，roadmap 独立轨）；W3 多策略 schema（划线）；16371 段数据全量收敛（Tushare 限频物理约束——代码侧已修，收敛需数日，Task 18 复核进度）；引擎重启（用户决策）。
- **类型一致性**：compute_price_levels 签名在 T7 定义后无他处引用；PortfolioBreakerThrottle 方法名（should_check/record_miss/reset）在 T2/T8 间无交叉；Ports 字段名 breaker_throttle 全文一致。✅
