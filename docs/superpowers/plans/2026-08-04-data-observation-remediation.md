# 数据观测与调度治理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 恢复数据观测层全链路——quick 批日频同步、服务器端同步入口、哨兵状态机、任务治理、qfq 除权重算、前端日期口径、GBK 输出——使 `/datasets` 状态真实反映健康度。

**Architecture:** 修复分四层：计划任务/同步入口（schtasks + data_service 子进程）→ 哨兵状态机（_derive_status + unavailable）→ 数据质量（sync_daily_incremental 除权重算 + _sync_by_date shard 校验）→ 输出治理（infra/pyio.py 统一 UTF-8 + 前端本地日期）。全部改动向后兼容，默认路径零行为变化。

**Tech Stack:** Python 3.10 / pandas / argparse / FastAPI data_service / schtasks / pytest / vitest + jsdom。

## Global Constraints

- 不重建任何已退役 schtasks（QuanterDataPipeline / QuanterBrief / QuanterDiscoveryDaemon / QuanterDailyBrief）。
- 不引入新第三方依赖（UTF-8 / 原子写 / 存活检测全部 stdlib）。
- 不改策略/信号逻辑；`data.sync`/`sync_dataset` 对外签名不变。
- `_unavailable` 数据集（top_list / hsgt_top10 / concept / concept_detail）只新增状态展示，不触发拉取。
- 新增 `DATASET_REGISTRY` 条目必须同时提供 `script + args` 契约（spec review 检查点）。
- 每次任务独立提交；提交前跑相关 pytest；最终跑 `pytest tests/ -q` 全绿。

---

## File Structure

| 文件 | 责任 |
|---|---|
| `presentation/server/services/data_service.py` | 同步子进程拼 key（T2）、_derive_status unavailable 分支（T3） |
| `data_lake/.syncing/*.failed` | 历史哨兵清理目标（T4） |
| `docs/superpowers/plans/2026-08-04-data-observation-remediation.md` | 本计划 |
| `infra/pyio.py` | 运维入口 stdout UTF-8 统一助手（T7） |
| `ops/data_pipeline.py`、`ops/brief_all.py` | 接入 pyio + 修残留 emoji（T7） |
| `trading/tools/*.py`、`compute_unit/*.py`、`discovery/tools/param_iter.py`、`scripts/export_sobol_task.py`、`data/tools/probe_tushare_fields.py`、`data/tools/scan_integrity.py` | 接入 pyio（T7） |
| `scripts/run_*.bat`（除 start_server.bat） | 补 `set PYTHONUTF8=1`（T7） |
| `discovery/schtasks.py` | `--register` 拒绝重建退役任务（T8） |
| `ops/manage_ops_schtasks.py` | `RETIRED_TASKS` 补 QuanterDailyBrief（T8） |
| `data/tools/sync_daily_incremental.py` | 除权标的历史 qfq 自动重算（T9） |
| `data/tushare_sync.py` | `_sync_by_date` 空/损坏 shard 校验（T10） |
| `presentation/web/src/utils/date.ts` + 3 个视图 | 本地时区业务日期（T6） |

---

### Task 1: 修复 quanter_sync_incremental 计划任务路径

**Files:**
- Modify: 系统计划任务 `\quanter_sync_incremental`（无代码文件）

**Interfaces:**
- Consumes: 现有仓库入口 `F:\quanter\scripts\run_sync_incremental.bat`
- Produces: 可被 `schtasks /Run` 手动触发并写 `data_lake/.syncing/sync_incremental.stdout.log` 的任务

- [ ] **Step 1: 确认当前断链状态**

Run: `schtasks /Query /TN quanter_sync_incremental /V /FO LIST`
Expected: `Task To Run: C:\Users\yzzhan\Desktop\quanter\scripts\run_sync_incremental.bat`、`Last Result: 1`

- [ ] **Step 2: 改指仓库入口**

```powershell
schtasks /Change /TN quanter_sync_incremental /TR "F:\quanter\scripts\run_sync_incremental.bat"
```

- [ ] **Step 3: 验证配置**

Run: `schtasks /Query /TN quanter_sync_incremental /V /FO LIST`
Expected: `Task To Run: F:\quanter\scripts\run_sync_incremental.bat`

- [ ] **Step 4: 手动触发验证**

```powershell
schtasks /Run /TN quanter_sync_incremental
```
Expected: 任务运行；`data_lake/.syncing/sync_incremental.stdout.log` 出现新 `=== 增量同步 START` 行。

- [ ] **Step 5: Commit（无代码变更，跳过）**

---

### Task 2: data_service 同步子进程拼 key（根治服务器端同步失败）

**Files:**
- Modify: `presentation/server/services/data_service.py`（`_run_sync_subprocess`，约 :184-190）
- Test: `tests/test_data_service.py`

**Interfaces:**
- Consumes: `DATASET_REGISTRY[key]["script"]`（`data/tools/sync_tushare.py` 或 `data/tools/sync_macro_credit.py`）
- Produces: 子进程命令 `[python, script, key, *args]`（仅 sync_tushare.py）；其余脚本保持 `[python, script, *args]`

- [ ] **Step 1: 写失败测试**

```python
def test_run_sync_subprocess_injects_key_for_sync_tushare(monkeypatch):
    """sync_tushare.py 必须带数据集 key（否则 argparse usage 退出码 2 → .failed）。"""
    import presentation.server.services.data_service as ds
    captured = {}
    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    monkeypatch.setattr(ds.subprocess, "run", fake_run)
    monkeypatch.setattr(ds, "_clear_sentinel", lambda key: None)
    monkeypatch.setattr(ds, "_mark_failed", lambda key, msg: None)
    monkeypatch.setattr(ds, "_PROJECT_ROOT", r"F:\quanter")
    monkeypatch.setattr(ds.DATASET_REGISTRY, "get", lambda k, d=None: {
        "moneyflow": {"script": "data/tools/sync_tushare.py"}}.get(k, d))
    ds._run_sync_subprocess("moneyflow")
    assert captured["cmd"][2] == "moneyflow"


def test_run_sync_subprocess_no_key_for_macro(monkeypatch):
    """sync_macro_credit.py 不消费 key（argv 忽略），不得注入。"""
    import presentation.server.services.data_service as ds
    captured = {}
    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    monkeypatch.setattr(ds.subprocess, "run", fake_run)
    monkeypatch.setattr(ds, "_clear_sentinel", lambda key: None)
    monkeypatch.setattr(ds, "_mark_failed", lambda key, msg: None)
    monkeypatch.setattr(ds, "_PROJECT_ROOT", r"F:\quanter")
    monkeypatch.setattr(ds.DATASET_REGISTRY, "get", lambda k, d=None: {
        "macro": {"script": "data/tools/sync_macro_credit.py"}}.get(k, d))
    ds._run_sync_subprocess("macro")
    assert len(captured["cmd"]) == 2          # [python, script]，不得注入任何 key
    assert captured["cmd"][1].endswith("sync_macro_credit.py")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_data_service.py -q`
Expected: 新测试 FAIL（cmd 缺 key）

- [ ] **Step 3: 实现**

在 `_run_sync_subprocess` 中，`cmd` 构造后插入：

```python
cmd = [sys.executable, script_abs, *args]
# C-9 A1：sync_tushare.py 要求位置参数 key（registry 未配 args 的历史缺陷）。
# sync_macro_credit.py 无 argparse（__main__ 忽略 argv），不注入。
if os.path.basename(script_rel) == "sync_tushare.py":
    cmd.insert(2, key)
```

（若文件顶部未 `import os`，补 `import os`。）

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_data_service.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add presentation/server/services/data_service.py tests/test_data_service.py
git commit -m "fix(data_service): pass dataset key to sync_tushare subprocess (C-9 A1)"
```

---

### Task 3: 哨兵状态机新增 unavailable 分支

**Files:**
- Modify: `presentation/server/services/data_service.py`（`_derive_status`，约 :94-122）
- Test: `tests/test_data_service.py`

**Interfaces:**
- Consumes: `TUSHARE_DATASETS[key]["_unavailable"]`（config 注册表）
- Produces: `list_datasets()` 对 unavailable 数据集返回 `status="unavailable"`，不再 failed/missing

- [ ] **Step 1: 写失败测试**

```python
def test_derive_status_unavailable(monkeypatch, tmp_path):
    """_unavailable 数据集 → 'unavailable'，哨兵/缺失均不压倒（设计使然，非故障）。"""
    import presentation.server.services.data_service as ds
    from config import TUSHARE_DATASETS
    monkeypatch.setattr(ds, "TUSHARE_DATASETS",
                        {"top_list": {"_unavailable": "代理无此接口"}})
    monkeypatch.setattr(ds, "_sentinel_path", lambda key, failed=False: str(tmp_path / f"{key}.failed"))
    (tmp_path / "top_list.failed").write_text("old error", encoding="utf-8")
    status, _ = ds._derive_status("top_list", str(tmp_path / "top_list.parquet"))
    assert status == "unavailable"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_data_service.py -q`
Expected: FAIL（当前返回 failed）

- [ ] **Step 3: 实现**

`_derive_status` 状态机顶部（syncing 检查前）插入：

```python
# C-9 A2：_unavailable 数据集是代理不支持（设计使然），不参与 failed/missing 告警。
if TUSHARE_DATASETS.get(key, {}).get("_unavailable"):
    return "unavailable", "代理接口不支持（_unavailable）"
```

文件顶部 `from config import DATASET_REGISTRY, LAKE_CONFIG, SYNCING_DIR` 改为同时导入 `TUSHARE_DATASETS`。`list_datasets` 无需改动（透传 status）。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_data_service.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add presentation/server/services/data_service.py tests/test_data_service.py
git commit -m "feat(data_service): mark _unavailable datasets as unavailable not failed (C-9 A2)"
```

---

### Task 4: 补跑 quick 批日频同步

**Files:**
- Modify: 无代码；数据湖 `data_lake/*.parquet`（moneyflow/margin/ths_daily/share_float/suspend_d/top_inst/margin_detail/moneyflow_hsgt 等）

**Interfaces:**
- Consumes: Task 1（任务入口）、Task 2（可选，服务器端不再刷 failed）
- Produces: quick 批 parquet mtime 推进到最近交易日；`sync_incremental.stdout.log` 新增 `OK N FAIL 0`

- [ ] **Step 1: 触发任务（等价 T1 Step 4）**

```powershell
schtasks /Run /TN quanter_sync_incremental
```

- [ ] **Step 2: 等待并确认完成**

Run（完成后）:
```powershell
Get-Content data_lake\.syncing\sync_incremental.stdout.log -Tail 8
```
Expected: `=== DONE | OK 18 | FAIL 0 ===`（top_list/hsgt_top10/concept 为 unavailable，不属 quick 批）

- [ ] **Step 3: 抽查 lag 归零**

Run:
```powershell
Get-Item data_lake\moneyflow.parquet, data_lake\ths_daily.parquet | Select Name, LastWriteTime
```
Expected: LastWriteTime 为今天（08-04）或最近交易日 18:00 后

- [ ] **Step 4: Commit（无代码变更，跳过）**

---

### Task 5: 清理历史 .failed 哨兵

**Files:**
- Modify: `data_lake/.syncing/*.failed`（502B 批 19 个 + 178B 批 7 个）

**Interfaces:**
- Consumes: Task 4（同步成功后再清，哨兵不会复活）；Task 2（重启后 sweep 不再刷新哨兵）
- Produces: `/datasets` 状态回到 healthy/stale/missing/unavailable 真实语义

- [ ] **Step 1: 清哨兵（仅限 data_lake/.syncing，验证路径）**

```powershell
$dir = (Resolve-Path 'F:\quanter\data_lake\.syncing').Path
Get-ChildItem -LiteralPath $dir -Filter '*.failed' | Remove-Item -Force
```

- [ ] **Step 2: 验证状态机**

Run: `pytest tests/test_data_service.py -q`（已有 list_datasets 相关用例）
然后人工：启动后请求 `/api/v1/data/datasets`（或前端 DataLakeView），断言 daily/index_daily 显示 healthy（mtime 新），unavailable 显示 unavailable。

- [ ] **Step 3: Commit（无代码变更，跳过）**

---

### Task 6: 前端业务日期改本地时区

**Files:**
- Create: `presentation/web/src/utils/date.ts`
- Create: `presentation/web/src/utils/date.spec.ts`
- Modify: `presentation/web/src/views/JobCockpitView.vue:34`、`presentation/web/src/components/cockpit/TradesTable.vue:63`、`presentation/web/src/views/LiveCockpitView.vue:97`

**Interfaces:**
- Produces: `toLocalDateStr(d?: Date): string`（YYYY-MM-DD，本地时区）

- [ ] **Step 1: 写失败测试**

```ts
// presentation/web/src/utils/date.spec.ts
import { describe, expect, it } from "vitest";
import { toLocalDateStr } from "./date";

describe("toLocalDateStr", () => {
  it("uses local timezone, not UTC", () => {
    // 北京 2026-08-04 00:30 = UTC 2026-08-03 16:30
    const d = new Date("2026-08-03T16:30:00Z");
    expect(toLocalDateStr(d)).toBe("2026-08-04");
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd presentation/web; npm run test -- date.spec.ts`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现工具函数**

```ts
// presentation/web/src/utils/date.ts
export function toLocalDateStr(d: Date = new Date()): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}
```

- [ ] **Step 4: 替换三处调用**

`JobCockpitView.vue` / `TradesTable.vue` / `LiveCockpitView.vue`：

```ts
import { toLocalDateStr } from "@/utils/date";
const businessDate = toLocalDateStr();
```

（`LiveCockpitView.vue` 区间起点/终点用 `toLocalDateStr(start)` / `toLocalDateStr(end)`。）

- [ ] **Step 5: 跑测试 + typecheck**

Run: `cd presentation/web; npm run test; npm run typecheck`
Expected: 全绿

- [ ] **Step 6: Commit**

```bash
git add presentation/web/src/utils/date.ts presentation/web/src/utils/date.spec.ts presentation/web/src/views/JobCockpitView.vue presentation/web/src/components/cockpit/TradesTable.vue presentation/web/src/views/LiveCockpitView.vue
git commit -m "fix(web): use local timezone for business date (C-9 A4)"
```

---

### Task 7: GBK 输出治理（infra/pyio + 入口接入 + bat PYTHONUTF8）

**Files:**
- Create: `infra/pyio.py`
- Create: `tests/ops/test_gbk_smoke.py`
- Modify: `ops/data_pipeline.py`、`ops/brief_all.py`、`trading/tools/qmt_live_smoke.py`、`trading/tools/qmt_live_smoke_realorder.py`、`trading/tools/qmt_live_smoke_moutai.py`、`trading/tools/qmt_reconcile_positions.py`、`trading/tools/smoke_trading_engine.py`、`trading/tools/trigger_eod_once.py`、`trading/tools/trigger_pre_open_once.py`、`compute_unit/__main__.py`、`compute_unit/task_export.py`、`discovery/tools/param_iter.py`、`scripts/export_sobol_task.py`、`data/tools/probe_tushare_fields.py`、`data/tools/scan_integrity.py`
- Modify: `scripts/run_brief_all.bat`、`scripts/run_broadcast.bat`、`scripts/run_daily_incremental.bat`、`scripts/run_data_brief.bat`、`scripts/run_data_check_t1.bat`、`scripts/run_data_check_t2.bat`、`scripts/run_data_pipeline.bat`、`scripts/run_strategy_brief.bat`、`scripts/run_sync_incremental.bat`、`scripts/run_trading_brief.bat`、`scripts/run_trading_engine.bat`（start_server.bat 已含）

**Interfaces:**
- Produces: `infra.pyio.force_utf8_stdout()`——stdout 非 UTF-8 时 reconfigure，幂等

- [ ] **Step 1: 写失败测试（GBK 管道冒烟）**

```python
# tests/ops/test_gbk_smoke.py
import os
import subprocess
import sys


def test_emoji_print_survives_gbk_pipe():
    """cp936 管道下 emoji print 不抛 UnicodeEncodeError（回归 GBK 崩溃）。"""
    code = (
        "import sys; sys.path.insert(0, r'F:\\quanter'); "
        "from infra.pyio import force_utf8_stdout; force_utf8_stdout(); "
        "print('✅ ok')"
    )
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "gbk"
    env.pop("PYTHONUTF8", None)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=env, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    assert "✅" in r.stdout
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/ops/test_gbk_smoke.py -q`
Expected: FAIL（UnicodeEncodeError）

- [ ] **Step 3: 实现 helper**

```python
# infra/pyio.py
# -*- coding: utf-8 -*-
"""运维入口统一 stdout UTF-8：GBK 管道/重定向下 emoji print 不再 UnicodeEncodeError。"""
from __future__ import annotations
import sys


def force_utf8_stdout() -> None:
    """stdout 非 UTF-8 时 reconfigure（errors=replace 兜底）；幂等，异常静默。"""
    try:
        enc = getattr(sys.stdout, "encoding", "") or ""
        if enc and enc.lower() not in ("utf-8", "utf8"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
```

- [ ] **Step 4: 接入全部入口**

每个入口 `if __name__ == "__main__":` 或 `main()` 顶部加：

```python
from infra.pyio import force_utf8_stdout
force_utf8_stdout()
```

同时修 `ops/brief_all.py` 残留 emoji（run_brief_all 失败路径）：

```python
print(f"[!] {bot} 播报失败 rc={rc}（继续其余 bot）")
```

bat 统一在 `cd /d F:\quanter` 后加：

```bat
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
```

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/ops/test_gbk_smoke.py tests/ops/test_brief_all_async.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add infra/pyio.py tests/ops/test_gbk_smoke.py ops/ compute_unit/ trading/tools/ discovery/tools/param_iter.py scripts/export_sobol_task.py data/tools/probe_tushare_fields.py data/tools/scan_integrity.py scripts/*.bat
git commit -m "fix(ops): force UTF-8 stdout on CLI entries to prevent GBK emoji crashes (C-9 A5)"
```

---

### Task 8: 任务治理（discovery 退役 + RETIRED_TASKS 补全）

**Files:**
- Modify: `discovery/schtasks.py`（register 拒绝重建）
- Modify: `ops/manage_ops_schtasks.py:35`（RETIRED_TASKS）
- Test: `tests/discovery/test_schtasks.py`、`tests/test_manage_ops_schtasks.py`

**Interfaces:**
- Produces: `discovery.schtasks.main(["--register"])` 返回非 0 且不发 /Create；`manage_ops_schtasks.RETIRED_TASKS` 含 QuanterDailyBrief

- [ ] **Step 1: 写失败测试**

```python
# tests/discovery/test_schtasks.py 追加
def test_register_refused_after_retirement(monkeypatch):
    """QuanterDiscoveryDaemon 已收编 lifespan（C-7 V2），--register 必须拒绝，防双跑。"""
    from discovery.schtasks import main
    calls: list[list[str]] = []
    monkeypatch.setattr("discovery.schtasks._schtasks", lambda a: calls.append(a) or 0)
    rc = main(["--register"])
    assert rc != 0
    assert all("/Create" not in c for c in calls), f"不得重建退役任务：{calls}"
```

```python
# tests/test_manage_ops_schtasks.py 追加
def test_retired_tasks_contains_daily_brief():
    """QuanterDailyBrief 已从系统删除；若被重建，--unregister-pipeline-brief 必须能清。"""
    from ops.manage_ops_schtasks import RETIRED_TASKS
    assert "QuanterDailyBrief" in RETIRED_TASKS
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/discovery/test_schtasks.py tests/test_manage_ops_schtasks.py -q`
Expected: 新测试 FAIL

- [ ] **Step 3: 实现**

`discovery/schtasks.py` 的 `register()` 改为拒绝：

```python
def register() -> None:
    """已退役（C-7 V2 收编 lifespan cron 02:00）：拒绝重建，防双跑。"""
    print("QuanterDiscoveryDaemon 已退役：discovery 收编 uvicorn lifespan "
          "（engine.sched cron 02:00 + 启动补跑）。禁止重建，请用 --unregister 清残留。")
```

`main` 的 `--register` 分支改返 `1`（`if args.register: register(); return 1`）。

`ops/manage_ops_schtasks.py:35`：

```python
RETIRED_TASKS = ["QuanterDataPipeline", "QuanterBrief", "QuanterDailyBrief"]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/discovery/test_schtasks.py tests/test_manage_ops_schtasks.py tests/scripts/test_manage_ops_schtasks.py -q`
Expected: PASS（若既有 register 用例断言创建，按新语义更新为断言拒绝）

- [ ] **Step 5: Commit**

```bash
git add discovery/schtasks.py ops/manage_ops_schtasks.py tests/discovery/test_schtasks.py tests/test_manage_ops_schtasks.py
git commit -m "fix(ops): refuse re-registering retired discovery task, retire QuanterDailyBrief (C-9 A3)"
```

---

### Task 9: qfq 除权标的历史全量重算

**Files:**
- Modify: `data/tools/sync_daily_incremental.py`
- Test: `tests/test_sync_ohlcv_qfq.py`（或同目录新增）

**Interfaces:**
- Consumes: 现有 `sync_daily_incremental(no_backscan)` 主流程
- Produces: `--no-recompute-div` 开关（缺省自动重算）；`_recompute_symbol(pro, symbol, todayc) -> DataFrame`

- [ ] **Step 1: 写失败测试**

```python
def test_recompute_symbol_rebuilds_full_history_baseline():
    """除权标的：历史行用新窗口最新 adj 重建（旧行不再停留在旧基线）。"""
    from data.tools.sync_daily_incremental import _recompute_symbol
    import pandas as pd

    class FakePro:
        def daily(self, ts_code, start_date, end_date):
            return pd.DataFrame({
                "ts_code": [ts_code] * 3,
                "trade_date": ["20231229", "20240102", "20240103"],
                "open": [9.8, 10.5, 11.0],
                "high": [10.2, 10.8, 11.3],
                "low": [9.7, 10.2, 10.8],
                "close": [10.0, 10.6, 11.2],
                "vol": [100, 110, 120],
                "amount": [1000, 1100, 1200],
            })
        def adj_factor(self, ts_code, start_date, end_date):
            return pd.DataFrame({
                "ts_code": [ts_code] * 3,
                "trade_date": ["20231229", "20240102", "20240103"],
                "adj_factor": [0.9, 0.95, 1.0],
            })

    out = _recompute_symbol(FakePro(), "000001.SZ", "20240103")
    closes = out.sort_index()["close"].astype(float)
    assert closes.iloc[-1] == pytest.approx(11.2)   # 最新日 adj/latest=1
    assert closes.iloc[0] == pytest.approx(9.0)     # 12-29: 10.0*0.9/1.0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_sync_ohlcv_qfq.py -q`
Expected: FAIL（函数不存在）

- [ ] **Step 3: 实现**

在 `sync_daily_incremental.py` 增加：

```python
from data.tushare_sync import _fetch_with_guard  # 限频 + 熔断（P2：per-symbol 全历史是新增配额路径，必须走统一守卫）


def _recompute_symbol(pro, symbol: str, todayc: str) -> pd.DataFrame:
    """按标的拉全历史 raw + adj，用窗口最新 adj 重建 qfq，返 MultiIndex(date, symbol)。"""
    # 起点 19900101 是哨兵下限，Tushare 按上市日自动截取（老股 1990-1999 段返空属正常，
    # 非 bug——P3 防后人误判）。
    raw = _fetch_with_guard("daily", ts_code=symbol,
                            start_date="19900101", end_date=todayc)
    adj = _fetch_with_guard("adj_factor", ts_code=symbol,
                            start_date="19900101", end_date=todayc)
    if raw is None or raw.empty:
        return pd.DataFrame()
    raw = raw.rename(columns={"ts_code": "symbol", "vol": "volume"})
    merged = raw.merge(
        adj[["ts_code", "trade_date", "adj_factor"]],
        left_on=["symbol", "trade_date"], right_on=["ts_code", "trade_date"],
        how="left",
    ).drop(columns=["ts_code"], errors="ignore")
    latest_adj = merged.sort_values("trade_date")["adj_factor"].iloc[-1]
    if pd.isna(latest_adj) or latest_adj == 0:
        latest_adj = 1.0
    for col in PRICE_COLS:
        if col in merged.columns:
            merged[col] = merged[col] * merged["adj_factor"] / latest_adj
    merged["trade_date"] = pd.to_datetime(merged["trade_date"], format="%Y%m%d")
    merged = merged.rename(columns={"trade_date": "date"})
    return merged[["date", "symbol"] + OUT_COLS].set_index(["date", "symbol"]).sort_index()
```

主流程 ④ 除权检测后追加（`div_syms` 非空且未传 `--no-recompute-div`）：

```python
if div_syms and not no_recompute_div:
    # ⚠️ 配额影响（P2）：per-symbol 全历史调用（1 标的 ≈ 2 次请求），除权季单次可能几十只；
    # _fetch_with_guard 统一限频（~60/min）兜底，超时/熔断按数据集语义返空跳过。
    logger.warning("除权标的 %d 只，全量重算历史 qfq 基线：%s", len(div_syms), div_syms)
    for sym in div_syms:
        fixed = _recompute_symbol(pro, sym, todayc)
        if fixed.empty:
            continue
        combined = combined[combined.index.get_level_values("symbol") != sym]
        combined = pd.concat([combined, fixed])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
```

`sync_daily_incremental(no_backscan=False, no_recompute_div=False)` 签名扩展；`__main__` 加 `--no-recompute-div`。

前置确认（未验证项 2）：`_sync_by_symbol` 的 adj 全区间重建分支已在 `data/tushare_sync.py` 存在
且被 `tests/test_tushare_sync.py::test_sync_dataset_resume_adj_refetches_full_range` 覆盖（本会话 81 测试全绿）。
`_fetch_with_guard` 复用同一守卫，T9 的"复用"前提成立。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_sync_ohlcv_qfq.py tests/test_sync_incremental.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add data/tools/sync_daily_incremental.py tests/test_sync_ohlcv_qfq.py
git commit -m "feat(sync): auto-recompute qfq baseline for ex-dividend symbols (C-9 A4)"
```

---

### Task 10: _sync_by_date 空/损坏 shard 校验

**Files:**
- Modify: `data/tushare_sync.py`（`_sync_by_date`，约 :418-437）
- Test: `tests/test_tushare_sync.py`

**Interfaces:**
- Consumes: 现有 `_sync_by_date(key, api, fields, date_col, symbol_col, start, end, resume, out, cfg)`
- Produces: 空/损坏 shard 视为缺失重拉；有效 shard 保持跳过

- [ ] **Step 1: 写失败测试**

```python
def test_sync_by_date_skips_only_valid_shard(tmp_path, fake_pro):
    """by=date：空 shard（0 行）不得永久跳过——视为缺失重拉。"""
    from config import TUSHARE_DATASETS
    shard_dir = tmp_path / "shards"
    shard_dir.mkdir()
    TUSHARE_DATASETS["moneyflow_test"] = {
        "api": "moneyflow", "by": "date",
        "date_col": "trade_date", "symbol_col": "ts_code",
        "fields": "ts_code,trade_date,buy_lg_amount",
        "lake": str(tmp_path / "moneyflow.parquet"),
        "shard_dir": str(shard_dir),
    }
    # 空 shard（历史中断写坏）
    pd.DataFrame(columns=["ts_code", "trade_date", "buy_lg_amount"]) \
        .to_parquet(shard_dir / "20240105.parquet")
    fake_pro._data["moneyflow"] = pd.DataFrame({
        "ts_code": ["000001.SZ"], "trade_date": ["20240105"],
        "buy_lg_amount": [1.0],
    })
    from data.tushare_sync import sync_dataset
    sync_dataset("moneyflow_test", "2024-01-05", "2024-01-05", resume=True)
    df = pd.read_parquet(TUSHARE_DATASETS["moneyflow_test"]["lake"])
    assert len(df) == 1  # 空 shard 被重拉
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_tushare_sync.py -q`
Expected: FAIL（空 shard 被跳过 → 湖空）

- [ ] **Step 3: 实现**

`_sync_by_date` 循环内替换：

```python
shard = os.path.join(shard_dir, f"{td}.parquet")
if resume and os.path.exists(shard):
    try:
        old = pd.read_parquet(shard)
        if old.empty:
            logger.warning("by=date shard 空（%s），重拉：%s", key, shard)
            os.remove(shard)
        else:
            continue
    except Exception:
        logger.warning("by=date shard 损坏（%s），重拉：%s", key, shard, exc_info=True)
        os.remove(shard)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_tushare_sync.py tests/test_sync_incremental.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add data/tushare_sync.py tests/test_tushare_sync.py
git commit -m "fix(sync): refetch empty or corrupt by=date shards instead of skipping forever (C-9 A4)"
```

---

### Task 11: 计划任务 Password 化（人工步骤，需 Windows 密码）

**Files:**
- Modify: 系统任务 `\QuanterServer`、`\quanter_sync_incremental`（无代码文件）

**Interfaces:**
- Produces: 两个任务 `Logon Mode = Password`，开机/18:00 不依赖登录

- [ ] **Step 1: GUI 修改（需 yzzhan 账户密码，PIN 不行）**

1. `Win+R` → `taskschd.msc`
2. `QuanterServer` → 属性 → 常规 → 勾选"不管用户是否登录都要运行" → 输密码确认
3. `quanter_sync_incremental` 同操作

- [ ] **Step 2: 验证**

Run: `schtasks /Query /TN QuanterServer /XML | Select-String LogonType`、`schtasks /Query /TN quanter_sync_incremental /XML | Select-String LogonType`
Expected: 两处 `<LogonType>Password</LogonType>`

- [ ] **Step 3: Commit（无代码变更，跳过）**

---

## Self-Review

**Spec 覆盖**：本 plan 仅覆盖 spec **A 路（数据观测与调度治理）**：A1→T2；A2→T3/T5；A3→T1/T8/T11；A4→T9/T10/T6；A5→T7。A 路内全部覆盖，无缺口；spec 的 **B（交易风控 P0）/ C（架构拆分）/ D（安全治理）另立 plan**，不在本 plan 范围（spec 第 12 行 workstream 拆分设计）。

**占位符扫描**：所有代码步骤含具体实现与断言；人工步骤（T1/T4/T5/T11）以命令+期望输出代替测试，无"TBD/TODO"。

**类型一致性**：`_run_sync_subprocess` 注入位置为 `cmd[2]`（`[python, script, key]`）；`_recompute_symbol` 返回 MultiIndex(date, symbol) 与主流程 `combined` 一致；`toLocalDateStr` 签名在三个消费点一致。
