# scripts/ 目录重组 Plan v2(2026-07-25 架构治理 · T6)

> 状态:**plan 已写,待执行**。本 plan 反映 v2 方向(废弃 scripts/,按业务属性分散),并记录 v1 实证发现的 namespace 技术障碍。
> v1 方向(scripts/ 内部分子目录)已被否决——目标是**彻底废弃顶层 scripts/,脚本归各业务包**。

---

## 0. 方向(v2 · 用户决策)

**废弃顶层 `scripts/`**,50 个脚本按业务属性分散到各业务包:
- 业务包内用 **`tools/` 子包**组织(如 `data/tools/sync_tushare.py`)
- 运维/诊断脚本无自然业务包 → 新建 **`ops/` + `diag/` 顶层包**

**子包名必须用 `tools`,绝不能用 `scripts`**(见 §1 技术障碍)。

---

## 1. ⚠️ 关键技术障碍(v1 实证)

**v1 尝试在 `discovery/scripts/` 建 `__init__.py`(regular 包),导致 pytest 下顶层 `scripts` 解析被劫持**:

```
ImportError: cannot import name 'manage_ops_schtasks' from 'scripts'
  (F:\quanter\discovery\scripts\__init__.py)
```

**根因**:Python namespace 包机制——顶层 `scripts/`(无 `__init__.py`,namespace 包)与 `discovery/scripts/`(有 `__init__.py`,regular 包)共存时,pytest 的 sys.path 配置下 `import scripts` 优先命中 regular 包 `discovery/scripts/`,劫持了顶层 namespace。实证:批 A 移 3 脚本到 `discovery/scripts/` 即导致 5 个测试 collect 失败(test_data_check/test_manage_ops_schtasks/test_sync_daily_incremental/test_daily_migration_parity/test_sync_data_lake)。已回退,897 collect 0 error。

**规避**:子包名用 `tools`(无顶层 `tools/` 包,不会劫持任何 namespace)。

---

## 2. 业务属性映射(50 脚本 → 7 目标)

| 目标 | 脚本 | 数量 | 风险 |
|---|---|---|---|
| `data/tools/` | sync_*(11)+ probe_tushare_fields/probe_rate_fields/probe_snapshot_fields + run_data_check | 15 | 🔴 高(改 config/registry script 字段 + tests) |
| `discovery/tools/` | param_iter + probe_champion_oos + identify_param_scan | 3 | 🟡 中(改 discovery/sampler.py:24) |
| `backtest/tools/` | regression_neckline_golden, neckline_method_diagnose, breakout_quality_analysis, analyze_fullscan, trend_filter_analysis, market_breadth, market_regime_filter, macro_regime_resonance, regime_micro_analysis, bluechip_check, kbkg_trailing_verify | 11 | 🟢 低(仅 md 引用) |
| `trading/tools/` | qmt_live_smoke(_headless/_realorder), qmt_smoke, probe_qmt_ratelimit, smoke_trading_engine | 6 | 🟢 低 |
| `infra/tools/` | dingtalk_review_bridge, llm_throttle_proxy, smoke_throttle_proxy | 3 | 🟡 中(schtasks) |
| `ops/`(顶层) | check_ports, clean_ports, run_checks, dev, check_contracts, manage_ops_schtasks, migrate_replay_runs_to_sqlite, _render_pdf | 8 | 🔴 高(sys.path/importlib 加载,见 §3) |
| `diag/`(顶层) | diag_2026_stops, diag_002882, diag_2024, diag_2026_cases | 4 | 🟢 低 |

---

## 3. 特殊处理(已实证的坑)

1. **sys.path/importlib 加载的脚本**(v1 批 2 实证):
   - `check_contracts`/`check_ports`:`tests/test_check_*.py` 用 `_SCRIPTS_DIR = .../scripts` + `sys.path.insert` + `import X`。移到 `ops/` 后须改 `_SCRIPTS_DIR = .../ops`。
   - `migrate_replay_runs_to_sqlite`:`tests/test_migrate_replay_runs.py` 用 `importlib` 从路径加载。移到 `ops/` 后改路径。
   - `manage_ops_schtasks`/`run_data_check`:被 `from scripts import X` / `from scripts.X import`。移到 `ops/` 后改 `from ops import X` / `from ops.X import`。
2. **`param_iter` coupling 债**:`discovery/sampler.py:24 from discovery.tools.param_iter` → 迁 `discovery/tools/param_iter.py` 后改 `from discovery.tools.param_iter`。
3. **`config/registry.py` script 字段**(🔴 最高危):`DATASET_REGISTRY[*]["script"]` 是前端 DataLakeView 反射 + POST /sync/{key} 子进程触发路径。sync_* 移到 `data/tools/` 后,字段改 `"data/tools/sync_X.py"`,**改错会让数据同步静默失效**。

---

## 4. 执行步骤

```bash
# Step 1: 建目标目录 + __init__.py(用 tools,不用 scripts!)
mkdir -p data/tools discovery/tools backtest/tools trading/tools infra/tools ops diag
for d in data/tools discovery/tools backtest/tools trading/tools infra/tools ops diag; do
  touch $d/__init__.py
done
# Step 2: git mv(按映射,declare -A 脚本)
# Step 3: sed 全项目引用(# 分隔符,避免 | 冲突;长名优先防前缀冲突)
#   scripts/X.py → <pkg>/tools/X.py(或 ops/X.py, diag/X.py)
#   scripts.X    → <pkg>.tools.X(或 ops.X, diag.X)
# Step 4: 手动核对 config/registry.py 的 script 字段(最高危)
# Step 5: 修 sys.path/importlib 测试(_SCRIPTS_DIR / importlib 路径)
# Step 6: 改 tests/scripts/ 的 from scripts.X → from <pkg>.tools.X / ops.X
# Step 7: schtasks/md 文档路径更新
# Step 8: collect + 受影响子集 pytest 验证
```

---

## 5. 分批执行(风险递增)

- **批 A(🟢)**:`discovery/tools/`(3)+ `backtest/tools/`(11)+ `trading/tools/`(6)+ `diag/`(4)= 24 文件。无 config/registry 依赖。每批 collect 验证。
- **批 B(🟡)**:`infra/tools/`(3)+ `ops/` 的包 import 类(run_data_check/manage_ops_schtasks)。
- **批 C(🔴)**:`ops/` 的 sys.path/importlib 类(check_contracts/check_ports/migrate)。
- **批 D(🔴 最高危)**:`data/tools/`(sync_* 11 + probe_* 3 + run_data_check)。**改 config/registry script 字段** + 10 tests import。每改一个 registry 字段跑 `pytest tests/test_dataset_registry.py tests/test_sync_*.py`。

每批独立 commit,便于回滚。

---

## 6. 验证闸

- `pytest --collect-only -q`:0 error(确认 import 更新到位 + 无 namespace 劫持)
- `pytest tests/test_dataset_registry.py tests/scripts/ tests/test_sync_*.py -q`:全绿
- `python -m data.tools.sync_macro_credit --help`(确认脚本可运行)
- `grep -rE "scripts/sync_tushare|from scripts\." --include="*.py"`:确认无旧路径残留
- 移动后顶层 `scripts/` 应空,删除前确认无残留 import。

---

## 7. 风险与回滚

- 🔴 config/registry script 字段改错 → 数据同步静默失效。sed 后逐条核对 + 跑数据集测试。
- 🔴 namespace 劫持复发:若误用 `scripts` 子包名(非 `tools`)。**必须用 tools**。
- 🟡 schtasks 本地任务:用户本地已注册的定时任务指向旧 scripts/ 路径,sed 改代码但本地 schtasks 不自动更新,需提示用户重新注册。
- 🟡 文件名前缀冲突:sed 替换须长名优先(如 `sync_tushare` 优先于 `sync`)。
- 回滚:每批独立 commit,`git revert`。
