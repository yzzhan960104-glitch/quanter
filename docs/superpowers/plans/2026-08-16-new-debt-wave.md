# 新债清偿实施计划（2026-08-16 · 停牌真值 + entry_date + 死种子 + 测试卫生 + Low 批）

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。Steps 用 checkbox。
> **用户授权**：延续 debt/full-wave-0815 模式（「继续完成新债」2026-08-16），subagent 执行到底，遇阻塞诚实记录。

**Goal:** 清偿 #6 于 2026-08-15/16 登记的全部新债：High 停牌真值缺口、Medium entry_date / save_plan_legacy 死种子、测试卫生（pytest 真实 spawn 补采）、Low 清单可执行项。

**Architecture:** 6 task 单波（T1 为核心）。全部基于 2026-08-16 勘探实证（关键反转：T6「000029.SZ 铁证」不成立——真实 scan 判其 justified；真根因=2019-2022 suspend_d 长停牌稀疏 + 段级 all() 放大 + 盲区段占配额）。

## Global Constraints

1. Python `.venv310/Scripts/python.exe` + PYTHONUTF8=1；全量 pytest 勿设 QUANTER_TESTING。
2. **live 引擎运行中（PID 树）——不碰进程；生产 DB 只读（除非 task 明确授权的单点写）；data_lake 写入仅经 repair 正规链路**。
3. TDD 先红后绿；中文注释 What+Why；每 task 一 commit（中文 conventional + 新债编号）。
4. 分支 `debt/new-debt-0816`（master @ ca11e7da 切出）；终态 ff 合回 + push。
5. 测试基线：全量 0 failed（1953+），不得新增红。

## 关键勘探结论（执行者必读）

- **scan 判定链**：`data/tools/scan_integrity.py:63-83` 组装 → `data/integrity.py:324-376` find_gaps——**段级 `justified = all(d in susp for d in seg)`**（:372）；suspend_d.parquet 191,835 行（2016-08-15~2026-08-11，S=184k/R=7.7k，2019 年后长停牌 S 稀疏化：2018=42,976 → 2019=5,752）。
- **实跑复现**：unjustified 16,371 段（418 标的）主峰 2016-2022（15,691）；2000-2015 仅 ~591（盲区）。铁证样本：000670.SZ 589 天洞仅 11 行 S / 000792.SZ 311 天 13 行 / 000995.SZ 399 天 8 行。
- **配额死循环**：`repair_gaps.py:144-148` 取 `unjustified[:50]`（symbol 序）→ 首批被低序号 2000-2004 盲区段永占（Tushare daily 无该年代数据）；**无 unfillable 概念**（:234-236 拉空只 warning）。
- **市场共识启发式实证**：湖日级在场计数 1.5s 可算（10.3M 行）；000670.SZ 洞窗每日在场 3,662-4,684 标的、零日低于中位数 80%；**2017 前湖仅 ~20 标的——启发式只对 2017+ 有效**。
- **entry_date**：`state_store.py:1039-1081` apply_fill_to_position——traded_time 已是第 6 位置参数但未用于 entry_date（:1054 取 clock.today()，:1071 首 BUY 锁定）；18 调用点全已传 traded_time（四态格式：ISO T/空格分隔/14 位数字/纯时间）；backfill 脚本 :84-99 有 SQL 事后订正 hack（修复后可删）。
- **死种子**：`tests/_legacy_plan_io.py`（save_plan_legacy JSON 镜像对 load_plan 恒不可见）；DB 双写同构三份：`test_probabilistic_broker.py:107-130 _seed_plan_truth`、`test_e2e_long_cycle.py:78-104 _fake_run_eod_phase`、`test_table_snapshot.py:12-30` 手写；账户口径分歧（resolve vs 硬编码 e2e_acc vs env）。
- **pytest spawn**：`pipeline.py:164-184` scan 真读生产湖 + 函数内 import subprocess Popen（无 patch 锚）；6 用例触发（test_pipeline_then_eod ×2 / test_c8_date_param ×3 / test_e2e_trading_flow ×1）；正确 mock 先例 `test_scan_fail_triggers_repair_but_eod_runs` :152-159。
- **Low 清单状态勘误**：⑦ ENGINE_FILES（T18 已做，标 stale）⑩ 盘中 T-1 兜底（终审修复已做，标 stale）；⑨ TerminalLogs SSE 未接 ⑪ _schtasks GBK 解码 ② engine._submit 零调用 ③ _ORDER_TIMEOUT 三拷贝——本波处置。

---

### Task 1: 【High】停牌真值——日级判定 + 长洞市场共识 + unfillable sidecar + 实弹验证

**Files:** Modify `data/integrity.py`（find_gaps 日级化 + 共识启发式）、`data/tools/scan_integrity.py`（在场计数喂入）、`data/tools/repair_gaps.py`（unfillable sidecar + 段选择跳过盲区年代可选）；Test `tests/test_integrity.py`/`test_repair_gaps.py` 扩展。

**设计（锁定）**：
1. **日级判定替代段级 all()**：gap 段拆「justified 日集 / unjustified 日集」；段输出升级为（保留旧字段兼容）`unjustified_days` 子段列表——repair 以子段为单位（真缺一日补一日）。
2. **长洞市场共识启发式（2017+）**：段长 ≥10 交易日且段内每日「湖市场在场数 ≥ 当段窗口市场中位数 × 0.8」且标的在段前后均有数据 → 判 `suspend_suspected=True`（justified-with-flag，scan 报告单列计数）；短段（<10 日）维持严格 suspend_d 判定（2017-2018 密集覆盖期，真短漏不放过）。中文注释写明两方向风险权衡（误放真缺 vs 误补停牌）与 000670.SZ 实证。
3. **unfillable sidecar**：repair 拉取「源全段零行」（真停牌残留/盲区年代）→ 记 `data_lake/.repair_unfillable.json`（symbol+range+reason+count），下轮 scan/repair 跳过已标记段；`--clear-unfillable` 入口防误标永久化。
4. **实弹验证（必做）**：改后真跑 scan——预期 unjustified 从 16,371 骤降（000670/000792/000995 转 suspend_suspected）；跑一轮 repair（配额 50）——预期**净补成 >0 段**（真缺段进队）；数字进报告与 commit。
5. 兼容：scan 报告新增字段的消费方（pipeline.py `_n_gaps` 用 unjustified_gaps 计数——对齐新语义）。

### Task 2: 【Medium】entry_date 取成交日

**Files:** Modify `trading/state_store.py`（apply_fill_to_position 解析 traded_time 四态 → entry_date；解析失败回退 clock.today() 保纯时间单测）、`scripts/archive/backfill_live_trades_to_state_store.py`（删 :84-99 SQL 订正 hack）；Test 新增跨日用例（traded_time=昨日 23:59 → entry_date=昨日）+ 既有 18 调用点回归。

### Task 3: 【Medium】save_plan_legacy 死种子 → 共享 DB 种子 helper

**Files:** Create `tests/_plan_seed.py`（`seed_plan(date_iso, orders, *, confirmed=True, json_mirror=False, account_id=None)`——DB SIGNAL(meta 全量) [+CONFIRMED]，默认 `engine._resolve_account_id()` 可覆盖；json_mirror 保 attribution/snapshot 老断言路径）；Modify `test_probabilistic_broker.py`/`test_e2e_long_cycle.py`/`test_table_snapshot.py` 三处同构迁移为调 helper；`_legacy_plan_io.py` 保留（纯 JSON 流测试仍用）。T15 的「第三份拷贝」Low 项同步销账。

### Task 4: 【测试卫生】pytest 不再真实 spawn 补采

**Files:** Modify `trading/orchestrate/pipeline.py`（Popen 块抽模块级 `_spawn_repair(lake_dir)`——生产语义零变化，产生干净 patch 锚）；6 个触发用例补 `patch("data.tools.scan_integrity.scan", return_value={...unjustified_gaps:0})`（照 :152-159 先例；附带省每用例真读 10M 行 parquet 的 5-10s）。验收：全量 pytest 后 `logs/repair_auto.log` 无新增写入 + `.repair_breaker.json` 不变。

### Task 5: 【Low 批】清单可执行项

① TerminalLogs.vue 接 `POST /api/v1/auth/read-cookie`（apiClient.post 先行 + catch 照常直连 + spec 补 vi.mock client）② `ops/manage_ops_schtasks.py` `_schtasks` Popen 加 `encoding/`errors` 修 GBK 解码 ③ `engine._submit` 核实零生产调用者→删+迁其测试 patch ④ `_ORDER_TIMEOUT` 三拷贝收口（io/business 改模块属性访问 `qmt_connection._ORDER_TIMEOUT`，文件头 patch 纪律注释同步）⑤ ⑦⑩ Low 清单标 ✅（stale 勘误）⑥ ① T13-C D5/D6/D7/D9 disposition 记录（读 T13 spec 对照 T1 覆盖面，未覆盖项如实留档）。

### Task 6: 终验 + 对账 + merge/push

全量 + run_checks + L3 → #6 新债销账（含根因叙事勘误：000029 铁证不成立，真形态 2019-2022 稀疏）→ deep-dive 后记补一行 → ff merge master + push → 记忆更新。

## Self-Review
- 覆盖：用户列的 1/2/3 全部（1→T1，2→T2/T3+Low 清单→T5，3→T4）；⑭⑮ 维持登记不强行重构（风控代码周一临战 + YAGNI，理由已在案）。✅
- 类型一致性：seed_plan 签名 T3 定义后无他处引用；_spawn_repair T4 定义即消费。✅
